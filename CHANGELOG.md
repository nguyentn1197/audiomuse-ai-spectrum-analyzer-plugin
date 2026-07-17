# Changelog

## 0.2.2

- Overview shows LOWPASSED counts: a header total and a per-album "Lowpassed"
  column (amber), both excluding manually verified tracks — the ambiguous
  possibly-dark-master bucket worth deep scanning.
- Secondary buttons (Filter, Re-analyze, Deep analyze, Deep scan all
  non-CLEAN) restyled amber/brown; the core's default gray-on-white was barely
  visible. A queued Deep analyze button now renders as clearly disabled.

## 0.2.1

- License: AGPL-3.0-only (LICENSE file + SPDX headers in the shipped modules).

## 0.2.0

- Search matches album, artist and song title (shows albums containing a
  match; per-album counts still cover the whole album).
- Deep-scan queueing is idempotent: repeat clicks while the "deep scan
  queued" tag is set enqueue nothing; the per-song button is disabled while
  pending. A plain Re-analyze clears an orphaned tag (escape hatch for lost
  jobs).
- Overview: "Deep scan all non-CLEAN" queues a whole-file deep scan for every
  unverified non-CLEAN track in the library (default queue) and reports how
  many were queued.

## 0.1.0 — first release

Everything below was developed pre-release; the version counter was reset to
0.1.0 when the repo was restructured (dist/ release flow, committed test
fixtures, unit tests). The original increments are preserved here.

### Pre-release history

- **0.6.2** — Detection recalibrated against real ground-truth fixtures built
  from a confirmed-genuine DSD file: silent-shelf threshold corrected for
  16-bit dither (−68 dB, catches 320 kbps→FLAC fakes that previously passed),
  aliasing images now mirrored around candidate source Nyquists (fixes
  source-rate attribution), hi-res UPSAMPLED requires corroboration (silent
  shelf / aliasing / sharp edge) so gradual dark masters stay CLEAN, deep-scan
  edge-variance thresholds retuned (≤300 wall / ≥2000 dark master) plus a
  pinned-edge check. Test harness in `tests/`.
- **0.6.1** — Album page: "Deep scan all non-CLEAN" button queues a whole-file
  deep scan for every track whose verdict is not CLEAN (verified and
  already-queued tracks are skipped; jobs run on the default queue so all
  workers share them).
- **0.6.0** — Overview filter rework: filter albums by the statuses their
  songs contain via per-verdict chips (any combination), optionally matching
  unverified tracks only. Combines with the min-suspect-count and has-verified
  filters; pagination preserves all filters.
- **0.5.1** — Deep-scan tracking: queuing a deep scan tags the track
  (`deep_pending`), shown as a "deep scan queued" badge on the song and
  cleared automatically when the scan finishes (even on failure). The album
  overview shows per-album tags: "deep scan ×N" (amber) and "verified ×N"
  (green).
- **0.5.0** — Deep analyze button per track: scans the entire file (chunked)
  and tracks the spectral edge over time — an edge that follows the music
  downgrades UPSAMPLED/FAKE_SUSPECT to a likely genuine dark master, a
  constant wall raises confidence. Manual "verified" checkbox per track:
  verified tracks are excluded from all suspect counts, shown as an overview
  column, and filterable.
- **0.4.1** — Fix: album names containing &, ?, # or other URL-special
  characters broke the album page link. All album links and pagination URLs
  are now properly URL-encoded.
- **0.4.0** — Fake 24-bit detection: PCM samples probed for zero-padded low
  bits (new UPSCALED verdict, effective/container bits stored and shown per
  track). UPSAMPLED confidence corroborated by aliasing-image analysis.
- **0.3.1** — UPSAMPLED detection matches the cutoff against every standard
  rate's Nyquist (22.05/24/44.1/48 kHz), so 48 kHz→96 kHz upsamples are
  caught, not just 44.1 kHz sources. Confidence graded by edge sharpness and
  shelf silence.
- **0.3.0** — New UPSAMPLED verdict for fake hi-res: a >48 kHz container whose
  content stops at CD bandwidth flagged as a resampled source instead of
  CLEAN. UPSAMPLED counts as suspect in the overview filters.
- **0.2.0** — Parallel scans: one task per album fanned out across all
  workers, with a parent task tracking done/remaining. Fewer false
  FAKE_SUSPECT verdicts: content reaching ~20.5 kHz counts as full bandwidth,
  and a sharp edge with audible noise above the cutoff is LOWPASSED, not
  FAKE. UI: filter albums by suspect count, re-analyze a whole album.
- **0.1.0 (original)** — Initial release: library scan grouped by album,
  change detection (metadata fingerprint + audio MD5), fake-lossless
  verdicts, base64 spectrograms, per-track re-analysis, on_song_analyzed
  hook, optional weekly incremental cron.
