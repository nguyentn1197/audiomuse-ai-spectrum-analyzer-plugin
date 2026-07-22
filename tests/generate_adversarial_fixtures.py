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
