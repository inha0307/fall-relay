#!/usr/bin/env python3
"""Unit checks for the event store: events from explicit messages, listing filters, metrics, restart."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import sys
import time

HERE = BRIDGE
sys.path.insert(0, str(HERE))
from camera_registry import CameraRegistry  # noqa: E402
from edge_event_store import EdgeEventStore  # noqa: E402
from edge_fixtures import event_messages  # noqa: E402
from event_store import EventStore  # noqa: E402
from _pg import Schema, url  # noqa: E402


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f'{name}: {detail}')
    print(f'PASS {name}')


def receive_event(edge, camera, run, wall, scores, close=True):
    """One event from explicit messages: open, one update per further score, then close."""
    rows = event_messages(camera, run, wall)
    first, update, last = rows[0], rows[1], rows[-1]
    messages = [{**first, 'max_score': scores[0]}]
    for seq in range(2, len(scores) + 1):
        score = max(scores[:seq])  # max_score is the running maximum of the event
        messages.append(
            {**update, 'message_seq': seq, 'max_score': score, 'decided_sensor_ns': first['decided_sensor_ns'] + seq}
        )
    if close:
        messages.append(
            {
                **last,
                'message_seq': len(messages) + 1,
                'max_score': max(scores),
                'decided_sensor_ns': first['decided_sensor_ns'] + len(messages) + 1,
                'decided_at': wall + 0.2,
            }
        )
    for message in messages:
        edge.receive(message, now=wall + 0.3)


with Schema():
    store = EventStore(url())
    CameraRegistry([{'id': '1'}, {'id': '2'}], connection=store._db, lock=store._lock)
    edge = EdgeEventStore(store)
    t0 = time.time()
    check('no events before the first open message', store.list_events()['total'] == 0)
    receive_event(edge, '1', 'b' * 32, t0, [0.95, 0.99, 0.96])
    receive_event(edge, '2', 'c' * 32, t0, [0.97], close=False)
    listing = store.list_events()
    cam1 = next(e for e in listing['events'] if e['camera_id'] == '1')
    cam2 = next(e for e in listing['events'] if e['camera_id'] == '2')
    check('one event per camera', listing['total'] == 2, listing)
    check('event keeps the maximum score', cam1['max_score'] == 0.99, cam1)
    check(
        'closed event is not ongoing; its duration is decision to close',
        not cam1['ongoing'] and abs(cam1['duration_s'] - 0.2) < 1e-6,
        cam1,
    )
    check('an event without close is ongoing', cam2['ongoing'], cam2)
    receive_event(edge, '1', 'd' * 32, t0 + 4.0, [0.9])
    receive_event(edge, '1', 'e' * 32, t0 + 6.0, [0.9])
    check('each event_uid is its own event', store.list_events(camera='1')['total'] == 3)

    check('camera filter', store.list_events(camera='2')['total'] == 1)
    check('review-status filter: every new event', store.list_events(review_status='new')['total'] == 4)

    store.record_metrics('1', t0, True, True, 29.8, 10.0, [90.0, 100.0, 110.0, 400.0], 100, 4)
    metric = store.metrics(t0 - 1)[0]
    check(
        'metrics store latency percentiles',
        metric['latency_p50_ms'] in (100.0, 110.0) and metric['latency_p95_ms'] == 400.0,
        metric,
    )
    store.record_metrics('1', t0 + 31 * 86400, True, True, 30.0, 10.0, [], 0, 0)
    check('metrics older than the retention window are pruned', len(store.metrics(0)) == 1)
    store.close()

    reopened = EventStore(url())
    check(
        'events survive a restart; an event the device has not closed stays open',
        reopened.list_events()['total'] == 4 and sum(e['ongoing'] for e in reopened.list_events()['events']) == 1,
    )
    reopened.close()
print('All event-store checks passed.')
