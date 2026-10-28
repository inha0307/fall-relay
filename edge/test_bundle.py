#!/usr/bin/env python3
"""The edge bundle is self-contained: every module a bundled file imports is bundled, standard library, or an
installed package listed in requirements-edge.txt (plus the system Picamera2/libcamera)."""

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_bundle  # noqa: E402

PACKAGES = {
    'numpy',
    'cv2',
    'requests',
    'ncnn',
    'ai_edge_litert',
    'picamera2',
    'libcamera',
    'tensorflow',  # fallback interpreter when ai_edge_litert is absent (server-side replay)
    'psycopg',
    'psycopg_pool',
}  # server-only branches of db_backend, imported lazily


def main():
    files = {name: HERE / name for name in make_bundle.CODE + make_bundle.EVENT_CODE}
    files.update({name: make_bundle.SHARED / name for name in make_bundle.SHARED_CODE + make_bundle.EVENT_SHARED})
    bundled = {Path(name).stem for name in files if name.endswith('.py')}
    missing = {}
    for name, path in files.items():
        if not name.endswith('.py'):
            # Data files (the head-point table is NTU-derived and not distributed, see NOTICE) may be absent here.
            continue
        assert path.is_file(), f'{name} listed in make_bundle.py but missing at {path}'
        for node in ast.walk(ast.parse(path.read_text(), name)):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                else []
            )
            for module in modules:
                top = module.split('.')[0]
                if top not in bundled and top not in sys.stdlib_module_names and top not in PACKAGES:
                    missing.setdefault(name, set()).add(top)
    assert not missing, f'bundled files import modules that are not in the bundle: {missing}'
    requirements = (HERE / 'requirements-edge.txt').read_text()
    for package in ('ai-edge-litert', 'ncnn', 'numpy', 'opencv-python', 'requests'):
        assert f'{package}==' in requirements, f'{package} not pinned in requirements-edge.txt'
    # Import the bundled modules from a copy of the bundle alone (isolated interpreter, no repository on sys.path).
    with tempfile.TemporaryDirectory() as folder:
        for name, path in files.items():
            if path.is_file():
                (Path(folder) / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, Path(folder) / name)
        example = HERE / 'deploy' / 'edge_config.example.json'
        probe = (
            'import sys, json; sys.path.insert(0, sys.argv[1]); import edge_config, event_bridge_v2, result_transport, '
            'video_source, decision_log_runtime, edge_outbox; c = edge_config.load_edge_config(sys.argv[2]); '
            'box = edge_outbox.EdgeOutbox(sys.argv[1] + "/outbox.sqlite3"); print(c["camera_id"])'
        )
        run = subprocess.run(
            [sys.executable, '-I', '-c', probe, folder, str(example)], text=True, capture_output=True, cwd=folder
        )
        assert run.returncode == 0, run.stderr[-800:]
    runtime = (HERE / 'fall_relay_edge_runtime.py').read_text()
    assert '/home/' not in runtime, 'the runtime must not hard-code device paths'
    print(f'PASS edge bundle is self-contained ({len(files)} files, {len(bundled)} modules)')


if __name__ == '__main__':
    main()
