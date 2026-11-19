# SpectrumAnalyzer - an AudioMuse-AI plugin
# https://github.com/nguyentn1197/audiomuse-ai-spectrum-analyzer-plugin
# Copyright (C) 2026 Nguyen
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root.

"""Container-level integrity verification: is this file damaged?

dsp.py asks whether a file is *authentic*; this module asks whether it is
*intact*. The two are independent, and the spectrum can't answer the second
question: a FLAC with zero-filled or truncated frames decodes cleanly right
up to the damage, and the garbage a decoder makes of a broken frame is
broadband noise that can drag a low-pass cutoff up. So damage is read from
the container's own checksums, never inferred from the audio.

Two methods, picked by content (magic bytes), not by suffix:

``flac_frames`` -- native FLAC, every scan. Walks every frame header (CRC-8)
and frame body (CRC-16) in numpy without decoding audio or spawning a
subprocess (~0.1-0.3 s for a typical track), and checks the frame/sample
sequence against STREAMINFO's total so a file cut exactly at a frame
boundary -- which no per-frame CRC can see -- is caught too. With
``verify_md5`` (deep scans) it also decodes the stream and compares it with
STREAMINFO's MD5. Together that is what ``flac -t`` checks.

``ffmpeg_decode`` -- every other format, deep scans only (one subprocess plus
a full decode is too much for every track of a library scan). A full decode
with ffmpeg's CRC and bitstream checks on, plus the decoded length against
the container's declared duration. Weaker by nature: MP3 rarely carries
CRCs and PCM/ALAC carry none, so a pass means "decodes cleanly to its
declared length", not "bit-identical to what was encoded".

Every public function returns a plain dict (stored under
``details.integrity.stream``) and never raises: an integrity check must not
be able to break the analysis it is attached to.
"""

import bisect
import hashlib
import json
import mmap
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter

import numpy as np

METHOD_FLAC = 'flac_frames'
METHOD_FFMPEG = 'ffmpeg_decode'

# Full-stream MD5 decode via ffmpeg (deep scans, depths libsndfile can't read)
_MD5_FFMPEG_TIMEOUT = 600.0

# Regions of damage closer together than this are reported as one region
# (the bad-frame count and damaged_seconds stay exact).
_REGION_MERGE_SECONDS = 1.0
_MAX_REGIONS = 32
# A run of identical zero bytes this long never occurs inside a valid FLAC
# frame (silence is a CONSTANT subframe, residual coding has no long zero
# runs), so inside a damaged frame it is the fingerprint of a zero-filled
# transfer gap rather than bit rot.
_ZERO_RUN_MIN = 256


# --------------------------------------------------------------------------
# CRCs (RFC 9639 section 9.1.8 / 9.3): MSB-first, init 0, no final xor.

def _crc_table(poly, width):
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    table = []
    for i in range(256):
        c = i << (width - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) & mask if c & top else (c << 1) & mask
        table.append(c)
    return table


_CRC8 = _crc_table(0x07, 8)
_CRC16 = _crc_table(0x8005, 16)
_CRC16_1 = np.array(_CRC16, dtype=np.uint16)


def _crc16_two_byte_table():
    # crc' = T[crc ^ (b0 << 8 | b1)]: sixteen register shifts of a 16-bit
    # value, so one table lookup consumes two bytes
    c = np.arange(65536, dtype=np.uint32)
    for _ in range(16):
        c = ((c << 1) & 0xFFFF) ^ np.where(c & 0x8000, 0x8005, 0).astype(np.uint32)
    return c.astype(np.uint16)


_CRC16_2 = _crc16_two_byte_table()


def _crc8(data):
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


def _crc16(data, crc=0):
    t = _CRC16
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ t[(crc >> 8) ^ b]
    return crc


def _crc16_spans(buf, starts, lengths):
    """CRC-16 of buf[s:s+n] for many spans at once. Each span ends with its
    own stored big-endian CRC, so 0 means intact.

    Frame-parallel: spans are end-aligned and consumed two bytes per step,
    so the Python loop runs (longest span / 2) times over arrays of at most
    len(starts) elements instead of once per byte (~12x faster than a
    per-byte loop on a real track). Sorted longest-first, the spans that
    have started at any step are always a prefix; an odd-length span eats
    its first byte up front through the one-byte table (equivalent to a
    leading zero byte, which a zero-init CRC ignores)."""
    n = len(starts)
    out = np.zeros(n, dtype=np.uint16)
    if n == 0:
        return out
    order = np.argsort(-np.asarray(lengths), kind='stable')
    s = np.asarray(starts, dtype=np.int64)[order]
    span = np.asarray(lengths, dtype=np.int64)[order]
    odd = (span & 1).astype(bool)
    crc = np.zeros(n, dtype=np.uint16)
    crc[odd] = _CRC16_1[buf[s[odd]]]
    s = s + odd
    span = span - odd
    longest = int(span[0])
    if longest:
        base = s + span - longest
        active = np.searchsorted(longest - span, np.arange(0, longest, 2), side='right')
        t2 = _CRC16_2
        for step in range(longest // 2):
            m = active[step]
            idx = base[:m] + 2 * step
            word = (buf[idx].astype(np.uint16) << 8) | buf[idx + 1]
            crc[:m] = t2[crc[:m] ^ word]
    out[order] = crc
    return out


# --------------------------------------------------------------------------
# FLAC

_BLOCK_SIZES = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608, 8: 256, 9: 512,
                10: 1024, 11: 2048, 12: 4096, 13: 8192, 14: 16384, 15: 32768}
_SAMPLE_RATES = {1: 88200, 2: 176400, 3: 192000, 4: 8000, 5: 16000, 6: 22050,
                 7: 24000, 8: 32000, 9: 44100, 10: 48000, 11: 96000}
_SAMPLE_SIZES = {1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}


def _id3v2_size(raw):
    """Length of a leading ID3v2 tag (non-standard on FLAC, but common and
    accepted by libFLAC and ffmpeg), else 0."""
    if len(raw) >= 10 and raw[:3] == b'ID3' and all(b < 0x80 for b in raw[6:10]):
        size = (raw[6] << 21) | (raw[7] << 14) | (raw[8] << 7) | raw[9]
        return 10 + size + (10 if raw[5] & 0x10 else 0)
    return 0


def flac_offset(raw):
    """Offset of the 'fLaC' marker in a file's bytes, else None."""
    off = _id3v2_size(raw[:10])
    if raw[off:off + 4] == b'fLaC':
        return off
    return None


def is_flac_file(path):
    """Content sniff without reading the whole file (an ID3v2 tag with
    embedded art can be megabytes long, so seek past it rather than read)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(10)
            off = _id3v2_size(head)
            f.seek(off)
            return f.read(4) == b'fLaC'
    except OSError:
        return False


def _parse_streaminfo(raw, off):
    """(streaminfo dict, audio start offset) or (None, problem string)."""
    pos = off + 4
    info = None
    first = True
    while True:
        if pos + 4 > len(raw):
            return None, 'metadata block chain runs past the end of the file'
        hdr = raw[pos]
        btype, last = hdr & 0x7F, hdr & 0x80
        length = int.from_bytes(raw[pos + 1:pos + 4], 'big')
        body = pos + 4
        if body + length > len(raw):
            return None, 'metadata block chain runs past the end of the file'
        if first:
            if btype != 0 or length != 34:
                return None, 'first metadata block is not a valid STREAMINFO'
            si = raw[body:body + 34]
            x = int.from_bytes(si[10:18], 'big')
            info = {
                'min_blocksize': int.from_bytes(si[0:2], 'big'),
                'max_blocksize': int.from_bytes(si[2:4], 'big'),
                'sample_rate': x >> 44,
                'channels': ((x >> 41) & 0x7) + 1,
                'bits': ((x >> 36) & 0x1F) + 1,
                'total_samples': x & ((1 << 36) - 1),
                'md5': bytes(si[18:34]),
            }
            if not info['sample_rate']:
                return None, 'STREAMINFO declares a zero sample rate'
            first = False
        elif btype == 127:
            return None, 'invalid metadata block type 127'
        pos = body + length
        if last:
            return info, pos


def _parse_frame_header(raw, p, info, strategy):
    """(header length incl. CRC-8, coded number, block size) for a valid,
    STREAMINFO-consistent frame header at p, else None."""
    if p + 6 > len(raw) or raw[p] != 0xFF or raw[p + 1] != 0xF8 | strategy:
        return None
    b2, b3 = raw[p + 2], raw[p + 3]
    bs_code, sr_code = b2 >> 4, b2 & 0xF
    ch_code, ss_code = b3 >> 4, (b3 >> 1) & 0x7
    if bs_code == 0 or sr_code == 0xF or ch_code > 10 or ss_code == 3 or b3 & 1:
        return None
    channels = ch_code + 1 if ch_code < 8 else 2
    if channels != info['channels']:
        return None
    if ss_code and _SAMPLE_SIZES[ss_code] != info['bits']:
        return None
    q = p + 4
    c = raw[q]
    if c < 0x80:
        n, extra = c, 0
    elif c & 0xE0 == 0xC0:
        n, extra = c & 0x1F, 1
    elif c & 0xF0 == 0xE0:
        n, extra = c & 0x0F, 2
    elif c & 0xF8 == 0xF0:
        n, extra = c & 0x07, 3
    elif c & 0xFC == 0xF8:
        n, extra = c & 0x03, 4
    elif c & 0xFE == 0xFC:
        n, extra = c & 0x01, 5
    elif c == 0xFE and strategy:  # 7-byte form: sample numbers only
        n, extra = 0, 6
    else:
        return None
    q += 1
    if q + extra + 3 > len(raw):
        return None
    for _ in range(extra):
        if raw[q] & 0xC0 != 0x80:
            return None
        n = (n << 6) | (raw[q] & 0x3F)
        q += 1
    if bs_code == 6:
        bs = raw[q] + 1
        q += 1
    elif bs_code == 7:
        bs = int.from_bytes(raw[q:q + 2], 'big') + 1
        q += 2
    else:
        bs = _BLOCK_SIZES[bs_code]
    # deliberately no check against STREAMINFO's max_blocksize: it only
    # filters false syncs, which CRC-8 plus the format fields already do,
    # and a wrong value from a buggy encoder would turn into false damage
    if sr_code == 12:
        rate = raw[q] * 1000
        q += 1
    elif sr_code in (13, 14):
        rate = int.from_bytes(raw[q:q + 2], 'big') * (10 if sr_code == 14 else 1)
        q += 2
    else:
        rate = _SAMPLE_RATES.get(sr_code)
    if rate is not None and rate != info['sample_rate']:
        return None
    if q >= len(raw) or _crc8(raw[p:q]) != raw[q]:
        return None
    return q + 1 - p, n, bs


def _longest_increasing_chain(keys):
    """Indices of the longest strictly increasing subsequence of keys.
    Corruption removes and garbles headers but never reorders the true ones,
    so the real frame sequence is the longest increasing run of coded
    numbers; a false sync inside frame data almost never fits it."""
    tails, tail_idx, prev = [], [], [-1] * len(keys)
    for i, k in enumerate(keys):
        j = bisect.bisect_left(tails, k)
        if j == len(tails):
            tails.append(k)
            tail_idx.append(i)
        else:
            tails[j] = k
            tail_idx[j] = i
        prev[i] = tail_idx[j - 1] if j else -1
    out = []
    i = tail_idx[-1] if tail_idx else -1
    while i >= 0:
        out.append(i)
        i = prev[i]
    return out[::-1]


def _trailer_ok(raw, end):
    """True when everything after `end` is nothing, zero padding, or a
    known trailing tag (ID3v1, APEv2); otherwise the trailing bytes are
    reported as unrecognized (still not damage -- decoders ignore them)."""
    tail = raw[end:]
    if not tail or not any(tail):
        return True
    if len(tail) >= 128 and tail[-128:-125] == b'TAG':
        tail = tail[:-128]
    if len(tail) >= 32 and tail[-32:-24] == b'APETAGEX':
        size = int.from_bytes(tail[-20:-16], 'little')
        flags = int.from_bytes(tail[-12:-8], 'little')
        tail = tail[:max(0, len(tail) - size - (32 if flags & 0x80000000 else 0))]
    return not tail or not any(tail)


class _BadFrame(Exception):
    """The bits do not form a valid frame (or run past the data)."""


def _frame_end(raw, p, hl, info, bs, limit):
    """Byte offset just past the CRC-16 of the frame at p, found by walking
    its structure the way a decoder does -- subframe headers, wasted bits,
    warm-up samples, LPC coefficients, Rice-coded residual partitions, zero
    padding -- or None if the bits are not a valid frame within `limit`
    bytes.

    This is what bounds the *final* frame, which has no next header after
    it. Looking for where a running CRC-16 happens to reach zero is not good
    enough: in an intact frame it also hits zero by chance about once per
    64 KiB, and once it has, every corruption after that point verifies
    (PR #16 review: 146 of 200 single-byte corruptions of a one-frame file
    passed that way). The structure gives the one true end; the CRC is then
    checked there, so a damaged final frame fails like any other frame.
    Only skipped fields are counted, never sample values decoded, so this
    costs a few ms for one frame."""
    b3 = raw[p + 3]
    ch_code, ss_code = b3 >> 4, (b3 >> 1) & 0x7
    bps = _SAMPLE_SIZES[ss_code] if ss_code else info['bits']
    channels = ch_code + 1 if ch_code < 8 else 2
    # stereo decorrelation: the side channel carries one extra bit
    side = {8: 1, 9: 0, 10: 1}.get(ch_code)
    start = p + hl
    data = np.frombuffer(raw[start:min(len(raw), start + limit)], dtype=np.uint8)
    bits = np.unpackbits(data)
    n = len(bits)
    # nxt[i] = position of the first 1 bit at or after i (n past the end):
    # each Rice residual is a unary run of zeros ended by a 1, then `k` bits
    pad = 64
    nxt_arr = np.full(n + pad, n, dtype=np.int64)
    ones = np.flatnonzero(bits)
    nxt_arr[ones] = ones
    nxt = np.minimum.accumulate(nxt_arr[::-1])[::-1].tolist()
    bitlist = bits.tolist() + [0] * pad

    def read(pos, k):
        if pos + k > n:
            raise _BadFrame
        v = 0
        for i in range(pos, pos + k):
            v = (v << 1) | bitlist[i]
        return v

    def residual(pos, order):
        method = read(pos, 2)
        if method > 1:
            raise _BadFrame
        pbits = 4 + method
        escape = (1 << pbits) - 1
        porder = read(pos + 2, 4)
        pos += 6
        psize = bs >> porder
        if psize << porder != bs or psize < order:
            raise _BadFrame
        for part in range(1 << porder):
            count = psize - order if part == 0 else psize
            param = read(pos, pbits)
            pos += pbits
            if param == escape:
                pos += 5 + read(pos, 5) * count
            else:
                step = 1 + param
                for _ in range(count):
                    pos = nxt[pos] + step
            if pos > n:
                raise _BadFrame
        return pos

    pos = 0
    try:
        for ch in range(channels):
            sbps = bps + (1 if ch == side else 0)
            if read(pos, 1):          # subframe header starts with a zero bit
                raise _BadFrame
            kind = read(pos + 1, 6)
            wasted = read(pos + 7, 1)
            pos += 8
            if wasted:                # unary k-1, so k = zeros + 1
                j = nxt[pos]
                if j >= n:
                    raise _BadFrame
                sbps -= j - pos + 1
                pos = j + 1
                if sbps <= 0:
                    raise _BadFrame
            if kind == 0:             # CONSTANT
                pos += sbps
            elif kind == 1:           # VERBATIM
                pos += sbps * bs
            elif 8 <= kind <= 12:     # FIXED, order 0-4
                order = kind - 8
                pos = residual(pos + order * sbps, order)
            elif kind >= 32:          # LPC, order 1-32
                order = (kind & 31) + 1
                pos += order * sbps
                precision = read(pos, 4)
                if precision == 15:
                    raise _BadFrame
                pos = residual(pos + 4 + 5 + order * (precision + 1), order)
            else:                     # reserved subframe type
                raise _BadFrame
            if pos > n:
                raise _BadFrame
    except (_BadFrame, IndexError):
        return None
    end = start + (pos + 7) // 8 + 2  # byte-aligned, then the CRC-16
    return end if end <= len(raw) else None


def _zero_filled_bytes(raw, spans):
    merged = []
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    total = 0
    for a, b in merged:
        for m in re.finditer(rb'\x00{%d,}' % _ZERO_RUN_MIN, raw[a:b]):
            total += m.end() - m.start()
    return total


def _merge_regions(intervals, sr):
    """Sample intervals -> merged [start_s, end_s] seconds."""
    if not intervals:
        return [], 0.0
    intervals = sorted(intervals)
    damaged = sum(b - a for a, b in intervals) / float(sr)
    merged = [list(intervals[0])]
    gap = _REGION_MERGE_SECONDS * sr
    for a, b in intervals[1:]:
        if a - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[round(a / sr, 2), round(b / sr, 2)] for a, b in merged], round(damaged, 2)


def _md5_libsndfile(path, bits, channels):
    """FLAC-layout MD5 via libsndfile, which reads 8/16/24-bit FLAC only.
    Its int reads are exact (no scaling); 24-bit arrives left-justified in
    int32, so its top three bytes are the sample."""
    import soundfile as sf
    h = hashlib.md5()
    with sf.SoundFile(path) as f:
        if f.channels != channels:
            raise ValueError(f'libsndfile sees {f.channels} channels, STREAMINFO {channels}')
        dtype = 'int16' if bits <= 16 else 'int32'
        while True:
            blk = f.read(1 << 16, dtype=dtype, always_2d=True)
            if not blk.size:
                break
            if bits == 16:
                h.update(blk.astype('<i2').tobytes())
            elif bits == 8:
                h.update((blk >> 8).astype('i1').tobytes())
            else:
                h.update(blk.astype('<i4').view(np.uint8)
                         .reshape(-1, 4)[:, 1:].tobytes())
    return h.digest()


def _md5_ffmpeg(path, bits, channels, timeout=_MD5_FFMPEG_TIMEOUT):
    """FLAC-layout MD5 via ffmpeg, for every depth FLAC allows (4-32 bit).
    ffmpeg's FLAC decoder hands out samples left-justified in s16 (up to 16
    bits) or s32 (above), so requesting exactly that PCM format involves no
    conversion and an arithmetic right shift recovers the true value; its
    low (bits+7)//8 little-endian bytes are FLAC's MD5 layout."""
    wide = bits > 16
    fmt, width = ('s32le', 4) if wide else ('s16le', 2)
    shift = (32 if wide else 16) - bits
    nbytes = (bits + 7) // 8
    dtype = '<i4' if wide else '<i2'
    frame = width * channels
    h = hashlib.md5()
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as err, subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-i', path,
             '-map', '0:a:0', '-f', fmt, '-c:a', 'pcm_' + fmt, '-'],
            stdout=subprocess.PIPE, stderr=err) as proc:
        rest = b''
        while True:
            chunk = proc.stdout.read(frame * 65536)
            if not chunk:
                break
            if time.monotonic() > deadline:
                proc.kill()
                raise TimeoutError(f'ffmpeg MD5 decode took over {timeout:g} s')
            chunk = rest + chunk
            cut = len(chunk) - len(chunk) % frame
            rest = chunk[cut:]
            v = np.frombuffer(chunk[:cut], dtype=dtype).astype('<i4') >> shift
            h.update(v.view(np.uint8).reshape(-1, 4)[:, :nbytes].tobytes())
        proc.wait()
        if proc.returncode != 0 or rest:
            err.seek(0)
            msg = err.read().decode('utf-8', 'replace').strip().splitlines()
            raise RuntimeError(msg[0] if msg else f'ffmpeg exited with status {proc.returncode}')
    return h.digest()


def _stream_md5(path, bits, channels):
    """(digest, None) -- the decoded stream's MD5 in FLAC's own layout --
    or (None, why it couldn't be computed). libsndfile first for 8/16/24
    bit (no subprocess; the only backend in this repo's CI); ffmpeg for
    every other depth, and as the fallback when libsndfile can't open the
    file. Never silent: a skipped check always says why (PR #16 review)."""
    reasons = []
    if bits in (8, 16, 24):
        try:
            return _md5_libsndfile(path, bits, channels), None
        except Exception as exc:
            reasons.append(f'libsndfile: {exc}')
    else:
        reasons.append(f'libsndfile reads only 8/16/24-bit FLAC, this is {bits}-bit')
    if not shutil.which('ffmpeg'):
        return None, '; '.join(reasons + ['ffmpeg is not on PATH'])
    try:
        return _md5_ffmpeg(path, bits, channels), None
    except Exception as exc:
        return None, '; '.join(reasons + [f'ffmpeg: {exc}'])


def check_flac(path, verify_md5=False):
    """Verify every frame of a native FLAC file. See the module docstring.

    Result keys: method, result ('ok' | 'damaged' | 'not_checked'), frames
    (expected), bad_frames, missing_frames (headers lost, counted in
    bad_frames), truncated, damaged_seconds, regions ([[start_s, end_s]...],
    merged, at most _MAX_REGIONS), regions_capped, zero_filled_bytes,
    trailing_bytes, md5 ('match' | 'mismatch' | 'unset' | 'not_checked' |
    None = not attempted), md5_detail (why md5 is not 'match'/'mismatch'
    when it was attempted), detail."""
    try:
        return _check_flac(path, verify_md5)
    except Exception as exc:  # never let the verifier break an analysis
        return {'method': METHOD_FLAC, 'result': 'not_checked',
                'detail': f'verifier error: {exc}'}


def frame_index(raw):
    """Parse a FLAC file's bytes: (info, audio_start, strategy, chain), where
    chain is the surviving frame sequence [(offset, header length, coded
    number, block size)] and strategy is 0 (fixed block size: coded number
    = frame index) or 1 (variable: coded number = first sample). On a
    broken metadata chain, info is None and audio_start is the problem."""
    off = flac_offset(raw)
    if off is None:
        return None, 'not a native FLAC stream', 0, []
    info, audio_start = _parse_streaminfo(raw, off)
    if info is None:
        return None, audio_start, 0, []
    buf = np.frombuffer(raw, dtype=np.uint8)
    # candidate syncs: 0xFF then 0xF8 (fixed block size) or 0xF9 (variable)
    a = buf[audio_start:]
    cand = np.flatnonzero((a[:-1] == 0xFF) & ((a[1:] & 0xFE) == 0xF8)) + audio_start
    parsed = {0: [], 1: []}
    for p in cand.tolist():
        strategy = raw[p + 1] & 1
        h = _parse_frame_header(raw, p, info, strategy)
        if h:
            parsed[strategy].append((p,) + h)
    strategy = 1 if len(parsed[1]) > len(parsed[0]) else 0
    headers = parsed[strategy]
    chain = [headers[i] for i in _longest_increasing_chain([h[2] for h in headers])]
    return info, audio_start, strategy, chain


def max_frame_bytes(info, chain):
    """Upper bound on one frame's size: every channel verbatim, one of them
    a side channel with an extra bit, plus header/subframe overhead. Spans
    longer than this between two headers are damaged without a CRC, and it
    bounds the final-frame structure walk."""
    max_bs = max(info['max_blocksize'], max(h[3] for h in chain))
    return max_bs * info['channels'] * (info['bits'] + 1) // 8 + 64


def _check_flac(path, verify_md5):
    # memory-mapped, not read: a long hi-res FLAC can be hundreds of MB and
    # several workers analyze at once; the OS pages in what the walk touches
    with open(path, 'rb') as f:
        try:
            raw = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:  # empty file: nothing to map
            raw = b''
        try:
            return _verify_flac(raw, path, verify_md5)
        finally:
            if isinstance(raw, mmap.mmap):
                try:
                    raw.close()
                except BufferError:  # a numpy view outlived the walk; GC closes it
                    pass


def _verify_flac(raw, path, verify_md5):
    info, audio_start, strategy, chain = frame_index(raw)
    if info is None and audio_start == 'not a native FLAC stream':
        return {'method': METHOD_FLAC, 'result': 'not_checked', 'detail': audio_start}
    if info is None:
        return {'method': METHOD_FLAC, 'result': 'damaged', 'frames': None,
                'bad_frames': None, 'missing_frames': None, 'truncated': None,
                'damaged_seconds': None, 'regions': [], 'regions_capped': False,
                'zero_filled_bytes': 0, 'trailing_bytes': None, 'md5': None,
                'md5_detail': None, 'detail': audio_start}
    sr = info['sample_rate']
    buf = np.frombuffer(raw, dtype=np.uint8)
    if not chain:
        return {'method': METHOD_FLAC, 'result': 'damaged', 'frames': None,
                'bad_frames': None, 'missing_frames': None, 'truncated': None,
                'damaged_seconds': round(info['total_samples'] / sr, 2) or None,
                'regions': [], 'regions_capped': False, 'zero_filled_bytes': 0,
                'trailing_bytes': None, 'md5': None, 'md5_detail': None,
                'detail': 'no valid frame header found'}

    if strategy:  # variable block size: coded number is the first sample
        nominal = info['max_blocksize'] or max(h[3] for h in chain)
        first_sample = [h[2] for h in chain]
    else:  # fixed: coded number is the frame index; every frame but the
        # last has the same block size, so take the most common one
        nominal = Counter(h[3] for h in chain).most_common(1)[0][0]
        first_sample = [h[2] * nominal for h in chain]
    total = info['total_samples']
    max_frame = max_frame_bytes(info, chain)

    def frames_in(samples):
        return -(-samples // nominal) if samples > 0 else 0

    bad = []           # (sample interval, byte span) per bad stretch
    bad_frames = 0
    missing = 0
    check_idx = []
    for i in range(len(chain) - 1):
        p, hl, num, bs = chain[i]
        nxt = chain[i + 1]
        if strategy:
            lost = frames_in(first_sample[i + 1] - first_sample[i] - bs)
        else:
            lost = max(0, nxt[2] - num - 1)
        if lost or first_sample[i] + bs != first_sample[i + 1]:
            missing += lost
            bad_frames += lost
            bad.append(((first_sample[i] + bs, max(first_sample[i + 1], first_sample[i] + bs)),
                        (p, nxt[0])))
        # the span up to the next surviving header is still checked as frame
        # i: when a whole frame was cut out cleanly, frame i itself is intact
        if hl + 2 <= nxt[0] - p <= max_frame:
            check_idx.append(i)
        else:
            bad_frames += 1
            bad.append(((first_sample[i], first_sample[i] + bs), (p, nxt[0])))
    if check_idx:
        idx = np.asarray(check_idx)
        starts = np.asarray([chain[i][0] for i in check_idx], dtype=np.int64)
        ends = np.asarray([chain[i + 1][0] for i in check_idx], dtype=np.int64)
        crcs = _crc16_spans(buf, starts, ends - starts)
        for k in np.flatnonzero(crcs).tolist():
            i = int(idx[k])
            bad_frames += 1
            bad.append(((first_sample[i], first_sample[i] + chain[i][3]),
                        (chain[i][0], chain[i + 1][0])))

    # head: frames before the first surviving header, or junk before it
    detail = None
    if first_sample[0] > 0 or chain[0][0] > audio_start:
        lost = frames_in(first_sample[0])
        if not lost:
            detail = f'{chain[0][0] - audio_start} unexpected bytes before the first frame'
        bad_frames += max(lost, 1)
        missing += lost
        bad.append(((0, max(first_sample[0], 1)), (audio_start, chain[0][0])))

    # the last frame: no next header to bound it, so its end comes from its
    # own structure and its CRC is checked there (see _frame_end)
    p, hl, num, bs = chain[-1]
    end = _frame_end(raw, p, hl, info, bs, max_frame)
    trailing = None
    if end is None or _crc16_spans(buf, [p], [end - p])[0]:
        bad_frames += 1
        bad.append(((first_sample[-1], first_sample[-1] + bs), (p, len(raw))))
    else:
        trailing = len(raw) - end

    # completeness against STREAMINFO: catches a tail cut on a frame boundary
    stream_end = first_sample[-1] + bs
    truncated = bool(total) and stream_end < total

    if trailing and not _trailer_ok(raw, end):
        if raw[end] == 0xFF and raw[end + 1:end + 2] in (b'\xf8', b'\xf9'):
            # a frame sync whose header failed its CRC-8: a damaged final
            # frame, not junk (with a known total the truncation check below
            # already counts it)
            if not truncated:
                bad_frames += 1
                missing += 1
                bad.append(((stream_end, stream_end + nominal), (end, len(raw))))
            if detail is None:
                detail = f'damaged frame data in the last {trailing} bytes'
        elif detail is None:
            detail = f'{trailing} unrecognized bytes after the last frame (not damage)'
    if truncated:
        lost = frames_in(total - stream_end)
        bad_frames += lost
        missing += lost
        bad.append(((stream_end, total), (len(raw), len(raw))))

    expected = -(-total // nominal) if total else (
        (chain[-1][2] + 1) if not strategy else len(chain) + missing)
    regions, damaged_s = _merge_regions([s for s, _ in bad], sr)
    result = {
        'method': METHOD_FLAC,
        'result': 'damaged' if bad else 'ok',
        'frames': expected,
        'bad_frames': bad_frames,
        'missing_frames': missing,
        'truncated': truncated,
        'damaged_seconds': damaged_s,
        'regions': regions[:_MAX_REGIONS],
        'regions_capped': len(regions) > _MAX_REGIONS,
        'zero_filled_bytes': _zero_filled_bytes(raw, [b for _, b in bad]),
        'trailing_bytes': trailing,
        'md5': None,
        'md5_detail': None,
        'detail': detail,
    }
    if verify_md5:
        if bad:
            result['md5_detail'] = 'skipped: frames are already damaged'
        elif not any(info['md5']):
            result['md5'] = 'unset'
            result['md5_detail'] = 'the encoder never stored an MD5'
        else:
            digest, why = _stream_md5(path, info['bits'], info['channels'])
            if digest is None:
                result['md5'] = 'not_checked'
                result['md5_detail'] = why
            elif digest == info['md5']:
                result['md5'] = 'match'
            else:
                result['md5'] = 'mismatch'
                result['result'] = 'damaged'
                result['detail'] = ('every frame passes its checksum but the decoded '
                                    'audio does not match the stream MD5')
    return result


# --------------------------------------------------------------------------
# Generic: ffmpeg decode

_FFMPEG_TIMEOUT = 300.0
_ADDR_RE = re.compile(r' @ 0x[0-9a-fA-F]+')
# a decode this much shorter than the declared duration is a truncation;
# clean files land within one packet (~0.1 s) of it
_TRUNCATION_TOLERANCE_S = 1.0
_TRUNCATION_TOLERANCE_FRAC = 0.01


def _declared_duration(path, timeout=30.0):
    """(seconds, reliable): how long the decoded audio stream (a:0, the one
    check_ffmpeg decodes) says it is.

    The audio stream's own duration, never the container's: a container
    lasts as long as its longest stream, so a valid MP4 with 4 s of video
    and 1 s of audio would otherwise "decode short" (PR #16 review). Only
    when the stream reports no duration (Matroska audio, for one) and the
    audio is the container's *only* stream is the container duration the
    same thing and used instead; otherwise there is no trustworthy promise
    and no truncation is claimed. A duration ffmpeg estimated from the
    bitrate (an MP3 with no Xing/VBRI header) is not a promise either."""
    try:
        p = subprocess.run(
            ['ffprobe', '-v', 'warning', '-select_streams', 'a:0',
             '-show_entries', 'stream=duration:format=duration,nb_streams',
             '-of', 'json', path],
            capture_output=True, text=True, timeout=timeout)
        meta = json.loads(p.stdout or '{}')
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, False
    streams = meta.get('streams') or []
    fmt = meta.get('format') or {}
    if not streams:
        return None, False

    def seconds(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    declared = seconds(streams[0].get('duration'))
    if declared is None and fmt.get('nb_streams') == 1:
        declared = seconds(fmt.get('duration'))
    if declared is None:
        return None, False
    return declared, 'estimating duration from bitrate' not in p.stderr.lower()


def check_ffmpeg(path, timeout=_FFMPEG_TIMEOUT):
    """Decode the whole first audio stream with ffmpeg's CRC/bitstream
    checks on. Result keys: method, result ('ok' | 'damaged' |
    'not_checked'), errors, messages (first five, decoder addresses
    stripped), declared_seconds (the audio stream's own duration),
    decoded_seconds, truncated (None when the declared duration is unknown,
    only estimated, or only the container's in a multi-stream file),
    detail."""
    base = {'method': METHOD_FFMPEG, 'result': 'not_checked', 'errors': None,
            'messages': [], 'declared_seconds': None, 'decoded_seconds': None,
            'truncated': None, 'detail': None}
    if not shutil.which('ffmpeg'):
        return dict(base, detail='ffmpeg not found on PATH')
    try:
        p = subprocess.run(
            ['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error',
             '-err_detect', 'crccheck+bitstream', '-i', path, '-map', '0:a:0',
             '-f', 'null', '-progress', 'pipe:1', '-nostats', '-'],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(base, detail=f'ffmpeg timed out after {timeout:g} s')
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(base, detail=f'ffmpeg failed to run: {exc}')
    lines = [_ADDR_RE.sub('', ln).strip() for ln in p.stderr.splitlines() if ln.strip()]
    decoded = None
    for ln in p.stdout.splitlines():
        if ln.startswith('out_time_us='):
            try:
                decoded = int(ln.split('=', 1)[1]) / 1e6
            except ValueError:
                pass
    if p.returncode != 0 and not lines:
        lines = [f'ffmpeg exited with status {p.returncode}']
    declared, reliable = _declared_duration(path)
    truncated = None
    if declared and reliable and decoded is not None:
        tol = max(_TRUNCATION_TOLERANCE_S, _TRUNCATION_TOLERANCE_FRAC * declared)
        truncated = decoded < declared - tol
    seen, messages = set(), []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            messages.append(ln)
    return dict(base,
                result='damaged' if (lines or truncated) else 'ok',
                errors=len(lines),
                messages=messages[:5],
                declared_seconds=round(declared, 2) if declared else None,
                decoded_seconds=round(decoded, 2) if decoded is not None else None,
                truncated=truncated)


# --------------------------------------------------------------------------

def check_file(path, deep=False):
    """The integrity check for one file: native FLAC frames on every scan
    (plus the stream MD5 when deep), ffmpeg decode for everything else on
    deep scans only, None when nothing applies (non-FLAC, normal scan)."""
    if is_flac_file(path):
        return check_flac(path, verify_md5=deep)
    if deep:
        return check_ffmpeg(path)
    return None


def _clock(seconds):
    m, s = divmod(max(0.0, float(seconds)), 60.0)
    return f'{int(m)}:{s:04.1f}'


def summarize(stream):
    """One ASCII line for est_source / notes, e.g. 'damaged file: 29 bad
    frames, 2.69 s affected (2:41.9-2:46.7, 3:01.0-3:01.6)'."""
    if not stream or stream.get('result') != 'damaged':
        return None
    if stream.get('method') == METHOD_FFMPEG:
        parts = []
        if stream.get('truncated'):
            parts.append(f"decodes to {_clock(stream['decoded_seconds'])} of a declared "
                         f"{_clock(stream['declared_seconds'])}")
        if stream.get('errors'):
            parts.append(f"decoder reported {stream['errors']} error"
                         f"{'s' if stream['errors'] != 1 else ''}")
        return 'damaged file: ' + ', '.join(parts)
    if stream.get('md5') == 'mismatch':
        return 'damaged file: frames intact but the stream MD5 does not match'
    if stream.get('bad_frames') is None:
        return f"damaged file: {stream.get('detail')}"
    n = stream['bad_frames']
    text = f"damaged file: {n} bad frame{'s' if n != 1 else ''}"
    if stream.get('damaged_seconds'):
        text += f", {stream['damaged_seconds']:g} s affected"
    regions = stream.get('regions') or []
    if regions:
        # ASCII hyphen: est_source is a raw TEXT column and has always been
        # plain ASCII (notes go through json.dumps, which escapes)
        shown = ', '.join(f'{_clock(a)}-{_clock(b)}' for a, b in regions[:4])
        more = len(regions) - 4 + (1 if stream.get('regions_capped') else 0)
        text += f' ({shown}{f", +{more} more" if more > 0 else ""})'
    if stream.get('truncated'):
        text += ', truncated'
    return text
