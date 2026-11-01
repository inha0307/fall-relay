#!/usr/bin/env python3
"""Check D2 (v1.1.0): a late event whose person is still lying is live; one who recovered during the cut goes to review.

Runs the any-detector adapter (edge/examples/any_detector_adapter.py) on the Raspberry Pi 5 with the two hand-written
score sequences of make_d2_scores.py, each in its own run with its own outbox, through the same TCP gate as the
link-loss trial (docs/evidence/link_loss/link_gate.py between the adapter and the SSH reverse tunnel to an isolated
bridge on this host). `--local` runs the same cases on this host without a device (adapter and gate here).
The link is cut before the adapter starts and restored 180 s later. The isolated bridge is the production bridge
(benchmark_server.py: create_app + passive request log) on its own PostgreSQL schema. No camera, no web
application and no browser are involved: the evidence is the bridge's events, alerts, alert log and receipts, the
request log with the delivery-context headers, the gate log and the device outbox. The device must already run the
v1.1.0 bundle; the staged bundle is a copy of the installed one and its manifest is recorded in stage.json.
"""

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'server' / 'bridge' / 'video_bridge.py').exists())
HERE = Path(__file__).resolve().parent
BRIDGE = ROOT / 'server/bridge'
assert (BRIDGE / 'video_bridge.py').exists(), ROOT
# Lab-specific host, SSH key and install folder come from edge/ntu_benchmark/setup.json (not versioned; see
# setup.example.json): the first device entry is used. With --local nothing of it is needed.
SETUP_PATH = Path(os.environ.get('FALLRELAY_BENCH_SETUP', ROOT / 'edge' / 'ntu_benchmark' / 'setup.json'))
KEY = None
DEVICE = {'device': 'pi5', 'camera': '1', 'gate_port': 15993, 'tunnel_port': 15992}
ADAPTER = ROOT / 'edge' / 'examples' / 'any_detector_adapter.py'
LINK_GATE = ROOT / 'docs' / 'evidence' / 'link_loss' / 'link_gate.py'
BENCHMARK_SERVER = ROOT / 'edge' / 'ntu_benchmark' / 'benchmark_server.py'
CASES = [('lying_still', 'lying_still.csv'), ('recovered_in_cut', 'recovered_in_cut.csv')]
CUT = (0.0, 180.0)  # (offset from the adapter start, duration) in seconds; 0 = the link is already dead when the adapter starts, so the open at 2.2 s is queued
CASE_TIMEOUT_S = 262 + 150 + 120  # sequence + adapter drain timeout + margin
# --local: rehearsal on this host without the device (adapter, gate and "device" folder here; same bridge and
# analysis). The staged bundle is then edge/ plus the shared outbox modules of server/bridge.
LOCAL = False
LOCAL_EXTRA = ['edge_outbox.py', 'edge_event_runtime.py', 'edge_event_contract.py', 'schema_migrations.py', 'migrations']


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def ssh(cmd, **kwargs):
    if LOCAL:
        return subprocess.run(['bash', '-c', cmd], check=True, **kwargs)
    # Exit status 255 is the SSH transport failing (the device's network flaps for a while after a reboot), not the
    # remote command: retry a few times before giving up.
    for attempt in range(12):
        try:
            return subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-i', KEY, DEVICE['target'], cmd], check=True, **kwargs
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 255 or attempt == 11:
                raise
            time.sleep(5)


def ssh_text(cmd):
    return ssh(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, text=True).stdout


def request(url, secret, method='GET', body=None):
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={'X-Write-Token': secret, 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def wait_url(url, process):
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('server exited')
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError('server startup timed out')


UPTIME = (
    "python3 -c \"import json,time,os;u=open('/proc/uptime').read().split();print(json.dumps({'uptime_s':float(u[0]),"
    "'booted_at':time.time()-float(u[0]),'loadavg':open('/proc/loadavg').read().split()[:3],'now':time.time(),"
    "'hostname':os.uname().nodename,'release':os.uname().release,'chrony':os.popen('chronyc tracking').read()}))\""
)

STAGE = r'''
import sys,tarfile,pathlib,shutil,json,hashlib
settings=json.loads(sys.argv[1]);dest=pathlib.Path(settings['stage']);dest.mkdir(parents=True,exist_ok=True)
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as t:
 for entry in t:
  target=(dest/entry.name).resolve()
  if not target.is_relative_to(dest.resolve()):raise ValueError('unsafe archive')
  t.extract(entry,dest)
bundle=dest/'bundle'
shutil.copytree(settings['original'],bundle,ignore=shutil.ignore_patterns('*.log','*.jsonl','*.sqlite3*','camera_token*','before-*','decision_logs','__pycache__','run_identity.json'),dirs_exist_ok=True)
for name in settings.get('extra',[]):
 src=pathlib.Path(settings['extra_root'])/name
 shutil.copytree(src,bundle/name,dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src,bundle/name)
(bundle/'examples').mkdir(exist_ok=True);shutil.copy2(dest/'any_detector_adapter.py',bundle/'examples'/'any_detector_adapter.py')
scores={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((dest/'scores').glob('*.csv'))}
print(json.dumps({'stage':str(dest),'manifest':json.loads((bundle/'bundle_manifest.json').read_text()),'scores_sha256':scores,'python':sys.version}))
'''


def stage(root):
    DEVICE['stage'] = DEVICE['home'] + '/Workspace/fig6_ntu_benchmark/' + root.name
    pack = root / 'stage.tar'
    with tarfile.open(pack, 'w') as t:
        t.add(ADAPTER, arcname='any_detector_adapter.py')
        t.add(LINK_GATE, arcname='link_gate.py')
        for path in sorted(HERE.glob('*.csv')):  # the two D2 sequences and idle.csv of the power-loss check
            t.add(path, arcname='scores/' + path.name)
    with pack.open('rb') as f:
        settings = {'stage': DEVICE['stage'], 'original': DEVICE['original']}
        if LOCAL:
            settings.update(extra=LOCAL_EXTRA, extra_root=str(BRIDGE))
        p = ssh(
            DEVICE['python'] + ' -c ' + shlex.quote(STAGE) + ' ' + shlex.quote(json.dumps(settings)),
            stdin=f,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    result = json.loads(p.stdout)
    (root / 'stage.json').write_text(json.dumps(result, indent=2) + '\n')
    pack.unlink()
    return result


def gate_start():
    g = DEVICE['stage'] + '/gate'
    ssh(
        'mkdir -p %s && rm -f %s && (nohup setsid %s %s --listen %d --target %d --cut %s --log %s </dev/null >/dev/null 2>&1 &)'
        % (
            shlex.quote(g),
            shlex.quote(g + '/cut'),
            DEVICE['python'],
            shlex.quote(DEVICE['stage'] + '/link_gate.py'),
            DEVICE['gate_port'],
            DEVICE['tunnel_port'],
            shlex.quote(g + '/cut'),
            shlex.quote(g + '/gate.jsonl'),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    time.sleep(1)
    return ssh_text('pgrep -f link_gate.py; cat ' + shlex.quote(g + '/gate.jsonl')).strip()


def gate_stop():
    try:
        ssh(
            'pkill -f link_gate.py; rm -f ' + shlex.quote(DEVICE['stage'] + '/gate/cut') + '; true',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except Exception:
        pass


def gate_cut(on):
    g = DEVICE['stage'] + '/gate/cut'
    ssh(('touch ' if on else 'rm -f ') + shlex.quote(g), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)


def run_case(root, case, scores, bridge_port, token):
    """One adapter run on the device with one link cut; returns the local folder with its records."""
    out = root / case
    out.mkdir()
    remote_out = DEVICE['stage'] + '/runs/' + case
    # The token travels on SSH stdin only; the adapter reads it from the environment of the remote shell.
    remote = (
        'mkdir -p %(out)s && IFS= read -r FALLRELAY_CAMERA_TOKEN && export FALLRELAY_CAMERA_TOKEN && '
        'python3 -c "import json,time;print(json.dumps({\'adapter_start_wall\':time.time()}))" > %(out)s/start.json && '
        'exec %(py)s %(adapter)s --server http://127.0.0.1:%(gate)d --camera %(cam)s --input %(csv)s '
        '--outbox %(out)s/outbox.sqlite3 --realtime --drain-timeout 150 > %(out)s/adapter.json 2> %(out)s/adapter.err'
    ) % {
        'out': shlex.quote(remote_out),
        'py': DEVICE['python'],
        'adapter': shlex.quote(DEVICE['stage'] + '/bundle/examples/any_detector_adapter.py'),
        'gate': DEVICE['gate_port'],
        'cam': DEVICE['camera'],
        'csv': shlex.quote(DEVICE['stage'] + '/scores/' + scores),
    }
    if LOCAL:
        cmd = ['bash', '-c', remote]
    else:
        cmd = [
            'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes', '-i', KEY,
            '-R', f"127.0.0.1:{DEVICE['tunnel_port']}:127.0.0.1:{bridge_port}", DEVICE['target'], remote,
        ]
    err = (out / 'ssh.log').open('w')
    plan = {'offset_s': CUT[0], 'duration_s': CUT[1], 'state': 'pending'}

    def note(**x):
        with (out / 'cuts.jsonl').open('a') as f:
            f.write(json.dumps({'wall': time.time(), **x}) + '\n')

    if plan['offset_s'] <= 0:
        # The link is dead before the adapter starts: its open (2.2 s into the sequence) can only be queued.
        gate_cut(True)
        plan.update(state='on', cut_at=time.time())
    started = time.time()
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
    process.stdin.write(token + '\n')
    process.stdin.flush()
    note(kind='adapter_started', case=case, scores=scores, host_started_at=started)
    if plan['state'] == 'on':
        note(kind='cut_start', **plan)
        print('CUT_START', case, json.dumps(plan), flush=True)
    try:
        while process.poll() is None:
            now = time.time()
            if now - started > CASE_TIMEOUT_S:
                raise RuntimeError(case + ': adapter run timed out')
            if plan['state'] == 'pending' and now >= started + plan['offset_s']:
                gate_cut(True)
                plan.update(state='on', cut_at=time.time())
                note(kind='cut_start', **plan)
                print('CUT_START', case, json.dumps(plan), flush=True)
            elif plan['state'] == 'on' and now >= started + plan['offset_s'] + plan['duration_s']:
                gate_cut(False)
                plan.update(state='done', restored_at=time.time())
                note(kind='cut_end', **plan)
                print('CUT_END', case, json.dumps(plan), flush=True)
            time.sleep(0.2)
    finally:
        if plan['state'] == 'on':
            try:
                gate_cut(False)
                plan.update(state='done', restored_at=time.time())
                note(kind='cut_end', **plan)
            except Exception as exc:
                note(kind='restore_failed', error=str(exc))
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        err.close()
        note(kind='adapter_exited', returncode=process.returncode, host_exited_at=time.time())
        tarpath = out / 'device.tar'
        with tarpath.open('wb') as f:
            ssh('tar -C ' + shlex.quote(remote_out) + ' -cf - .', stdout=f, stderr=subprocess.PIPE, timeout=90)
        with tarfile.open(tarpath) as t:
            for member in t:
                if not (out / member.name).resolve().is_relative_to(out.resolve()):
                    raise ValueError('unsafe result archive')
                t.extract(member, out)
        tarpath.unlink()
        try:
            (out / 'gate.jsonl').write_text(ssh_text('cat ' + shlex.quote(DEVICE['stage'] + '/gate/gate.jsonl')))
        except Exception as exc:
            note(kind='gate_log_failed', error=str(exc))
    # The adapter exits 1 when a message stayed unacknowledged; that is a finding, not a controller failure.
    print('CASE_DONE', case, 'returncode', process.returncode, (out / 'adapter.json').read_text().strip(), flush=True)
    return out


def create_schema(conninfo, root):
    sys.path.insert(0, str(BRIDGE))
    import psycopg
    from postgres.schema import initialize
    from psycopg.conninfo import make_conninfo

    schema = 'bench_' + uuid.uuid4().hex[:12]
    with psycopg.connect(conninfo) as connection:
        initialize(connection, schema)
    (root / 'database.json').write_text(
        json.dumps({'conninfo': conninfo, 'schema': schema, 'note': 'dedicated local PostgreSQL instance; no password'}, indent=2)
        + '\n'
    )
    return schema, make_conninfo(conninfo, options='-c search_path=' + schema)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--case', action='append', choices=[c for c, _ in CASES], help='default: both cases, in order')
    ap.add_argument('--local', action='store_true', help='rehearse on this host without the device')
    a = ap.parse_args()
    root = a.root.resolve()
    root.mkdir(parents=True)
    cases = [(c, f) for c, f in CASES if not a.case or c in a.case]
    conninfo = os.environ['BENCHMARK_PG_URL']
    global LOCAL
    LOCAL = a.local
    bridge_port = port()
    global KEY
    if LOCAL:
        DEVICE.update(
            device='host', target=None, home=str(root / 'device'), original=str(ROOT / 'edge'), python=sys.executable,
            gate_port=port(), tunnel_port=bridge_port,
        )
    else:
        setup = json.loads(SETUP_PATH.read_text())
        KEY = setup['ssh_key']
        lab = setup['devices'][0]
        DEVICE.update({k: lab[k] for k in ('device', 'camera', 'target', 'home', 'original', 'python')})
    for name in ('lying_still.csv', 'recovered_in_cut.csv'):
        assert (HERE / name).exists(), 'run make_d2_scores.py first'
    (root / 'code_snapshot').mkdir()
    for path in (Path(__file__), HERE / 'analyze_d2.py', HERE / 'make_d2_scores.py', ADAPTER, LINK_GATE, BENCHMARK_SERVER):
        shutil.copy2(path, root / 'code_snapshot' / path.name)
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for parent in (ROOT / 'edge', BRIDGE)
        for p in parent.rglob('*.py')
        if '__pycache__' not in p.parts and 'tests' not in p.parts
    }
    (root / 'code_hashes.json').write_text(
        json.dumps(
            {
                'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'git_status': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True),
                'files': hashes,
            },
            indent=2,
        )
        + '\n'
    )
    secret = secrets.token_urlsafe(32)
    bridge = f'http://127.0.0.1:{bridge_port}'
    schema, scoped = create_schema(conninfo, root)
    cfg = {
        'schema_version': 1,
        'host': '127.0.0.1',
        'port': bridge_port,
        'cameras': [{'id': '1', 'label': 'Pi 5 adapter (D2)'}],
        'database_url_env': 'BENCHMARK_DATABASE_URL',
        'archive_dir': str(root / 'decision_logs_archive'),
        'clip_dir': str(root / 'clips'),
        'clip_pre_s': 3,
        'clip_post_s': 2,
        'clip_fps': 10,
        'clip_retention_days': 30,
        'metrics_interval_s': 10,
        'metrics_retention_days': 30,
        'write_token_env': 'BENCHMARK_SERVER_SECRET',
        'require_camera_tokens': True,
        'require_media_token': True,
        'edge_stale_after_s': 3,
        'features': {'alerts': True},
    }
    (root / 'server_config.json').write_text(json.dumps(cfg, indent=2) + '\n')
    env = {**os.environ, 'BENCHMARK_SERVER_SECRET': secret, 'BENCHMARK_DATABASE_URL': scoped}
    log = (root / 'server.log').open('w')
    api = subprocess.Popen(
        [sys.executable, str(BENCHMARK_SERVER), '--config', str(root / 'server_config.json'), '--log-dir', str(root)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    def interrupted(*_):
        raise KeyboardInterrupt('controller interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    staged = False
    try:
        wait_url(bridge + '/benchmark/time', api)
        token = request(bridge + '/api/admin/cameras/1/token', secret, 'POST', {})['token']
        (root / 'uptime_start.json').write_text(json.dumps({'device': json.loads(ssh_text(UPTIME)), 'host_now': time.time()}, indent=2) + '\n')
        result = stage(root)
        staged = True
        print('STAGED', json.dumps({k: v for k, v in result.items() if k != 'manifest'}), 'bundle', result['manifest'].get('bundle_id'), flush=True)
        (root / 'gate.json').write_text(gate_start() + '\n')
        print('GATE_STARTED', flush=True)
        for case, scores in cases:
            run_case(root, case, scores, bridge_port, token)
            time.sleep(5)
        (root / 'uptime_end.json').write_text(json.dumps({'device': json.loads(ssh_text(UPTIME)), 'host_now': time.time()}, indent=2) + '\n')
        (root / 'completed.json').write_text(
            json.dumps({'completed_at': time.time(), 'cases': cases, 'cut': CUT, 'device': DEVICE['device'], 'schema': schema}, indent=2) + '\n'
        )
    except BaseException as exc:
        (root / 'failure.json').write_text(json.dumps({'at': time.time(), 'error': str(exc), 'type': type(exc).__name__}, indent=2) + '\n')
        raise
    finally:
        if staged:
            gate_stop()
        time.sleep(1)
        if api.poll() is None:
            os.killpg(api.pid, signal.SIGTERM)
            try:
                api.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(api.pid, signal.SIGKILL)
                api.wait()
        log.close()


if __name__ == '__main__':
    main()
