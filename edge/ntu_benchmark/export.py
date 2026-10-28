#!/usr/bin/env python3
"""Copy already generated, reviewable benchmark outputs to the requested Windows folder."""

import argparse, json, shutil, hashlib, time
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--results', type=Path)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    src = a.results or a.root / 'results'
    report = json.loads((src / 'summary.json').read_text())
    dest = a.out / ('pilot_validation' if report['partial'] else 'main_results')
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
            files.append(
                {'file': str((dest / f.name).relative_to(a.out)), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()}
            )
    for name in (
        'playlist.csv',
        'playlist.json',
        'selection_frozen.json',
        'production_audit.json',
        'pilot_gate.json',
        'code_hashes.json',
    ):
        if (a.root / name).is_file():
            shutil.copy2(a.root / name, dest / name)
    shutil.copy2(Path(__file__).with_name('README.md'), dest / 'REPRODUCTION.md')
    (a.out / 'LOCATION.json').write_text(
        json.dumps(
            {
                'experiment_root': str(a.root.resolve()),
                'results': str(dest),
                'partial': report['partial'],
                'exported_at': time.time(),
                'files': files,
            },
            indent=2,
        )
        + '\n'
    )
    print(json.dumps({'output': str(dest), 'partial': report['partial'], 'files': len(files)}))


if __name__ == '__main__':
    main()
