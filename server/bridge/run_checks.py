#!/usr/bin/env python3
"""Run standalone checks in isolated processes with bounded runtime and JSON results."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
CHECKS = (
    'tests/test_v2_foundation.py',
    'tests/test_notification_policy.py',
    'tests/test_identity_v2.py',
    '../../edge/test_result_transport.py',
    '../../edge/test_bundle.py',
    '../../edge/test_code_provenance.py',
    'tests/test_watchdog.py',
    'tests/test_alerts.py',
    'tests/test_alert_stream.py',
    'tests/test_edge_events.py',
    'tests/test_edge_event_http.py',
    '../../edge/test_event_bridge_v2.py',
    '../../edge/test_any_detector_adapter.py',
    'tests/test_edge_config.py',
    'tests/test_event_store.py',
    'tests/smoke_test_edge_api.py',
    'tests/test_camera_registry.py',
    'tests/test_monitor_api.py',
    'tests/test_system_api.py',
    'tests/test_clip_recorder.py',
    'tests/test_hardware.py',
    'tests/test_device_profiles.py',
    'tests/test_device_commands.py',
    'tests/test_decision_logs.py',
    'tests/test_dashboard_stats.py',
    'tests/test_traffic.py',
    'tests/test_model_management.py',
    'tests/test_postgres.py',
    'tests/test_postgres_http.py',
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=60, help='seconds per check (default 60)')
    parser.add_argument('--report', type=Path, help='write machine-readable results')
    parser.add_argument('checks', nargs='*', help='check filenames; default: all checks')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if any(name not in CHECKS for name in args.checks):
        parser.error('unknown check; choose from: ' + ', '.join(CHECKS))
    results = []
    for name in args.checks or CHECKS:
        print(f'RUN {name}', flush=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, '-u', str(HERE / name)], cwd=HERE, capture_output=True, text=True, timeout=args.timeout
            )
            item = {
                'check': name,
                'status': 'passed' if proc.returncode == 0 else 'failed',
                'exit_code': proc.returncode,
                'stdout': proc.stdout,
                'stderr': proc.stderr,
            }
        except subprocess.TimeoutExpired as exc:

            def decode(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else value or ''

            item = {
                'check': name,
                'status': 'timeout',
                'exit_code': None,
                'stdout': decode(exc.stdout),
                'stderr': decode(exc.stderr),
            }
        item['duration_s'] = round(time.monotonic() - started, 3)
        results.append(item)
        print(item['stdout'], end='', flush=True)
        print(item['stderr'], end='', file=sys.stderr, flush=True)
        print(f"{item['status'].upper()} {name} ({item['duration_s']}s)", flush=True)
    report = {
        'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'python': sys.version,
        'executable': sys.executable,
        'timeout_s': args.timeout,
        'results': results,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    failures = sum(item['status'] != 'passed' for item in results)
    print(f'{len(results) - failures}/{len(results)} check scripts passed.', flush=True)
    return int(failures > 0)


if __name__ == '__main__':
    raise SystemExit(main())
