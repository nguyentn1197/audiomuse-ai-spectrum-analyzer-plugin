"""Minimal librosa replacement so dsp.py runs outside AudioMuse-AI.

dsp.py only uses librosa.load() and librosa.stft(); both are implemented here
with soundfile + numpy. Import this module BEFORE importing dsp — it installs
itself into sys.modules['librosa'].
"""
import sys
import types

import numpy as np
import soundfile as sf


def load(path, sr=None, mono=True, offset=0.0, duration=None):
    with sf.SoundFile(path) as f:
        rate = f.samplerate
        f.seek(min(int(offset * rate), f.frames))
        frames = -1 if duration is None else int(duration * rate)
        y = f.read(frames, dtype='float32', always_2d=True)
    if mono:
        y = y.mean(axis=1)
    return np.ascontiguousarray(y, dtype=np.float32), rate


def stft(y, n_fft=2048, hop_length=None, window='hann'):
    hop = hop_length or n_fft // 4
    y = np.pad(y, n_fft // 2, mode='reflect')  # librosa center=True
    n = max(1, 1 + (len(y) - n_fft) // hop)
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)).astype(np.float32)
    view = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop][:n]
    out = np.empty((n_fft // 2 + 1, n), dtype=np.complex64)
    for s in range(0, n, 1024):  # batch FFTs to bound memory
        out[:, s:s + 1024] = np.fft.rfft(view[s:s + 1024] * win, axis=1).T
    return out


_mod = types.ModuleType('librosa')
_mod.load = load
_mod.stft = stft
sys.modules.setdefault('librosa', _mod)
