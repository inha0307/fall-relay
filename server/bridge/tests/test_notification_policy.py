"""Notification policy, version 2: rows written under version 1 read back unchanged; a recent event is live at once;
a settled alert never changes; receipts and acknowledgements leave the mode alone; old and new SSE clients."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import asyncio
import copy
import json
import uuid

from fastapi import Request
from _pg import Schema, url
from alert_store import AlertStore
from camera_registry import CameraRegistry
from edge_event_store import EdgeEventStore
from event_store import EventStore
from notification_policy import VERSION
from routers.alerts import alerts_router


async def stream_snapshot(events, policy):
    async def receive():
        await asyncio.Event().wait()

    query = 'device=policy-fixture' + (f'&policy={VERSION}' if policy else '&policy=1')
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/api/alerts/stream',
            'query_string': query.encode(),
            'headers': [(b'x-alert-actor', b'test'), (b'x-alert-role', b'admin'), (b'x-alert-cameras', b'*')],
        },
        receive,
    )
    router = alerts_router(AlertStore(events), lambda _: True, True)
    endpoint = next(route.endpoint for route in router.routes if route.path == '/api/alerts/stream')
    response = await endpoint(request)
    iterator = response.body_iterator
    chunk = await anext(iterator)
    await iterator.aclose()
    return json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith('data: ')))


def stored_policy(events):
    """Rows stored by version 1 (its modes, reasons and clock-derived decision time) are read back unchanged."""
    db = events._db
    with db:
        for index, (mode, reason, state) in enumerate(
            [
                ('manual_review', 'event_too_old', 'pending'),
                ('manual_review', 'clock_unreliable', 'resolved'),
                ('live', None, 'acknowledged'),
            ],
            1,
        ):
            event_id = db.execute(
                "INSERT INTO events(camera_id,started_at,max_score,source,late_arrival,clock_usable,decided_at,clock_offset_s,received_at) VALUES ('1',100,.97,'edge_event',?,?,100,0,1000)",
                (int(reason == 'event_too_old'), int(reason != 'clock_unreliable')),
            ).lastrowid
            db.execute(
                "INSERT INTO alerts(event_id,type,camera_id,created_at,state,acknowledged_by,acknowledged_at,resolution,note,notification_mode,suppression_reason) VALUES (?,'fall','1',1000,?,'operator',1001,?,'keep this note',?,?)",
                (event_id, state, 'uncertain' if state == 'resolved' else None, mode, reason),
            )
    alerts = AlertStore(events)
    old = {r['id']: r for r in alerts.snapshot(None)['alerts']}
    assert old[1]['notification_mode'] == 'manual_review' and old[1]['suppression_reason'] == 'event_too_old'
    assert old[1]['event_decided_at'] == 100 and old[1]['policy_version'] is None
    assert old[2]['notification_mode'] == 'manual_review' and old[2]['suppression_reason'] == 'clock_unreliable'
    assert old[2]['state'] == 'resolved' and old[2]['note'] == 'keep this note' and old[2]['resolution'] == 'uncertain'
    assert old[2]['event_decided_at'] is None
    assert old[3]['notification_mode'] == 'live' and old[3]['state'] == 'acknowledged'
    # Version-1 manual-review rows have no classification time, so they never get a reminder.
    assert EdgeEventStore(events, 120).tick(now=10**9) == {'settled': 0, 'reminded': 0}
    return alerts


def scenarios(events):
    alerts = stored_policy(events)
    edge = EdgeEventStore(events, 120, delivery_enabled=True)
    fixtures = json.loads((FIXTURES / 'edge_event_v1.json').read_text())
    created = []
    # A recent open is live at once whatever the clock sample says; the age comes from the device's boot clock.
    for age_ms, rtt, backlog, expected in [
        (10_000, 0.01, 0, 'live'),
        (120_000, 0.01, 5, 'live'),
        (120_001, 0.01, 0, 'live'),  # late, but nothing else is queued: the open is the latest state
        (120_001, 0.01, 1, 'classifying'),
        (10_000, 5, 2, 'live'),  # uncalibrated clock no longer demotes
        (10_000, 0.01, None, 'live'),  # no backlog count (older device), recent
    ]:
        run = uuid.uuid4().hex
        first = copy.deepcopy(fixtures[0])
        first.update(
            run_id=run,
            event_uid=run + ':1',
            decided_at=1000 - age_ms / 1000,
            sent_at=1000,
            clock={'offset_s': 0.0, 'rtt_s': rtt},
        )
        transport = {'event_age_ms': age_ms}
        if backlog is not None:
            transport['event_backlog'] = backlog
        receipt = edge.receive(first, now=1000, transport=transport)
        id = receipt['alert_id']
        result = alerts.get(id, None)
        assert result['notification_mode'] == expected, (age_ms, rtt, backlog, result['notification_mode'])
        assert result['suppression_reason'] is None
        assert result['event_decided_at'] == 1000 - age_ms / 1000, 'the device age dates the detection'
        assert (result['clock_usable'] == 1) == (rtt <= 2)
        job = events._db.execute('SELECT state FROM alert_dispatch_jobs WHERE alert_id=?', (id,)).fetchone()[0]
        assert job == 'queued'
        assert edge.receive(first, now=5000, transport=transport)['duplicate']
        update = copy.deepcopy(fixtures[1])
        update.update(
            run_id=run,
            event_uid=run + ':1',
            decided_at=max(1001, first['decided_at']),
            sent_at=5000,
            clock=first['clock'],
        )
        edge.receive(update, now=5000, transport={'event_age_ms': age_ms + 4_000_000, 'event_backlog': 0})
        after = alerts.get(id, None)['notification_mode']
        # A settled alert never changes; a classifying one is settled by the latest state (still open: live).
        assert after == ('live' if expected == 'classifying' else expected), 'policy changed after a delayed update'
        delivery = alerts.delivery(id, None, 'test', 'browser')
        delivered = alerts.transition(id, None, 'test', 'received', {'device_id': 'browser', 'delivery_id': delivery})
        assert delivered['state'] == 'delivered' and delivered['notification_mode'] == after
        created.append(id)
    snapshot = alerts.snapshot(None)
    assert any(a['notification_mode'] == 'manual_review' for a in snapshot['alerts'])
    assert all(a['notification_mode'] == 'live' for a in alerts.snapshot(None, True)['alerts'])
    assert all(row['alert']['notification_mode'] == 'live' for row in alerts.changes(None, 0, True))
    capable = asyncio.run(stream_snapshot(events, True))
    legacy = asyncio.run(stream_snapshot(events, False))
    assert any(a['notification_mode'] == 'manual_review' for a in capable['alerts'])
    assert all(a['notification_mode'] == 'live' for a in legacy['alerts'])
    assert any(a['late_arrival'] == 1 for a in legacy['alerts']), 'late live alerts reach legacy clients'
    print(
        'PASS stored version-1 rows read back, immediate classification of recent events, settled alerts unchanged, receipts and old/new SSE clients'
    )
    return {
        id: (alerts.get(id, None)['notification_mode'], alerts.get(id, None)['classification_reason']) for id in created
    }


def main():
    with Schema():
        events = EventStore(url())
        CameraRegistry([{'id': '1'}], connection=events._db, lock=events._lock)
        expected = scenarios(events)
        events.close()
        restored = EventStore(url())
        alerts = AlertStore(restored)
        assert all(
            (alerts.get(id, None)['notification_mode'], alerts.get(id, None)['classification_reason']) == value
            for id, value in expected.items()
        )
        restored.close()
    print('PASS classifications survive a restart of the store')


if __name__ == '__main__':
    main()
