# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: SpectrumAnalyzer

An AudioMuse-AI plugin for audio spectrum analysis that detects fake lossless files and transcodes via frequency-cutoff heuristics. Results include spectrograms (base64 PNG) and verdicts (CLEAN, CONSISTENT_LOSSY, LOWPASSED, TRANSCODED_LOSSY, FAKE_SUSPECT).

## Architecture

### Three-module design

- **`__init__.py`** — Flask blueprint for the web UI (album overview, per-track detail, settings). Handles database migration, plugin registration, and routes for scan/rescan/settings actions. Verdicts are color-coded; spectrograms are rendered inline.

- **`jobs.py`** — Background job logic. Three scan modes:
  - `changed`: skip tracks whose metadata fingerprint (path, size, bitrate, etc.) hasn't changed
  - `verify`: download every file, re-analyze only if audio MD5 differs (catches in-place edits invisible to metadata)
  - `force`: re-download and re-analyze everything
  - `scan_library_job()` is a parent orchestrator (must run on the **high** queue): it fans out one `scan_album_job()` per album onto the default queue with `parent_task_id`, throttles in-flight children to `config.MAX_QUEUED_ANALYSIS_JOBS`, and tracks done/remaining by polling the children's `task_status` rows (statuses + counters aggregated from their details JSON). In `changed` mode it settles all-unchanged albums itself without spawning a child. Cancellation: it checks its own row for REVOKED each poll; children check the parent's status at start. This mirrors core's `run_analysis_task`/`analyze_album_task` pattern.
  - `scan_album_job()` also runs standalone (no parent) for the album Re-analyze button.
  - `on_song_analyzed()` hook piggybacks on core analysis when the audio is already on disk (no extra download).

- **`dsp.py`** — Audio DSP (librosa + numpy) and analysis pipeline:
  - `_load_segment()` extracts a 90s segment (default: middle of track to skip intros/fades).
  - `_spectrum()` computes STFT, reduces to per-frequency 95th-percentile profile (robust against silence/clicks).
  - `_find_cutoff()` locates the highest frequency within a drop threshold (default: 40 dB below 1–8 kHz reference).
  - `_edge_sharpness()` measures rolloff steepness in dB/kHz (encoder low-pass filters are near-vertical; natural rolloff is gradual).
  - `_verdict()` decision logic: cutoff ≥ min(0.93 × Nyquist, 20.5 kHz) → CLEAN (genuine masters roll off at 20–21 kHz; near-Nyquist edges are meaningless), unless the container is hi-res (> 48 kHz sample rate) and the cutoff aligns with a standard lower rate's Nyquist (`_resample_match`, asymmetric −3%/+7% window: resampler leakage pushes detected cutoffs up) or sits below 23 kHz — then UPSAMPLED **only with corroboration** (silent shelf, aliasing images via `_best_alias_match` mirrored around candidate source Nyquists, or sharp edge); without corroboration it's a CLEAN "dark master?" note. Lossless + sharp edge + silent shelf (≤ −68 dB vs. reference — calibrated to real 16-bit re-encodes whose dither refills encoder silence at −73…−106; genuine noise sits ~−53) → FAKE_SUSPECT (this catches LAME 320→FLAC; cutoffs ≥ 20.5 kHz like AAC 256 still pass); sharp edge but audible noise above cutoff → LOWPASSED; gradual rolloff → LOWPASSED; lossy with low cutoff relative to declared bitrate → TRANSCODED_LOSSY; otherwise CONSISTENT_LOSSY.
  - `_bit_depth_probe()` reads PCM via soundfile as int32 (left-justified) and computes effective bit depth from trailing zero bits: a >16-bit container with ≤16 effective bits is fake 24-bit → UPSCALED verdict (when spectrum is otherwise CLEAN) or a note. Stored as `container_bits`/`effective_bits` columns.
  - `_deep_spectrum()` (deep=True on `analyze_file`, from the per-track Deep analyze button): chunked whole-file scan (120 s chunks, 30 min cap) producing a global max-hold profile, a 1-column/second spectrogram, and a per-second spectral-edge series. Measured calibration: encoder wall ±198 Hz, content-varying upsample ±1035 Hz, genuine dark master ±7236 Hz → edge variance ≥ 2000 Hz downgrades UPSAMPLED/FAKE_SUSPECT → LOWPASSED "likely genuine dark master"; ≤ 300 Hz (constant wall) raises confidence; a pinned edge (≥50% of seconds within 4% of a standard Nyquist, ≤5% above it) confirms a resampler wall regardless of variance. Quiet seconds (window ref < chunk ref − 25 dB) are skipped.
  - `verified` column (0.5.0): manual per-track flag toggled from the album page; excluded from all suspect counts/filters in `__init__.py` (`bad_expr`), preserved across re-analysis (not in the upsert column list).
  - `deep_pending` column (0.5.1): set by the `deep_rescan` route at queue time, cleared in `analyze_track_job`'s finally block (survives job failure). Shown as a "deep scan queued" badge on the song card and aggregated into per-album overview tags (deep scan ×N / verified ×N).
  - `_render_spectrogram()` matplotlib-based PNG (spek-style color map, red line at detected cutoff), base64 encoded, tunable in settings.

### Database

Results table with foreign key to core's `score(item_id)` (cascades on delete). Fingerprints and MD5s stored to avoid redundant downloads/analysis.

### Plugin integration points

- Settings page for segment length, cutoff threshold, spectrogram dimensions, and optional on_song_analyzed hook toggle.
- Optional cron task (`plugin.spectrum_analyzer.scan_changed`, seeded disabled, Sunday 04:00).
- Manual re-analyze button per track (enqueued to the `high` queue).

## Development workflow

- **Run tests**: `python3 -m unittest discover tests -v` from the repo root (needs `numpy soundfile matplotlib` via pip; librosa is stubbed by `tests/librosa_stub.py`). `tests/fixtures/` holds committed 45 s ground-truth files (genuine masters + every fake type built from a confirmed-genuine DSD source). Always run after touching `dsp.py`.
- **Ad-hoc analysis**: `python3 tests/run_verdicts.py "path::suffix::kbps::EXPECTED::seg|deep"`.
- **Build**: `./build.sh [version]` → `dist/spectrum_analyzer-<version>.zip` (the flat zip AudioMuse installs). No argument: version discovered from the newest zip in `dist/`, falling back to `plugin.json`. The script rewrites the matching `plugin.json` entry's `sourceUrl` to the built filename and warns if the version has no entry yet. Old versioned zips are kept so published entries stay downloadable.
- **Release**: add version + changelog entry in `plugin.json` (full history lives in `CHANGELOG.md` — the version counter was reset to 0.1.0 at the repo restructure; pre-release increments are recorded there), run `./build.sh <version>`, serve the repo root (`python3 -m http.server 8120`), update from the AudioMuse catalog, Apply now (restart).
- **Regenerate fixtures**: `./tests/generate_fixtures.sh <genuine-hires-file> tests/fixtures [ffmpeg] [start] [dur]` (committed set: start=60, dur=45). Calibration constants measured from these fixtures are documented in `tests/README.md` — update both when retuning thresholds.
- The web/jobs layers (`__init__.py`, `jobs.py`) still require a live AudioMuse instance to test.
- **Dependencies**: librosa, numpy (included in AudioMuse-AI), matplotlib (declared in `plugin.json`, auto-installed).
- **Change detection strategy** is a two-layer optimization: (1) metadata fingerprint for cheap skips, (2) audio MD5 to catch byte-level changes. This avoids downloading unchanged files during incremental scans.
- **Spectrogram caching**: base64-encoded PNG stored in Postgres. Database size ~100–300 KB per spectrogram (default 800×280 px); 10k songs ≈ 1–3 GB.
