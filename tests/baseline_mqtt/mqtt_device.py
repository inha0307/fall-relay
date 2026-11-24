"""Baseline device side: publishes one fall event with MQTT QoS 1 over a persistent session (no local outbox).

--event-id E          publish this event once (omit to only reconnect and let the client resend its session state)
--kill-after-publish  SIGKILL this process right after publish() returns
The client resends unacknowledged QoS 1 messages itself after a reconnect (paho clean_start=False)."""

import argparse
import json
import os
import signal
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

ap = argparse.ArgumentParser()
ap.add_argument('--port', type=int, required=True)
ap.add_argument('--client-id', default='camera-1')
ap.add_argument('--event-id')
ap.add_argument('--seconds', type=float, default=8.0)
ap.add_argument('--kill-after-publish', action='store_true')
a = ap.parse_args()

acked = threading.Event()


def log(**kw):
    print(json.dumps({'t': round(time.time(), 3), 'who': 'device', **kw}), flush=True)


def on_connect(client, userdata, flags, reason_code, properties):
    log(event='connected', session_present=bool(flags.session_present), reason=str(reason_code))


def on_disconnect(client, userdata, flags, reason_code, properties):
    log(event='disconnected', reason=str(reason_code))


def on_publish(client, userdata, mid, reason_code, properties):
    log(event='puback', mid=mid, reason=str(reason_code))
    acked.set()


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=a.client_id, protocol=mqtt.MQTTv5)
c.on_connect, c.on_disconnect, c.on_publish = on_connect, on_disconnect, on_publish
c.reconnect_delay_set(min_delay=1, max_delay=2)
props = Properties(PacketTypes.CONNECT)
props.SessionExpiryInterval = 3600
c.connect_async('127.0.0.1', a.port, clean_start=False, properties=props)
c.loop_start()
time.sleep(1.0)
if a.event_id:
    info = c.publish('fall/events', json.dumps({'event_id': a.event_id, 'detected_at': time.time()}), qos=1)
    log(event='published', event_id=a.event_id, rc=info.rc, mid=info.mid, connected=c.is_connected())
    if a.kill_after_publish:
        log(event='sigkill_after_publish')
        os.kill(os.getpid(), signal.SIGKILL)
    acked.wait(a.seconds)
    log(event='result', puback_received=acked.is_set())
else:
    time.sleep(a.seconds)
c.disconnect()
c.loop_stop()
log(event='exit')
