#!/usr/bin/env python3
"""Build a clean frontend copy and check it against an active isolated demo.py bridge."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo-output', type=Path, required=True)
    parser.add_argument(
        '--output', type=Path, required=True, help='new directory for frontend copy, logs and account DB'
    )
    parser.add_argument('--serve', action='store_true', help='keep the verified demo web server open until Ctrl+C')
    args = parser.parse_args()
    demo = args.demo_output.resolve()
    report = json.loads((demo / 'report.json').read_text())
    if report.get('synthetic_only') is not True or report.get('real_model_inference') is not False:
        raise ValueError('Expected demo.py synthetic report')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Parse as data; never execute the supplied env file as shell code.
    settings = dict(line.split('=', 1) for line in (demo / 'demo.env').read_text().splitlines() if line)
    env = {**os.environ, **settings, 'NEXT_TELEMETRY_DISABLED': '1'}
    env.pop(
        'CHECK_BRIDGE_URL', None
    )  # older check expects an unauthenticated test camera; our demo authenticates cameras
    frontend = output / 'frontend'
    source = Path(__file__).resolve().parents[1] / 'frontend'
    shutil.copytree(source, frontend, ignore=shutil.ignore_patterns('node_modules', '.next*', '.env*', '.git'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', int(settings['FALLRELAY_DEMO_WEB_PORT'])))
        port = sock.getsockname()[1]
    env['CHECK_BASE_URL'] = f'http://127.0.0.1:{port}'

    def run(command, name, timeout=600):
        print(f'RUN {name}', flush=True)
        with (output / f'{name}.log').open('w') as log:
            result = subprocess.run(
                command, cwd=frontend, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout
            )
        if result.returncode:
            raise RuntimeError(f'{name} failed; inspect {output / (name + ".log")}')

    run(['npm', 'ci', '--no-audit', '--no-fund'], 'npm-ci')
    run(['npm', 'run', 'build', '--', '--webpack'], 'build')
    run(['node', 'scripts/check-account-store.mjs'], 'account-store', 60)
    run(['node', 'scripts/check-alert-policy.mjs'], 'alert-policy', 30)
    run(['node', 'scripts/check-app-api.mjs', '--seed'], 'seed', 30)
    with (output / 'web.log').open('w') as log:
        process = subprocess.Popen(
            ['npm', 'start', '--', '--hostname', '127.0.0.1', '--port', str(port)],
            cwd=frontend,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError('web server exited; inspect web.log')
                try:
                    with urllib.request.urlopen(env['CHECK_BASE_URL'] + '/api/auth/me', timeout=2):
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code == 401:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError('web startup timeout')
                time.sleep(0.2)
            run(['node', 'scripts/check-demo-api.mjs'], 'demo-web-check', 60)
            run(['node', 'scripts/check-app-api.mjs'], 'account-check', 60)
            run(['node', 'scripts/check-responder-api.mjs'], 'responder-check', 60)
            result = {
                'clean_npm_ci': True,
                'build': 'passed',
                'web_demo': 'passed',
                'account_checks': 'passed',
                'node': subprocess.check_output(['node', '--version'], text=True).strip(),
                'base_url': env['CHECK_BASE_URL'],
                'synthetic_only': True,
                'account_store': 'passed',
                'alert_policy': 'passed',
                'responder_permissions': 'passed',
            }
            (output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2), flush=True)
            if args.serve:
                print(
                    'Demo login: tadmin / admin-pass-123 (isolated synthetic environment only). Ctrl+C to stop.',
                    flush=True,
                )
                process.wait()
        except KeyboardInterrupt:
            pass
        finally:
            import signal

            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == '__main__':
    main()
