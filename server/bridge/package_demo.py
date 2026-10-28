#!/usr/bin/env python3
"""Build a local review bundle from the version-controlled files only."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Only files under version control: databases, credentials, captures, model weights and the NTU-derived head-point
    # table are never tracked (see .gitignore and NOTICE), so they cannot enter the bundle.
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    files = [ROOT / name for name in tracked.split('\0') if name]
    content = {}
    for path in sorted(set(files)):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != ROOT):
            raise ValueError(f'Symlink refused: {path}')
        name = path.relative_to(ROOT).as_posix()
        content[name] = path.read_bytes()
    manifest = {
        'purpose': 'local review bundle of the tracked sources (synthetic reproduction), not a release',
        'project_license': 'AGPL-3.0-only (see LICENSE and NOTICE)',
        'excludes': [
            'operational databases',
            'credentials and env files',
            'real captures',
            'model weights',
            'external Pi modules',
            'installed dependencies',
        ],
        'files_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in content.items()},
    }
    content['REVIEW_BUNDLE_MANIFEST.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(content.items()):
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, compress_type=zipfile.ZIP_DEFLATED)
    print(
        json.dumps(
            {
                'output': str(args.output.resolve()),
                'files': len(content),
                'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
