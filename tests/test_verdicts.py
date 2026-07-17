"""Ground-truth verdict tests against the committed fixtures.

Runs the real dsp.analyze_file (librosa stubbed with soundfile + numpy).
Requires: numpy, soundfile, matplotlib. Run from the repo root:

    python3 -m unittest discover tests -v
"""
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'plugins', 'SpectrumAnalyzer'))

import librosa_stub  # noqa: F401  (installs the librosa stand-in)
import dsp  # noqa: E402

FIXTURES = os.path.join(_HERE, 'fixtures')

# (file, suffix, declared kbps, expected verdict) — segment analysis
SEGMENT_CASES = [
    ('genuine_dsd_2496.flac', 'flac', 2800, 'CLEAN'),
    ('genuine_cd_1644.flac', 'flac', 900, 'CLEAN'),
    ('fake_hires_44to96.flac', 'flac', 1400, 'UPSAMPLED'),
    ('fake_hires_48to96.flac', 'flac', 1400, 'UPSAMPLED'),
    ('fake_24bit_96k.flac', 'flac', 1000, 'UPSCALED'),
    ('fake_lossless_128.flac', 'flac', 500, 'FAKE_SUSPECT'),
    ('fake_lossless_320.flac', 'flac', 600, 'FAKE_SUSPECT'),
    ('transcoded_128as320.mp3', 'mp3', 320, 'TRANSCODED_LOSSY'),
    ('consistent_320.mp3', 'mp3', 320, 'CONSISTENT_LOSSY'),
    ('dark_master_96k.flac', 'flac', 2000, 'CLEAN'),
]

# deep (whole-file) analysis — the dark-master discriminator must never
# turn a genuine file into a suspect or launder a fake into CLEAN
DEEP_CASES = [
    ('fake_hires_44to96.flac', 'flac', 1400, 'UPSAMPLED'),
    ('dark_master_96k.flac', 'flac', 2000, 'CLEAN'),
    ('fake_lossless_128.flac', 'flac', 500, 'FAKE_SUSPECT'),
]


def _analyze(name, suffix, kbps, deep=False):
    return dsp.analyze_file(os.path.join(FIXTURES, name), suffix=suffix,
                            bitrate_kbps=kbps, deep=deep)


class TestSegmentVerdicts(unittest.TestCase):
    def test_verdicts(self):
        for name, suffix, kbps, expected in SEGMENT_CASES:
            with self.subTest(fixture=name):
                r = _analyze(name, suffix, kbps)
                self.assertEqual(r['verdict'], expected,
                                 f"{name}: got {r['verdict']} ({r['est_source']}), "
                                 f"details={r['details']}")

    def test_bit_depth_probe(self):
        r = _analyze('fake_24bit_96k.flac', 'flac', 1000)
        self.assertEqual((r['container_bits'], r['effective_bits']), (24, 16))
        r = _analyze('genuine_dsd_2496.flac', 'flac', 2800)
        self.assertEqual((r['container_bits'], r['effective_bits']), (24, 24))

    def test_result_contract(self):
        r = _analyze('genuine_cd_1644.flac', 'flac', 900)
        for key in ('sample_rate', 'cutoff_hz', 'edge_db_khz', 'verdict',
                    'confidence', 'details', 'spectrogram_b64'):
            self.assertIn(key, r)
        self.assertTrue(len(r['spectrogram_b64']) > 1000)
        json.loads(r['details'])  # must be valid JSON


class TestDeepVerdicts(unittest.TestCase):
    def test_verdicts(self):
        for name, suffix, kbps, expected in DEEP_CASES:
            with self.subTest(fixture=name):
                r = _analyze(name, suffix, kbps, deep=True)
                self.assertEqual(r['verdict'], expected,
                                 f"{name}: got {r['verdict']} ({r['est_source']}), "
                                 f"details={r['details']}")
                d = json.loads(r['details'])
                self.assertTrue(d['deep'])

    def test_deep_covers_whole_file(self):
        r = _analyze('genuine_cd_1644.flac', 'flac', 900, deep=True)
        self.assertGreater(r['seg_seconds'], 40)  # 45 s fixture, not a segment


if __name__ == '__main__':
    unittest.main()
