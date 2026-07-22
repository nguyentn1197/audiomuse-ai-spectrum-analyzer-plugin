# Local detection tests

Ground-truth testing for `dsp.analyze_file` without an AudioMuse instance:
`librosa_stub.py` replaces librosa with soundfile + numpy (only `load` and
`stft` are used), so the real production code runs end to end — cutoff, edge,
shelf, aliasing images, bit-depth probe, verdict logic, deep scan and
spectrogram rendering.

## Run (out of the box)

```bash
python3 -m pip install --user numpy soundfile matplotlib   # once
python3 -m unittest discover tests -v                      # from the repo root
```

`tests/fixtures/` is committed: 45-second ground-truth excerpts built from a
confirmed-genuine DSD64 file — genuine hi-res and CD masters, 44.1→96k and
48→96k upsamples, zero-padded fake 24-bit, 128k/320k MP3→FLAC fakes, a
128k-as-320k MP3 transcode, an honest 320k MP3, and a synthetic dark master —
plus the Phase-1 minimum adversarial set: a narrow ultrasonic pilot tone on a
sharp 15 kHz wall, AAC and ALAC in `.m4a`, Opus and Vorbis in `.ogg`, an
honest low-passed first-generation 320k MP3, half-truncated FLAC and MP3, and
a dithered/DSP-processed 16-into-24-bit FLAC — and the Phase-1b pair: a
quiet-intro 44.1→96k upsample (ref-bias regression) and a genuine hi-res
track with one dark passage (window-disagreement guard). See "Regenerating fixtures"
below for how each is built and `TestAdversarialFixtures` in
`test_verdicts.py` for what each one arbitrates.

## Ad-hoc analysis of arbitrary files

```bash
python3 tests/run_verdicts.py "/path/file.flac::flac::900::CLEAN::seg"
# case format: path::suffix::declared_kbps::EXPECTED_VERDICT::seg|deep
```

## Regenerating fixtures

```bash
./tests/generate_fixtures.sh /path/to/genuine-hires-file tests/fixtures \
    [ffmpeg-binary] [start-seconds] [duration-seconds]
```

The committed set used `start=60 duration=45`. Any confirmed-genuine hi-res
source works; needs ffmpeg with libmp3lame (a johnvansickle.com static build
is fine — extract the .tar.xz with `python3 -m tarfile` if `xz` is missing).

The Phase-1 adversarial set (pilot tone, codec-probe fixtures, truncations,
dithered bit-depth) is regenerated separately, straight from the
already-committed `genuine_cd_1644.flac` — no external source needed:

```bash
python3 tests/generate_adversarial_fixtures.py [--ffmpeg /path/to/ffmpeg]
```

Only the MP3/AAC/ALAC/Opus/Vorbis fixtures need a real ffmpeg (with
libmp3lame, aac, alac, libopus, libvorbis — the `zackees/ffmpeg_bins` or
johnvansickle.com static builds both have all five); the pilot tone,
truncations, and dithered fixture are pure numpy/soundfile and don't. Running
the script with no ffmpeg on PATH regenerates those three and leaves the
other six untouched.

## Calibration notes (measured on these fixtures, 2026-07)

- Shelf above a transcode/resample cutoff: −73…−106 dB relative to reference
  (16-bit dither refills the encoder's digital silence — hence
  `SILENT_SHELF_DB = -68`, not a float-era −120).
- Genuine dark-master noise shelf: ~−53 dB.
- Per-second edge variance (deep scan): encoder wall ±198 Hz, content-varying
  upsample ±1035 Hz, genuine dark master ±7236 Hz — hence the ≤300 / ≥2000
  thresholds.
- Robust cutoff detection (`_find_cutoff`): all widths in hertz, converted to
  bins per file. Smoothing 100 Hz, minimum sustained bandwidth 250 Hz,
  occupancy 75% (gaps up to 120 Hz bridged first), pilot-tone/spike exclusion
  below 80 Hz. Calibrated by requiring the full fixture suite's verdicts to
  stay unchanged (they did, on the first pass); `tests/test_verdicts.py::
  TestCutoffDetection` covers the mask logic (gap tolerance, min-width
  rejection, tone exclusion, sample-rate independence) with synthetic
  spectra. `pilot_tone_lowpass.flac` (minimum adversarial fixtures) is the
  real-fixture counterpart: a true brick-wall 15 kHz lowpass plus a narrow,
  loud 19 kHz tone. The tone's amplitude turned out to matter more than
  expected — a Hann-windowed STFT spreads even a stationary pure tone across
  many more bins than its "true" width via sidelobe leakage, and the 100 Hz
  smoothing widens it further; loud enough to clear the −40 dB threshold but
  quiet enough to stay under the 80 Hz narrow-tone cutoff after smoothing
  turned out to be a narrow amplitude window (worked at 0.0006–0.001 in
  [-1, 1] float scale against this fixture's content; both louder and much
  quieter missed). `TestAdversarialFixtures::
  test_pilot_tone_does_not_drag_up_cutoff` confirms the detected cutoff
  stays at the wall (~15 kHz), not the tone (~19 kHz), and that the tone
  trips `details.evidence.narrow_high_frequency_tone_present`.
- Codec-aware transcode gating (`_verdict`'s `TRANSCODED_LOSSY` branch):
  verdict-changing effect restricted to `_CALIBRATED_TRANSCODE_CODECS =
  {'mp3'}` (the only codec `_expected_cutoff_for_bitrate`'s table is tuned
  against); `_TRANSCODE_GATE_CONFIDENCE_PENALTY = 0.10` (floored at 0.5) on
  top of the existing margin formula, plus a note naming the
  intentionally-low-passed-first-gen alternative, even for MP3.
  `tests/test_verdicts.py::TestCodecGating` covers the gate logic (calibrated
  vs. uncalibrated vs. unknown codec, the mp3 suffix-trust fallback, the
  ffprobe tier-3 fallback's failure/success paths) with synthetic/mocked
  calls; `test_transcode_gate_mp3_reduced_confidence` confirms the real
  `transcoded_128as320.mp3` fixture still verdicts `TRANSCODED_LOSSY` at
  reduced confidence. `honest_lowpassed_320.mp3` (minimum adversarial
  fixtures — a true 15 kHz brick-wall source, single-generation 320k MP3
  encode, never touched by a second lossy pass) is the real
  low-passed-first-gen counterpart the gate can't actually rule out: it
  *does* still verdict `TRANSCODED_LOSSY` (confidence ~0.58, well under the
  ~0.9 ceiling — `TestAdversarialFixtures::
  test_honest_lowpassed_mp3_gate_fires_at_reduced_confidence`), confirming
  the gate is evidence, not proof, exactly as designed — not a case the
  detector is expected to get right by itself.
- Distributed segment sampling (`_load_windows`): `_WINDOW_MIN_SECONDS = 5`
  (window viability floor, mirrors `_load_segment`'s old bar),
  `_WINDOW_NEAR_SILENT_REF_DB = -85` dB (conservative on purpose — only true
  silence/dropouts should be skipped, not quiet-but-real content),
  `_WINDOW_AGREEMENT_TOLERANCE_HZ = 1500` Hz (mirrors the existing
  transcode-detection margin scale). `_NATIVE_WINDOW_POSITIONS` (5 windows,
  SoundFile-seekable containers) and `_FFMPEG_WINDOW_POSITIONS` (3 windows,
  everything else) are still not fixture-calibrated — none of the committed
  fixtures (including the new `lossy_aac_256.m4a`/`lossless_alac.m4a`) vary
  bandwidth mid-track, so there's nothing to arbitrate `windows_agree`
  against, and the "m4a perf spot check" that would decide whether to raise
  the ffmpeg tier from 3 to 5 windows needs real timing data from a real
  library, not a 45s fixture — both stay deferred past "minimum adversarial
  fixtures". What that item *did* resolve: a real m4a fixture now exists to
  exercise the ffmpeg-tier decode path itself end-to-end (needs a real
  `ffmpeg` binary — gated behind `shutil.which('ffmpeg')` in
  `TestAdversarialFixtures`, since this repo's own dev/CI environment has
  none). `tests/test_verdicts.py::TestDistributedSampling` covers the
  window-offset/silence-detection logic synthetically, the real committed
  45s FLAC/MP3 fixtures confirm the 5-window native tier actually engages
  (`test_native_fixture_uses_multiple_windows`), and the ffmpeg tier's
  failure path is exercised for real (ffmpeg genuinely absent in normal dev/
  CI runs here) with its success path covered by a mocked `subprocess.run`
  — and now, when a real `ffmpeg` happens to be on PATH, by the m4a fixture
  for real too.
- Progressive integrity states, real-file counterpart: `truncated.flac` and
  `truncated.mp3` (both a byte-level half-truncation of an existing 45s
  fixture — no new source audio) turned out to exercise two genuinely
  different backend behaviors, both correct and both worth pinning down.
  FLAC: `soundfile.info()` still reports the original (stale, header-encoded)
  duration, but any read that reaches the truncation point raises ("flac
  decoder lost sync") rather than returning a short result — segment mode's
  windows before the truncation point still decode fine
  (`sampled_decode_ok`/`sampled`), deep mode's single large sequential read
  hits the corruption on its very first chunk and fails outright
  (`decode_failed`/`None`, INCONCLUSIVE) — the round-5 correction this
  documents: a per-mode expectation, not "detection missed a truncation".
  MP3: the frame-based decoder instead returns a clean short read right at
  the truncation point with no error at all — both modes report a normal
  decode-ok status (`sampled_decode_ok`/`sampled` and
  `full_decode_ok`/`full`) even though the file is half its declared length.
  This is the documented, deliberate non-goal from the container/codec-probe
  item ("distinguishing a genuine mid-file decode error from a backend that
  raises at true EOF instead of returning a short read") shown for real, not
  a gap this item was meant to close. See
  `TestAdversarialFixtures::test_truncated_flac_partial_windows_vs_deep_decode_failure`
  and `::test_truncated_mp3_decodes_short_without_raising`.
- Phase 1b segment-mode verdict wiring (field-validated 2026-07-22 against
  five real 96 kHz tracks, see IMPROVEMENT_PLAN.md "Phase 1b"):
  - **Program-level shelf reference**: the shelf is measured against the
    loudest valid window's 1–8 kHz median (`shelf_ref_db` in details), not
    the verdict window's own ref — the minimum-cutoff window is
    systematically a quiet one, and its own ref inflates the shelf by the
    ref deficit (field: a confirmed 96k upscale read −61.9 dB vs its quiet
    window's ref but −77.5 dB vs program level). `SILENT_SHELF_DB = -68`
    unchanged — it still splits the populations under the new reference
    (fixture fakes −73…−108, dark master −56.4, genuine DSD −45.6).
  - **`_DEEP_SILENCE_SHELF_DB = -85`**: a quiet-but-genuine passage's shelf
    also reads "silent" against the program ref (−71.7 field-measured), but
    a resampler's floor sits far deeper (−103…−108 on the upsampled
    fixtures) — the variable-bandwidth guard uses this floor to tell them
    apart.
  - **`_WINDOW_AGREEMENT_TOLERANCE_HZ = 1500` now field-calibrated**:
    constant walls and genuine full-bandwidth masters spread 108–727 Hz
    across windows; content-varying bandwidth spreads 2156–7617 Hz.
    Agreement is asymmetric evidence — a fake can still *disagree*
    (resampler image leakage pushed `fake_hires_44to96`'s spread to
    2320 Hz), which is also why the envelope source-rate label
    (`_consensus_source_rate`) is gated on agreement: ungated it would
    mislabel that fixture 48 kHz.
  - `upsampled_quiet_intro_44to96.flac`: 44.1→96k FFT upsample with a
    moderate-slope wall (−30 dB over ~750 Hz — too soft for the 25 dB/kHz
    sharp gate, like a real SRC filter), the first 18 s (exactly the first
    sampled window) 25 dB quieter with an HF tilt so the *quiet* window
    drives the verdict, plus a calibrated broadband noise floor (shelf
    −51.5 dB vs the quiet window's own ref = the old escape; −73.1 dB vs
    program ref = correctly silent). Pins the ref-bias regression:
    reverting the program ref flips it back to a false CLEAN
    ("dark master?"). Windows agree (spread 211 Hz) → also exercises the
    constant-wall corroboration note/confidence and the 44.1 kHz envelope
    label.
  - `variable_bandwidth_96k.flac`: genuine hi-res content whose first 18 s
    are 20 dB quieter with a gradual HF rolloff (window cutoff ~19.5 kHz,
    below the full-bandwidth threshold) while the other windows stay full
    bandwidth (up to 48 kHz) — windows disagree, widest reaches full
    bandwidth, no machine signature (edge 4.5 dB/kHz, shelf −79.6 dB vs
    program ref, above the −85 deep-silence floor thanks to a calibrated
    acoustic-floor noise) → CLEAN 'variable bandwidth (content-dependent)',
    pinning the disagreement guard that fixes the field false LOWPASSED.
  - `TestPhase1b` in `test_verdicts.py` asserts the mechanisms (ref gap,
    old-ref escape arithmetic, guard preconditions, corroboration-only
    pinning via `dark_master_96k` — which *also* pins at a resample-matched
    frequency and must stay CLEAN — and the 48 kHz envelope label on
    `fake_hires_48to96`).
- Bit-depth histogram, real-file counterpart: `dithered_1624.flac` (16-bit
  source, ~3% of samples given genuine content below the 16-bit line —
  simulating a 32-bit float DSP pass before a 24-bit export, not a plain
  zero-pad) confirms the exact trailing-zero test correctly does *not* fire
  (`effective_bits: 24`, unlike `fake_24bit_96k.flac`'s exact `16`), while
  the histogram still surfaces `predominant_bit_depth: 16` and a
  `lower_bit_activity_fraction` around 3% as an informational note — exactly
  the "a few genuinely deeper samples" case the histogram was built to
  describe without ever changing the verdict. See
  `TestAdversarialFixtures::test_dithered_upscale_not_flagged_exact_but_noted_statistically`.
