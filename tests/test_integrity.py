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
                         f'(0:{first / self.sr:04.1f}–0:{(first + self.bs) / self.sr:04.1f})')
        self.assertIsNone(integrity.summarize({'result': 'ok'}))


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

    def test_damaged_file_is_inconclusive_corrupt(self):
        # decodes fine and reads CLEAN spectrally, but it is damaged: damage
        # outranks the spectral verdict, which survives as a note
        path = self.write('zf.flac', G.zero_fill_burst(self.cd))
        r, d = self._run(path, skip_spectrogram_for_clean=True)
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertEqual(r['confidence'], 0.0)
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
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertEqual(d['integrity']['stream']['bad_frames'], 1)

    def test_md5_mismatch_needs_deep(self):
        path = self.write('md5.flac', G.md5_flip(self.cd))
        r, d = self._run(path)
        self.assertEqual(r['verdict'], 'CLEAN')
        self.assertEqual(d['integrity']['stream']['result'], 'ok')
        r, d = self._run(path, deep=True)
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertEqual(d['integrity']['stream']['md5'], 'mismatch')
        self.assertEqual(d['integrity']['status'], 'corrupt')

    def test_trailing_junk_keeps_spectral_verdict(self):
        r, d = self._run(self.write('junk.flac', G.junk_suffix(self.cd)))
        self.assertEqual(r['verdict'], 'CLEAN')
        self.assertEqual(d['integrity']['stream']['trailing_bytes'], 4000)

    def test_damage_also_explains_a_decode_failure(self):
        # the frame walk runs before decoding, so an INCONCLUSIVE from a
        # failed decode says why
        r, d = self._run(os.path.join(FIXTURES, 'truncated.flac'), deep=True)
        self.assertIsNone(r['cutoff_hz'])
        self.assertIn('decode_error', d)
        self.assertTrue(r['est_source'].startswith('damaged file:'))
        self.assertTrue(any(n.startswith('decode failed') for n in d['notes']))

    @unittest.skipUnless(HAS_FFMPEG, 'requires ffmpeg on PATH')
    def test_deep_scan_of_damaged_mp3(self):
        path = self.write('zf.mp3', G.zero_fill_burst(_read('consistent_320.mp3')))
        r, d = self._run(path)  # normal scan: no container check for MP3
        self.assertNotEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertIsNone(d['integrity']['stream'])
        r, d = self._run(path, deep=True)
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertEqual(d['integrity']['stream']['method'], 'ffmpeg_decode')


if __name__ == '__main__':
    unittest.main()
