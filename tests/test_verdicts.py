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
        # tier-2 soundfile subtype resolves the codec for free -- 'delivery'
        # is no longer None, but the resolved codec itself is a true no-op
        self.assertEqual(json.loads(r['details'])['delivery'], {'codec': 'mp3'})

    def test_transcode_gate_mp3_reduced_confidence(self):
        # the shipped, fixture-validated MP3 gate must still fire -- but at
        # reduced confidence, with a note naming the low-passed-first-gen
        # alternative explanation (codec-aware transcode gating)
        r = _analyze('transcoded_128as320.mp3', 'mp3', 320)
        self.assertEqual(r['verdict'], 'TRANSCODED_LOSSY')
        d = json.loads(r['details'])
        self.assertEqual(d['delivery']['codec'], 'mp3')
        self.assertTrue(any('low-passed first-generation' in n for n in d['notes']),
                        d['notes'])
        # confidence must be strictly below what the pre-penalty formula gives
        full_threshold = min(0.93 * (r['sample_rate'] / 2.0), 20500.0)
        margin = max(0.0, min(1.0, (full_threshold - r['cutoff_hz']) / full_threshold))
        pre_penalty_conf = min(0.9, 0.55 + 0.5 * margin)
        self.assertLess(r['confidence'], pre_penalty_conf)


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


class TestCodecGating(unittest.TestCase):
    """Codec-aware transcode gating: the TRANSCODED_LOSSY-changing effect of
    _expected_cutoff_for_bitrate is calibrated on MP3 only. Direct synthetic
    calls -- no real AAC/Opus fixtures are available (would need ffmpeg to
    generate, deferred to "Minimum adversarial fixtures")."""

    # a cutoff/bitrate combo that trips the ~320 kbps expected-cutoff gate
    # (19500 - 1500 = 18000 threshold) for any lossy suffix
    _GATED_KWARGS = dict(sr=44100, bitrate_kbps=320, cutoff_hz=15500,
                         edge_db_khz=10.0, shelf_db=-40.0, is_lossless=False)

    def test_uncalibrated_codec_does_not_flip_verdict(self):
        verdict, est, conf, notes, alias = dsp._verdict(
            suffix='m4a', codec='aac', **self._GATED_KWARGS)
        self.assertEqual(verdict, 'CONSISTENT_LOSSY')
        self.assertTrue(any('no calibrated' in n for n in notes), notes)

    def test_unknown_codec_gate_disabled(self):
        verdict, est, conf, notes, alias = dsp._verdict(
            suffix='m4a', codec=None, **self._GATED_KWARGS)
        self.assertEqual(verdict, 'CONSISTENT_LOSSY')
        self.assertTrue(any('no calibrated' in n for n in notes), notes)

    def test_mp3_codec_flips_verdict(self):
        verdict, est, conf, notes, alias = dsp._verdict(
            suffix='mp3', codec='mp3', **self._GATED_KWARGS)
        self.assertEqual(verdict, 'TRANSCODED_LOSSY')
        self.assertTrue(any('low-passed first-generation' in n for n in notes), notes)

    def test_resolve_lossy_codec_mp3_suffix_fallback(self):
        self.assertEqual(dsp._resolve_lossy_codec('mp3', None), 'mp3')
        self.assertIsNone(dsp._resolve_lossy_codec('aac', None))
        self.assertEqual(dsp._resolve_lossy_codec('m4a', 'alac'), 'alac')

    def test_probe_ffprobe_missing_binary(self):
        # ffprobe is genuinely absent from PATH in this sandbox -- exercises
        # the degrade path for real, not just via a mock
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.m4a') as f:
            f.write(b'not actually audio')
            f.flush()
            info = dsp._probe_ffprobe(f.name, timeout=2.0)
        self.assertEqual(info, {'codec': None, 'sample_rate': None, 'duration': None})

    def test_probe_ffprobe_parses_success(self):
        import subprocess
        import unittest.mock as mock

        payload = json.dumps({
            'streams': [{'codec_name': 'alac', 'sample_rate': '48000'}],
            'format': {'duration': '183.42'},
        }).encode()
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload)
        with mock.patch('subprocess.run', return_value=fake):
            info = dsp._probe_ffprobe('irrelevant.m4a')
        self.assertEqual(info, {'codec': 'alac', 'sample_rate': 48000, 'duration': 183.42})


class TestDistributedSampling(unittest.TestCase):
    """Distributed segment sampling: several windows spread through the
    track instead of one fixed-offset segment."""

    def test_window_offsets_long_file_full_spread(self):
        # a real-length track: positions should stay distinct
        offsets = dsp._window_offsets(240.0, dsp._FFMPEG_WINDOW_POSITIONS, 30.0)
        self.assertEqual(len(offsets), 3)
        self.assertEqual(offsets, sorted(offsets))
        for off in offsets:
            self.assertGreaterEqual(off, 0.0)
            self.assertLessEqual(off + 30.0, 240.0 + 1e-6)

    def test_window_offsets_short_file_collapses(self):
        # duration barely above the window size: positions collapse onto
        # nearly the same audio and must be deduplicated, never producing an
        # offset that runs past the end of the file
        offsets = dsp._window_offsets(12.0, dsp._NATIVE_WINDOW_POSITIONS, 10.0)
        self.assertGreaterEqual(len(offsets), 1)
        for off in offsets:
            self.assertGreaterEqual(off, 0.0)
            self.assertLessEqual(off, 2.0 + 1e-6)

    def test_is_near_silent(self):
        freqs = np.linspace(0, 22050, 2049)
        loud = np.full_like(freqs, -20.0)
        silent = np.full_like(freqs, -95.0)
        self.assertFalse(dsp._is_near_silent(loud, freqs)[0])
        self.assertTrue(dsp._is_near_silent(silent, freqs)[0])

    def test_read_window_ffmpeg_missing_binary(self):
        # ffmpeg genuinely absent from PATH here -- real degrade path
        self.assertIsNone(dsp._read_window_ffmpeg('irrelevant.m4a', 10.0, 15.0, 44100, timeout=2.0))

    def test_read_window_ffmpeg_parses_success(self):
        import subprocess
        import unittest.mock as mock

        raw = np.linspace(-1.0, 1.0, 4410, dtype='<f4').tobytes()
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=raw)
        with mock.patch('subprocess.run', return_value=fake):
            y = dsp._read_window_ffmpeg('irrelevant.m4a', 10.0, 0.1, 44100)
        self.assertIsNotNone(y)
        self.assertEqual(y.size, 4410)

    def test_load_windows_falls_back_when_ffmpeg_absent(self):
        # duration known (simulated), but ffmpeg itself is absent -- every
        # window decode fails, so _load_windows must fall back to the single
        # proven _load_segment path rather than returning nothing
        path = os.path.join(FIXTURES, 'consistent_320.mp3')
        windows = dsp._load_windows(
            path, segment_seconds=90, native_seekable=False,
            ffprobe_info={'codec': 'aac', 'sample_rate': 44100, 'duration': 45.0})
        self.assertEqual(len(windows), 1)

    def test_native_fixture_uses_multiple_windows(self):
        # the committed 45s FLAC/MP3 fixtures are soundfile-seekable -- the
        # 5-window native tier must actually engage, not silently collapse
        # to a single window
        for name, suffix, kbps in (('genuine_cd_1644.flac', 'flac', 900),
                                   ('consistent_320.mp3', 'mp3', 320)):
            with self.subTest(fixture=name):
                r = _analyze(name, suffix, kbps)
                d = json.loads(r['details'])
                self.assertGreater(len(d['windows']['samples']), 1)
                for w in d['windows']['samples']:
                    self.assertIn('offset', w)
                    self.assertIn('seconds', w)
                    self.assertIn('cutoff_hz', w)
                    self.assertIn('silent', w)
                self.assertIn(d['windows']['agree'], (True, False))


class TestStructuredEvidence(unittest.TestCase):
    """Structured evidence flags: every fact gets one namespaced home in
    `details`, and the newly-added flags are nullable (None = not
    evaluated, True/False = tested). Verdict logic itself is untouched --
    these tests check only the reported shape and nullability."""

    def test_edge_sharpness_none_near_nyquist(self):
        # cutoff within 500 Hz of Nyquist -- structurally not enough band
        # above the cutoff to measure a slope; must report None, not a
        # misleading 0.0 that would read as "measured and gradual"
        freqs = np.linspace(0, 22050, 2049)
        profile = np.where(freqs <= 21900, 0.0, -60.0)
        self.assertIsNone(dsp._edge_sharpness(freqs, profile, 21900))

    def test_edge_sharpness_measures_when_band_available(self):
        freqs = np.linspace(0, 22050, 2049)
        profile = np.where(freqs <= 15000, 0.0, -60.0)
        edge = dsp._edge_sharpness(freqs, profile, 15000)
        self.assertIsNotNone(edge)
        self.assertGreater(edge, 25.0)  # near-vertical wall

    def test_verdict_alias_none_outside_upsampled_branch(self):
        # a plain 44.1 kHz lossy case never reaches the alias-correlation
        # branch (that's gated behind nyquist > 24000) -- must report None
        # ("not evaluated"), not a False that implies absence was tested
        verdict, est, conf, notes, alias = dsp._verdict(
            sr=44100, suffix='mp3', bitrate_kbps=320, cutoff_hz=19000,
            edge_db_khz=5.0, shelf_db=-40.0, is_lossless=False)
        self.assertEqual(verdict, 'CONSISTENT_LOSSY')
        self.assertIsNone(alias)

    def test_verdict_alias_evaluated_in_upsampled_branch(self):
        # a real upsampled fixture reaches the alias-correlation branch
        # (hi-res container, cutoff aligned with a lower standard Nyquist)
        # -- alias_image_detected must come back as a real True/False, not None
        r = _analyze('fake_hires_44to96.flac', 'flac', 1400)
        self.assertEqual(r['verdict'], 'UPSAMPLED')
        d = json.loads(r['details'])
        self.assertIn(d['evidence']['alias_image_detected'], (True, False))

    def test_details_shape_end_to_end(self):
        r = _analyze('genuine_cd_1644.flac', 'flac', 900)
        d = json.loads(r['details'])

        self.assertEqual(set(d['evidence']), {
            'edge_machine_like', 'shelf_digitally_silent',
            'alias_image_detected', 'narrow_high_frequency_tone_present',
        })
        for key in ('edge_machine_like', 'shelf_digitally_silent',
                    'narrow_high_frequency_tone_present'):
            self.assertIn(d['evidence'][key], (True, False, None))
        # 44.1 kHz file never reaches the alias branch -- must be None
        self.assertIsNone(d['evidence']['alias_image_detected'])

        self.assertEqual(d['bit_depth'], {
            'container_bits': r['container_bits'],
            'effective_bits': r['effective_bits'],
            # genuine_cd_1644.flac is a 16-bit container -- nothing to fake,
            # so the histogram is never computed (not evaluated == None)
            'predominant_bit_depth': None,
            'lower_bit_activity_fraction': None,
            'coverage': None,
        })

        self.assertEqual(set(d['windows']), {'samples', 'agree'})

        self.assertEqual(d['analysis_rev'], dsp.analysis_rev(40, 90))

        # moved, not duplicated -- no leftover flat keys at the top level
        for stale_key in ('narrow_high_frequency_tone_present', 'windows_agree',
                          'container_bits', 'effective_bits'):
            self.assertNotIn(stale_key, d)

    def test_error_paths_carry_analysis_rev(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.flac') as f:
            f.write(b'not actually audio' * 100)
            f.flush()
            r = dsp.analyze_file(f.name, suffix='flac', bitrate_kbps=900)
        d = json.loads(r['details'])
        self.assertEqual(d['analysis_rev'], dsp.analysis_rev(40, 90))


class TestBitDepthHistogram(unittest.TestCase):
    """Bit-depth histogram alongside -- not instead of -- the exact minimum.
    The exact trailing-zero test (dsp.py's original behavior) stays the
    only UPSCALED trigger; the histogram adds informational
    predominant_bit_depth/lower_bit_activity_fraction fields and lets the
    exact test's confidence reflect how much of the file was actually
    checked (sampled window vs. deep mode's full-file scan)."""

    def test_window_stats_odd_multiplier_gives_consistent_bitpos(self):
        # 1000 samples padded to 16-bit-equivalent (low 16 bits zero: an
        # odd multiplier of 2^16 keeps bit 16 as the lowest set bit
        # regardless of the multiplier's own factors) plus 5 samples with
        # genuine content down at bit 7 (odd multiplier of 2^7)
        padded = ((2 * np.arange(1, 1001) + 1) * 65536).astype(np.int64)
        deep = ((2 * np.arange(1, 6) + 1) * 128).astype(np.int64)
        x = np.concatenate([padded, deep]).astype(np.int32).reshape(-1, 1)

        min_bitpos, hist = dsp._bit_depth_window_stats(x, 24)
        total = int(hist.sum())
        self.assertEqual(total, 1005)
        self.assertEqual(int(np.argmax(hist)), 16)  # predominant depth
        self.assertAlmostEqual(float(hist[17:].sum()) / total, 5 / 1005, places=4)
        self.assertEqual(min(24, int(32 - min_bitpos)), 24)  # NOT exact padding

    def test_window_stats_all_silent_chunk(self):
        x = np.zeros((100, 1), dtype=np.int32)
        min_bitpos, hist = dsp._bit_depth_window_stats(x, 24)
        self.assertIsNone(min_bitpos)
        self.assertEqual(int(hist.sum()), 0)

    def test_sampled_vs_full_confidence_and_coverage(self):
        # fake_24bit_96k.flac is a straightforward 16-bit-source-in-24-bit
        # container conversion (no dither), so the padding is consistent
        # across the whole file -- exact in both a sampled window and a
        # full-file scan, but confidence should reflect the coverage
        r_seg = _analyze('fake_24bit_96k.flac', 'flac', 1000, deep=False)
        self.assertEqual(r_seg['verdict'], 'UPSCALED')
        self.assertAlmostEqual(r_seg['confidence'], 0.9)
        d_seg = json.loads(r_seg['details'])
        self.assertEqual(d_seg['bit_depth']['coverage'], 'sampled')

        r_deep = _analyze('fake_24bit_96k.flac', 'flac', 1000, deep=True)
        self.assertEqual(r_deep['verdict'], 'UPSCALED')
        self.assertAlmostEqual(r_deep['confidence'], 0.95)
        d_deep = json.loads(r_deep['details'])
        self.assertEqual(d_deep['bit_depth']['coverage'], 'full')

    def test_genuine_file_reports_no_padding_evidence(self):
        r = _analyze('genuine_dsd_2496.flac', 'flac', 2800)
        d = json.loads(r['details'])
        self.assertEqual(d['bit_depth']['container_bits'], 24)
        self.assertEqual(d['bit_depth']['effective_bits'], 24)
        self.assertEqual(d['bit_depth']['coverage'], 'sampled')
        # not exact-padded, and the modal depth is above 16 -- no
        # statistical note either, both fields describe real content
        self.assertIsNotNone(d['bit_depth']['predominant_bit_depth'])
        self.assertGreater(d['bit_depth']['predominant_bit_depth'], 16)


if __name__ == '__main__':
    unittest.main()
