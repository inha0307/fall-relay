#!/usr/bin/env python3
"""Check D3: power loss on the device while its outbox is filling (v1.1.0, Raspberry Pi 5, adapter through the gate).

Per repeat: the link is dead from the start (gate cut); the any-detector adapter runs lying_still.csv so that an open
and one update every 2 s accumulate in its outbox; about 60 s in, the operator is told to pull the device's power.
This host probes the device's SSH port every 0.2 s, so the last answer dates the power loss. After the device is
back, the outbox database is copied untouched and integrity-checked, chrony and the wall clock are recorded, and the
adapter is started again with the same outbox on a short idle sequence and the link restored: it closes the previous
run's event as runtime_stop and resends every unacknowledged message to the isolated bridge (production bridge,
own PostgreSQL schema), which classifies the alert without an age header (the boot id changed).
Evidence: how many of the last committed messages were lost, whether the database stayed usable, and what the
bridge did with the resent messages. Uses the helpers of docs/evidence/late_alert_policy/d2/d2_controller.py (device and SSH key from
edge/ntu_benchmark/setup.json); `--analyze` summarises a finished root.
"""

import argparse
import csv
import json
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
D2DIR = HERE.parent / 'late_alert_policy' / 'd2'  # shared helpers, score sequences and the lab setup lookup
sys.path.insert(0, str(D2DIR))
import d2_controller as d2  # noqa: E402

PULL_AFTER_S = 60.0
IDLE = 'idle.csv'


def write_idle():
    path = D2DIR / IDLE
    with path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['t_s', 'score', 'eligible', 'upright', 'lying'])
        for i in range(100):
            w.writerow([f'{i / 10:.1f}', '0.020', 1, 1, 0])


class Prober(threading.Thread):
    """Liveness probes of the device every 0.2 s: a TCP connect to a closed port (9). A refusal proves the host is
    up; a timeout means it is down. Never the SSH port: OpenSSH >= 9.8 penalises a source that opens connections
    without authenticating (PerSourcePenalties) and then refuses it for up to 10 min, which is what broke the
    earlier runs. Keeps the last success and the first success after a gap."""

    def __init__(self, host, port=9, interval=0.2):
        super().__init__(daemon=True)
        self.host, self.port, self.interval = host, port, interval
        self.last_ok = None
        self.first_ok_after_gap = None
        self.gap_started = None
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            t = time.time()
            try:
                with socket.create_connection((self.host, self.port), timeout=0.3):
                    pass
                alive = True
            except ConnectionRefusedError:
                alive = True  # the host answered (RST): it is up
            except OSError:
                alive = False  # timeout or unreachable
            if alive:
                if self.gap_started is not None and self.first_ok_after_gap is None:
                    self.first_ok_after_gap = t
                self.last_ok = t
            elif self.last_ok is not None and self.gap_started is None:
                self.gap_started = t
            self.stop.wait(self.interval)


def adapter_command(remote_out, scores, server_port):
    return (
        'mkdir -p %(out)s && IFS= read -r FALLRELAY_CAMERA_TOKEN && export FALLRELAY_CAMERA_TOKEN && '
        'python3 -c "import json,time;print(json.dumps({\'adapter_start_wall\':time.time()}))" > %(out)s/start_%(tag)s.json && '
        'exec %(py)s %(adapter)s --server http://127.0.0.1:%(port)d --camera %(cam)s --input %(csv)s '
        '--outbox %(out)s/outbox.sqlite3 --realtime --drain-timeout 150 > %(out)s/adapter_%(tag)s.json 2> %(out)s/adapter_%(tag)s.err'
    ) % {
        'out': shlex.quote(remote_out),
        'tag': Path(scores).stem,
        'py': d2.DEVICE['python'],
        'adapter': shlex.quote(d2.DEVICE['stage'] + '/bundle/examples/any_detector_adapter.py'),
        'port': server_port,
        'cam': d2.DEVICE['camera'],
        'csv': shlex.quote(d2.DEVICE['stage'] + '/scores/' + scores),
    }


def ssh_adapter(remote, bridge_port, token, err_path):
    cmd = [
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=3',
        '-o', 'ExitOnForwardFailure=yes', '-i', d2.KEY, '-R', f"127.0.0.1:{d2.DEVICE['tunnel_port']}:127.0.0.1:{bridge_port}",
        d2.DEVICE['target'], remote,
    ]
    err = open(err_path, 'w')
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
    process.stdin.write(token + '\n')
    process.stdin.flush()
    return process, err


def wait_device_settled(note, min_uptime_s=90.0, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            uptime = float(d2.ssh_text('cat /proc/uptime').split()[0])
        except Exception:
            time.sleep(5)
            continue
        if uptime >= min_uptime_s:
            note(kind='device_settled', uptime_s=uptime)
            return
        time.sleep(min(10, min_uptime_s - uptime + 1))
    raise RuntimeError('device did not settle')


def wait_ssh(timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d2.ssh('true', stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            return time.time()
        except Exception:
            time.sleep(2)
    raise RuntimeError('device did not come back')


POST_BOOT = r'''
import json, os, shutil, sqlite3, subprocess, sys, time
out = sys.argv[1]
db = os.path.join(out, 'outbox.sqlite3')
copy = os.path.join(out, 'outbox_after_poweroff.sqlite3')
report = {'device_wall': time.time(), 'files': sorted(os.listdir(out))}
for suffix in ('', '-wal', '-shm'):
    if os.path.exists(db + suffix):
        shutil.copy2(db + suffix, copy + suffix)
        report['size' + (suffix or '-db')] = os.path.getsize(db + suffix)
con = sqlite3.connect('file:' + copy + '?mode=ro', uri=True)
report['integrity_check'] = [r[0] for r in con.execute('PRAGMA integrity_check')]
report['rows'] = [dict(zip(('event_uid', 'message_seq', 'kind', 'created_at', 'acked_at', 'attempts'), r)) for r in con.execute('SELECT event_uid,message_seq,kind,created_at,acked_at,attempts FROM outbox ORDER BY message_seq')]
report['origin'] = [dict(zip(('event_uid', 'boot_id', 'opened_boot_s'), r)) for r in con.execute('SELECT event_uid,boot_id,opened_boot_s FROM edge_event_origin')]
con.close()
u = open('/proc/uptime').read().split()
report['uptime_s'] = float(u[0]); report['booted_at'] = time.time() - float(u[0])
report['boot_id'] = open('/proc/sys/kernel/random/boot_id').read().strip()
try:
    report['chrony'] = subprocess.run(['chronyc', 'tracking'], capture_output=True, text=True).stdout
except OSError as exc:
    report['chrony'] = 'unavailable: ' + str(exc)
print(json.dumps(report))
'''


def one_repeat(root, case, bridge_port, token, prober):
    out = root / case
    out.mkdir()
    remote_out = d2.DEVICE['stage'] + '/runs/' + case
    log = []

    def note(**x):
        x = {'wall': time.time(), **x}
        log.append(x)
        with (out / 'timeline.jsonl').open('a') as f:
            f.write(json.dumps(x) + '\n')

    # Phase 1: link dead, adapter fills its outbox, operator pulls the power. After a reboot the device's network
    # flaps for up to a minute, so wait until it has been up for a while and answers.
    wait_device_settled(note)
    d2.gate_start()
    d2.gate_cut(True)
    note(kind='gate_cut')
    host_before = time.time()
    device_wall = float(d2.ssh_text('date +%s.%N'))
    note(kind='clock_before', device_minus_host_s=device_wall - (host_before + time.time()) / 2)
    process, err = ssh_adapter(adapter_command(remote_out, 'lying_still.csv', d2.DEVICE['gate_port']), bridge_port, token, out / 'ssh_phase1.log')
    started = time.time()
    prober.gap_started = prober.first_ok_after_gap = None
    note(kind='adapter_started', host_started_at=started)
    cued = False
    while process.poll() is None:
        if not cued and time.time() - started >= PULL_AFTER_S:
            cued = True
            note(kind='power_cue')
            print(f'\a\n>>> {case}: PULL THE POWER OF THE DEVICE NOW (outbox has been filling for {PULL_AFTER_S:.0f} s) <<<\n', flush=True)
        if time.time() - started > 600:
            raise RuntimeError(case + ': the device was not powered off within 10 min')
        time.sleep(0.2)
    err.close()
    time.sleep(1.0)
    power_off_at = prober.last_ok
    note(kind='ssh_session_ended', returncode=process.returncode, power_off_at=power_off_at)
    print(case, 'SSH session ended; last answer from the device at +%.1f s' % (power_off_at - started), flush=True)
    # Wait for the device to come back: a success recorded after the SSH session died.
    prober.gap_started = time.time()
    prober.first_ok_after_gap = None
    while prober.first_ok_after_gap is None:
        if time.time() - power_off_at > 900:
            raise RuntimeError(case + ': device did not answer within 15 min of the power loss')
        time.sleep(0.5)
    note(kind='port_back', first_ok_after_gap=prober.first_ok_after_gap)
    ssh_ready = wait_ssh()
    host_after = time.time()
    device_wall = float(d2.ssh_text('date +%s.%N'))
    note(kind='ssh_ready', ssh_ready_at=ssh_ready, device_minus_host_s=device_wall - (host_after + time.time()) / 2)
    post = json.loads(d2.ssh_text('python3 -c ' + shlex.quote(POST_BOOT) + ' ' + shlex.quote(remote_out)))
    (out / 'post_boot.json').write_text(json.dumps(post, indent=2) + '\n')
    note(kind='post_boot', integrity=post['integrity_check'], rows=len(post['rows']), uptime_s=post['uptime_s'])
    print(case, 'back: integrity', post['integrity_check'], 'rows in outbox', len(post['rows']), 'uptime %.0f s' % post['uptime_s'], flush=True)
    resend_and_fetch(root, case, bridge_port, token, note)


def resend_and_fetch(root, case, bridge_port, token, note):
    """Phase 2: same outbox, link restored (no gate), short idle input: the adapter resends what is pending and
    closes the old event. The SSH transport is retried: the device's network flaps for up to a minute after a reboot."""
    out = root / case
    remote_out = d2.DEVICE['stage'] + '/runs/' + case
    d2.gate_stop()
    marker = out / f'adapter_{Path(IDLE).stem}.json'
    for attempt in range(8):
        process, err = ssh_adapter(adapter_command(remote_out, IDLE, d2.DEVICE['tunnel_port']), bridge_port, token, out / 'ssh_phase2.log')
        note(kind='resend_started', attempt=attempt + 1)
        deadline = time.time() + 400
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.5)
        if process.poll() is None:
            process.terminate()
        err.close()
        note(kind='resend_ended', returncode=process.returncode, attempt=attempt + 1)
        if process.returncode != 255:
            break
        time.sleep(6)
    tar = out / 'device.tar'
    for attempt in range(6):
        try:
            with tar.open('wb') as f:
                d2.ssh('tar -C ' + shlex.quote(remote_out) + ' -cf - .', stdout=f, stderr=subprocess.PIPE, timeout=120)
            break
        except subprocess.CalledProcessError:
            if attempt == 5:
                raise
            note(kind='fetch_retry', attempt=attempt + 1)
            time.sleep(5)
    import tarfile

    with tarfile.open(tar) as t:
        for m in t:
            if not (out / m.name).resolve().is_relative_to(out.resolve()):
                raise ValueError('unsafe result archive')
            t.extract(m, out)
    tar.unlink()
    for attempt in range(3):
        try:
            (out / 'gate.jsonl').write_text(d2.ssh_text('cat ' + shlex.quote(d2.DEVICE['stage'] + '/gate/gate.jsonl')))
            break
        except Exception as exc:
            note(kind='gate_log_failed', error=str(exc))
            time.sleep(5)
    if not marker.exists():
        raise RuntimeError(case + ': the resend never ran (SSH kept failing); rerun with --resume')
    print(case, 'done:', marker.read_text().strip()[:300], flush=True)


def timeline_writer(out):
    def note(**x):
        x = {'wall': time.time(), **x}
        with (out / 'timeline.jsonl').open('a') as f:
            f.write(json.dumps(x) + '\n')
    return note


def analyze(root):
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    dbinfo = json.loads((root / 'database.json').read_text())
    requests = [json.loads(l) for l in (root / 'server_requests.jsonl').read_text().splitlines() if '"/api/edge_event"' in l]
    report = {'root': root.name, 'repeats': {}}
    with psycopg.connect(make_conninfo(dbinfo['conninfo'], options='-c search_path=' + dbinfo['schema']), row_factory=dict_row) as db:
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith('cut')):
            tl = [json.loads(l) for l in (folder / 'timeline.jsonl').read_text().splitlines()]
            get = lambda kind: next((x for x in tl if x['kind'] == kind), {})  # noqa: E731
            started = get('adapter_started')['host_started_at']
            power_off = get('ssh_session_ended')['power_off_at']
            post = json.loads((folder / 'post_boot.json').read_text())
            rows = post['rows']
            created = [r['created_at'] for r in rows]
            intervals = [b - a for a, b in zip(created, created[1:])]
            interval = sorted(intervals)[len(intervals) // 2] if intervals else None
            last = max(created) if created else None
            lost_window = power_off - last if last else None
            run_id = rows[0]['event_uid'].split(':')[0] if rows else None
            con = sqlite3.connect('file:' + str(folder / 'outbox.sqlite3') + '?mode=ro', uri=True)
            final = [dict(zip(('seq', 'kind', 'created_at', 'acked_at', 'attempts'), r)) for r in con.execute('SELECT message_seq,kind,created_at,acked_at,attempts FROM outbox ORDER BY message_seq')]
            con.close()
            ev = db.execute('SELECT * FROM events WHERE run_id=%s', (run_id,)).fetchall() if run_id else []
            al = db.execute('SELECT * FROM alerts WHERE event_id=%s', (ev[0]['id'],)).fetchone() if ev else None
            receipts = db.execute('SELECT message_seq,kind,received_at FROM edge_event_receipts WHERE event_uid=%s ORDER BY received_at,message_seq', (rows[0]['event_uid'],)).fetchall() if rows else []
            seqs = [r['message_seq'] for r in receipts]
            resent_open = next((r for r in requests if r.get('run_id') == run_id and r.get('message_seq') == 1 and r.get('status') == 200), None)
            report['repeats'][folder.name] = {
                'power_off_after_start_s': round(power_off - started, 1),
                'device_back_after_s': round(get('port_back')['first_ok_after_gap'] - power_off, 1),
                'clock_device_minus_host_s': {'before': get('clock_before').get('device_minus_host_s'), 'after_boot': get('ssh_ready').get('device_minus_host_s')},
                'chrony_after_boot': post['chrony'].splitlines()[:4],
                'boot_id_changed': bool(post['origin']) and post['origin'][0]['boot_id'] != post['boot_id'],
                'integrity_check': post['integrity_check'],
                'survived_messages': len(rows),
                'last_committed_before_power_off_s': round(lost_window, 2) if lost_window is not None else None,
                'message_interval_s': round(interval, 2) if interval else None,
                'estimated_lost_messages': max(0, int((lost_window - interval) // interval)) if (lost_window is not None and interval) else None,
                'final_outbox': {'rows': len(final), 'unacked': sum(r['acked_at'] is None for r in final), 'kinds': {k: sum(r['kind'] == k for r in final) for k in ('open', 'update', 'close')}},
                'server': {
                    'events': len(ev),
                    'receipts': len(receipts),
                    'receipts_in_order': seqs == sorted(seqs) and len(seqs) == len(set(seqs)) and len(seqs) == len(rows) + 1,  # survivors + the runtime_stop close
                    'close_reason': ev[0]['close_reason'] if ev else None,
                    'age_source': ev[0]['age_source'] if ev else None,
                    'event_age_on_arrival_ms': ev[0]['event_age_on_arrival_ms'] if ev else None,
                    'late_arrival': ev[0]['late_arrival'] if ev else None,
                    'open_age_header_ms': resent_open.get('event_age_ms') if resent_open else None,
                    'open_backlog_header': resent_open.get('event_backlog') if resent_open else None,
                    'notification_mode': al['notification_mode'] if al else None,
                    'classification_reason': al['classification_reason'] if al else None,
                    'event_started_at_minus_decided_s': round(ev[0]['started_at'] - ev[0]['decided_at'], 1) if ev and ev[0]['decided_at'] else None,
                },
            }
    (root / 'analysis').mkdir(exist_ok=True)
    (root / 'analysis' / 'd3_summary.json').write_text(json.dumps(report, indent=2, default=str) + '\n')
    for k, r in report['repeats'].items():
        print(k, 'power off at +%s s' % r['power_off_after_start_s'], 'integrity', r['integrity_check'], 'survived', r['survived_messages'],
              'last commit %.1f s before power off' % (r['last_committed_before_power_off_s'] or -1), 'est. lost', r['estimated_lost_messages'],
              '| server', r['server']['receipts'], 'receipts in order', r['server']['receipts_in_order'], 'age_source', r['server']['age_source'],
              'mode', r['server']['notification_mode'], r['server']['classification_reason'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--analyze', action='store_true', help='only summarise a finished root')
    ap.add_argument('--resume', action='store_true', help='continue an interrupted root: same schema and stage, missing repeats only')
    a = ap.parse_args()
    root = a.root.resolve()
    if a.analyze:
        return analyze(root)
    if (root / 'database.json').exists() and not a.resume:
        print('root exists: resuming it (same schema and stage, missing repeats only)', flush=True)
        a.resume = True
    import hashlib
    import os
    import secrets
    import shutil
    import signal

    conninfo = os.environ['BENCHMARK_PG_URL']
    secret = secrets.token_urlsafe(32)
    if a.resume:
        from psycopg.conninfo import make_conninfo

        cfg = json.loads((root / 'server_config.json').read_text())
        bridge_port = cfg['port']
        schema = json.loads((root / 'database.json').read_text())['schema']
        scoped = make_conninfo(conninfo, options='-c search_path=' + schema)
        d2.DEVICE['stage'] = d2.DEVICE['home'] + '/Workspace/fig6_ntu_benchmark/' + root.name
        (root / 'failure.json').unlink(missing_ok=True)
    else:
        root.mkdir(parents=True)
        write_idle()
        (root / 'code_snapshot').mkdir()
    for path in (Path(__file__), D2DIR / 'd2_controller.py', d2.ADAPTER, d2.LINK_GATE, d2.BENCHMARK_SERVER):
        shutil.copy2(path, root / 'code_snapshot' / path.name)
        (root / 'code_hashes.json').write_text(json.dumps({
            'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=d2.ROOT, text=True).strip(),
            'git_status': subprocess.check_output(['git', 'status', '--short'], cwd=d2.ROOT, text=True),
            'files': {str(p.relative_to(d2.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for parent in (d2.ROOT / 'edge', d2.BRIDGE) for p in parent.rglob('*.py') if '__pycache__' not in p.parts and 'tests' not in p.parts},
        }, indent=2) + '\n')
        bridge_port = d2.port()
        schema, scoped = d2.create_schema(conninfo, root)
        cfg = {
            'schema_version': 1, 'host': '127.0.0.1', 'port': bridge_port, 'cameras': [{'id': '1', 'label': 'Pi 5 adapter (D3)'}],
            'database_url_env': 'BENCHMARK_DATABASE_URL', 'archive_dir': str(root / 'decision_logs_archive'), 'clip_dir': str(root / 'clips'),
            'clip_pre_s': 3, 'clip_post_s': 2, 'clip_fps': 10, 'clip_retention_days': 30, 'metrics_interval_s': 10, 'metrics_retention_days': 30,
            'write_token_env': 'BENCHMARK_SERVER_SECRET', 'require_camera_tokens': True, 'require_media_token': True, 'edge_stale_after_s': 3,
            'features': {'alerts': True},
        }
        (root / 'server_config.json').write_text(json.dumps(cfg, indent=2) + '\n')
    bridge = f'http://127.0.0.1:{bridge_port}'
    env = {**os.environ, 'BENCHMARK_SERVER_SECRET': secret, 'BENCHMARK_DATABASE_URL': scoped}
    log = (root / 'server.log').open('a')
    api = subprocess.Popen([sys.executable, str(d2.BENCHMARK_SERVER), '--config', str(root / 'server_config.json'), '--log-dir', str(root)],
                           cwd=d2.ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    prober = Prober(d2.DEVICE['target'].split('@')[1])
    try:
        d2.wait_url(bridge + '/benchmark/time', api)
        token = d2.request(bridge + '/api/admin/cameras/1/token', secret, 'POST', {})['token']
        if a.resume:
            print('RESUMING', root.name, 'schema', schema, 'repeats done:', sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith('cut')), flush=True)
        else:
            (root / 'uptime_start.json').write_text(json.dumps({'device': json.loads(d2.ssh_text(d2.UPTIME)), 'host_now': time.time()}, indent=2) + '\n')
            result = d2.stage(root)
            print('STAGED bundle', result['manifest'].get('bundle_id'), flush=True)
        prober.start()
        for k in range(a.repeats):
            case = f'cut{k + 1}'
            if (root / case / 'post_boot.json').exists():
                if not (root / case / f'adapter_{Path(IDLE).stem}.json').exists():
                    print('RESEND pending for', case, flush=True)
                    resend_and_fetch(root, case, bridge_port, token, timeline_writer(root / case))
                continue
            if (root / case).exists():
                shutil.rmtree(root / case)
            one_repeat(root, case, bridge_port, token, prober)
            time.sleep(5)
        (root / 'completed.json').write_text(json.dumps({'completed_at': time.time(), 'repeats': a.repeats, 'pull_after_s': PULL_AFTER_S, 'schema': schema}, indent=2) + '\n')
        analyze(root)
    except BaseException as exc:
        (root / 'failure.json').write_text(json.dumps({'at': time.time(), 'error': str(exc), 'type': type(exc).__name__}, indent=2) + '\n')
        raise
    finally:
        prober.stop.set()
        try:
            d2.gate_stop()
        except Exception:
            pass
        if api.poll() is None:
            os.killpg(api.pid, signal.SIGTERM)
            try:
                api.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(api.pid, signal.SIGKILL)
        log.close()


if __name__ == '__main__':
    sys.exit(main() or 0)
