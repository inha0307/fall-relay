#!/usr/bin/env python3
"""Stage, pilot, gate and run paired 1+20 minute NTU replay trials with one real browser.

The isolated bridge runs on PostgreSQL (its own schema in a dedicated local PostgreSQL 14
instance, like the laboratory deployment's backend); the Raspberry Pi 4 runs no unrelated services during the
trials; 2 x 20 min per condition after a 1 min warm-up; device and server uptime are recorded because the devices
had been running for a long time before the run. Only isolated credentials are used. SSH supervisors restore the
operational inference services on normal exit, child failure, SIGTERM, controller disconnect or lease expiry.
"""

import argparse, concurrent.futures, hashlib, json, os, secrets, selectors, shlex, shutil, signal, socket, subprocess, sys, tarfile, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BRIDGE = ROOT / 'server/bridge'
# Lab-specific hosts, SSH key, browser paths and the services to stop on a device come from setup.json
# (copy setup.example.json; not versioned). The PostgreSQL instance for the isolated bridge comes from BENCHMARK_PG_URL.
SETUP = json.loads(Path(os.environ.get('FALLRELAY_BENCH_SETUP', HERE / 'setup.json')).read_text())
KEY = os.path.expanduser(SETUP['ssh_key'])
DEVICES = [{'unrelated_services': [], **d} for d in SETUP['devices']]
WARMUP_S, MEASURE_S, REPLICATES = 60, 1200, 2


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def ssh(d, cmd, **kwargs):
    return subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-i', KEY, d['target'], cmd], check=True, **kwargs
    )


def ssh_text(d, cmd):
    return ssh(d, cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, text=True).stdout


def request(url, secret, method='GET', body=None):
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method, headers={'X-Write-Token': secret, 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(r, timeout=5) as response:
        return json.load(response)


def wait_url(url, process):
    import urllib.request, urllib.error

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


UPTIME = "python3 -c \"import json,time,os;u=open('/proc/uptime').read().split();print(json.dumps({'uptime_s':float(u[0]),'booted_at':time.time()-float(u[0]),'loadavg':open('/proc/loadavg').read().split()[:3],'now':time.time(),'hostname':os.uname().nodename,'release':os.uname().release}))\""


def host_uptime():
    u = open('/proc/uptime').read().split()
    return {
        'uptime_s': float(u[0]),
        'booted_at': time.time() - float(u[0]),
        'loadavg': open('/proc/loadavg').read().split()[:3],
        'now': time.time(),
        'hostname': os.uname().nodename,
        'release': os.uname().release,
    }


def record_uptime(root, tag):
    data = {'server': host_uptime()}
    for d in DEVICES:
        data[d['device']] = json.loads(ssh_text(d, UPTIME))
    (root / f'uptime_{tag}.json').write_text(json.dumps(data, indent=2) + '\n')
    return data


def unrelated(d, action):
    """Stop (or start again) the device's unrelated system services; returns their states after the action."""
    if not d['unrelated_services']:
        return {}
    names = ' '.join(d['unrelated_services'])
    if action == 'stop':
        ssh(d, 'sudo -n systemctl stop ' + names, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    else:
        ssh(d, 'sudo -n systemctl start ' + names, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    states = ssh_text(d, 'systemctl is-active ' + names + '; true').split()
    return dict(zip(d['unrelated_services'], states))


STAGE = r'''
import sys,tarfile,pathlib,shutil,json,hashlib,os
settings=json.loads(sys.argv[1]);dest=pathlib.Path(settings['stage']);dest.mkdir(parents=True,exist_ok=True)
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as t:
 for entry in t:
  target=(dest/entry.name).resolve()
  if not target.is_relative_to(dest.resolve()):raise ValueError('unsafe archive')
  t.extract(entry,dest)
bundle=dest/'bundle'
shutil.copytree(settings['original'],bundle,ignore=shutil.ignore_patterns('*.log','*.jsonl','*.sqlite3*','camera_token*','before-*','decision_logs','__pycache__','run_identity.json'),dirs_exist_ok=True)
for name in ('benchmark_device.py','remote_supervisor.py','pi_hardware_agent.py'):shutil.copy2(dest/name,bundle/name)
cfg=json.loads(pathlib.Path(settings['config']).read_text());cfg['pose_model_dir']=str((pathlib.Path(settings['config']).parent/cfg['pose_model_dir']).resolve());cfg['server_url']='http://127.0.0.1:15992';cfg['camera_id']=settings['camera']
if cfg.get('camera_tuning_file'):cfg['camera_tuning_file']=str((pathlib.Path(settings['config']).parent/cfg['camera_tuning_file']).resolve())
(dest/'config.json').write_text(json.dumps(cfg,indent=2))
playlist=json.loads((dest/'playlist.json').read_text())
for video in playlist:
 data=(dest/'videos'/video['file']).read_bytes();assert hashlib.sha256(data).hexdigest()==video['sha256']
print(json.dumps({'stage':str(dest),'videos_verified':len(playlist),'manifest':json.loads((bundle/'bundle_manifest.json').read_text()),'clip_settings':cfg.get('event_clip',{}),'config_sha256':hashlib.sha256((dest/'config.json').read_bytes()).hexdigest()}))
'''


def stage(d, root):
    d['stage'] = d['home'] + '/Workspace/fig6_ntu_benchmark/' + root.name
    pack = root / (d['device'] + '-stage.tar')
    with tarfile.open(pack, 'w') as t:
        for name in ('benchmark_device.py', 'remote_supervisor.py'):
            t.add(HERE / name, arcname=name)
        t.add(BRIDGE / 'pi_hardware_agent.py', arcname='pi_hardware_agent.py')
        t.add(root / 'playlist.json', arcname='playlist.json')
        t.add(root / 'videos', arcname='videos')
    with pack.open('rb') as f:
        settings = {k: d[k] for k in ('stage', 'original', 'config', 'camera')}
        p = ssh(
            d,
            'python3 -c ' + shlex.quote(STAGE) + ' ' + shlex.quote(json.dumps(settings)),
            stdin=f,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    result = json.loads(p.stdout)
    (root / (d['device'] + '-stage.json')).write_text(json.dumps(result, indent=2) + '\n')
    pack.unlink()
    return result


def trial(root, tag, clips, rep, warmup, measure, bridge_port, tokens):
    running = []
    sel = selectors.DefaultSelector()
    out = root / tag
    out.mkdir()
    try:
        for d in DEVICES:
            remote_out = d['stage'] + '/runs/' + tag
            job = {
                'experiment_id': root.name,
                'run_id': uuid.uuid4().hex,
                'device': d['device'],
                'clips': clips,
                'trial': tag,
                'replicate': rep,
                'output': remote_out,
                'config': d['stage'] + '/config.json',
                'playlist': d['stage'] + '/playlist.json',
                'video_dir': d['stage'] + '/videos',
                'bundle': d['stage'] + '/bundle',
                'service': d['service'],
                'python': d['python'],
                'warmup_s': warmup,
                'measure_s': measure,
                'release_file': remote_out + '/release.json',
                'camera_token': tokens[d['camera']],
            }
            # Credential travels only on stdin and the remote owner-only job file.
            remote_job = d['stage'] + '/jobs/' + tag + '.json'
            writer = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)"
            ssh(
                d,
                'python3 -c ' + shlex.quote(writer) + ' ' + shlex.quote(remote_job),
                input=json.dumps(job).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            public = {k: v for k, v in job.items() if k != 'camera_token'}
            (out / (d['device'] + '-job.json')).write_text(json.dumps(public, indent=2) + '\n')
            err = (out / (d['device'] + '-ssh.log')).open('w')
            cmd = [
                'ssh',
                '-o',
                'BatchMode=yes',
                '-o',
                'ConnectTimeout=8',
                '-o',
                'ExitOnForwardFailure=yes',
                '-i',
                KEY,
                '-R',
                f'127.0.0.1:15992:127.0.0.1:{bridge_port}',
                d['target'],
                'python3 '
                + shlex.quote(d['stage'] + '/bundle/remote_supervisor.py')
                + ' --job '
                + shlex.quote(remote_job),
            ]
            process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1
            )
            item = {
                'device': d,
                'process': process,
                'err': err,
                'ready': False,
                'restored': False,
                'remote_out': remote_out,
                'job': public,
            }
            running.append(item)
            sel.register(process.stdout, selectors.EVENT_READ, item)
        start = None
        last_ping = 0
        deadline = time.monotonic() + warmup + measure + 240
        ready_deadline = time.monotonic() + 120
        while any(x['process'].poll() is None for x in running):
            now = time.monotonic()
            if now > deadline:
                raise RuntimeError('paired trial timed out')
            if start is None and now > ready_deadline:
                raise RuntimeError('both devices did not become ready')
            if now - last_ping >= 5:
                for x in running:
                    if x['process'].poll() is None:
                        x['process'].stdin.write('{"action":"ping"}\n')
                        x['process'].stdin.flush()
                last_ping = now
            for key, _ in sel.select(timeout=1):
                line = key.fileobj.readline()
                if not line:
                    sel.unregister(key.fileobj)
                    continue
                msg = json.loads(line)
                x = key.data
                with (out / 'supervisor.jsonl').open('a') as f:
                    f.write(json.dumps(msg) + '\n')
                if msg['kind'] == 'ready':
                    x['ready'] = True
                if msg['kind'] == 'service_restored':
                    x['restored'] = msg['restored']
                if msg['kind'] == 'error':
                    raise RuntimeError(msg['device'] + ': ' + msg['error'])
                if msg['kind'] == 'progress':
                    v = msg.get('latest_video') or {}
                    print(
                        json.dumps(
                            {
                                'trial': tag,
                                'device': msg['device'],
                                'phase': v.get('phase'),
                                'video': v.get('video_id'),
                                'repeat': v.get('repeat'),
                                'missing': v.get('missing_slots'),
                                'wall': msg['wall'],
                            }
                        ),
                        flush=True,
                    )
            if start is None and all(x['ready'] for x in running):
                start = time.time() + 5
                schedule = {
                    'start_at': start,
                    'measure_start': start + warmup + 3,
                    'measure_end': start + warmup + 3 + measure,
                    'inter_phase_gap_s': 3,
                    'warmup_s': warmup,
                    'measure_s': measure,
                    'clips': clips,
                    'replicate': rep,
                }
                (out / 'schedule.json').write_text(json.dumps(schedule, indent=2) + '\n')
                for x in running:
                    x['process'].stdin.write(json.dumps({'action': 'release', 'start_at': start}) + '\n')
                    x['process'].stdin.flush()
                print('RELEASED', tag, json.dumps(schedule), flush=True)
            for x in running:
                if x['process'].poll() not in (None, 0):
                    raise RuntimeError(x['device']['device'] + ' SSH/supervisor failed')
        for x in running:
            if x['process'].wait() != 0:
                raise RuntimeError('remote trial failed')
    finally:
        for x in running:
            p = x['process']
            if p.poll() is None:
                try:
                    p.stdin.write('{"action":"abort"}\n')
                    p.stdin.flush()
                except (OSError, BrokenPipeError):
                    pass
                try:
                    p.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    p.terminate()
                    p.wait(timeout=10)
            x['err'].close()
            # Always retrieve raw logs, including interrupted/failed trials and restoration proof.
            tarpath = out / (x['device']['device'] + '.tar')
            with tarpath.open('wb') as f:
                ssh(
                    x['device'],
                    'tar -C ' + shlex.quote(x['remote_out']) + ' -cf - .',
                    stdout=f,
                    stderr=subprocess.PIPE,
                    timeout=90,
                )
            local = out / x['device']['device']
            local.mkdir(exist_ok=True)
            with tarfile.open(tarpath) as t:
                for member in t:
                    if not (local / member.name).resolve().is_relative_to(local.resolve()):
                        raise ValueError('unsafe result archive')
                    t.extract(member, local)
            tarpath.unlink()
            proof = json.loads((local / 'service_after.json').read_text())
            assert proof['restored'], proof
    return out


def create_schema(conninfo, root):
    """Empty bridge schema of this run's own in the dedicated PostgreSQL instance; returns (schema, scoped conninfo)."""
    sys.path.insert(0, str(BRIDGE))
    import psycopg
    from psycopg.conninfo import make_conninfo
    from postgres.schema import initialize

    schema = 'bench_' + uuid.uuid4().hex[:12]
    with psycopg.connect(conninfo) as connection:
        initialize(connection, schema)
    (root / 'database.json').write_text(
        json.dumps(
            {
                'conninfo': conninfo,
                'schema': schema,
                'note': 'dedicated local PostgreSQL 14 instance, trust authentication on a private unix socket; no password',
            },
            indent=2,
        )
        + '\n'
    )
    return schema, make_conninfo(conninfo, options='-c search_path=' + schema)


def db_counts(scoped):
    import psycopg

    with psycopg.connect(scoped) as c:
        return c.execute('SELECT COUNT(*) FROM events').fetchone()[0], c.execute(
            'SELECT COUNT(*) FROM alert_receipts'
        ).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--pilot-only', action='store_true')
    a = ap.parse_args()
    root = a.root.resolve()
    children = []
    handles = []
    secret = secrets.token_urlsafe(32)
    conninfo = os.environ['BENCHMARK_PG_URL']
    shutil.copytree(
        HERE, root / 'code_snapshot', ignore=shutil.ignore_patterns('__pycache__', 'setup.json'), dirs_exist_ok=True
    )
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for parent in (ROOT / 'edge', BRIDGE, ROOT / 'server/frontend/context', ROOT / 'server/frontend/app/api/alerts')
        for p in parent.rglob('*')
        if p.is_file()
        and p.suffix in ('.py', '.js', '.jsx')
        and '__pycache__' not in p.parts
        and 'tests' not in p.parts
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
    bridge_port, web_port = port(), port()
    web = f'http://127.0.0.1:{web_port}'
    bridge = f'http://127.0.0.1:{bridge_port}'
    schema, scoped = create_schema(conninfo, root)
    cfg = {
        'schema_version': 1,
        'host': '127.0.0.1',
        'port': bridge_port,
        'cameras': [{'id': '1', 'label': 'Pi 5 benchmark'}, {'id': '2', 'label': 'Pi 4 benchmark'}],
        'database_url_env': 'BENCHMARK_DATABASE_URL',
        'archive_dir': str(root / 'decision_logs'),
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
    private = Path('/tmp') / ('fig6-browser-' + root.name)
    if private.exists():
        shutil.rmtree(private)
    private.mkdir(mode=0o700)
    frontend_source = Path(os.environ.get('BENCHMARK_FRONTEND', str(ROOT / 'server/frontend')))
    frontend = private / 'frontend'
    frontend.mkdir(exist_ok=True)
    shutil.copytree(frontend_source / '.next', frontend / '.next', dirs_exist_ok=True)
    shutil.copy2(frontend_source / 'package.json', frontend / 'package.json')
    if (frontend_source / 'public').exists():
        shutil.copytree(frontend_source / 'public', frontend / 'public', dirs_exist_ok=True)
    if not (frontend / 'node_modules').exists():
        (frontend / 'node_modules').symlink_to(ROOT / 'server/frontend/node_modules', target_is_directory=True)
    env = {
        **os.environ,
        'BENCHMARK_SERVER_SECRET': secret,
        'BENCHMARK_DATABASE_URL': scoped,
        'FALLRELAY_WRITE_TOKEN': secret,
        'FALLRELAY_DATABASE_URL': conninfo,
        'FALLRELAY_APP_SCHEMA': schema,
        'PIPELINE_SERVER_URL': bridge,
        'NEXT_TELEMETRY_DISABLED': '1',
        'LD_LIBRARY_PATH': os.environ.get(
            'BENCHMARK_BROWSER_LIBS', str(ROOT / 'artifacts/fig6_ntu_benchmark/browser_libs')
        ),
    }

    def spawn(name, cmd, cwd):
        f = (root / (name + '.log')).open('w')
        handles.append(f)
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=f, stderr=f, start_new_session=True)
        children.append(p)
        return p

    def interrupted(*_):
        raise KeyboardInterrupt('controller interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    stopped = {}
    try:
        record_uptime(root, 'start')
        api = spawn(
            'server',
            [
                sys.executable,
                str(HERE / 'benchmark_server.py'),
                '--config',
                str(root / 'server_config.json'),
                '--log-dir',
                str(root),
            ],
            ROOT,
        )
        wait_url(bridge + '/benchmark/time', api)
        tokens = {
            d['camera']: request(bridge + '/api/admin/cameras/' + d['camera'] + '/token', secret, 'POST', {})['token']
            for d in DEVICES
        }
        # A dedicated admin test account and DB; operational accounts are never opened.
        password = secrets.token_urlsafe(24)
        seed = """import {openDb,newId,nextUserCode,nowIso} from './server/frontend/lib/appDb.mjs';import {hashPassword} from './server/frontend/lib/password.mjs';const db=await openDb(),t=nowIso();await db.transaction(async tx=>tx.run(\"INSERT INTO accounts(id,user_code,login_id,name,role,status,must_change_password,password_hash,created_at,updated_at) VALUES (?,?,?,?,?,'active',0,?,?,?)\",newId(),await nextUserCode(tx),'ntubenchmark','NTU benchmark','admin',hashPassword(process.env.BENCH_PASSWORD),t,t));await db.close();"""
        seeded = subprocess.run(
            ['node', '--input-type=module', '-e', seed],
            cwd=ROOT,
            env={**env, 'BENCH_PASSWORD': password},
            capture_output=True,
            text=True,
        )
        if seeded.returncode:
            raise RuntimeError('account seed failed: ' + seeded.stderr[-1500:])
        webproc = spawn(
            'web',
            ['node', 'node_modules/next/dist/bin/next', 'start', '--hostname', '127.0.0.1', '--port', str(web_port)],
            frontend,
        )
        wait_url(web + '/api/auth/me', webproc)
        browsercfg = {
            'experiment_id': root.name,
            'playwright_module': SETUP['playwright_module'],
            'chromium': SETUP['chromium'],
            'web': web,
            'login': 'ntubenchmark',
            'password': password,
            'output': str(root / 'browser.jsonl'),
            'ready_file': str(root / 'browser_ready.json'),
            'stop_file': str(root / 'browser_stop'),
            'screenshot': str(root / 'browser.png'),
        }
        (private / 'browser.json').write_text(json.dumps(browsercfg))
        (private / 'browser.json').chmod(0o600)
        browser = spawn('browser', ['node', str(HERE / 'browser.mjs'), str(private / 'browser.json')], ROOT)
        deadline = time.monotonic() + 50
        while not (root / 'browser_ready.json').exists():
            if browser.poll() is not None:
                raise RuntimeError('real browser failed: ' + (root / 'browser.log').read_text()[-2500:])
            if time.monotonic() > deadline:
                raise RuntimeError('real browser readiness timeout')
            time.sleep(0.2)
        print('BROWSER_READY', flush=True)
        for d in DEVICES:
            before = (
                {
                    n: s
                    for n, s in zip(
                        d['unrelated_services'],
                        ssh_text(d, 'systemctl is-active ' + ' '.join(d['unrelated_services']) + '; true').split(),
                    )
                }
                if d['unrelated_services']
                else {}
            )
            stopped[d['device']] = {'before': before, 'after_stop': unrelated(d, 'stop')}
        (root / 'unrelated_services.json').write_text(json.dumps(stopped, indent=2) + '\n')
        print('UNRELATED_SERVICES_STOPPED', json.dumps(stopped), flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda d: stage(d, root), DEVICES))
        print('STAGED_BOTH_DEVICES', flush=True)
        pilot = trial(root, 'pilot_clips_on', True, 0, 10, 75, bridge_port, tokens)
        # Gate uses real raw logs, no synthetic decision or browser calls.
        gate = {}
        for d in DEVICES:
            folder = pilot / d['device']
            load = [json.loads(s) for s in (folder / 'lifecycle.jsonl').read_text().splitlines()]
            slots = [json.loads(s) for s in (folder / 'slots.jsonl').read_text().splitlines()]
            summaries = [json.loads(s) for s in (folder / 'summary.jsonl').read_text().splitlines()]
            assert any(r['kind'] == 'ready' for r in load) and any(
                r['phase'] == 'measure' and r['segment'] == 'source' for r in slots
            )
            assert (
                summaries[-1]['outbox_pending'] == 0
                and summaries[-1]['outbox_blocked'] == 0
                and summaries[-1]['result_send_failures'] == 0
                and summaries[-1]['clips_failed'] == 0
                and summaries[-1]['clip_marks_pending'] == 0
            ), summaries
            assert all(
                r['history_len'] == 0 and r['engine_state'] == 'IDLE'
                for r in map(json.loads, (folder / 'boundaries.jsonl').read_text().splitlines())
                if r['kind'] == 'reset'
            )
            gate[d['device']] = {
                'real_source_slots': sum(r['phase'] == 'measure' and r['segment'] == 'source' for r in slots),
                'summary': summaries[-1],
            }
        gate['events'], gate['browser_receipts'] = db_counts(scoped)
        clip_http = [
            json.loads(s)
            for s in (root / 'server_requests.jsonl').read_text().splitlines()
            if json.loads(s)['path'] == '/api/edge_clip'
        ]
        gate['clip_http_correlated'] = bool(clip_http) and all(
            r.get('camera_id') in ('1', '2') and r.get('run_id') and r['http_bytes'] > 32768 for r in clip_http
        )
        if not gate['clip_http_correlated']:
            raise RuntimeError('large clip HTTP request attribution failed')
        gate['browser_live'] = browser.poll() is None
        gate['pilot_clips_saved'] = len(list((root / 'clips').glob('*.json')))
        (root / 'pilot_gate.json').write_text(json.dumps(gate, indent=2) + '\n')
        print('PILOT_GATE', json.dumps(gate), flush=True)
        if not gate['browser_live']:
            raise RuntimeError('browser stopped in pilot')
        # A zero-event pilot cannot validate delivery or evidence; preserve it and report the gate.
        if gate['events'] and (not gate['browser_receipts'] or not gate['pilot_clips_saved']):
            raise RuntimeError('real event delivery/clip readiness failed in pilot')
        if not a.pilot_only:
            for index in range(2 * REPLICATES):
                if browser.poll() is not None or api.poll() is not None or webproc.poll() is not None:
                    raise RuntimeError('server/browser failed before next trial')
                clips = bool(index % 2)
                rep = index // 2 + 1
                trial(
                    root,
                    f'main_{index + 1:02d}_clips_{"on" if clips else "off"}_r{rep}',
                    clips,
                    rep,
                    WARMUP_S,
                    MEASURE_S,
                    bridge_port,
                    tokens,
                )
        (root / 'completed.json').write_text(
            json.dumps(
                {
                    'completed_at': time.time(),
                    'main_completed': not a.pilot_only,
                    'pilot_only': a.pilot_only,
                    'replicates': REPLICATES,
                    'warmup_s': WARMUP_S,
                    'measure_s': MEASURE_S,
                }
            )
        )
    except BaseException as exc:
        (root / 'failure.json').write_text(
            json.dumps({'at': time.time(), 'error': str(exc), 'type': type(exc).__name__}, indent=2) + '\n'
        )
        raise
    finally:
        for d in DEVICES:
            if d['device'] in stopped:
                try:
                    stopped[d['device']]['after_start'] = unrelated(d, 'start')
                except Exception as exc:
                    stopped[d['device']]['after_start'] = {'error': str(exc)}
        (root / 'unrelated_services.json').write_text(json.dumps(stopped, indent=2) + '\n')
        try:
            record_uptime(root, 'end')
        except Exception as exc:
            (root / 'uptime_end.json').write_text(json.dumps({'error': str(exc)}))
        (root / 'browser_stop').touch()
        time.sleep(1)
        for p in reversed(children):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
        for f in handles:
            f.close()


if __name__ == '__main__':
    main()
