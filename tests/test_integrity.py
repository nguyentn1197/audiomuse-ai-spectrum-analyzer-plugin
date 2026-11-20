"""Container integrity check (plugins/SpectrumAnalyzer/integrity.py) and its
wiring into dsp.analyze_file.

The damage variants are deterministic byte transforms of committed fixtures
(tests/generate_adversarial_fixtures.py, "Container-damage variants"),
built into a temp dir here instead of being committed. Run from the repo
root:

    python3 -m unittest discover tests -v
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'plugins', 'SpectrumAnalyzer'))

import librosa_stub  # noqa: F401,E402  (installs the librosa stand-in)
import dsp  # noqa: E402
import generate_adversarial_fixtures as G  # noqa: E402
import integrity  # noqa: E402

FIXTURES = os.path.join(_HERE, 'fixtures')
HAS_FFMPEG = bool(shutil.which('ffmpeg'))


def _read(name):
    with open(os.path.join(FIXTURES, name), 'rb') as f:
        return f.read()


class _TempFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='sa_integrity_')
        cls.cd = _read('genuine_cd_1644.flac')
        cls.info, cls.audio_start, cls.chain = G._frames(cls.cd)
        cls.bs = cls.chain[0][3]
        cls.sr = cls.info['sample_rate']

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path


class TestCrc(unittest.TestCase):
    def test_frame_parallel_crc_matches_bytewise_reference(self):
        # odd and even lengths, many at once, against the plain per-byte loop
        rng = np.random.default_rng(7)
        buf = rng.integers(0, 256, 60000, dtype=np.uint8)
        starts = rng.integers(0, 50000, 300)
        lengths = rng.integers(2, 9000, 300)
        lengths = np.minimum(lengths, len(buf) - starts)
        got = integrity._crc16_spans(buf, starts, lengths)
        raw = buf.tobytes()
        want = [integrity._crc16(raw[s:s + n]) for s, n in zip(starts, lengths)]
        self.assertEqual(got.tolist(), want)

    def test_crc_of_frame_including_its_crc_is_zero(self):
        data = bytes(range(200))
        crc = integrity._crc16(data)
        framed = np.frombuffer(data + crc.to_bytes(2, 'big'), dtype=np.uint8)
        self.assertEqual(integrity._crc16_spans(framed, [0], [len(framed)]).tolist(), [0])


class TestCleanFixtures(unittest.TestCase):
    def test_every_clean_flac_fixture_verifies_with_md5(self):
        # no false alarms: every committed FLAC except the deliberately
        # truncated one passes every frame CRC and its stream MD5
        for path in sorted(glob.glob(os.path.join(FIXTURES, '*.flac'))):
            if os.path.basename(path) == 'truncated.flac':
                continue
            with self.subTest(fixture=os.path.basename(path)):
                r = integrity.check_flac(path, verify_md5=True)
                self.assertEqual(r['result'], 'ok', r)
                self.assertEqual(r['bad_frames'], 0)
                self.assertFalse(r['truncated'])
                self.assertEqual(r['md5'], 'match')
                self.assertEqual(r['trailing_bytes'], 0)

    def test_segment_mode_only_checks_native_flac(self):
        # one subprocess per non-FLAC track is too much for a library scan
        self.assertIsNone(integrity.check_file(os.path.join(FIXTURES, 'consistent_320.mp3')))
        r = integrity.check_file(os.path.join(FIXTURES, 'genuine_cd_1644.flac'))
        self.assertEqual((r['method'], r['result'], r['md5']), ('flac_frames', 'ok', None))

    def test_truncated_fixture(self):
        r = integrity.check_flac(os.path.join(FIXTURES, 'truncated.flac'))
        self.assertEqual(r['result'], 'damaged')
        self.assertTrue(r['truncated'])
        self.assertEqual(r['regions'][-1][1], 45.0)  # damage runs to the declared end


class TestFlacDamage(_TempFiles):
    def test_zero_filled_transfer_gap(self):
        # the real-world case: chunks lost in transfer, filled with zeros
        r = integrity.check_flac(self.write('zf.flac', G.zero_fill_burst(self.cd)))
        self.assertEqual(r['result'], 'damaged')
        self.assertGreaterEqual(r['bad_frames'], 1)
        self.assertEqual(r['zero_filled_bytes'], 6 * 1158)
        self.assertEqual(len(r['regions']), 1)
        start, end = r['regions'][0]
        self.assertTrue(20.0 < start < end < 32.0, r['regions'])  # ~60% into 45 s

    def test_single_bit_flip_pinned_to_its_frame(self):
        # bit rot that a decoder plays without complaint -- only the CRC sees it
        data, num, first = G.bit_flip(self.cd)
        r = integrity.check_flac(self.write('flip.flac', data))
        self.assertEqual(r['result'], 'damaged')
        self.assertEqual((r['bad_frames'], r['missing_frames']), (1, 0))
        self.assertEqual(r['regions'], [[round(first / self.sr, 2),
                                         round((first + self.bs) / self.sr, 2)]])
        self.assertEqual(r['zero_filled_bytes'], 0)

    def test_dropped_frame_blames_only_the_missing_one(self):
        r = integrity.check_flac(self.write('drop.flac', G.drop_frame(self.cd, 200)))
        self.assertEqual(r['result'], 'damaged')
        self.assertEqual((r['bad_frames'], r['missing_frames']), (1, 1))
        self.assertEqual(r['regions'], [[round(200 * self.bs / self.sr, 2),
                                         round(201 * self.bs / self.sr, 2)]])

    def test_cut_on_frame_boundary_caught_by_sample_count(self):
        # every remaining frame passes its CRC: only STREAMINFO's total
        # sample count can reveal this one
        r = integrity.check_flac(self.write('cut.flac', G.cut_at_frame(self.cd, 400)))
        self.assertEqual(r['result'], 'damaged')
        self.assertTrue(r['truncated'])
        self.assertEqual(r['missing_frames'], len(self.chain) - 400)
        self.assertEqual(r['bad_frames'], r['missing_frames'])

    def test_header_damage(self):
        # a flipped bit in a frame header fails its CRC-8: the header drops
        # out of the chain and the frame is reported missing
        p = self.chain[100][0]
        data = bytearray(self.cd)
        data[p + 2] ^= 0x01
        r = integrity.check_flac(self.write('hdr.flac', bytes(data)))
        self.assertEqual(r['result'], 'damaged')
        self.assertGreaterEqual(r['missing_frames'], 1)
        self.assertAlmostEqual(r['regions'][0][0], 99 * self.bs / self.sr, delta=0.2)

    def test_md5_mismatch_only_seen_by_deep_check(self):
        path = self.write('md5.flac', G.md5_flip(self.cd))
        self.assertEqual(integrity.check_flac(path)['result'], 'ok')
        r = integrity.check_flac(path, verify_md5=True)
        self.assertEqual((r['result'], r['md5'], r['bad_frames']), ('damaged', 'mismatch', 0))

    def test_unset_md5_is_not_damage(self):
        r = integrity.check_flac(self.write('nomd5.flac', G.md5_zero(self.cd)), verify_md5=True)
        self.assertEqual((r['result'], r['md5']), ('ok', 'unset'))
        self.assertIn('never stored', r['md5_detail'])

    def test_large_leading_id3v2_tag(self):
        # bigger than any fixed sniff window, as embedded cover art makes it
        path = self.write('id3v2.flac', G.id3v2_prefix(self.cd, 100_000))
        self.assertTrue(integrity.is_flac_file(path))
        r = integrity.check_file(path, deep=True)
        self.assertEqual((r['method'], r['result'], r['md5']), ('flac_frames', 'ok', 'match'))

    def test_trailing_id3v1_tag_is_recognized(self):
        r = integrity.check_flac(self.write('id3v1.flac', G.id3v1_suffix(self.cd)))
        self.assertEqual((r['result'], r['trailing_bytes'], r['detail']), ('ok', 128, None))

    def test_trailing_junk_is_reported_not_damage(self):
        # libFLAC and ffmpeg both play such a file to completion
        r = integrity.check_flac(self.write('junk.flac', G.junk_suffix(self.cd)))
        self.assertEqual((r['result'], r['trailing_bytes']), ('ok', 4000))
        self.assertIn('not damage', r['detail'])

    def test_variable_block_size_stream(self):
        vb = G.variable_blocksize(self.cd)
        path = self.write('vb.flac', vb)
        import soundfile as sf
        a, _ = sf.read(os.path.join(FIXTURES, 'genuine_cd_1644.flac'), dtype='int16')
        b, _ = sf.read(path, dtype='int16')
        self.assertTrue(np.array_equal(a, b), 'rewrite must be a valid stream')
        r = integrity.check_flac(path, verify_md5=True)
        self.assertEqual((r['result'], r['md5'], r['frames']), ('ok', 'match', len(self.chain)))
        # and damage is still pinned when coded numbers are sample numbers
        flipped, first_sample, _ = G.bit_flip(vb)
        r = integrity.check_flac(self.write('vb_flip.flac', flipped))
        self.assertEqual((r['result'], r['bad_frames']), ('damaged', 1))
        self.assertEqual(r['regions'][0][0], round(first_sample / self.sr, 2))

    def test_many_scattered_regions_are_capped(self):
        data = self.cd
        for i in range(0, len(self.chain) - 1, 12):  # > 1 s apart: never merged
            data = G.bit_flip(data, frame=i)[0]
        r = integrity.check_flac(self.write('many.flac', data))
        self.assertEqual(r['bad_frames'], len(range(0, len(self.chain) - 1, 12)))
        self.assertEqual(len(r['regions']), integrity._MAX_REGIONS)
        self.assertTrue(r['regions_capped'])
        self.assertIn('more)', integrity.summarize(r))

    def test_broken_metadata_chain(self):
        r = integrity.check_flac(self.write('meta.flac', self.cd[:30]))
        self.assertEqual(r['result'], 'damaged')
        self.assertIn('metadata', r['detail'])

    def test_not_flac_and_unreadable_never_raise(self):
        r = integrity.check_flac(os.path.join(FIXTURES, 'consistent_320.mp3'))
        self.assertEqual(r['result'], 'not_checked')
        r = integrity.check_flac(os.path.join(self.tmp, 'does_not_exist.flac'))
        self.assertEqual(r['result'], 'not_checked')
        self.assertIn('verifier error', r['detail'])
        self.assertIsNone(integrity.check_file(os.path.join(self.tmp, 'missing.flac')))

    def test_summary_text(self):
        data, num, first = G.bit_flip(self.cd)
        r = integrity.check_flac(self.write('flip2.flac', data))
        self.assertEqual(integrity.summarize(r),
                         f'damaged file: 1 bad frame, {r["damaged_seconds"]:g} s affected '
                         f'(0:{first / self.sr:04.1f}-0:{(first + self.bs) / self.sr:04.1f})')
        self.assertIsNone(integrity.summarize({'result': 'ok'}))


class TestFinalFrame(_TempFiles):
    """PR #16 review, P1: the final frame has no next header, and accepting
    the first place a running CRC-16 hits zero let corruption after a chance
    zero pass (146/200 single-byte corruptions of a one-frame file). Its end
    now comes from walking its structure; the CRC is checked there."""

    def _flip(self, raw, offset, mask=0x5A):
        data = bytearray(raw)
        data[offset] ^= mask
        return bytes(data)

    def test_bit_flip_inside_the_final_frame(self):
        p, hl, num, bs = self.chain[-1]
        last_start = num * self.bs
        r = integrity.check_flac(self.write('last.flac',
                                            self._flip(self.cd, (p + hl + len(self.cd)) // 2)))
        self.assertEqual(r['result'], 'damaged')
        self.assertEqual((r['bad_frames'], r['missing_frames']), (1, 0))
        self.assertEqual(r['regions'], [[round(last_start / self.sr, 2),
                                         round((last_start + bs) / self.sr, 2)]])

    def test_every_byte_position_of_a_single_frame_file(self):
        # the reviewer's scenario: a one-frame file, one byte changed. Before
        # the fix most positions passed; now every one must be caught.
        import soundfile as sf
        rng = np.random.default_rng(0)
        path = os.path.join(self.tmp, 'single.flac')
        sf.write(path, (rng.standard_normal((4000, 2)) * 3000).astype(np.int16), 44100,
                 subtype='PCM_16')
        raw = open(path, 'rb').read()
        self.assertEqual(integrity.check_flac(path)['result'], 'ok')
        p, hl = integrity.frame_index(raw)[3][0][:2]
        offsets = np.linspace(p + hl, len(raw) - 3, 120).astype(int)
        missed = [int(o) for o in offsets
                  if integrity.check_flac(self.write('s.flac', self._flip(raw, o)))['result']
                  != 'damaged']
        self.assertEqual(missed, [])

    def test_structural_end_matches_the_next_header_on_every_fixture(self):
        # the frame walker must land exactly on the next frame header for
        # every encoding the fixtures hold (sampled to keep the suite quick)
        for path in sorted(glob.glob(os.path.join(FIXTURES, '*.flac'))):
            if os.path.basename(path) == 'truncated.flac':
                continue
            raw = open(path, 'rb').read()
            info, _start, _strategy, chain = integrity.frame_index(raw)
            limit = integrity.max_frame_bytes(info, chain)
            with self.subTest(fixture=os.path.basename(path)):
                picks = sorted(set(np.linspace(0, len(chain) - 1, 25).astype(int)))
                for i in picks:
                    p, hl, _num, bs = chain[i]
                    want = chain[i + 1][0] if i + 1 < len(chain) else len(raw)
                    self.assertEqual(integrity._frame_end(raw, p, hl, info, bs, limit), want,
                                     f'frame {i}')

    def test_damaged_final_header_without_a_declared_total(self):
        # no STREAMINFO total to compare against: a final frame whose header
        # fails its CRC-8 must still not pass as "trailing junk"
        data = bytearray(self.cd)
        si = integrity.flac_offset(self.cd) + 8
        x = int.from_bytes(data[si + 10:si + 18], 'big') & ~((1 << 36) - 1)
        data[si + 10:si + 18] = x.to_bytes(8, 'big')       # total_samples = 0
        data[self.chain[-1][0] + 2] ^= 0x01                # break the last header
        r = integrity.check_flac(self.write('nototal.flac', bytes(data)))
        self.assertEqual(r['result'], 'damaged')
        self.assertIn('damaged frame data', r['detail'])

    def test_trailing_tag_and_junk_still_pass(self):
        for name, data in (('id3v1', G.id3v1_suffix(self.cd)), ('junk', G.junk_suffix(self.cd))):
            with self.subTest(case=name):
                r = integrity.check_flac(self.write(name + '.flac', data))
                self.assertEqual(r['result'], 'ok')


class TestMd5AllBitDepths(_TempFiles):
    """PR #16 review, P3: FLAC allows 4-32-bit samples; libsndfile reads
    only 8/16/24, so other depths go through ffmpeg -- and when neither can,
    md5 is 'not_checked' with md5_detail saying why, never silently."""
    DEPTHS = (4, 8, 12, 16, 20, 24, 32)

    def _file(self, bits):
        return self.write(f'v{bits}.flac',
                          G.verbatim_flac(G.odd_depth_samples(bits), 44100, bits))

    def test_verbatim_writer_output_decodes_to_its_samples(self):
        # the generator is only a fair test input if real decoders agree
        import soundfile as sf
        for bits in (8, 16, 24):
            with self.subTest(bits=bits):
                y, _ = sf.read(self._file(bits), dtype='int32')
                self.assertTrue(np.array_equal(y >> (32 - bits), G.odd_depth_samples(bits)))
        for bits in self.DEPTHS:
            with self.subTest(bits=bits):
                self.assertEqual(integrity.check_flac(self._file(bits))['result'], 'ok')

    def test_libsndfile_depths_need_no_ffmpeg(self):
        with mock.patch.object(integrity.shutil, 'which', return_value=None):
            for bits in (8, 16, 24):
                with self.subTest(bits=bits):
                    r = integrity.check_flac(self._file(bits), verify_md5=True)
                    self.assertEqual((r['md5'], r['md5_detail']), ('match', None))

    def test_other_depths_without_ffmpeg_say_why(self):
        with mock.patch.object(integrity.shutil, 'which', return_value=None):
            for bits in (4, 12, 20, 32):
                with self.subTest(bits=bits):
                    r = integrity.check_flac(self._file(bits), verify_md5=True)
                    self.assertEqual((r['result'], r['md5']), ('ok', 'not_checked'))
                    self.assertIn(f'{bits}-bit', r['md5_detail'])
                    self.assertIn('ffmpeg is not on PATH', r['md5_detail'])

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_every_depth_verifies_and_catches_a_wrong_md5(self):
        for bits in self.DEPTHS:
            with self.subTest(bits=bits):
                path = self._file(bits)
                r = integrity.check_flac(path, verify_md5=True)
                self.assertEqual((r['result'], r['md5']), ('ok', 'match'), r['md5_detail'])
                bad = integrity.check_flac(self.write(f'v{bits}_bad.flac',
                                                      G.md5_flip(open(path, 'rb').read())),
                                           verify_md5=True)
                self.assertEqual((bad['result'], bad['md5']), ('damaged', 'mismatch'))

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_real_encoder_32_bit(self):
        # independent of the generator: ffmpeg's own 32-bit FLAC encoder
        import subprocess
        path = os.path.join(self.tmp, 'enc32.flac')
        p = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-y', '-f',
                            'lavfi', '-i', 'anoisesrc=d=1:c=pink:r=48000:a=0.3', '-ac', '2',
                            '-sample_fmt', 's32', '-bits_per_raw_sample', '32', '-strict',
                            'experimental', '-c:a', 'flac', path], capture_output=True)
        if p.returncode != 0:
            self.skipTest('this ffmpeg cannot encode 32-bit FLAC')
        r = integrity.check_flac(path, verify_md5=True)
        self.assertEqual((r['result'], r['md5']), ('ok', 'match'), r['md5_detail'])


class TestResourceBounds(_TempFiles):
    """Second PR #16 review: the final-frame walk expanded the frame into
    per-bit arrays and lists (415 MiB for one valid 786 KB frame), and the
    ffmpeg MD5 deadline could not fire while a read was blocked. Memory must
    stay near the frame/file-independent baseline, and a stalled ffmpeg must
    be killed and reaped at the deadline."""

    def _peak(self, fn, *args):
        import tracemalloc
        tracemalloc.start()
        try:
            result = fn(*args)
            return result, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_large_multichannel_frame_walk_is_bounded(self):
        # the reviewer's case: 8 channels x 24 bit x 32768 samples, one
        # ~786 KB frame
        rng = np.random.default_rng(5)
        raw = G.verbatim_flac(rng.integers(-(1 << 23), 1 << 23, (32768, 8)), 48000, 24,
                              blocksize=32768)
        info, _start, _strategy, chain = integrity.frame_index(raw)
        p, hl, _num, bs = chain[0]
        frame = len(raw) - p
        self.assertGreater(frame, 780_000)
        end, peak = self._peak(integrity._frame_end, raw, p, hl, info, bs,
                               integrity.max_frame_bytes(info, chain))
        self.assertEqual(end, len(raw))
        self.assertLess(peak, 3 * frame)   # was ~550x the frame
        path = self.write('big8ch.flac', raw)
        integrity._crc16_shift_table()   # one-time table, not per file
        r, peak = self._peak(integrity.check_flac, path)
        self.assertEqual(r['result'], 'ok')
        self.assertLess(peak, 8 * frame)   # whole check incl. the CRC's batches
        bad = bytearray(raw)
        bad[p + hl + frame // 2] ^= 0x10
        self.assertEqual(integrity.check_flac(self.write('big8ch_bad.flac', bytes(bad)))['result'],
                         'damaged')

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_large_rice_coded_frames_are_bounded(self):
        # same size class through the residual-decoding path (a real encoder)
        import subprocess
        path = os.path.join(self.tmp, 'big8ch_rice.flac')
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'anoisesrc=d=1.5:c=pink:r=48000:a=0.3', '-af',
                        'pan=7.1|c0=c0|c1=0.9*c0|c2=0.8*c0|c3=0.2*c0|c4=0.7*c0|c5=0.6*c0'
                        '|c6=0.5*c0|c7=0.4*c0', '-sample_fmt', 's32', '-bits_per_raw_sample',
                        '24', '-c:a', 'flac', '-frame_size', '32768', path], check=True)
        raw = open(path, 'rb').read()
        info, _start, _strategy, chain = integrity.frame_index(raw)
        p, hl, _num, bs = chain[-1]
        end, peak = self._peak(integrity._frame_end, raw, p, hl, info, bs,
                               integrity.max_frame_bytes(info, chain))
        self.assertEqual(end, len(raw))
        self.assertLess(peak, 3 * (len(raw) - p))
        self.assertEqual(integrity.check_flac(path, verify_md5=True)['md5'], 'match')

    def test_whole_check_memory_does_not_scale_with_file_size(self):
        # the sync scan used to build ~3x the file in numpy temporaries; now
        # the working set is a constant few MiB (the CRC's batches) plus a
        # few hundred bytes per frame
        import soundfile as sf
        rng = np.random.default_rng(9)
        integrity._crc16_shift_table()   # one-time table, not per file
        peaks, sizes = [], []
        for seconds in (30, 90):
            path = os.path.join(self.tmp, f'long{seconds}.flac')
            sf.write(path, (rng.standard_normal((44100 * seconds, 2)) * 8000).astype(np.int16),
                     44100, subtype='PCM_16')
            r, peak = self._peak(integrity.check_flac, path)
            self.assertEqual(r['result'], 'ok')
            peaks.append(peak)
            sizes.append(os.path.getsize(path))
        self.assertGreater(sizes[1], 12_000_000)
        self.assertLess(peaks[1], 1.3 * peaks[0] + 1_000_000)   # 3x the file, ~same peak
        self.assertLess(peaks[1], sizes[1])                     # was ~3x the file

    def test_block_crc_matches_reference_for_any_span_layout(self):
        rng = np.random.default_rng(11)
        buf = rng.integers(0, 256, 1_500_000, dtype=np.uint8)
        raw = buf.tobytes()
        starts = rng.integers(0, 700_000, 200)
        lengths = rng.integers(1, 20000, 200)
        lengths[:9] = [1, 2, 255, 256, 257, 511, 512, 513, 790_000]
        lengths = np.minimum(lengths, len(buf) - starts)
        got = integrity._crc16_spans(buf, starts, lengths)
        self.assertEqual(got.tolist(), [integrity._crc16(raw[s:s + n])
                                        for s, n in zip(starts, lengths)])

    def _fake_ffmpeg(self, mode):
        """An 'ffmpeg' on PATH that writes some PCM, then stalls: 'silent'
        keeps stdout open and writes nothing more; 'linger' closes stdout but
        never exits. Records its pid so the test can prove it was reaped."""
        bindir = os.path.join(self.tmp, 'fakebin_' + mode)
        os.makedirs(bindir, exist_ok=True)
        pidfile = os.path.join(bindir, 'pid')
        with open(os.path.join(bindir, 'ffmpeg'), 'w') as f:
            f.write(f'#!{sys.executable}\n'
                    'import os, sys, time\n'
                    f'open({pidfile!r}, "w").write(str(os.getpid()))\n'
                    'sys.stdout.buffer.write(b"\\0" * 4096); sys.stdout.flush()\n'
                    + ('os.close(1)\n' if mode == 'linger' else '')
                    + 'time.sleep(60)\n')
        os.chmod(os.path.join(bindir, 'ffmpeg'), 0o755)
        return bindir, pidfile

    def _assert_gone(self, pidfile):
        pid = int(open(pidfile).read())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    @unittest.skipUnless(os.name == 'posix', 'fake executable needs a POSIX shebang')
    def test_stalled_ffmpeg_is_killed_at_the_deadline(self):
        import time
        flac = self.write('v20.flac', G.verbatim_flac(G.odd_depth_samples(20), 44100, 20))
        for mode in ('silent', 'linger'):
            with self.subTest(mode=mode):
                bindir, pidfile = self._fake_ffmpeg(mode)
                env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get('PATH', ''))
                with mock.patch.dict(os.environ, env):
                    t = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        integrity._md5_ffmpeg(flac, 20, 2, timeout=0.5)
                    self.assertLess(time.monotonic() - t, 10)   # not the fake's 60 s
                self._assert_gone(pidfile)

    @unittest.skipUnless(os.name == 'posix', 'fake executable needs a POSIX shebang')
    def test_stalled_md5_degrades_to_not_checked_with_a_reason(self):
        flac = self.write('v12.flac', G.verbatim_flac(G.odd_depth_samples(12), 44100, 12))
        bindir, pidfile = self._fake_ffmpeg('silent')
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get('PATH', ''))
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(integrity, '_MD5_FFMPEG_TIMEOUT', 0.5):
            r = integrity.check_flac(flac, verify_md5=True)
        self.assertEqual((r['result'], r['md5']), ('ok', 'not_checked'))
        self.assertIn('killed', r['md5_detail'])
        self._assert_gone(pidfile)


class TestFfmpegCheck(_TempFiles):
    def test_without_ffmpeg_reports_not_checked(self):
        with mock.patch.object(integrity.shutil, 'which', return_value=None):
            r = integrity.check_ffmpeg(os.path.join(FIXTURES, 'consistent_320.mp3'))
        self.assertEqual(r['result'], 'not_checked')
        self.assertIn('ffmpeg', r['detail'])

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_clean_non_flac_fixtures_pass(self):
        for name in ('consistent_320.mp3', 'honest_lowpassed_320.mp3', 'transcoded_128as320.mp3',
                     'lossy_aac_256.m4a', 'lossless_alac.m4a', 'lossy_opus_128.ogg',
                     'lossy_vorbis_q6.ogg'):
            with self.subTest(fixture=name):
                r = integrity.check_file(os.path.join(FIXTURES, name), deep=True)
                self.assertEqual((r['method'], r['result'], r['errors']),
                                 ('ffmpeg_decode', 'ok', 0), r)
                self.assertFalse(r['truncated'])

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_truncated_mp3_caught_by_declared_duration(self):
        r = integrity.check_ffmpeg(os.path.join(FIXTURES, 'truncated.mp3'))
        self.assertEqual(r['result'], 'damaged')
        self.assertTrue(r['truncated'])
        self.assertLess(r['decoded_seconds'], r['declared_seconds'] - 10)
        self.assertIn('decodes to', integrity.summarize(r))

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_video_longer_than_audio_is_not_truncation(self):
        # PR #16 review, P2: a container lasts as long as its longest stream;
        # a valid MP4 with 4 s of video and 1 s of audio is not truncated
        import subprocess
        path = os.path.join(self.tmp, 'av.mp4')
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc=duration=4:size=64x64:rate=10',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                        '-c:v', 'mpeg4', '-c:a', 'aac', path], check=True)
        r = integrity.check_ffmpeg(path)
        self.assertEqual((r['result'], r['truncated'], r['errors']), ('ok', False, 0))
        self.assertAlmostEqual(r['declared_seconds'], 1.0, delta=0.1)
        r, _d = (lambda res: (res, json.loads(res['details'])))(
            dsp.analyze_file(path, suffix='mp4', bitrate_kbps=128, deep=True))
        self.assertNotEqual(r['verdict'], 'DAMAGED')

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_single_stream_container_without_stream_duration(self):
        # Matroska reports no per-stream duration; with audio as the only
        # stream, the container duration is the same promise
        import subprocess
        path = os.path.join(self.tmp, 'a.mka')
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-y',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                        '-c:a', 'flac', path], check=True)
        r = integrity.check_ffmpeg(path)
        self.assertEqual((r['result'], r['truncated']), ('ok', False))
        self.assertAlmostEqual(r['declared_seconds'], 3.0, delta=0.1)

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_zero_filled_mp3_raises_decoder_errors(self):
        path = self.write('zf.mp3', G.zero_fill_burst(_read('consistent_320.mp3')))
        r = integrity.check_ffmpeg(path)
        self.assertEqual(r['result'], 'damaged')
        self.assertGreater(r['errors'], 0)
        self.assertTrue(r['messages'])
        self.assertNotIn('@ 0x', r['messages'][0])  # decoder addresses stripped


class TestAnalyzeFileWiring(_TempFiles):
    def _run(self, path, deep=False, **kw):
        r = dsp.analyze_file(path, suffix=os.path.splitext(path)[1][1:],
                             bitrate_kbps=900, deep=deep, **kw)
        return r, json.loads(r['details'])

    def test_damaged_file_gets_its_own_verdict(self):
        # decodes fine and reads CLEAN spectrally, but it is damaged: damage
        # outranks the spectral verdict, which survives as a note. DAMAGED is
        # its own primary verdict (not INCONCLUSIVE): the evidence is the
        # file's own checksums, so confidence is 1.0
        path = self.write('zf.flac', G.zero_fill_burst(self.cd))
        r, d = self._run(path, skip_spectrogram_for_clean=True)
        self.assertEqual(r['verdict'], 'DAMAGED')
        self.assertEqual(r['confidence'], 1.0)
        self.assertFalse(r['deep_eligible'])
        self.assertEqual((d['integrity']['status'], d['integrity']['coverage']), ('corrupt', 'full'))
        self.assertTrue(r['est_source'].startswith('damaged file:'), r['est_source'])
        self.assertTrue(d['notes'][0].startswith('integrity: damaged file'))
        self.assertTrue(any(n.startswith('spectral reading of the decodable audio: CLEAN')
                            for n in d['notes']), d['notes'])
        self.assertIsNotNone(r['cutoff_hz'])           # measurements kept
        self.assertIsNotNone(r['spectrogram_b64'])     # rendered: not CLEAN any more

    def test_bit_rot_that_decodes_cleanly_is_still_caught(self):
        r, d = self._run(self.write('flip.flac', G.bit_flip(self.cd)[0]))
        self.assertEqual(r['verdict'], 'DAMAGED')
        self.assertEqual(d['integrity']['stream']['bad_frames'], 1)

    def test_md5_mismatch_needs_deep(self):
        path = self.write('md5.flac', G.md5_flip(self.cd))
        r, d = self._run(path)
        self.assertEqual(r['verdict'], 'CLEAN')
        self.assertEqual(d['integrity']['stream']['result'], 'ok')
        r, d = self._run(path, deep=True)
        self.assertEqual(r['verdict'], 'DAMAGED')
        self.assertEqual(d['integrity']['stream']['md5'], 'mismatch')
        self.assertEqual(d['integrity']['status'], 'corrupt')

    def test_trailing_junk_keeps_spectral_verdict(self):
        r, d = self._run(self.write('junk.flac', G.junk_suffix(self.cd)))
        self.assertEqual(r['verdict'], 'CLEAN')
        self.assertEqual(d['integrity']['stream']['trailing_bytes'], 4000)

    def test_damage_also_explains_a_decode_failure(self):
        # the frame walk runs before decoding, so a failed decode on a
        # damaged file is DAMAGED with the location, not a bare INCONCLUSIVE
        r, d = self._run(os.path.join(FIXTURES, 'truncated.flac'), deep=True)
        self.assertEqual((r['verdict'], r['confidence']), ('DAMAGED', 1.0))
        self.assertIsNone(r['cutoff_hz'])
        self.assertIn('decode_error', d)
        self.assertTrue(r['est_source'].startswith('damaged file:'))
        self.assertTrue(any(n.startswith('decode failed') for n in d['notes']))

    def test_undecodable_but_not_provably_damaged_stays_inconclusive(self):
        # garbage with no FLAC marker: nothing to verify, so no damage claim
        # -- the row is INCONCLUSIVE (couldn't analyze), never DAMAGED
        r, d = self._run(self.write('garbage.flac', b'not actually audio' * 100))
        self.assertEqual((r['verdict'], r['confidence']), ('INCONCLUSIVE', 0.0))
        self.assertEqual(d['integrity']['status'], 'decode_failed')
        self.assertIsNone(d['integrity']['stream'])

    def test_damaged_is_not_a_suspect_verdict(self):
        # the web layer's suspect counts and bulk deep scans must not pick
        # damaged rows up; the verdict still gets a style (badge + filter)
        src = open(os.path.join(os.path.dirname(_HERE), 'plugins', 'SpectrumAnalyzer',
                                '__init__.py'), encoding='utf-8').read()
        self.assertIn("'DAMAGED':", src)
        suspects = src.split('SUSPECT_VERDICTS = ', 1)[1].split('\n', 1)[0]
        self.assertNotIn('DAMAGED', suspects)

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_deep_scan_of_damaged_mp3(self):
        path = self.write('zf.mp3', G.zero_fill_burst(_read('consistent_320.mp3')))
        r, d = self._run(path)  # normal scan: no container check for MP3
        self.assertNotEqual(r['verdict'], 'DAMAGED')
        self.assertIsNone(d['integrity']['stream'])
        r, d = self._run(path, deep=True)
        self.assertEqual(r['verdict'], 'DAMAGED')
        self.assertEqual(d['integrity']['stream']['method'], 'ffmpeg_decode')


if __name__ == '__main__':
    unittest.main()
