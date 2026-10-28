#!/usr/bin/env python3
"""The frozen decision and feature modules still run the code that was trained and evaluated.

edge/code_provenance.json links the byte hashes in the training and bundle records to the current files; this check
recomputes every hash and the formatting-independent code identity (code_identity.ast_sha256)."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from code_identity import ast_sha256, file_sha256  # noqa: E402

ROOT = HERE.parent


def main():
    record = json.loads((HERE / 'code_provenance.json').read_text())['modules']
    trained = json.loads((ROOT / 'training/tcn_v2_meta/frozen_operating_point_v2.json').read_text())['features']
    bundled = json.loads((HERE / 'bundle_manifest.json').read_text())['sha256']
    assert set(record) == {
        'causal_tracker.py',
        'decision_engine.py',
        'edge_runtime_v2_core.py',
        'features_v2.py',
        'head_point.py',
        'pose17.py',
        'shape_features_v2.py',
    }
    for name, entry in record.items():
        path = HERE / name
        assert file_sha256(path) == entry['current_sha256'], f'{name} changed: update code_provenance.json'
        assert ast_sha256(path) == entry['ast_sha256'], f'{name}: executed code differs from the record'
        assert bundled[name] == entry['frozen_sha256'], f'{name}: bundle record disagrees'
        if name in trained['code_sha256']:
            assert trained['code_sha256'][name] == entry['frozen_sha256'], f'{name}: training record disagrees'
        change = entry.get('description_change')
        if change:
            # The only edit to executed code is one description string, which no computation reads.
            restored = ast_sha256(path, replace={change['to']: change['from']})
            assert restored == entry['frozen_ast_sha256'], f'{name}: more than the documented description changed'
            assert path.read_text().count('identity_limit') == 1, f'{name}: the description is read somewhere'
        else:
            assert 'frozen_ast_sha256' not in entry
    print(f'PASS {len(record)} frozen modules: records agree and the executed code matches the trained version')


if __name__ == '__main__':
    main()
