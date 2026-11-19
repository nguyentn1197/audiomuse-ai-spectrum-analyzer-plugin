# Changelog

## 0.5.0 — Damaged-file detection

Whether a file is authentic and whether it is intact are separate questions,
and the spectrum can't answer the second. A FLAC with zero-filled or
truncated frames decodes cleanly right up to the damage, so a normal scan
whose sampled windows miss it (or silently drop the window that hits it)
reported CLEAN. The garbage a decoder makes of a broken frame is broadband
noise, so the spectral reading of a damaged file isn't evidence either way.
The new `integrity.py` reads damage from the container's own checksums.

### What is checked

- **FLAC, every scan:** every frame header (CRC-8) and body (CRC-16),
  verified in numpy without decoding or spawning anything (~0.05–0.3 s per
  track, ~13 % of segment-analysis time on the fixtures). The frame
  sequence is checked against STREAMINFO's total sample count, so a file
  cut exactly on a frame boundary, which no per-frame CRC can see, is
  caught too. Fixed and variable block size, ID3v2-prefixed files of any tag
  size.
- **FLAC, deep scans:** the decoded stream is also compared with STREAMINFO's
  MD5. That is the same pair of checks `flac -t` runs.
- **Other formats, deep scans only:** a full ffmpeg decode with CRC and
  bitstream checks on, plus the decoded length against the declared
  duration. This catches `truncated.mp3`, which decodes without a single
  error. MP3, PCM and ALAC carry few or no checksums, so a pass means
  "decodes cleanly to its declared length". It is deep-only because one
  subprocess plus a full decode per track is too much for a library scan.
- Bytes after the last frame (ID3v1/APE tags, padding, junk) are reported in
  `trailing_bytes` and never count as damage, since libFLAC and ffmpeg both
  play such files to completion.

### Result

- New primary verdict **`DAMAGED`**, kept separate from `INCONCLUSIVE`:
  "this file is broken, replace it" and "this file couldn't be analyzed"
  call for different actions. A damaged file gets `integrity.status =
  corrupt` (coverage `full`), `confidence` 1.0 (the evidence is checksums
  or decoder errors, not a heuristic) and `deep_eligible = false`.
  `est_source` gives the bad-frame count and the merged regions as plain
  ASCII. The spectral reading it would have had is kept as a note, along
  with all measurements and the spectrogram.
- Overview: a damaged count next to suspect / lowpassed / inconclusive, a
  Damaged column in the album table, and albums sorted by suspects, then
  damaged. The verdict gets its own badge and filter chip. `DAMAGED` is not
  a suspect verdict, and "deep scan all non-clean" skips it.
- This amends the plan's verdict/substatus contract (INCONCLUSIVE was the
  only non-authenticity verdict). `DAMAGED` clears all three objections that
  contract guarded against: it can't vanish from the UI, pollute suspect
  counts, or be swept into bulk deep scans. Amendment recorded in AGENTS.md
  and IMPROVEMENT_PLAN.md.
- New `details.integrity.stream`: method, result, frames / bad / missing,
  truncated, damaged seconds, regions, zero-filled bytes (the
  transfer-gap fingerprint), trailing bytes, MD5 status, or the ffmpeg
  errors and durations. It is `null` when no check ran.
- A decode failure on a damaged FLAC comes back `DAMAGED` with the
  location. One with no provable damage (no FLAC marker, no ffmpeg check)
  stays `INCONCLUSIVE`.
- `ANALYSIS_REV` 7 → 8: every stored row is re-checked once on the next scan.

### Review fixes (before release)

- **Final FLAC frame.** The last frame has no next header to bound it. It
  used to be bounded at the first place a running CRC-16 reached zero, but
  in an intact frame that also happens by chance. After such a point every
  corruption verified: 146/200 single-byte corruptions of a one-frame file
  passed. Its end is now found by walking its structure the way a decoder
  does (subframes, wasted bits, warm-up samples, LPC coefficients,
  Rice/escape residual partitions, padding), and the CRC is checked there.
  The walker lands exactly on the next header on every frame of every
  fixture, and on ffmpeg-encoded mono/6-channel/24/32-bit and
  compression-level 0/12 streams. Final-frame corruptions caught: 1000/1000
  across five encodings. Trailing bytes that start with a frame sync are a
  damaged final frame, not junk.
- **Multi-stream containers.** The truncation check compares against the
  audio stream's own duration, never the container's. A valid MP4 with 4 s
  of video and 1 s of audio was flagged truncated. The container duration
  is used only when the stream reports none and audio is the only stream
  (Matroska).
- **MD5 at every FLAC depth (4–32 bit).** libsndfile handles 8/16/24-bit;
  ffmpeg handles every other depth and is the fallback. A skipped check is
  never silent: `md5: not_checked` comes with a new `md5_detail` saying
  why.

### Tests

- `tests/test_integrity.py` (44 tests). Damage variants are deterministic
  transforms of committed fixtures, built on the fly rather than committed:
  zero-fill burst, single bit flip, dropped frame, cut on a frame boundary,
  header damage, MD5 flip/zero, large ID3v2 prefix, ID3v1 and junk suffixes,
  a variable-block-size rewrite (verified sample-identical), scattered
  damage beyond the region cap, and a zero-filled MP3. Review regressions:
  byte flips inside the final frame (including 120 positions of a one-frame
  file), the structural frame end checked against the next header on every
  fixture, a damaged final header with no declared total, an MP4 whose
  audio is shorter than its video, audio-only Matroska, and MD5 at
  4/8/12/16/20/24/32 bit. The odd depths come from a new spec-complete
  verbatim FLAC writer, cross-checked by libsndfile and ffmpeg. A 32-bit
  file from ffmpeg's own encoder is also covered, and the no-ffmpeg path is
  tested.
- No false alarms: every committed FLAC fixture passes every frame and its
  MD5. ffmpeg reports zero errors on every committed MP3/M4A/OGG.
- Changed expectations: `truncated.flac` is now caught in segment mode too.
  The old test pinned that as a documented limitation, not as the desired
  outcome. Both modes now read `DAMAGED`/`corrupt`; deep mode used to give
  `INCONCLUSIVE`/`decode_failed`.
  `truncated.mp3` is caught by deep scans when ffmpeg is present. Exact
  `details.integrity` comparisons gained the `stream` key.
- Field check: a real 16-bit/44.1 kHz FLAC damaged in a download
  (54 zero-filled 1158-byte holes on a 1348-byte grid) is reported as 29
  bad frames, 6 missing, 62,532 zero-filled bytes, at 2:41.9-2:46.7 and
  3:01.0-3:01.6, matching a manual frame-by-frame analysis. Before, it was
  CLEAN in both modes. A clean 24-bit/48 kHz copy of the same track passes,
  MD5 included.

## 0.4.0 — AudioMuse-AI 3.6.4

AudioMuse-AI 3.2 replaced Redis/RQ with a task queue living in Postgres, and
3.3.1 moved the core to numpy 2 and librosa 1.0. This release supports
AudioMuse-AI 3.6.4 only and runs on the Postgres queue alone.

### Support policy

- Requires AudioMuse-AI 3.6.4 or newer (`min_core_version` 3.6.4). Cores older
  than 3.6.4, including 2.x and the 3.x line before it, are no longer supported;
  stay on 0.3.3 for them.
- The RQ queue is no longer read. A deep-scan claim recorded against it is
  quarantined as `outcome_unknown`, so it is only ever retried through the
  explicit requeue action, which asks for confirmation first.

### Ids and legacy claims

- A track on a secondary server that the registry cannot map to a catalogue id
  is counted as `unmapped` and skipped. It is never stored under its native id,
  which `score` does not use. Native ids the registry returns for the default
  server are still valid catalogue ids. The count is in the task details and
  the log; no page renders it yet.
- A registry failure fails the album, or skips it while the library scan plans
  it, and is logged. It never falls back to native ids.
- The `on_song_analyzed` hook keys its row by the core's catalogue id, resolves
  one through the registry when the core does not supply it, and skips the track
  when no id resolves.
- Deep-scan claims on the retired RQ queue (`deep_queue_backend = 'rq'`) are
  quarantined by the recovery cron whatever their completion marker says, since
  nothing can confirm how they ended.

### Verification

- Run on a live AudioMuse-AI 3.6.4 (Postgres 15, Navidrome): install and
  migration, incremental and forced scans, the `on_song_analyzed` hook (one
  correctly keyed row per analyzed track), a deep scan through its whole claim
  lifecycle, the recovery cron quarantining seeded `rq` claims, and cancelling a
  scan mid-flight. Not run: a multi-server core, and the single-album and
  per-track rescan buttons.
- `tests/test_postgres_integration.py` runs the migration and the claim,
  quarantine and reconciler SQL against a real Postgres (opt-in via
  `SA_TEST_PG_DSN`). It caught a tz-aware/naive comparison in the recovery
  cron's grace check, fixed before release.

### The queue rewrite (core 3.2)

- **Scan task rows follow the core's id again.** `jobs._rq_task_id()` read the
  running RQ job's id; `rq` is not installed from 3.2, so it returned `None` and
  every scan invented a uuid instead. That left a second row in Active Tasks
  beside the queue's own, made the Cancel button tombstone a row nothing was
  polling (so cancelling a scan no longer stopped it launching more albums), and
  produced a `func IS NULL` row exposed to the core's stale-row sweep. Replaced
  by `jobs._current_task_id()`, which reads `taskqueue.current_task_id()`.
- **Lost album jobs are detected again.** `enqueue()` returns a task-id string
  on 3.2+ where it used to return an RQ `Job`, so the `job.get_status()` probe
  raised `AttributeError` into a bare `except` and silently never fired: a scan
  whose worker died drained forever waiting on a child that would never report.
  `_child_job_lost` now polls the child's queue row.
- **A long backlog no longer ends a scan early.** The core fails any live task
  row it does not own once it has gone 30 minutes without an update. Album child
  rows are written once at launch, so a child waiting behind a big library was
  failed out from under the parent, counted done, and the scan reported finished
  while the album was still queued. `_touch_live_children` refreshes the rows
  that actually approach the deadline.
- **Bulk actions work again.** The 3.2 queue guard counts live plugin tasks, not
  just core ones, so the first track a route queued blocked the second: every bulk
  button — Deep scan all non-CLEAN, Force re-queue, bulk selected, album
  re-analyze without an album id — would have queued exactly one track. They now
  start a single orchestrator task (`bulk_analyze_job`) that claims the tracks and
  feeds them onto the default queue from inside a worker, capped at
  `MAX_QUEUED_ANALYSIS_JOBS` in flight, reporting tracks done/remaining under
  Active Tasks — the same shape the library scan uses for albums. Cancelling it
  untags whatever it claimed but never launched. The overview's green
  "Queued N deep scans" banner becomes "started — follow progress under Active
  Tasks", because N is now decided when the job starts, not when you click.
  The orphan-recovery cron feeds its backlog through the same throttle.
- **Buttons explain a busy queue instead of returning 500.** From 3.2 the core
  refuses a plugin enqueue issued from the web process while another queue job
  is running. Scan, Re-analyze, Deep analyze, the bulk actions and Re-analyze
  album now report that and stop, and the deep-scan buttons undo the
  "deep scan queued" tag they had already written, so a refused click can no
  longer leave a track stuck until the orphan-recovery TTL. Force-requeue only
  bumps `deep_pending_since` for rows that actually reached the queue, so a row
  it could not queue stays visible to the recovery cron.

### The dependency bump (core 3.3.1)

- **Deep scans of m4a/mp4/ALAC/opus/wma work again.** librosa 1.0 removed the
  audioread backend, so `librosa.load` reads only what libsndfile can open.
  Normal scans already had an ffmpeg tier for those containers; deep scans had
  none and died on their first chunk — a deep scan of an ALAC `.m4a` came back
  INCONCLUSIVE. Both the deep scan and the single-window fallback now fall back
  to ffmpeg. The fallback is deliberately limited to containers libsndfile
  cannot open at all: for a corrupt FLAC, the decode failure is the finding, and
  ffmpeg decoding its intact prefix would launder that into a verdict.
- **matplotlib floor raised to `>=3.10`** for the core's numpy 2.4.6. matplotlib
  3.8's C extensions are built against the numpy 1.x ABI and fail to import
  under numpy 2, and pip does not upgrade an already-satisfied requirement.

### Queue-aware deep-scan recovery

The deep-scan "pending" tag was previously recovered by age alone: a track
still tagged after the orphan TTL was re-queued. That treated time as proof a
child was lost, which is wrong now that the queue lives in Postgres — the core
persists queued work and reclaims work whose worker died, so a restart needs no
plugin intervention. Recovery is now a bounded reconciler that only settles the
plugin's own claim markers and never auto-retries an uncertain outcome.

- **Each deep scan is owned by a claim.** Queuing a deep scan (per-track button
  or a bulk action) writes a distinct `deep_claim_id` plus the queued child's
  id, the queue it went into (`postgres`, or `rq` on the retired queue), and a state
  (`claimed`/`launched`/`running`/`failed`/`outcome_unknown`). Every deep-state
  write is fenced on that claim id, so a stale or superseded child can never
  clear a newer claim. `deep_pending` stays set for every active, failed,
  unknown and legacy claim, so the existing boolean deep controls keep
  disabling; retry eligibility is now state-aware.
- **A failed launch no longer strands tags.** A bulk launch failure
  (`_LaunchFailed`) cleans only the never-enqueued normal claims and restores
  the prior record for never-enqueued force-requeue claims (compare-and-set on
  the fresh claim id), then re-raises the original cause so the parent task
  stays FAILURE.
- **The recovery cron is a reconciler, not a retryer.** `recover_deep_pending`
  (name kept, so existing enabled schedules keep working) reads each owned
  claim's real queue state and only settles the plugin's markers: a confirmed
  success or cancel clears the claim, a confirmed failure marks it `failed`,
  and a missing/unreadable row or a task-less claim past the warning grace
  becomes `outcome_unknown`. It never re-queues. A retry is always an explicit
  user action — failed claims use the normal confirmed retry, unknown/task-less
  claims require a separately labelled "retry despite unknown outcome"
  confirmation that warns duplicate work may occur, and live/unreadable claims
  have no retry control.
- **The TTL setting is now a warning.** `deep_orphan_ttl_hours` is renamed
  `deep_pending_warning_hours` (value migrated); it only warns about stale
  claims and never selects work for enqueue.

### Branch remediation

- `ANALYSIS_REV` bumped to 7 so rows stamped under r6 are re-analysed under the
  result-changing ffmpeg decode fallback.
- Overview confidence filtering preserves an explicit `max_conf=0`, treats an
  omitted/empty value as "no bound", and applies both bounds to one matching
  track.

### Also

- Album and library overviews gained a **max confidence %** filter alongside the
  existing minimum, and every verdict chip now shows how many tracks match it.

## 0.3.1

- Self-healing provider-id backfill: library and album scans re-run the
  `provider_track_id`/`server_id` backfill from `track_server_map` at start
  (one cheap idempotent UPDATE). The 0.3.0 install-time backfill was a
  one-shot that could race the core's v3 migration — on a stuck 3.0 boot the
  map table exists but is still empty when the plugin updates (the core
  commits the id relabel and the map in one transaction, on the next boot), so
  the columns stayed NULL. The backfill also repairs rows pointing at a
  media-server entry the core has since re-keyed (delete/re-add or provider
  migration); a scan during such a window reported every track as
  `not_in_score` because the id translation ran against a server id whose
  mappings did not exist yet. No re-analysis needed — once the map is
  consistent, the next scan heals the rows and settles everything as skipped.

## 0.3.0 — AudioMuse-AI 3.0 support

- v3.0's multi-server architecture keys `score` by canonical fingerprint ids
  (`fp_...`) instead of native media-server ids. All id handling now translates
  at the edges (`track_server_map` / `tasks.mediaserver.registry`), fixing scans
  reporting every track as `not_in_score` on 3.x. Results rows are keyed by the
  canonical id; new `provider_track_id`/`server_id` columns keep the native id
  for downloads and re-analysis. The `on_song_analyzed` hook now uses
  `media_item._catalog_item_id` (its `item_id` field is the native id on v3).
- **Lossless data migration**: the results FK is recreated as
  `ON UPDATE CASCADE ON DELETE CASCADE`, so the core's in-place id relabel
  migrates our rows automatically — verdicts, spectrograms, verified flags and
  the MD5 change-detection cache all survive, no re-analysis needed. This also
  fixes core 3.0 boot migrations that were failing/retrying because of the old
  plugin FK (update the plugin + Apply restart; the next boot completes the
  core migration). A defensive re-key via `track_server_map` covers tables
  whose FK was removed. Verified against live Postgres 16 in four upgrade
  scenarios (plugin-first upgrade, stuck 3.0 migration, orphaned rows,
  fresh 3.x install).
- Multi-server scanning: a manual library scan plans across **every**
  configured media server, launching each album child bound to the server it
  was listed on (the core's server binding is a contextvar and does not cross
  RQ job boundaries, so every job binds its own). Tracks shared between
  servers collapse onto one canonical id — no duplicate rows. Per-track
  re-analysis and deep scans bind the server that supplied the row (stored
  `server_id`), so downloads hit the right catalogue. Cron scans follow the
  schedule's server scope (the core binds each run).
- Task rows are keyed by the RQ job id: the 3.x janitor fails any top-level
  task row whose id has no RQ job behind it, so a route-invented uuid row got
  reaped as "orphaned" mid-scan (Error 9999 / "task disappeared from the
  queue"). Routes no longer pre-create rows; each job creates its own, keyed
  by its job id. The orchestrator also reports progress while settling
  unchanged albums, so long planning phases no longer look stalled.
- Hook-analyzed tracks skip the next scan cheaply: `on_song_analyzed` now
  stores the same metadata fingerprint a scan computes (from the raw media
  item), so a follow-up `changed` scan skips those tracks outright instead of
  re-downloading each one to discover its MD5 is unchanged.
- Single codebase supports both 2.6.2 and 3.0.1+ (graceful fallbacks when the
  3.x translation APIs are absent).

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
