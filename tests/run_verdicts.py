"""Ad-hoc verdict runner: analyze arbitrary files with the real dsp pipeline.

Usage: python3 run_verdicts.py "path::suffix::kbps::EXPECTED::seg|deep" ...
For the regression suite use `python3 -m unittest discover tests` instead.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'plugins', 'SpectrumAnalyzer'))

import librosa_stub  # noqa: F401  (installs the librosa stand-in)
import dsp  # noqa: E402

CASES = sys.argv[1:]
for spec_line in CASES:
    path, suffix, bitrate, expect, deep = spec_line.split('::')
    r = dsp.analyze_file(path, suffix=suffix,
                         bitrate_kbps=int(bitrate) if bitrate else None,
                         deep=(deep == 'deep'))
    d = json.loads(r['details'])
    ok = r['verdict'] == expect
    print(f"{'PASS' if ok else 'FAIL':4s} {path.rsplit('/', 1)[-1]:32s} "
          f"expect={expect:16s} got={r['verdict']:16s} conf={r['confidence']:.2f} "
          f"cutoff={r['cutoff_hz'] / 1000:.1f}k edge={r['edge_db_khz']} "
          f"shelf={r['shelf_db']} bits={r['container_bits']}/{r['effective_bits']} "
          f"est='{r['est_source']}'")
    for n in d['notes']:
        print(f"     note: {n}")
    if d.get('deep'):
        print(f"     deep: edge_var={d['edge_var_hz']} Hz median={d['edge_median_hz']} Hz")
