"""Ground-truth verdict tests against the committed fixtures.

Runs the real dsp.analyze_file (librosa stubbed with soundfile + numpy).
Requires: numpy, soundfile, matplotlib. Run from the repo root:

    python3 -m unittest discover tests -v
"""
import json
import os
import sys
import unittest

import numpy as np

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
                    'confidence', 'details', 'spectrogram_b64', 'analysis_rev'):
            self.assertIn(key, r)
        self.assertTrue(len(r['spectrogram_b64']) > 1000)
        json.loads(r['details'])  # must be valid JSON
        self.assertEqual(r['analysis_rev'], dsp.analysis_rev(40, 90))  # defaults

    def test_analysis_rev(self):
        # the stamp must move with the analyzer constant and every
        # verdict-relevant setting, so stored results re-analyze on change
        base = dsp.analysis_rev(40, 90)
        self.assertNotEqual(base, dsp.analysis_rev(45, 90))
        self.assertNotEqual(base, dsp.analysis_rev(40, 120))
        self.assertIn(f'r{dsp.ANALYSIS_REV}-', base)

    def test_decode_failure_is_inconclusive(self):
        # a file that can't be decoded at all must come back as a storable
        # INCONCLUSIVE result, not a raised exception -- otherwise the
        # track silently vanishes from scans instead of surfacing as
        # "never meaningfully analyzed" (jobs.py drops uncaught exceptions)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.flac') as f:
            f.write(b'not actually audio' * 100)
            f.flush()
            r = dsp.analyze_file(f.name, suffix='flac', bitrate_kbps=900)
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertFalse(r['deep_eligible'])
        self.assertEqual(r['analysis_rev'], dsp.analysis_rev(40, 90))
        d = json.loads(r['details'])
        self.assertEqual(d['integrity'], {'status': 'decode_failed', 'coverage': None})

    def test_dsd_magic_bytes_unsupported(self):
        # DSD must be recognized (and refused) by content, not extension --
        # the audioread/ffmpeg fallback would otherwise silently decode DSD
        # noise shaping as full-bandwidth PCM and read it as CLEAN
        import struct
        import tempfile

        cases = [
            ('dsf', b'DSD ' + b'\x00' * 60),
            ('dff', b'FRM8' + struct.pack('>Q', 60) + b'DSD ' + b'\x00' * 40),
        ]
        for fmt, header in cases:
            with self.subTest(fmt=fmt):
                with tempfile.NamedTemporaryFile(suffix=f'.{fmt}') as f:
                    f.write(header)
                    f.flush()
                    r = dsp.analyze_file(f.name, suffix=fmt, bitrate_kbps=2800)
                self.assertEqual(r['verdict'], 'INCONCLUSIVE')
                self.assertFalse(r['deep_eligible'])
                d = json.loads(r['details'])
                self.assertEqual(d['detected_format'], fmt)
                self.assertEqual(d['integrity'], {'status': 'unsupported', 'coverage': None})

    def test_container_probe_no_regression(self):
        # the soundfile-based tier-2 probe must be a no-op for every fixture
        # we have (suffix and detected format already agree)
        r = _analyze('genuine_cd_1644.flac', 'flac', 900)
        d = json.loads(r['details'])
        self.assertEqual(d['integrity'], {'status': 'sampled_decode_ok', 'coverage': 'sampled'})
        self.assertIsNone(d['delivery'])
        r = _analyze('consistent_320.mp3', 'mp3', 320)
        self.assertIsNone(json.loads(r['details'])['delivery'])


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

    def test_deep_integrity_full_decode(self):
        # these fixtures (45 s) run off the natural end of file, nowhere near
        # the 1800 s cap, with no chunk errors -> full_decode_ok/full
        r = _analyze('genuine_cd_1644.flac', 'flac', 900, deep=True)
        d = json.loads(r['details'])
        self.assertEqual(d['integrity'], {'status': 'full_decode_ok', 'coverage': 'full'})


class TestCutoffDetection(unittest.TestCase):
    """Synthetic-array tests for the Hz-based, gap-tolerant _find_cutoff --
    no real audio payload needed since these exercise the mask logic
    directly. Full adversarial fixtures (real encoded pilot tones etc.) are
    scoped to the later "minimum adversarial fixtures" plan item."""

    @staticmethod
    def _spectrum(sr, edge_hz, n=2049, floor_db=-60.0, notches=(), tone=None):
        # notches: list of (start_hz, end_hz) dropped to floor within the
        # real band. tone: (center_hz, width_hz) isolated spike at 0 dB.
        freqs = np.linspace(0, sr / 2.0, n)
        profile = np.where(freqs <= edge_hz, 0.0, floor_db)
        for lo, hi in notches:
            profile[(freqs >= lo) & (freqs < hi)] = floor_db
        if tone:
            center, width = tone
            profile[(freqs >= center - width / 2) & (freqs <= center + width / 2)] = 0.0
        return freqs, profile

    def test_gap_tolerant_occupancy(self):
        # four small notches (20 Hz each, well under the 120 Hz max-gap)
        # packed into the last ~250 Hz before the real edge -- a strict
        # "every bin in the window must be above threshold" rule would fail
        # here (combined gap loss ~32%), but gap-closing plus a 75%
        # occupancy requirement should still find the real edge.
        freqs, profile = self._spectrum(
            44100, edge_hz=12000,
            notches=[(11800, 11820), (11840, 11860), (11880, 11900), (11920, 11940)])
        cutoff_hz, ref, tone = dsp._find_cutoff(freqs, profile, drop_db=40)
        self.assertAlmostEqual(cutoff_hz, 12000, delta=150)
        self.assertFalse(tone)

        # white-box check: without gap-closing, the same window is below the
        # occupancy threshold -- confirms gap-closing is load-bearing here,
        # not just the occupancy tolerance alone.
        bin_hz = float(freqs[1] - freqs[0])
        above = profile >= (ref - 40)
        tone_max_bins = dsp._hz_to_bins(dsp._CUTOFF_TONE_MAX_WIDTH_HZ, bin_hz)
        above, _ = dsp._exclude_narrow_tones(above, freqs, tone_max_bins)
        min_width_bins = dsp._hz_to_bins(dsp._CUTOFF_MIN_WIDTH_HZ, bin_hz)
        raw_occ = dsp._sliding_occupancy(above, min_width_bins)
        edge_idx = int(np.argmin(np.abs(freqs - 12000)))
        self.assertLess(raw_occ[edge_idx], dsp._CUTOFF_OCCUPANCY_FRAC)

    def test_pilot_tone_excluded_from_cutoff(self):
        # a lone ~70 Hz-wide tone far above the real edge, deep in the noise
        # floor -- the same shape that fooled the old 3-consecutive-bin
        # detector into reporting the tone's frequency as the cutoff
        # (IMPROVEMENT_PLAN.md's "Verified weaknesses"). The new detector
        # must ignore it and flag it instead.
        freqs, profile = self._spectrum(44100, edge_hz=12000, floor_db=-80.0,
                                        tone=(15000, 70))
        cutoff_hz, _, tone = dsp._find_cutoff(freqs, profile, drop_db=30)
        self.assertAlmostEqual(cutoff_hz, 12000, delta=150)
        self.assertTrue(cutoff_hz < 14000)  # never drags up to the tone
        self.assertTrue(tone)

    def test_narrow_content_below_min_width_rejected(self):
        # a genuine-looking bump that's above threshold but only ~100 Hz
        # wide (below the 250 Hz minimum) must not read as real bandwidth.
        freqs, profile = self._spectrum(44100, edge_hz=8500)
        profile[(freqs >= 9000) & (freqs <= 9100)] = 0.0
        cutoff_hz, _, _ = dsp._find_cutoff(freqs, profile, drop_db=40)
        self.assertLess(cutoff_hz, 9000)

    def test_hz_widths_are_sample_rate_independent(self):
        # the same Hz-scale content at two very different bin resolutions
        # (44.1 kHz vs 88.2 kHz Nyquist) must produce the same qualitative
        # result -- guards against a fixed-bin-count regression, where the
        # same bin count would mean a different Hz width at each resolution.
        for sr, n in ((44100, 2049), (88200, 2049)):
            with self.subTest(sr=sr):
                freqs, profile = self._spectrum(sr, edge_hz=12000, n=n, floor_db=-80.0,
                                                tone=(15000, 70))
                cutoff_hz, _, tone = dsp._find_cutoff(freqs, profile, drop_db=30)
                self.assertAlmostEqual(cutoff_hz, 12000, delta=250)
                self.assertTrue(tone)


if __name__ == '__main__':
    unittest.main()
