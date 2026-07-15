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


def _find_cutoff(freqs, profile, drop_db):
    """Highest frequency whose level stays within `drop_db` of the reference
    (median level of the 1-8 kHz band), sustained over 3 consecutive bins."""
    band = profile[(freqs >= 1000) & (freqs <= 8000)]
    ref = float(np.median(band)) if band.size else float(np.max(profile))
    above = profile >= (ref - drop_db)
    if above.size < 3:
        return float(freqs[-1]), ref
    runs = above[:-2] & above[1:-1] & above[2:]
    idx = np.where(runs)[0]
    if idx.size == 0:
        return float(freqs[int(np.argmax(profile))]), ref
    return float(freqs[int(idx[-1]) + 2]), ref


def _edge_sharpness(freqs, profile, cutoff_hz):
    """Level drop across the cutoff in dB per kHz. A lossy encoder's low-pass
    is a near-vertical wall; a natural rolloff is gradual."""
    lo = profile[(freqs >= cutoff_hz - 1000) & (freqs < cutoff_hz)]
    hi = profile[(freqs > cutoff_hz) & (freqs <= cutoff_hz + 1500)]
    if lo.size == 0 or hi.size == 0:
        return 0.0
    return float((np.mean(lo) - np.mean(hi)) / 1.25)


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


def _verdict(sr, suffix, bitrate_kbps, cutoff_hz, edge_db_khz, shelf_db):
    nyquist = sr / 2.0
    suffix = (suffix or '').lower().lstrip('.')
    full_threshold = 21000.0 if nyquist >= 22050 else 0.955 * nyquist
    sharp = edge_db_khz >= 25.0
    margin = max(0.0, min(1.0, (full_threshold - cutoff_hz) / full_threshold))
    notes = []

    if nyquist > 24000 and cutoff_hz < 23000:
        notes.append('content ends below 23 kHz despite high sample rate: possible upsample')

    if cutoff_hz >= full_threshold:
        return 'CLEAN', 'full bandwidth', 0.9, notes

    est = _estimate_source(cutoff_hz)

    if suffix in LOSSLESS_SUFFIXES:
        if sharp:
            conf = min(0.95, 0.6 + 0.5 * margin + min(0.15, (edge_db_khz - 25.0) / 200.0))
            return 'FAKE_SUSPECT', est, conf, notes
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


def analyze_file(path, suffix=None, bitrate_kbps=None, segment_seconds=90,
                 drop_db=40, img_w=800, img_h=280):
    """Full pipeline. Returns a dict ready to be stored."""
    if suffix is None:
        suffix = os.path.splitext(path)[1].lstrip('.')

    y, sr, offset = _load_segment(path, segment_seconds)
    seg_len = y.size / float(sr)
    S_db, profile, freqs = _spectrum(y, sr)
    cutoff_hz, ref = _find_cutoff(freqs, profile, drop_db)
    edge = _edge_sharpness(freqs, profile, cutoff_hz)
    shelf = _shelf_level(freqs, profile, cutoff_hz, ref)
    verdict, est, conf, notes = _verdict(sr, suffix, bitrate_kbps, cutoff_hz, edge, shelf)
    png_b64 = _render_spectrogram(S_db, sr, offset, seg_len, cutoff_hz, img_w, img_h)

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
        'details': json.dumps({
            'ref_level_db': round(ref, 2),
            'drop_db': drop_db,
            'nyquist_hz': sr / 2.0,
            'declared_bitrate_kbps': bitrate_kbps,
            'suffix': suffix,
            'notes': notes,
        }),
        'spectrogram_b64': png_b64,
    }
