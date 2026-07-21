# SpectrumAnalyzer - an AudioMuse-AI plugin
# https://github.com/nguyentn1197/audiomuse-ai-spectrum-analyzer-plugin
# Copyright (C) 2026 Nguyen
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root.

"""Spectrum analysis: frequency-cutoff detection ("fake lossless" heuristics)
and spectrogram rendering.

Uses librosa + numpy (built into AudioMuse-AI) and matplotlib (declared in
plugin.json requirements) for the PNG.
"""

import base64
import io
import json
import os

import numpy as np

# Containers we treat as claiming to be lossless.
LOSSLESS_SUFFIXES = {'flac', 'wav', 'alac', 'ape', 'aiff', 'aif', 'wv', 'tta', 'shn'}
LOSSY_SUFFIXES = {'mp3', 'ogg', 'opus', 'aac', 'm4a', 'mp4', 'wma', 'mpc'}

N_FFT = 4096

# Bump when verdict logic changes so stored results re-analyze on the next
# non-force scan (the skip paths in jobs.py require a rev match).
ANALYSIS_REV = 2


def analysis_rev(drop_db, segment_seconds):
    """Revision stamp stored with each result: the analyzer constant folded
    with the verdict-relevant settings (rendering settings excluded)."""
    return f'r{ANALYSIS_REV}-d{drop_db}-s{segment_seconds}'

# Deep scan: analyze the whole file in chunks (bounded so a mislabelled
# 10-hour stream can't eat the worker).
DEEP_MAX_SECONDS = 1800
_DEEP_CHUNK_SECONDS = 120


def _sniff_dsd(path):
    """Recognize DSF/DFF by magic bytes, before any decode is attempted.

    librosa's audioread/ffmpeg fallback will happily decode DSD to PCM, but
    the noise-shaped 1-bit modulator content reads as full-bandwidth audio to
    the spectrum heuristics (a lossy-sourced fake DSD would pass as CLEAN).
    Returns 'dsf', 'dff', or None; suffix-independent since a mislabeled file
    would otherwise slip past a suffix-based check."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:4] == b'DSD ':
        return 'dsf'
    if head[:4] == b'FRM8' and head[12:16] == b'DSD ':
        return 'dff'
    return None


def _load_segment(path, segment_seconds):
    """Load a mono segment at native sample rate. Prefer the middle of the
    track (skip intros/fades); fall back to the start for short files."""
    import librosa

    for offset in (30.0, 0.0):
        try:
            y, sr = librosa.load(path, sr=None, mono=True,
                                 offset=offset, duration=segment_seconds)
        except Exception:
            y, sr = None, None
        if y is not None and sr and y.size >= sr * 5:  # at least 5s of audio
            return y, int(sr), offset
    if y is not None and sr and y.size > 0:
        return y, int(sr), 0.0
    raise ValueError('could not decode audio (empty signal)')


def _spectrum(y, sr):
    """STFT magnitude in dB plus a robust per-frequency 'max hold' profile."""
    import librosa

    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=N_FFT // 4, window='hann'))
    S_db = 20.0 * np.log10(S + 1e-10)
    # 95th percentile over time per bin: robust against quiet passages,
    # still ignores single-frame clicks.
    profile = np.percentile(S_db, 95, axis=1)
    freqs = np.linspace(0, sr / 2.0, S_db.shape[0])
    return S_db, profile, freqs


def _deep_spectrum(path, drop_db):
    """Whole-file analysis in chunks (bounded memory): a global max-hold
    profile, a ~1 column/second display matrix for the spectrogram, and the
    per-second spectral-edge series used to tell a machine low-pass (constant
    wall) from a genuine dark master (edge follows the music)."""
    import librosa

    hop = N_FFT // 4
    profile = None
    display = []
    edge_cutoffs = []
    sr = None
    total = 0.0
    t = 0.0
    chunk_error = False

    while total < DEEP_MAX_SECONDS:
        try:
            y, y_sr = librosa.load(path, sr=None, mono=True, offset=t,
                                   duration=_DEEP_CHUNK_SECONDS)
        except Exception:
            chunk_error = True  # decode error, not a clean end-of-file signal
            break
        if y is None or not y_sr or y.size < y_sr:  # under 1 s left
            break
        sr = int(y_sr)
        S_db = 20.0 * np.log10(
            np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=hop, window='hann')) + 1e-10)
        freqs = np.linspace(0, sr / 2.0, S_db.shape[0])

        chunk_profile = np.percentile(S_db, 95, axis=1)
        profile = chunk_profile if profile is None else np.maximum(profile, chunk_profile)
        band = chunk_profile[(freqs >= 1000) & (freqs <= 8000)]
        chunk_ref = float(np.median(band)) if band.size else float(np.max(chunk_profile))

        n_frames = S_db.shape[1]
        group = max(1, int(round(sr / hop)))  # STFT frames per second
        starts = np.arange(0, n_frames, group)
        for s0 in starts:
            win = np.percentile(S_db[:, s0:s0 + group], 95, axis=1)
            wband = win[(freqs >= 1000) & (freqs <= 8000)]
            if not wband.size or float(np.median(wband)) < chunk_ref - 25.0:
                continue  # quiet second: the edge position is meaningless
            c, _, _ = _find_cutoff(freqs, win, drop_db)
            edge_cutoffs.append(c)
        # ~1 column per second, max-pooled so peaks survive
        display.append(np.maximum.reduceat(S_db, starts, axis=1))

        got = y.size / float(sr)
        total += got
        t += got
        if got < _DEEP_CHUNK_SECONDS - 1.0:
            break  # ran off the end of the file

    if profile is None or sr is None or total < 5.0:
        raise ValueError('could not decode audio for deep analysis')
    freqs = np.linspace(0, sr / 2.0, profile.shape[0])
    capped = total >= DEEP_MAX_SECONDS
    return (np.concatenate(display, axis=1), profile, freqs, sr, total,
            edge_cutoffs, capped, chunk_error)


# Cutoff-detection widths, all specified in hertz and converted to bins from
# each file's actual bin resolution (sr/N_FFT) -- a fixed bin count would
# mean ~160 Hz at 44.1 kHz but ~1.2 kHz at 96 kHz. Initial values picked
# during fixture calibration, recalibrated together since they interact
# (smoothing changes what "sharp" means for _edge_sharpness too).
_CUTOFF_SMOOTH_HZ = 100.0
_CUTOFF_MIN_WIDTH_HZ = 250.0
_CUTOFF_MAX_GAP_HZ = 120.0
_CUTOFF_OCCUPANCY_FRAC = 0.75
# Narrower than a real sustained band: an isolated run this short is a pilot
# tone or numerical spike, not bandwidth -- excluded before gap-closing so it
# can't get bridged into a genuine band and drag the reported cutoff up.
_CUTOFF_TONE_MAX_WIDTH_HZ = 80.0


def _hz_to_bins(width_hz, bin_hz):
    return max(1, int(round(width_hz / bin_hz))) if bin_hz > 0 else 1


def _smooth_profile(profile, bin_hz, smooth_hz=_CUTOFF_SMOOTH_HZ):
    """Light moving-average smoothing so a single noisy bin can't flip the
    threshold mask; edge-replicated padding keeps the array length and
    avoids pulling boundary bins toward zero."""
    w = _hz_to_bins(smooth_hz, bin_hz)
    if w <= 1 or profile.size < 2:
        return profile
    kernel = np.ones(w) / w
    padded = np.pad(profile, (w // 2, w - 1 - w // 2), mode='edge')
    return np.convolve(padded, kernel, mode='valid')


def _exclude_narrow_tones(mask, freqs, tone_max_bins):
    """Zero out isolated True-runs shorter than `tone_max_bins` -- too narrow
    to be real sustained bandwidth. Flags (but excludes regardless of
    location) only when the run sits at/above 8 kHz, matching the reference
    band's upper edge: that's the region where a pilot tone or spike could
    otherwise be mistaken for cutoff-extending content."""
    out = mask.copy()
    tone_present = False
    n = mask.size
    i = 0
    while i < n:
        if out[i]:
            j = i
            while j < n and out[j]:
                j += 1
            if j - i < tone_max_bins:
                out[i:j] = False
                if freqs[i] >= 8000.0:
                    tone_present = True
            i = j
        else:
            i += 1
    return out, tone_present


def _close_gaps(mask, max_gap_bins):
    """Bridge interior False-runs of at most `max_gap_bins` (harmonic gaps
    and notches are normal in real spectra); leading/trailing gaps -- no True
    bin on one side -- are left alone."""
    if max_gap_bins <= 0:
        return mask
    out = mask.copy()
    n = mask.size
    i = 0
    while i < n:
        if not out[i]:
            j = i
            while j < n and not out[j]:
                j += 1
            if 0 < i and j < n and (j - i) <= max_gap_bins:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def _sliding_occupancy(mask, w):
    """Fraction of True bins in a trailing window of width `w` ending at each
    index (via a cumulative sum -- windows near index 0 are shorter)."""
    n = mask.size
    if w <= 1:
        return mask.astype(np.float64)
    cum = np.concatenate(([0.0], np.cumsum(mask.astype(np.float64))))
    idx = np.arange(n)
    start = np.maximum(0, idx - w + 1)
    counts = cum[idx + 1] - cum[start]
    lengths = (idx - start + 1).astype(np.float64)
    return counts / lengths


def _find_cutoff(freqs, profile, drop_db):
    """Highest frequency whose level stays within `drop_db` of the reference
    (median level of the 1-8 kHz band), over a band at least
    `_CUTOFF_MIN_WIDTH_HZ` wide where `_CUTOFF_OCCUPANCY_FRAC` of bins are
    above threshold (gaps up to `_CUTOFF_MAX_GAP_HZ` bridged first) -- real
    spectra have harmonic gaps/notches, so an unbroken run is too strict, but
    a lone narrow tone must not count as bandwidth either.

    Returns (cutoff_hz, ref_db, narrow_tone_present).
    """
    band = profile[(freqs >= 1000) & (freqs <= 8000)]
    ref = float(np.median(band)) if band.size else float(np.max(profile))
    n = profile.size
    if n < 3:
        return float(freqs[-1]), ref, False

    bin_hz = float(freqs[1] - freqs[0])
    smoothed = _smooth_profile(profile, bin_hz)
    above = smoothed >= (ref - drop_db)

    tone_max_bins = _hz_to_bins(_CUTOFF_TONE_MAX_WIDTH_HZ, bin_hz)
    above, tone_present = _exclude_narrow_tones(above, freqs, tone_max_bins)

    max_gap_bins = _hz_to_bins(_CUTOFF_MAX_GAP_HZ, bin_hz)
    closed = _close_gaps(above, max_gap_bins)

    min_width_bins = min(_hz_to_bins(_CUTOFF_MIN_WIDTH_HZ, bin_hz), n)
    occ = _sliding_occupancy(closed, min_width_bins)
    passing = np.where(occ >= _CUTOFF_OCCUPANCY_FRAC)[0]
    if passing.size == 0:
        idx = np.where(closed)[0]
        if idx.size == 0:
            return float(freqs[int(np.argmax(profile))]), ref, tone_present
        return float(freqs[int(idx[-1])]), ref, tone_present
    return float(freqs[int(passing[-1])]), ref, tone_present


def _edge_sharpness(freqs, profile, cutoff_hz):
    """Level drop across the cutoff in dB per kHz. A lossy encoder's low-pass
    is a near-vertical wall; a natural rolloff is gradual. Near Nyquist the
    upper band is truncated and every rolloff looks like a wall, so the
    measurement needs at least ~500 Hz of band above the cutoff to mean
    anything."""
    nyquist = float(freqs[-1])
    hi_top = min(cutoff_hz + 1500, nyquist)
    span_khz = (hi_top - cutoff_hz) / 1000.0
    if span_khz < 0.5:
        return 0.0
    lo = profile[(freqs >= cutoff_hz - 1000) & (freqs < cutoff_hz)]
    hi = profile[(freqs > cutoff_hz) & (freqs <= hi_top)]
    if lo.size == 0 or hi.size == 0:
        return 0.0
    return float((np.mean(lo) - np.mean(hi)) / (0.5 + span_khz / 2.0))


def _shelf_level(freqs, profile, cutoff_hz, ref):
    """Mean level above the cutoff relative to the reference (dB)."""
    hi = profile[freqs > cutoff_hz + 1500]
    if hi.size == 0:
        return None
    return float(np.mean(hi) - ref)


def _estimate_source(cutoff_hz):
    if cutoff_hz < 11500:
        return 'very low bitrate (<=64k) or heavy low-pass'
    if cutoff_hz < 15200:
        return '~96-112 kbps class'
    if cutoff_hz < 16800:
        return '~128 kbps class'
    if cutoff_hz < 18200:
        return '~160-192 kbps class'
    if cutoff_hz < 19600:
        return '~192-256 kbps class'
    if cutoff_hz < 20900:
        return '~320 kbps / high-quality lossy'
    return 'full bandwidth'


def _expected_cutoff_for_bitrate(bitrate_kbps):
    """Rough lower bound of the cutoff a *first-generation* lossy file of this
    bitrate should reach. Used to flag lossy->lossy transcodes."""
    if not bitrate_kbps:
        return None
    if bitrate_kbps >= 300:
        return 19500
    if bitrate_kbps >= 220:
        return 18500
    if bitrate_kbps >= 180:
        return 17000
    if bitrate_kbps >= 150:
        return 16000
    if bitrate_kbps >= 110:
        return 15000
    return None


# Above the cutoff a transcode/resample leaves only the container's
# quantization floor: measured on real 16-bit re-encodes of lossy sources the
# shelf sits 73-106 dB below the reference (the encoder's digital silence is
# refilled by 16-bit dither at re-encode, so a float-era -120 dB threshold
# never fires). Genuine masters keep analog/tape noise well above that
# (~-45..-65 dB relative). -68 splits the two populations.
SILENT_SHELF_DB = -68.0

# Nyquist frequencies of the standard rates a hi-res file may be upsampled from.
_STD_NYQUISTS = (22050.0, 24000.0, 44100.0, 48000.0)


def _bit_depth_probe(path, max_seconds=30.0):
    """(container_bits, effective_bits) from the PCM samples themselves.

    A 16-bit master padded into a 24-bit container leaves the low 8 bits of
    every sample zero; libsndfile left-justifies samples into int32, so the
    effective depth is 32 minus the fewest trailing zero bits seen. Returns
    (None, None) when the container has no fixed bit depth or can't be read
    (lossy formats, ALAC-in-m4a, float WAV...).
    """
    try:
        import soundfile as sf
        info = sf.info(path)
    except Exception:
        return None, None
    digits = ''.join(ch for ch in (info.subtype or '') if ch.isdigit())
    bits = int(digits) if digits else None
    if not bits or bits <= 16:
        return bits, bits  # nothing to fake in a 16-bit container
    try:
        with sf.SoundFile(path) as f:
            frames = int(max_seconds * f.samplerate)
            if f.seekable() and f.frames > frames * 2:
                f.seek((f.frames - frames) // 2)
            data = f.read(frames, dtype='int32', always_2d=True)
    except Exception:
        return bits, None
    x = np.asarray(data).ravel().astype(np.int64)  # int64: -x of INT32_MIN overflows
    x = x[x != 0]
    if x.size < 1000:
        return bits, None  # too quiet to judge
    lowest_set_bit = (x & -x).astype(np.float64)
    effective = int(32 - np.min(np.log2(lowest_set_bit)))
    return bits, min(bits, effective)


# Formats libsndfile reports for containers it can read natively (verified
# against this repo's fixtures: libsndfile 1.2.2 reports 'FLAC' and 'MP3'
# distinctly, so this is a real signal, not a guess).
_SNDFILE_LOSSLESS_FORMATS = {'FLAC', 'WAV', 'WAVEX', 'AIFF', 'W64', 'CAF', 'RF64'}
_SNDFILE_LOSSY_FORMATS = {'MP3', 'OGG'}


def _probe_container(path):
    """Tier 2 of the codec probe: identify the actual container/codec for
    formats libsndfile can read natively, independent of a (possibly wrong)
    file extension. Returns (detected_format, is_lossless), or (None, None)
    when soundfile can't open the file at all (m4a/opus/wma/mpc - an ffprobe
    fallback tier, not implemented here)."""
    try:
        import soundfile as sf
        info = sf.info(path)
    except Exception:
        return None, None
    fmt = (info.format or '').upper()
    if fmt in _SNDFILE_LOSSLESS_FORMATS:
        return fmt, True
    if fmt in _SNDFILE_LOSSY_FORMATS:
        return fmt, False
    return fmt or None, None  # readable but unclassified: don't override suffix


def _alias_image_corr(freqs, profile, mirror_hz, width_hz=4000.0):
    """Correlation between the band above `mirror_hz` and the band below it,
    mirrored. A cheap resampler folds images of the source content around the
    old Nyquist, so a high correlation means the energy above the cutoff is a
    resampler artifact rather than genuine noise or a dark master."""
    nyquist = float(freqs[-1])
    width = min(width_hz, nyquist - mirror_hz - 200.0, mirror_hz - 200.0)
    if width < 1500.0:
        return None
    lo = profile[(freqs >= mirror_hz - width) & (freqs < mirror_hz)]
    hi = profile[(freqs > mirror_hz) & (freqs <= mirror_hz + width)]
    n = min(lo.size, hi.size)
    if n < 32:
        return None
    lo = lo[-n:]
    hi = hi[:n][::-1]  # hi[k] at mirror+k*df pairs with lo at mirror-k*df
    if np.ptp(lo) < 1e-6 or np.ptp(hi) < 1e-6:
        return None  # flat band (e.g. digital silence): correlation undefined
    c = np.corrcoef(lo, hi)[0, 1]
    return float(c) if np.isfinite(c) else None


def _resample_match(nyquist, cutoff_hz):
    """Source sample rate whose Nyquist the cutoff aligns with (within ~6%),
    when that Nyquist sits well below the container's own — the signature a
    resampler's anti-alias filter leaves in an upsampled file. The window is
    asymmetric: resampler image leakage pushes the *detected* cutoff above
    the true wall, never below it."""
    for q in _STD_NYQUISTS:
        if q <= 0.75 * nyquist and -0.03 * q <= (cutoff_hz - q) <= 0.07 * q:
            return int(q * 2)
    return None


def _best_alias_match(freqs, profile, nyquist):
    """(correlation, source_rate) of the standard Nyquist whose mirrored
    bands correlate best — images mirror around the source's Nyquist, not
    around the detected cutoff (leakage shifts the latter upward)."""
    best_corr, best_rate = None, None
    for q in _STD_NYQUISTS:
        if q <= 0.75 * nyquist:
            c = _alias_image_corr(freqs, profile, q)
            if c is not None and (best_corr is None or c > best_corr):
                best_corr, best_rate = c, int(q * 2)
    return best_corr, best_rate


def _verdict(sr, suffix, bitrate_kbps, cutoff_hz, edge_db_khz, shelf_db,
             freqs=None, profile=None, is_lossless=None):
    nyquist = sr / 2.0
    suffix = (suffix or '').lower().lstrip('.')
    # Genuine 44.1 kHz masters legitimately roll off at 20-21 kHz (ADC
    # anti-alias filters, mastering chains), and any rolloff measured against
    # the Nyquist wall looks sharp, so content reaching ~93% of Nyquist
    # (capped at 20.5 kHz for high-rate files) counts as full bandwidth.
    full_threshold = min(0.93 * nyquist, 20500.0)
    sharp = edge_db_khz >= 25.0
    silent_shelf = shelf_db is None or shelf_db <= SILENT_SHELF_DB
    margin = max(0.0, min(1.0, (full_threshold - cutoff_hz) / full_threshold))
    notes = []

    if cutoff_hz >= full_threshold:
        # hi-res container: content stopping at a lower standard rate's
        # Nyquist (22.05 k / 24 k / 44.1 k / 48 k), or below 23 kHz at all,
        # is a resampled source sold as hi-res; a genuine hi-res master
        # keeps content or noise well above that
        if nyquist > 24000.0:
            src_rate = _resample_match(nyquist, cutoff_hz)
            if src_rate or cutoff_hz < 23000.0:
                corr, corr_rate = ((None, None) if freqs is None or profile is None
                                   else _best_alias_match(freqs, profile, nyquist))
                aliased = corr is not None and corr >= 0.6
                if aliased and corr_rate:
                    src_rate = corr_rate  # images name the true source rate
                if silent_shelf or aliased or sharp:
                    if src_rate:
                        est = f'likely {src_rate / 1000.0:g} kHz source'
                    else:
                        est = ('likely 44.1/48 kHz source'
                               if cutoff_hz >= 21600.0 else 'CD-bandwidth source')
                    conf = (0.5 + (0.25 if sharp else 0.0)
                            + (0.15 if silent_shelf else 0.0))
                    if aliased:
                        # the "noise" above the wall mirrors the band below it:
                        # resampler aliasing images, not a genuine noise floor
                        notes.append(f'content above the cutoff mirrors the band '
                                     f'below (correlation {corr:.2f}): resampler '
                                     f'aliasing images')
                        conf = max(conf, 0.9)
                    elif not silent_shelf:
                        notes.append('some content above the cutoff: '
                                     'could be a genuine but dark master')
                    return 'UPSAMPLED', est, conf, notes
                # soft edge + audible noise + no aliasing: a genuine dark
                # master fading out, not a resampler wall
                notes.append('bandwidth is limited but the edge is gradual with '
                             'audible noise above and no aliasing images: likely '
                             'a genuine dark master')
                return 'CLEAN', 'limited bandwidth (dark master?)', 0.6, notes
        return 'CLEAN', 'full bandwidth', 0.9, notes

    if nyquist > 24000.0 and cutoff_hz < 23000.0:
        notes.append('content ends below 23 kHz despite high sample rate: possible upsample')

    est = _estimate_source(cutoff_hz)

    lossless = suffix in LOSSLESS_SUFFIXES if is_lossless is None else is_lossless
    if lossless:
        if sharp and silent_shelf:
            conf = min(0.95, 0.6 + 0.5 * margin + min(0.15, (edge_db_khz - 25.0) / 200.0))
            return 'FAKE_SUSPECT', est, conf, notes
        if sharp:
            notes.append('sharp edge but audible noise floor above the cutoff: '
                         'more likely a low-passed master than an encoder wall')
            return 'LOWPASSED', est, 0.5 + 0.3 * margin, notes
        notes.append('gradual rolloff: could be a genuine low-passed master, not a transcode')
        return 'LOWPASSED', est, 0.45 + 0.3 * margin, notes

    # lossy container
    expected = _expected_cutoff_for_bitrate(bitrate_kbps)
    if expected and cutoff_hz < expected - 1500:
        notes.append(
            f'declared ~{bitrate_kbps} kbps but bandwidth matches a lower-bitrate source'
        )
        return 'TRANSCODED_LOSSY', est, min(0.9, 0.55 + 0.5 * margin), notes
    return 'CONSISTENT_LOSSY', est, 0.7, notes


def _render_spectrogram(S_db, sr, seg_offset, seg_len_s, cutoff_hz,
                        img_w=800, img_h=280):
    """Spek-style spectrogram PNG, returned as base64 (no data: prefix)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    n_frames = S_db.shape[1]
    cols = min(img_w, n_frames)
    if n_frames > cols:
        # max-pool along time so the PNG stays small but peaks survive
        edges = np.linspace(0, n_frames, cols + 1, dtype=int)
        pooled = np.maximum.reduceat(S_db, edges[:-1], axis=1)
    else:
        pooled = S_db
    vmax = float(np.max(pooled))

    fig, ax = plt.subplots(figsize=(img_w / 100.0, img_h / 100.0), dpi=100)
    ax.imshow(
        pooled, origin='lower', aspect='auto', cmap='magma',
        vmin=vmax - 90.0, vmax=vmax,
        extent=[seg_offset, seg_offset + seg_len_s, 0.0, sr / 2000.0],
    )
    if cutoff_hz and cutoff_hz < sr / 2.0 * 0.99:
        ax.axhline(cutoff_hz / 1000.0, color='#ff5555', ls='--', lw=0.8)
    ax.set_ylabel('kHz', fontsize=8)
    ax.set_xlabel('s', fontsize=8)
    ax.tick_params(labelsize=7)
    fig.tight_layout(pad=0.4)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', pil_kwargs={'compress_level': 9})
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _inconclusive_result(suffix, bitrate_kbps, deep, drop_db, segment_seconds, exc):
    """Result dict for a file that could not be decoded at all: a stored,
    visible INCONCLUSIVE row instead of a silently dropped analysis.
    deep_eligible=False — if the file couldn't be decoded once, a repeat
    attempt (segment or deep) won't fare better. Same key shape as a normal
    result so callers (_upsert, on_song_analyzed) need no special-casing."""
    return {
        'sample_rate': None,
        'seg_offset': 0.0,
        'seg_seconds': None,
        'cutoff_hz': None,
        'edge_db_khz': None,
        'shelf_db': None,
        'verdict': 'INCONCLUSIVE',
        'est_source': 'could not decode audio',
        'confidence': 0.0,
        'container_bits': None,
        'effective_bits': None,
        'deep_eligible': False,
        'details': json.dumps({
            'decode_error': str(exc),
            'declared_bitrate_kbps': bitrate_kbps,
            'suffix': suffix,
            'deep': deep,
            'drop_db': drop_db,
            'integrity': {'status': 'decode_failed', 'coverage': None},
            'notes': [f'decode failed: {exc}'],
        }),
        'spectrogram_b64': None,
        'analysis_rev': analysis_rev(drop_db, segment_seconds),
    }


def _unsupported_format_result(suffix, bitrate_kbps, deep, drop_db, segment_seconds, fmt):
    """Result dict for a format we recognize but deliberately don't analyze
    (DSD): an honest 'not evaluated' instead of running PCM heuristics on
    decoded DSD noise shaping, which silently reads as full-bandwidth content
    (see _sniff_dsd). Same shape as _inconclusive_result: deep_eligible=False,
    since nothing about a retry would change the outcome."""
    return {
        'sample_rate': None,
        'seg_offset': 0.0,
        'seg_seconds': None,
        'cutoff_hz': None,
        'edge_db_khz': None,
        'shelf_db': None,
        'verdict': 'INCONCLUSIVE',
        'est_source': f'{fmt.upper()} (DSD): not analyzed, unsupported format',
        'confidence': 0.0,
        'container_bits': None,
        'effective_bits': None,
        'deep_eligible': False,
        'details': json.dumps({
            'detected_format': fmt,
            'declared_bitrate_kbps': bitrate_kbps,
            'suffix': suffix,
            'deep': deep,
            'drop_db': drop_db,
            'integrity': {'status': 'unsupported', 'coverage': None},
            'notes': [f'{fmt.upper()} recognized by magic bytes: DSD content is '
                      f'not analyzed by the PCM spectrum heuristics'],
        }),
        'spectrogram_b64': None,
        'analysis_rev': analysis_rev(drop_db, segment_seconds),
    }


def analyze_file(path, suffix=None, bitrate_kbps=None, segment_seconds=90,
                 drop_db=40, img_w=800, img_h=280, deep=False):
    """Full pipeline. Returns a dict ready to be stored.

    deep=True analyzes the entire file (chunked) instead of one segment and
    tracks the spectral edge over time: a resampler/encoder wall sits at a
    constant frequency for the whole file, while a genuine dark master's edge
    moves with the music.
    """
    if suffix is None:
        suffix = os.path.splitext(path)[1].lstrip('.')

    dsd_fmt = _sniff_dsd(path)
    if dsd_fmt:
        return _unsupported_format_result(suffix, bitrate_kbps, deep, drop_db,
                                          segment_seconds, dsd_fmt)

    detected_fmt, detected_lossless = _probe_container(path)
    codec_mismatch = None
    if detected_lossless is True and suffix in LOSSY_SUFFIXES:
        codec_mismatch = f'suffix .{suffix} suggests lossy, container is {detected_fmt} (lossless)'
    elif detected_lossless is False and suffix in LOSSLESS_SUFFIXES:
        codec_mismatch = f'suffix .{suffix} suggests lossless, container is {detected_fmt} (lossy)'

    edge_var = edge_med = None
    pinned_q = None
    capped = chunk_error = False
    try:
        if deep:
            (S_db, profile, freqs, sr, seg_len, edge_series,
             capped, chunk_error) = _deep_spectrum(path, drop_db)
            offset = 0.0
        else:
            y, sr, offset = _load_segment(path, segment_seconds)
            seg_len = y.size / float(sr)
            S_db, profile, freqs = _spectrum(y, sr)
    except ValueError as exc:
        return _inconclusive_result(suffix, bitrate_kbps, deep, drop_db,
                                    segment_seconds, exc)

    if deep:
        if len(edge_series) >= 10:
            arr = np.asarray(edge_series)
            edge_var = float(np.std(arr))
            edge_med = float(np.median(arr))
            # a resampler wall caps the edge at the source Nyquist: content may
            # dip below on quiet seconds (large variance!) but never crosses it;
            # a dark master's edge wanders freely with no standard-rate ceiling
            for q in _STD_NYQUISTS:
                if (q <= 0.75 * (sr / 2.0)
                        and float(np.mean(np.abs(arr - q) <= 0.04 * q)) >= 0.5
                        and float(np.mean(arr > 1.06 * q)) <= 0.05):
                    pinned_q = q
                    break

    cutoff_hz, ref, narrow_tone = _find_cutoff(freqs, profile, drop_db)
    edge = _edge_sharpness(freqs, profile, cutoff_hz)
    shelf = _shelf_level(freqs, profile, cutoff_hz, ref)
    verdict, est, conf, notes = _verdict(sr, suffix, bitrate_kbps, cutoff_hz, edge, shelf,
                                         freqs=freqs, profile=profile,
                                         is_lossless=detected_lossless)

    if deep:
        if edge_var is None:
            notes.append('deep scan: not enough loud content to track the spectral edge')
        elif verdict in ('UPSAMPLED', 'FAKE_SUSPECT', 'LOWPASSED'):
            if pinned_q is not None:
                notes.append(
                    f'deep scan: the spectral edge stays capped at '
                    f'{pinned_q / 1000.0:g} kHz (the Nyquist of a '
                    f'{pinned_q * 2 / 1000.0:g} kHz source) for the whole file: '
                    f'a resampler wall, not natural content')
                conf = max(conf, 0.9)
            elif edge_var >= 2000.0:
                notes.append(
                    f'deep scan: the spectral edge follows the music '
                    f'(varies ±{edge_var:.0f} Hz over the whole file) with no '
                    f'standard-rate ceiling — consistent with a genuine dark master')
                if verdict in ('UPSAMPLED', 'FAKE_SUSPECT'):
                    verdict = 'LOWPASSED'
                    est = 'likely genuine dark master'
                    conf = 0.4
                else:
                    conf = min(0.9, conf + 0.2)
            elif edge_var <= 300.0:
                notes.append(
                    f'deep scan: constant spectral edge across the whole file '
                    f'(±{edge_var:.0f} Hz): a machine low-pass, not natural content')
                conf = min(0.97, conf + 0.15)
            else:
                notes.append(f'deep scan: edge variability ±{edge_var:.0f} Hz: inconclusive')

    container_bits, effective_bits = _bit_depth_probe(path)
    if (container_bits or 0) > 16 and effective_bits and effective_bits <= 16:
        if verdict == 'CLEAN':
            verdict = 'UPSCALED'
            est = f'{effective_bits}-bit source in a {container_bits}-bit container'
            conf = 0.95  # zero-padded low bits are a deterministic signature
        else:
            notes.append(f'bit depth: only {effective_bits} effective bits in a '
                         f'{container_bits}-bit container (padded)')

    png_b64 = _render_spectrogram(S_db, sr, offset, seg_len, cutoff_hz, img_w, img_h)

    if not deep:
        integrity = {'status': 'sampled_decode_ok', 'coverage': 'sampled'}
    elif chunk_error:
        # a chunk load raised before reaching either the natural end of the
        # file or the time budget - some usable data, but cut short
        integrity = {'status': 'sampled_decode_ok', 'coverage': 'partial'}
    elif capped:
        integrity = {'status': 'sampled_decode_ok', 'coverage': 'capped'}
    else:
        integrity = {'status': 'full_decode_ok', 'coverage': 'full'}
    delivery = {'codec_mismatch': codec_mismatch} if codec_mismatch else None

    return {
        'sample_rate': sr,
        'seg_offset': round(offset, 2),
        'seg_seconds': round(seg_len, 2),
        'cutoff_hz': round(cutoff_hz, 1),
        'edge_db_khz': round(edge, 2),
        'shelf_db': round(shelf, 2) if shelf is not None else None,
        'verdict': verdict,
        'est_source': est,
        'confidence': round(conf, 2),
        'container_bits': container_bits,
        'effective_bits': effective_bits,
        'deep_eligible': True,
        'details': json.dumps({
            'ref_level_db': round(ref, 2),
            'drop_db': drop_db,
            'nyquist_hz': sr / 2.0,
            'full_bandwidth_threshold_hz': round(min(0.93 * sr / 2.0, 20500.0), 1),
            'shelf_db': round(shelf, 2) if shelf is not None else None,
            'container_bits': container_bits,
            'effective_bits': effective_bits,
            'declared_bitrate_kbps': bitrate_kbps,
            'suffix': suffix,
            'deep': deep,
            'edge_var_hz': round(edge_var, 1) if edge_var is not None else None,
            'edge_median_hz': round(edge_med, 1) if edge_med is not None else None,
            'integrity': integrity,
            'delivery': delivery,
            'narrow_high_frequency_tone_present': narrow_tone,
            'notes': notes,
        }),
        'spectrogram_b64': png_b64,
        'analysis_rev': analysis_rev(drop_db, segment_seconds),
    }
