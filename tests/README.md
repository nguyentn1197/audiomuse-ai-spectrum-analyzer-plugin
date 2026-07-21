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
128k-as-320k MP3 transcode, an honest 320k MP3, and a synthetic dark master.

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
  stay unchanged (they did, on the first pass) — a real narrow-pilot-tone
  fixture is deferred to the "minimum adversarial fixtures" plan item;
  meanwhile `tests/test_verdicts.py::TestCutoffDetection` covers the mask
  logic (gap tolerance, min-width rejection, tone exclusion, sample-rate
  independence) with synthetic spectra.
