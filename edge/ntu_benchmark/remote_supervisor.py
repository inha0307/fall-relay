#!/usr/bin/env python3
"""An attached, leased supervisor restores exactly the prior inference service state."""

import argparse, json, os, selectors, signal, subprocess, sys, time
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--job', required=True)
    a = p.parse_args()
    job = json.loads(Path(a.job).read_text())
    out = Path(job['output'])
    out.mkdir(parents=True, exist_ok=True)
    service = ['systemctl', '--user']
    name = job['service']
    was_active = subprocess.run(service + ['is-active', '--quiet', name]).returncode == 0
    (out / 'service_before.json').write_text(json.dumps({'active': was_active, 'service': name, 'at': time.time()}))
    child = None
    handle = None
    aborted = False

    def emit(**x):
        print(json.dumps(x), flush=True)

    def interrupt(*_):
        raise KeyboardInterrupt('supervisor interrupted')

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    try:
        if was_active:
            subprocess.run(service + ['stop', name], check=True, timeout=30)
        handle = (out / 'runtime.log').open('w')
        child = subprocess.Popen(
            [job['python'], str(Path(job['bundle']) / 'benchmark_device.py'), '--job', a.job],
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
        sel = selectors.DefaultSelector()
        sel.register(sys.stdin, selectors.EVENT_READ)
        last = time.monotonic()
        ready = False
        reported = 0
        while child.poll() is None:
            for key, _ in sel.select(timeout=0.5):
                line = sys.stdin.readline()
                if not line:
                    raise InterruptedError('controller disconnected')
                message = json.loads(line)
                last = time.monotonic()
                if message['action'] == 'abort':
                    raise InterruptedError('controller requested abort')
                if message['action'] == 'release':
                    tmp = Path(job['release_file'] + '.tmp')
                    tmp.write_text(json.dumps({'start_at': message['start_at']}))
                    os.replace(tmp, job['release_file'])
            if time.monotonic() - last > 90:
                raise InterruptedError('controller lease expired')
            if not ready and (out / 'ready.json').exists():
                emit(kind='ready', device=job['device'], run_id=job['run_id'])
                ready = True
            if time.monotonic() - reported > 30:
                reported = time.monotonic()
                latest = None
                if (out / 'videos.jsonl').exists():
                    with (out / 'videos.jsonl').open('rb') as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 10000))
                        lines = f.read().splitlines()
                        latest = json.loads(lines[-1]) if lines else None
                emit(kind='progress', device=job['device'], run_id=job['run_id'], wall=time.time(), latest_video=latest)
        if child.returncode:
            raise RuntimeError(
                'runtime exited ' + str(child.returncode) + ': ' + (out / 'runtime.log').read_text()[-2400:]
            )
        emit(kind='runtime_complete', device=job['device'])
    except BaseException as exc:
        aborted = True
        emit(kind='error', device=job['device'], error=str(exc))
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        if handle:
            handle.close()
        if was_active:
            subprocess.run(service + ['start', name], check=True, timeout=30)
        restored = subprocess.run(service + ['is-active', '--quiet', name]).returncode == 0
        proof = {
            'was_active': was_active,
            'active_after': restored,
            'restored': restored == was_active,
            'aborted': aborted,
            'at': time.time(),
        }
        (out / 'service_after.json').write_text(json.dumps(proof))
        emit(kind='service_restored', device=job['device'], **proof)
        if not proof['restored']:
            raise RuntimeError('service state restoration failed')
    if aborted:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
