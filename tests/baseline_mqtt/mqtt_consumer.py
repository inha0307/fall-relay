"""Baseline server side: an MQTT subscriber that stores each fall event and creates its alert in PostgreSQL.

--mode naive        one event row and one alert per received message
--mode idempotent   the event identifier is the primary key; a repeated identifier creates nothing
The message is acknowledged (PUBACK, manual_ack) only after the database transaction has committed.
--crash-after-commit kills this process (SIGKILL) between the commit and the acknowledgement of the first message.
--subscribe-only connects, subscribes and disconnects, leaving a persistent session at the broker."""

import argparse
import json
import os
import signal
import time

import paho.mqtt.client as mqtt
import psycopg
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

ap = argparse.ArgumentParser()
ap.add_argument('--mode', choices=['naive', 'idempotent'], required=True)
ap.add_argument('--port', type=int, required=True)
ap.add_argument('--client-id', default='bridge')
ap.add_argument('--seconds', type=float, default=5.0)
ap.add_argument('--crash-after-commit', action='store_true')
ap.add_argument('--subscribe-only', action='store_true')
a = ap.parse_args()

db = psycopg.connect(os.environ['BASELINE_DATABASE_URL'], autocommit=True)


def log(**kw):
    print(json.dumps({'t': round(time.time(), 3), 'who': 'consumer', **kw}), flush=True)


def on_connect(client, userdata, flags, reason_code, properties):
    log(event='connected', session_present=bool(flags.session_present), reason=str(reason_code))
    client.subscribe('fall/events', qos=1)


def on_subscribe(client, userdata, mid, reason_codes, properties):
    log(event='subscribed')


def on_message(client, userdata, m):
    ev = json.loads(m.payload)
    with db.transaction():
        if a.mode == 'naive':
            db.execute('INSERT INTO events(event_id) VALUES (%s)', (ev['event_id'],))
            db.execute('INSERT INTO alerts(event_id) VALUES (%s)', (ev['event_id'],))
            created = True
        else:
            row = db.execute(
                'INSERT INTO events(event_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING event_id', (ev['event_id'],)
            ).fetchone()
            created = row is not None
            if created:
                db.execute('INSERT INTO alerts(event_id) VALUES (%s)', (ev['event_id'],))
    log(event='stored', event_id=ev['event_id'], new_alert=created, dup_flag=bool(m.dup), mid=m.mid)
    if a.crash_after_commit:
        log(event='sigkill_before_puback')
        os.kill(os.getpid(), signal.SIGKILL)
    client.ack(m.mid, m.qos)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=a.client_id, protocol=mqtt.MQTTv5, manual_ack=True)
c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
props = Properties(PacketTypes.CONNECT)
props.SessionExpiryInterval = 3600
c.connect('127.0.0.1', a.port, clean_start=False, properties=props)
c.loop_start()
time.sleep(1.0 if a.subscribe_only else a.seconds)
c.disconnect()
c.loop_stop()
log(event='exit')
