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


# --------------------------------------------------------------------------
# Container-damage variants for integrity.py. Deliberately NOT committed:
# each is a deterministic byte transform of an already-committed fixture,
# so tests/test_integrity.py builds them into a temp dir on the fly (no
# ~4 MB binary per variant in the repo). `--integrity` writes them out for
# manual inspection (run_verdicts.py, the debug app, a real AudioMuse).

def _plugin_integrity():
    import sys
    plugin_dir = os.path.join(os.path.dirname(_HERE), 'plugins', 'SpectrumAnalyzer')
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    import integrity
    return integrity


def _frames(raw):
    """(info, audio_start, [(offset, header len, number, block size)]) of a
    clean FLAC -- the frame boundaries the transforms below cut along."""
    info, audio_start, _strategy, chain = _plugin_integrity().frame_index(raw)
    assert info is not None and chain, 'source must be a clean native FLAC'
    return info, audio_start, chain


def zero_fill_burst(raw, at=0.6, stride=1348, run=1158, holes=(0, 4, 9, 11, 14, 15)):
    """The real-world case this check was built for: a transfer that lost
    chunks and filled them with zeros -- `run` zero bytes on a `stride`-byte
    grid, a burst of `holes` starting `at` of the way into the audio. Works
    on any container (no frame parsing)."""
    out = bytearray(raw)
    start = len(raw) // 10 if raw[:4] != b'fLaC' else _frames(raw)[1]
    base = start + int(at * (len(raw) - start))
    for k in holes:
        s = base + k * stride
        out[s:s + run] = b'\x00' * run
    return bytes(out)


def bit_flip(raw, frame=None):
    """Bit rot: one flipped bit in the middle of one frame's body. A decoder
    often plays the garbage without complaint; only the frame CRC sees it.
    Returns (bytes, frame number, first sample of that frame)."""
    _info, _start, chain = _frames(raw)
    i = len(chain) // 2 if frame is None else frame
    p, hl, num, bs = chain[i]
    end = chain[i + 1][0]
    out = bytearray(raw)
    out[p + hl + (end - p - hl) // 2] ^= 0x10
    return bytes(out), num, num * chain[0][3]


def drop_frame(raw, frame):
    """A whole frame missing (header and body): the frame numbering jumps."""
    _info, _start, chain = _frames(raw)
    return raw[:chain[frame][0]] + raw[chain[frame + 1][0]:]


def cut_at_frame(raw, frame):
    """Truncated exactly on a frame boundary: every remaining frame passes
    its CRC, so only STREAMINFO's total sample count reveals the loss."""
    _info, _start, chain = _frames(raw)
    return raw[:chain[frame][0]]


def _md5_offset(raw):
    return _plugin_integrity().flac_offset(raw) + 4 + 4 + 18


def md5_flip(raw):
    """Every frame intact but STREAMINFO's MD5 no longer matches the audio
    (what `flac -t` reports as an MD5 mismatch)."""
    out = bytearray(raw)
    out[_md5_offset(raw)] ^= 0xFF
    return bytes(out)


def md5_zero(raw):
    """An encoder that never computed the MD5 (all zeros = unset)."""
    o = _md5_offset(raw)
    return raw[:o] + b'\x00' * 16 + raw[o + 16:]


def id3v2_prefix(raw, size=100_000):
    """A leading ID3v2 tag (non-standard on FLAC but common) larger than any
    fixed sniff window, as embedded cover art makes it."""
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                       (size >> 7) & 0x7F, size & 0x7F])
    return b'ID3\x04\x00\x00' + synchsafe + b'\x00' * size + raw


def id3v1_suffix(raw):
    return raw + b'TAG' + b'x' * 125


def junk_suffix(raw, n=4000, seed=0):
    """Random bytes appended after the last frame. Decoders play the file to
    completion, so this is reported as trailing data, never as damage."""
    return raw + np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8).tobytes()


def _coded_number(v):
    """FLAC's UTF-8-like frame/sample number coding (up to 36 bits)."""
    if v < 0x80:
        return bytes([v])
    for n, bits in ((2, 11), (3, 16), (4, 21), (5, 26), (6, 31), (7, 36)):
        if v < (1 << bits):
            tail = []
            for _ in range(n - 1):
                tail.append(0x80 | (v & 0x3F))
                v >>= 6
            lead = 0xFE if n == 7 else (0xFF << (8 - n)) & 0xFF
            return bytes([lead | v] + tail[::-1])
    raise ValueError('number too large for FLAC coding')


def variable_blocksize(raw):
    """Rewrite a fixed-block-size FLAC as a variable-block-size stream: every
    header gets the variable strategy bit and the frame's first sample
    number instead of its frame index, with both CRCs recomputed. The audio
    is untouched, so it must decode sample-identical to the source."""
    integ = _plugin_integrity()
    _info, audio_start, chain = _frames(raw)
    nominal = chain[0][3]
    end_of_audio = len(raw)
    out = bytearray(raw[:audio_start])
    for i, (p, hl, num, _bs) in enumerate(chain):
        end = chain[i + 1][0] if i + 1 < len(chain) else end_of_audio
        old_num_len = len(_coded_number(num))
        # header = sync(2) b2 b3 | number | optional bs/sr bytes | crc8
        extra = raw[p + 4 + old_num_len:p + hl - 1]
        head = bytes([0xFF, 0xF9, raw[p + 2], raw[p + 3]]) + \
            _coded_number(num * nominal) + extra
        head += bytes([integ._crc8(head)])
        body = raw[p + hl:end - 2]
        frame = head + body
        crc = integ._crc16(frame)
        out += frame + crc.to_bytes(2, 'big')
    return bytes(out)


INTEGRITY_VARIANTS = {
    'corrupt_zerofill.flac': lambda cd, mp3: zero_fill_burst(cd),
    'corrupt_bitflip.flac': lambda cd, mp3: bit_flip(cd)[0],
    'corrupt_dropped_frame.flac': lambda cd, mp3: drop_frame(cd, 200),
    'corrupt_cut_on_boundary.flac': lambda cd, mp3: cut_at_frame(cd, 400),
    'corrupt_md5.flac': lambda cd, mp3: md5_flip(cd),
    'variable_blocksize.flac': lambda cd, mp3: variable_blocksize(cd),
    'corrupt_zerofill.mp3': lambda cd, mp3: zero_fill_burst(mp3),
}


def gen_integrity_variants(out):
    with open(CD, 'rb') as f:
        cd = f.read()
    with open(os.path.join(FIXTURES, 'consistent_320.mp3'), 'rb') as f:
        mp3 = f.read()
    for name, make in INTEGRITY_VARIANTS.items():
        with open(os.path.join(out, name), 'wb') as f:
            f.write(make(cd, mp3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ffmpeg', default='ffmpeg')
    ap.add_argument('--out', default=FIXTURES)
    ap.add_argument('--integrity', metavar='DIR',
                    help='only write the (uncommitted) container-damage variants to DIR')
    args = ap.parse_args()
    if args.integrity:
        os.makedirs(args.integrity, exist_ok=True)
        gen_integrity_variants(args.integrity)
        print(f'container-damage variants written to {args.integrity}')
        return
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
