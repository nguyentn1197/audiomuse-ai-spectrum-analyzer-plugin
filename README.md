# SpectrumAnalyzer — AudioMuse-AI plugin

Spectrum analysis over the whole library with fake-lossless / transcode detection and a stored spectrogram per song.

## What it does

- **Parallel library scan** — the scan is an orchestrator task (high queue) that fans out one task per album onto the default queue, so every worker picks up albums concurrently (same parent/child pattern as core analysis). The parent task under Active Tasks tracks albums done / remaining and aggregate counters; each album is its own sub-task. In *changed* mode, albums with nothing to do are settled by the orchestrator without spawning a task. Cancelling the parent stops new albums from being dispatched.
- **Each song analyzed once, re-analyzed only when the file changes** — two layers:
  1. *Metadata fingerprint* (path, size, suffix, bitRate, created…) from the media-server track object → unchanged tracks are skipped without downloading.
  2. *Audio MD5* of the downloaded bytes → if metadata changed but bytes didn't, only the fingerprint is refreshed; if bytes changed, full re-analysis.
- **Fake detection output per song** — frequency cutoff (Hz), edge sharpness (dB/kHz), noise-shelf level, verdict (`CLEAN`, `CONSISTENT_LOSSY`, `LOWPASSED`, `UPSAMPLED`, `TRANSCODED_LOSSY`, `FAKE_SUSPECT`), estimated source-bitrate class, confidence, raw metrics JSON. `FAKE_SUSPECT`, `TRANSCODED_LOSSY` and `UPSAMPLED` count as suspect in the overview.
- **Spectrogram stored as base64 PNG** in the plugin's own Postgres table (`plugin_spectrum_analyzer__results`), rendered spek-style with a red line at the detected cutoff.
- **Reacts to the core cleanup task** — `item_id` is a foreign key to `score(item_id)` with `ON DELETE CASCADE` (same pattern as core's `embedding` tables), so when `tasks/cleaning.py` removes an orphaned track, its spectrogram row disappears too.
- **Manual re-run** — a Re-analyze button on every track (runs on the `high` queue) and a Re-analyze album button on each album page (one forced album task on the default queue), plus three scan modes: *changed* (default), *verify* (re-hash everything), *force* (redo everything).
- **Album filters** — the overview can be narrowed to albums containing suspect tracks (checkbox) or to albums with at least N suspects.
- **Bonus hook** — `on_song_analyzed` analyzes new songs for free during core analysis (the audio is already on disk; deduped by MD5). Toggleable in settings.
- **Optional cron** — a `plugin.spectrum_analyzer.scan_changed` schedule is seeded *disabled* (Sun 04:00); enable it under Administration → Scheduled Tasks for automatic incremental scans.

## How the fake detection works

A 90 s segment from the middle of the track is loaded at native sample rate, STFT'd (n_fft 4096), and reduced to a robust per-frequency "max hold" profile (95th percentile over time). The cutoff is the highest frequency still within 40 dB of the 1–8 kHz reference level. A lossy encoder's low-pass leaves a near-vertical wall (high dB/kHz edge) with digital silence above it; a genuine master rolls off gradually and keeps dither/analog noise above the rolloff. Verdict logic:

- cutoff ≥ ~93 % of Nyquist (capped at 20.5 kHz) → `CLEAN` — genuine 44.1 kHz masters legitimately roll off at 20–21 kHz (anti-alias filters, mastering chains), and edge sharpness measured against the Nyquist wall means nothing
- **except** in a hi-res container (> 48 kHz): content stopping below 23 kHz there means a resampled 44.1/48 kHz source → `UPSAMPLED` (fake hi-res; 85 % confidence with digital silence above the cutoff, 60 % if there's noise — could be a very dark genuine master)
- lossless container + low cutoff + sharp edge + **silent shelf** above the cutoff → `FAKE_SUSPECT` (estimated source class, e.g. "~128 kbps")
- lossless container + sharp edge but audible noise above the cutoff → `LOWPASSED` (an encoder wall leaves digital silence; noise points at a genuine low-passed master)
- lossless container + gradual rolloff → `LOWPASSED` (lower confidence)
- lossy container whose cutoff is far below what its declared bitrate should reach → `TRANSCODED_LOSSY`
- otherwise → `CONSISTENT_LOSSY`

These are heuristics — treat `FAKE_SUSPECT` as "look at the spectrogram," not a conviction. Note the deliberate trade-off in the CLEAN rule: a 320 kbps transcode (cutoff ~20.5 kHz) is spectrally indistinguishable from a genuine master, so it will read as CLEAN rather than risk false accusations against real lossless files.

## Install (local repository, per the official docs)

1. Put `spectrum_analyzer.zip`, `plugin.json` and `manifest.json` in one folder.
2. Edit both JSON files: replace `http://<your-ip>:8000` with an address your AudioMuse containers can reach (e.g. an LXC IP on your services VLAN — not `localhost`).
3. `python3 -m http.server 8000` in that folder.
4. AudioMuse-AI → Plugins → Repositories → add `http://<your-ip>:8000/manifest.json`.
5. Install from the Catalog tab, then **Apply now (restart)**.

`matplotlib` is pulled in automatically at install (`requirements` in plugin.json) — this means **Docker/Kubernetes only**; the standalone builds can't install extra pip packages.

## Notes & caveats

- **Only tracks already analyzed by core AudioMuse** get spectrum rows during a scan (the FK requires a `score` row). Un-analyzed tracks are counted as `not_in_score` and get picked up automatically by the hook when you run core analysis.
- **Navidrome transcoding**: tracks are fetched via the Subsonic `stream` endpoint. Make sure the AudioMuse player/client in Navidrome has **no transcoding profile** ("raw"), otherwise you'd be analyzing the transcode, not the file.
- **Database size**: at the default 800×280 px, a spectrogram is roughly 100–300 KB of base64. 10 000 songs ≈ 1–3 GB in Postgres. Tune the image size in settings if that matters; deleting rows for an album and rescanning regenerates them.
- **Verify mode** downloads every track (to hash it) — heavy on a big library over the network; use it when you suspect in-place file edits that Navidrome metadata wouldn't reveal.

## Files

- `plugins/SpectrumAnalyzer/__init__.py` — blueprint, pages (album overview with suspect filters, per-album detail), settings, migration, `register(ctx)`
- `plugins/SpectrumAnalyzer/jobs.py` — scan orchestrator + per-album child tasks, single-track re-run, fingerprints, hook, DB upsert
- `plugins/SpectrumAnalyzer/dsp.py` — STFT profile, cutoff/edge/shelf metrics, verdict, spectrogram PNG
- `spectrum_analyzer.zip` — the flat code zip AudioMuse installs
- `plugin.json`, `manifest.json` — descriptor + local catalog
