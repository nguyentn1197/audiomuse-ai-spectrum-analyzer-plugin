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
  - `scan_library_job()` iterates albums from the media server, implements change detection (fingerprint → MD5), and upserts results.
  - `on_song_analyzed()` hook piggybacks on core analysis when the audio is already on disk (no extra download).

- **`dsp.py`** — Audio DSP (librosa + numpy) and analysis pipeline:
  - `_load_segment()` extracts a 90s segment (default: middle of track to skip intros/fades).
  - `_spectrum()` computes STFT, reduces to per-frequency 95th-percentile profile (robust against silence/clicks).
  - `_find_cutoff()` locates the highest frequency within a drop threshold (default: 40 dB below 1–8 kHz reference).
  - `_edge_sharpness()` measures rolloff steepness in dB/kHz (encoder low-pass filters are near-vertical; natural rolloff is gradual).
  - `_verdict()` decision logic: full bandwidth → CLEAN; lossless + sharp edge → FAKE_SUSPECT; gradual rolloff → LOWPASSED; lossy with low cutoff relative to declared bitrate → TRANSCODED_LOSSY; otherwise CONSISTENT_LOSSY.
  - `_render_spectrogram()` matplotlib-based PNG (spek-style color map, red line at detected cutoff), base64 encoded, tunable in settings.

### Database

Results table with foreign key to core's `score(item_id)` (cascades on delete). Fingerprints and MD5s stored to avoid redundant downloads/analysis.

### Plugin integration points

- Settings page for segment length, cutoff threshold, spectrogram dimensions, and optional on_song_analyzed hook toggle.
- Optional cron task (`plugin.spectrum_analyzer.scan_changed`, seeded disabled, Sunday 04:00).
- Manual re-analyze button per track (enqueued to the `high` queue).

## Development notes

- **No local tests** — the plugin is tightly coupled to the AudioMuse-AI framework. Testing requires installing in an AudioMuse instance.
- **Dependencies**: librosa, numpy (included in AudioMuse-AI), matplotlib (declared in `plugin.json`, auto-installed).
- **File distribution**: the plugin source is zipped as `spectrum_analyzer.zip` and served locally via `python3 -m http.server 8000`.
- **Change detection strategy** is a two-layer optimization: (1) metadata fingerprint for cheap skips, (2) audio MD5 to catch byte-level changes. This avoids downloading unchanged files during incremental scans.
- **Spectrogram caching**: base64-encoded PNG stored in Postgres. Database size ~100–300 KB per spectrogram (default 800×280 px); 10k songs ≈ 1–3 GB.
