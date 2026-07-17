#!/usr/bin/env bash
# Build the distributable plugin package into dist/.
# Usage: ./build.sh [version]
#   with an argument : build dist/spectrum_analyzer-<version>.zip
#   without          : reuse the version of the newest spectrum_analyzer-*.zip
#                      already in dist/, falling back to plugin.json
# The sourceUrl of the matching version entry in plugin.json is updated to
# point at the built file. Old versioned zips are kept so previously
# published plugin.json entries stay downloadable.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-}" python3 - <<'EOF'
import glob
import json
import os
import re
import zipfile

version = os.environ.get('VERSION') or ''
if not version:
    found = []
    for p in glob.glob('dist/spectrum_analyzer-*.zip'):
        m = re.fullmatch(r'spectrum_analyzer-(\d+(?:\.\d+)*)\.zip', os.path.basename(p))
        if m:
            found.append(m.group(1))
    if found:
        version = max(found, key=lambda v: tuple(map(int, v.split('.'))))
        print(f'version from dist/: {version}')
    else:
        with open('plugin.json') as fh:
            version = json.load(fh)['versions'][0]['version']
        print(f'version from plugin.json: {version}')
if not re.fullmatch(r'\d+(\.\d+)*', version):
    raise SystemExit(f'invalid version: {version!r}')

os.makedirs('dist', exist_ok=True)
out = f'dist/spectrum_analyzer-{version}.zip'
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for f in ('__init__.py', 'jobs.py', 'dsp.py'):
        z.write(f'plugins/SpectrumAnalyzer/{f}', f)
print(f'built {out}')

with open('plugin.json') as fh:
    desc = json.load(fh)
entry = next((v for v in desc['versions'] if v['version'] == version), None)
if entry is None:
    print(f'WARNING: {version} has no entry in plugin.json — '
          f'add one with a changelog before publishing')
else:
    new_url = re.sub(r'/dist/[^/]*$', f'/dist/spectrum_analyzer-{version}.zip',
                     entry['sourceUrl'])
    if new_url != entry['sourceUrl']:
        entry['sourceUrl'] = new_url
        with open('plugin.json', 'w') as fh:
            json.dump(desc, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        print(f'plugin.json sourceUrl -> {new_url}')
EOF
