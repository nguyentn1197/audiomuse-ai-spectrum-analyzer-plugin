#!/usr/bin/env bash
# Build the distributable plugin package.
# Usage: ./build.sh [version|local]
#   local            : replace dist-local/spectrum_analyzer.zip (fast dev
#                      iteration; dist-local/ has its own fixed catalog files)
#   with a version   : build dist/spectrum_analyzer-<version>.zip
#   without          : reuse the version of the newest spectrum_analyzer-*.zip
#                      already in dist/, falling back to plugin.json
# The zip contains every file under plugins/SpectrumAnalyzer/ except the
# exclusion list (plugin.json is metadata and per the spec never ships inside
# the zip). For versioned builds the matching plugin.json entry's sourceUrl
# and md5 checksum are updated. Old versioned zips are kept so previously
# published entries stay downloadable; published versions are immutable.
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-}" python3 - <<'EOF'
import glob
import hashlib
import json
import os
import re
import sys
import zipfile

PLUGIN_DIR = 'plugins/SpectrumAnalyzer'
PLUGIN_JSON = f'{PLUGIN_DIR}/plugin.json'
EXCLUDED = {'plugin.json'}  # keep in sync with .github/workflows/build.yml


def write_plugin_zip(out):
    """Zip every file under the plugin folder except EXCLUDED."""
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(PLUGIN_DIR):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for name in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, name), PLUGIN_DIR)
                if rel in EXCLUDED or name.endswith('.pyc'):
                    continue
                z.write(os.path.join(dirpath, name), rel)
        return z.namelist()


mode = os.environ.get('MODE') or ''

if mode == 'local':
    if not os.path.isdir('dist-local'):
        sys.exit('dist-local/ does not exist')
    names = write_plugin_zip('dist-local/spectrum_analyzer.zip')
    print('built dist-local/spectrum_analyzer.zip:', names)
    sys.exit(0)

version = mode
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
        with open(PLUGIN_JSON) as fh:
            version = json.load(fh)['versions'][0]['version']
        print(f'version from plugin.json: {version}')
if not re.fullmatch(r'\d+(\.\d+)*', version):
    sys.exit(f'invalid version: {version!r}')

out = f'dist/spectrum_analyzer-{version}.zip'
if os.path.exists(out) and os.environ.get('FORCE') != '1':
    sys.exit(
        f'{out} already exists. Published versions are immutable: installed '
        f'copies re-download them by checksum, so rebuilding breaks installs. '
        f'Bump the version (./build.sh <new-version>) or, if this version was '
        f'never published, rerun with FORCE=1.')

os.makedirs('dist', exist_ok=True)
names = write_plugin_zip(out)
with open(out, 'rb') as fh:
    checksum = hashlib.md5(fh.read()).hexdigest()
print(f'built {out} (md5 {checksum}): {names}')

with open(PLUGIN_JSON) as fh:
    desc = json.load(fh)
entry = next((v for v in desc['versions'] if v['version'] == version), None)
if entry is None:
    print(f'WARNING: {version} has no entry in plugin.json — '
          f'add one with a changelog before publishing')
else:
    new_url = re.sub(r'/dist/[^/]*$', f'/dist/spectrum_analyzer-{version}.zip',
                     entry['sourceUrl'])
    if new_url != entry.get('sourceUrl') or checksum != entry.get('checksum'):
        entry['sourceUrl'] = new_url
        entry['checksum'] = checksum
        with open(PLUGIN_JSON, 'w') as fh:
            json.dump(desc, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        print(f'plugin.json updated: sourceUrl -> {new_url}, checksum -> {checksum}')
EOF
