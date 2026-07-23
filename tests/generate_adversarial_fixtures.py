#!/usr/bin/env python3
"""Regenerate the "minimum adversarial fixtures" set (IMPROVEMENT_PLAN.md,
Phase 1) from the already-committed base fixtures -- no external source file
needed, unlike generate_fixtures.sh. Two of the nine need a real ffmpeg with
libmp3lame/aac/alac/libopus/libvorbis (a static build from
https://github.com/zackees/ffmpeg_bins or johnvansickle.com works); the rest
are pure numpy/soundfile.

Usage: python3 tests/generate_adversarial_fixtures.py [--ffmpeg PATH] [--out DIR]
"""
import argparse
import os
import shutil
import subprocess

import numpy as np
import soundfile as sf

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(_HERE, 'fixtures')
CD = os.path.join(FIXTURES, 'genuine_cd_1644.flac')
DSD = os.path.join(FIXTURES, 'genuine_dsd_2496.flac')


def _fft_gain_curve(sig, sr, points):
    """Smooth frequency-dependent gain via rFFT multiplication. `points` is
    [(hz, db), ...]; linear dB interpolation between them, endpoints held
    outside the given range."""
    n = sig.shape[0]
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    hz = [p[0] for p in points]
    db = [p[1] for p in points]
    gain = 10.0 ** (np.interp(freqs, hz, db) / 20.0)
    Y = np.fft.rfft(sig, axis=0)
    return np.fft.irfft(Y * gain[:, None], n=n, axis=0)


def _fft_resample(sig, sr, new_sr):
    """Exact FFT-domain upsample (zero-pad the spectrum): everything above
    the old Nyquist is digital zero, like an ideal SRC."""
    n = sig.shape[0]
    new_n = int(round(n * new_sr / sr))
    Y = np.fft.rfft(sig, axis=0)
    out = np.zeros((new_n // 2 + 1, sig.shape[1]), dtype=complex)
    out[:Y.shape[0]] = Y
    return np.fft.irfft(out * (new_n / n), n=new_n, axis=0)


def _gain_envelope(n, sr, quiet_seconds, quiet_db, ramp_seconds=0.5):
    """Per-sample gain: `quiet_db` for the first `quiet_seconds`, ramping
    linearly to 0 dB over `ramp_seconds`, then unity."""
    g = np.ones(n)
    q = int(quiet_seconds * sr)
    r = int(ramp_seconds * sr)
    g[:q] = 10.0 ** (quiet_db / 20.0)
    if r and q + r <= n:
        g[q:q + r] = np.linspace(10.0 ** (quiet_db / 20.0), 1.0, r)
    return g[:, None]


def _brickwall_lowpass(sig, sr, cutoff_hz):
    """True brick-wall FFT lowpass -- much sharper than ffmpeg's biquad
    `lowpass` filter, needed so the detected cutoff lands close to
    `cutoff_hz` regardless of filter order."""
    n = sig.shape[0]
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    Y = np.fft.rfft(sig, axis=0)
    Y[freqs > cutoff_hz] = 0.0
    return np.fft.irfft(Y, n=n, axis=0)


def gen_pilot_tone(out):
    """Narrow ultrasonic pilot tone riding on a sharp 15 kHz wall: robust
    cutoff detection must report ~15 kHz (not drag up to the 19 kHz tone)
    and flag details.evidence.narrow_high_frequency_tone_present."""
    y, sr = sf.read(CD, dtype='float64', always_2d=True)
    lp = _brickwall_lowpass(y, sr, 15000.0)
    t = np.arange(y.shape[0]) / sr
    # amplitude calibrated against dsp._find_cutoff/_exclude_narrow_tones:
    # loud enough to clear the -40dB threshold, quiet enough that 100Hz
    # smoothing keeps it under the 80Hz narrow-tone width after averaging.
    tone = 0.0008 * np.sin(2 * np.pi * 19000.0 * t)
    pilot = lp + tone[:, None]
    sf.write(os.path.join(out, 'pilot_tone_lowpass.flac'),
             pilot.astype(np.float32), sr, subtype='PCM_16')


def gen_honest_lowpassed_mp3(out, ffmpeg):
    """Single-generation 320kbps MP3 of genuinely low-passed (~15kHz)
    content: the calibrated MP3 transcode gate can't tell this apart from a
    real transcode by bandwidth alone, so it must still fire but at reduced
    confidence with the low-passed-first-gen alternative noted."""
    y, sr = sf.read(CD, dtype='float64', always_2d=True)
    lp = _brickwall_lowpass(y, sr, 15000.0)
    src_wav = os.path.join(out, '_lowpass15k_src.wav')
    sf.write(src_wav, lp.astype(np.float32), sr, subtype='FLOAT')
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                     '-i', src_wav, '-c:a', 'libmp3lame', '-b:a', '320k',
                     os.path.join(out, 'honest_lowpassed_320.mp3')], check=True)
    os.remove(src_wav)


def gen_dithered_1624(out):
    """16-bit content upsized to a 24-bit container where ~3% of samples
    carry genuine content below the 16-bit line (simulating a 32-bit float
    DSP pass before export) -- the exact zero-padding test must NOT flag
    UPSCALED, but the histogram's predominant_bit_depth/lower_bit_activity_
    fraction note should still surface it as informational evidence."""
    data, sr = sf.read(CD, dtype='int32', always_2d=True)
    rng = np.random.default_rng(42)
    mask = rng.random(data.shape) < 0.03
    noise = rng.integers(1, 32768, size=data.shape, dtype=np.int64).astype(np.int32)
    data_out = data.copy()
    data_out[mask] = data[mask] + noise[mask]
    sf.write(os.path.join(out, 'dithered_1624.flac'), data_out, sr, subtype='PCM_24')


def gen_codec_fixtures(out, ffmpeg):
    """AAC/ALAC in .m4a (need a real ffmpeg to decode -- libsndfile can't
    open MP4 boxes at all) and Opus/Vorbis in .ogg (soundfile-native, work
    even without ffmpeg at analysis time)."""
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                     '-i', CD, '-c:a', 'aac', '-b:a', '256k',
                     os.path.join(out, 'lossy_aac_256.m4a')], check=True)
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                     '-i', CD, '-c:a', 'alac',
                     os.path.join(out, 'lossless_alac.m4a')], check=True)
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                     '-i', CD, '-c:a', 'libopus', '-b:a', '128k',
                     os.path.join(out, 'lossy_opus_128.ogg')], check=True)
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                     '-i', CD, '-c:a', 'libvorbis', '-q:a', '6',
                     os.path.join(out, 'lossy_vorbis_q6.ogg')], check=True)


def gen_upsampled_quiet_intro(out):
    """Phase 1b ref-bias regression: a 44.1->96k upsample whose verdict
    window is a *quiet* one. The first 18 s (exactly the first sampled
    window) are 25 dB quieter with a mild HF tilt, so the minimum-cutoff
    window -- the one that drives the verdict -- is the quiet intro. A
    broadband noise floor is calibrated so the shelf above the wall reads
    *audible* against the quiet window's own reference (the pre-Phase-1b
    escape: 'genuine dark master') but digitally silent against the
    program-level reference -- the exact shape of the field-validated
    false CLEAN (IMPROVEMENT_PLAN.md Phase 1b). The wall itself is a
    moderate-slope taper (-30 dB over ~750 Hz), like a real SRC filter,
    not a brick wall -- deliberately too soft for the 25 dB/kHz sharp
    gate, so the shelf is the only evidence that can catch it."""
    y, sr = sf.read(CD, dtype='float64', always_2d=True)
    y = _fft_gain_curve(y, sr, [(21300.0, 0.0), (22050.0, -30.0)])
    y96 = _fft_resample(y, sr, 96000)
    n_intro = 18 * 96000
    intro = _fft_gain_curve(y96[:n_intro], 96000,
                            [(8000.0, 0.0), (20000.0, -15.0)])
    y96[:n_intro] = intro
    y96 *= _gain_envelope(y96.shape[0], 96000, 18.0, -25.0)
    rng = np.random.default_rng(7)
    # noise amplitude calibrated against dsp._shelf_level (see
    # tests/README.md): measured shelf -51.5 dB vs the quiet window's own
    # ref (escapes SILENT_SHELF_DB = -68), -73.1 dB vs the program-level
    # ref (correctly silent)
    y96 += 1.3e-5 * rng.standard_normal(y96.shape)
    sf.write(os.path.join(out, 'upsampled_quiet_intro_44to96.flac'),
             y96.astype(np.float32), 96000, subtype='PCM_24')


def gen_variable_bandwidth(out):
    """Phase 1b disagreement-guard regression: a genuine hi-res track with
    one genuinely dark passage. The first 18 s (the first sampled window)
    are 20 dB quieter with a strong but *gradual* HF rolloff, pulling that
    window's detected cutoff below the full-bandwidth threshold while the
    other windows stay full bandwidth: windows disagree, the widest reaches
    full bandwidth, the verdict window has no machine signature (edge
    gradual, shelf above its cutoff kept at a believable acoustic noise
    floor, well above the deep-silence floor) -> CLEAN 'variable
    bandwidth', not the pre-Phase-1b false LOWPASSED."""
    y, sr = sf.read(DSD, dtype='float64', always_2d=True)
    n_dark = 18 * sr
    dark = _fft_gain_curve(y[:n_dark], sr, [(8000.0, 0.0), (20000.0, -30.0)])
    rng = np.random.default_rng(11)
    # acoustic-floor noise: keeps the dark window's shelf audible (measured
    # -79.6 dB vs program ref -- comfortably above _DEEP_SILENCE_SHELF_DB =
    # -85; at 1.8e-5 it measured -84.7, a fragile 0.3 dB margin), the way a
    # real quiet passage keeps room/mic noise, unlike a resampler's silence
    dark += 5.0e-5 * rng.standard_normal(dark.shape)
    y[:n_dark] = dark
    y *= _gain_envelope(y.shape[0], sr, 18.0, -20.0)
    sf.write(os.path.join(out, 'variable_bandwidth_96k.flac'),
             y.astype(np.float32), sr, subtype='PCM_24')


def gen_truncated(out):
    """Byte-truncate committed fixtures to half length: FLAC's decoder loses
    sync and raises (segment mode: partial windows still decode fine ->
    sampled_decode_ok; deep mode's single large read fails outright ->
    decode_failed). MP3's frame-based decoder instead returns a clean short
    read at the truncation point -- no error at all, full_decode_ok on a
    file that never had all its declared audio. Both are real, documented
    backend behaviors, not detection misses (see tests/README.md)."""
    for src_name, dst_name in (('genuine_cd_1644.flac', 'truncated.flac'),
                                ('consistent_320.mp3', 'truncated.mp3')):
        with open(os.path.join(FIXTURES, src_name), 'rb') as f:
            data = f.read()
        with open(os.path.join(out, dst_name), 'wb') as f:
            f.write(data[:len(data) // 2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ffmpeg', default='ffmpeg')
    ap.add_argument('--out', default=FIXTURES)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    gen_pilot_tone(args.out)
    gen_dithered_1624(args.out)
    gen_truncated(args.out)
    gen_upsampled_quiet_intro(args.out)
    gen_variable_bandwidth(args.out)

    if shutil.which(args.ffmpeg):
        gen_honest_lowpassed_mp3(args.out, args.ffmpeg)
        gen_codec_fixtures(args.out, args.ffmpeg)
    else:
        print(f'"{args.ffmpeg}" not found on PATH -- skipping the mp3/aac/alac/'
              f'opus/vorbis fixtures (honest_lowpassed_320.mp3, lossy_aac_256.m4a, '
              f'lossless_alac.m4a, lossy_opus_128.ogg, lossy_vorbis_q6.ogg)')
    print(f'adversarial fixtures written to {args.out}')


if __name__ == '__main__':
    main()
