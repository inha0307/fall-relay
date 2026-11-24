"""Fault-injection comparison with a broker-based baseline (Mosquitto + paho-mqtt QoS 1, persistent sessions).

Each scenario is one deterministic run, as the rows A1-C1 of the Fall-Relay tests. One fall event is detected per run;
the result counts the event rows and alerts in PostgreSQL after the fault.
  S1  the broker's PUBACK to the device is lost (proxy drops it and closes the connection); the device resends
  S2  the consumer commits event and alert, then is killed before its PUBACK; it restarts with the same session
  S3  the device process is killed after publish() while the broker is unreachable; it restarts later
  S4  the broker acknowledges the message (PUBACK) while the consumer is offline, then the broker is killed (SIGKILL);
      S4-control stops the broker with SIGTERM instead
Environment: BASELINE_DATABASE_URL (an empty PostgreSQL database).  Usage:
  python run_baseline.py --mosquitto /usr/sbin/mosquitto --out results/"""

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
PY = sys.executable
BROKER, PROXY = 18830, 18831
ap = argparse.ArgumentParser()
ap.add_argument('--mosquitto', required=True)
ap.add_argument('--out', required=True)
a = ap.parse_args()
OUT = Path(a.out)
OUT.mkdir(parents=True, exist_ok=True)
DB = os.environ['BASELINE_DATABASE_URL']


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(('127.0.0.1', port)) == 0


def wait_port(port, up=True, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port) == up:
            return
        time.sleep(0.1)
    raise RuntimeError(f'port {port} not {"up" if up else "down"}')


class Broker:
    def __init__(self, work, persistence):
        self.work = work
        work.mkdir(parents=True, exist_ok=True)
        self.conf = work / 'mosquitto.conf'
        lines = [f'listener {BROKER} 127.0.0.1', 'allow_anonymous true', f'log_dest file {work}/mosquitto.log']
        if persistence:
            lines += ['persistence true', f'persistence_location {work}/', 'autosave_interval 1800']
        self.conf.write_text('\n'.join(lines) + '\n')
        self.p = None

    def start(self):
        self.p = subprocess.Popen(
            [a.mosquitto, '-c', str(self.conf)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
        wait_port(BROKER, True)

    def stop(self, sig=signal.SIGTERM):
        self.p.send_signal(sig)
        self.p.wait(10)
        wait_port(BROKER, False)


def spawn(script, *args):
    return subprocess.Popen(
        [PY, str(HERE / script), *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, 'PYTHONUNBUFFERED': '1'},
    )


def finish(p, timeout=30):
    out, _ = p.communicate(timeout=timeout)
    lines = []
    for line in out.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            lines.append({'raw': line})
    return {'returncode': p.returncode, 'log': lines}


def run(script, *args):
    return finish(spawn(script, *args))


def reset_db(mode):
    with psycopg.connect(DB, autocommit=True) as db:
        db.execute('DROP TABLE IF EXISTS alerts, events')
        if mode == 'naive':
            db.execute(
                'CREATE TABLE events (id serial PRIMARY KEY, event_id text NOT NULL, stored_at timestamptz DEFAULT now())'
            )
        else:
            db.execute('CREATE TABLE events (event_id text PRIMARY KEY, stored_at timestamptz DEFAULT now())')
        db.execute(
            'CREATE TABLE alerts (id serial PRIMARY KEY, event_id text NOT NULL, created_at timestamptz DEFAULT now())'
        )


def counts():
    with psycopg.connect(DB) as db:
        events = db.execute('SELECT count(*) FROM events').fetchone()[0]
        alerts = db.execute('SELECT count(*) FROM alerts').fetchone()[0]
    return events, alerts


def s1(mode, tag):
    b = Broker(OUT / f'broker_{tag}', persistence=False)
    b.start()
    cons = spawn('mqtt_consumer.py', '--mode', mode, '--port', BROKER, '--client-id', f'bridge-{tag}', '--seconds', 14)
    time.sleep(1.5)
    proxy = spawn('fault_proxy.py', PROXY, BROKER)
    wait_port(PROXY, True)
    dev = run(
        'mqtt_device.py', '--port', PROXY, '--client-id', f'camera-{tag}', '--event-id', f'E-{tag}', '--seconds', 10
    )
    c = finish(cons)
    proxy.terminate()
    p = finish(proxy)
    b.stop()
    return {'device': dev, 'consumer': [c], 'proxy': p}


def s2(mode, tag):
    b = Broker(OUT / f'broker_{tag}', persistence=False)
    b.start()
    c1 = spawn(
        'mqtt_consumer.py',
        '--mode',
        mode,
        '--port',
        BROKER,
        '--client-id',
        f'bridge-{tag}',
        '--seconds',
        10,
        '--crash-after-commit',
    )
    time.sleep(1.5)
    dev = run(
        'mqtt_device.py', '--port', BROKER, '--client-id', f'camera-{tag}', '--event-id', f'E-{tag}', '--seconds', 5
    )
    r1 = finish(c1)
    r2 = run('mqtt_consumer.py', '--mode', mode, '--port', BROKER, '--client-id', f'bridge-{tag}', '--seconds', 5)
    b.stop()
    return {'device': dev, 'consumer': [r1, r2]}


def s3(mode, tag):
    b = Broker(OUT / f'broker_{tag}', persistence=False)
    d1 = run(
        'mqtt_device.py',
        '--port',
        BROKER,
        '--client-id',
        f'camera-{tag}',
        '--event-id',
        f'E-{tag}',
        '--seconds',
        5,
        '--kill-after-publish',
    )
    b.start()
    cons = spawn('mqtt_consumer.py', '--mode', mode, '--port', BROKER, '--client-id', f'bridge-{tag}', '--seconds', 8)
    time.sleep(1.5)
    d2 = run('mqtt_device.py', '--port', BROKER, '--client-id', f'camera-{tag}', '--seconds', 5)
    c = finish(cons)
    b.stop()
    return {'device': [d1, d2], 'consumer': [c]}


def s4(mode, tag, crash):
    b = Broker(OUT / f'broker_{tag}', persistence=True)
    b.start()
    c0 = run('mqtt_consumer.py', '--mode', mode, '--port', BROKER, '--client-id', f'bridge-{tag}', '--subscribe-only')
    dev = run(
        'mqtt_device.py', '--port', BROKER, '--client-id', f'camera-{tag}', '--event-id', f'E-{tag}', '--seconds', 5
    )
    b.stop(signal.SIGKILL if crash else signal.SIGTERM)
    b.start()
    c1 = run('mqtt_consumer.py', '--mode', mode, '--port', BROKER, '--client-id', f'bridge-{tag}', '--seconds', 5)
    b.stop()
    return {'device': dev, 'consumer': [c0, c1], 'broker_stop': 'SIGKILL' if crash else 'SIGTERM'}


PLAN = [
    ('S1', 'naive', s1),
    ('S1', 'idempotent', s1),
    ('S2', 'naive', s2),
    ('S2', 'idempotent', s2),
    ('S3', 'naive', s3),
    ('S3', 'idempotent', s3),
    ('S4', 'naive', lambda m, t: s4(m, t, crash=True)),
    ('S4', 'idempotent', lambda m, t: s4(m, t, crash=True)),
    ('S4-control', 'naive', lambda m, t: s4(m, t, crash=False)),
    ('S4-control', 'idempotent', lambda m, t: s4(m, t, crash=False)),
]
mosq_version = subprocess.run([a.mosquitto, '-h'], capture_output=True, text=True).stdout.splitlines()[0]
results = []
for name, mode, fn in PLAN:
    tag = f'{name.lower()}-{mode}'
    reset_db(mode)
    detail = fn(mode, tag)
    events, alerts = counts()
    row = {
        'scenario': name,
        'consumer': mode,
        'events_detected': 1,
        'event_rows': events,
        'alerts': alerts,
        'event_lost': events == 0,
        'duplicate_alerts': max(0, alerts - 1),
    }
    results.append({**row, 'detail': detail})
    print(json.dumps(row), flush=True)
report = {
    'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    'versions': {
        'mosquitto': mosq_version,
        'paho-mqtt': version('paho-mqtt'),
        'psycopg': version('psycopg'),
        'python': platform.python_version(),
    },
    'results': results,
}
(OUT / 'baseline_results.json').write_text(json.dumps(report, indent=1) + '\n')
print('wrote', OUT / 'baseline_results.json')
