#!/usr/bin/env python3
"""Alert state races, durable catch-up, delivery ownership and scoped API/media."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from _pg import Schema
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
from alert_store import AlertStore, AlertError
from test_edge_events import messages
from test_hardware import config, call
from video_bridge import create_app


def main(root):
    cfg = config(root)
    cfg['features'] = {'alerts': True}
    cfg['require_media_token'] = True
    cfg['require_camera_tokens'] = True
    cfg['clip_dir'] = str(root / 'clips')
    app = create_app(cfg)
    edge = app.state.edge_events
    store = AlertStore(app.state.store)
    receipt = edge.receive(messages(time.time())[0])
    id = receipt['alert_id']
    detail = json.loads(
        store.db.execute("SELECT detail FROM alert_log WHERE alert_id=? AND action='created'", (id,)).fetchone()[0]
    )
    assert detail['delivery_enabled'] is True
    assert len(store.snapshot(['1'])['alerts']) == 1 and not store.snapshot([])['alerts']

    def rejected(fn, status):
        try:
            fn()
        except AlertError as exc:
            assert exc.status == status
            return
        raise AssertionError('operation accepted')

    rejected(lambda: store.get(id, ['2']), 404)
    delivery = store.delivery(id, ['1'], 'U1', 'D1')
    rejected(lambda: store.transition(id, ['1'], 'U2', 'received', {'delivery_id': delivery, 'device_id': 'D1'}), 403)
    row = store.transition(id, ['1'], 'U1', 'received', {'delivery_id': delivery, 'device_id': 'D1'})
    assert row['state'] == 'delivered'

    def ack(actor):
        try:
            return store.transition(id, ['1'], actor, 'ack', {})['acknowledged_by']
        except AlertError as exc:
            assert exc.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(ack, ['U1', 'U2']))
    assert sum(x is not None for x in result) == 1
    # Late delivery never rewinds acknowledgment.
    assert (
        store.transition(id, ['1'], 'U1', 'received', {'delivery_id': delivery, 'device_id': 'D1'})['state']
        == 'acknowledged'
    )
    rejected(lambda: store.transition(id, ['1'], 'U1', 'resolve', {'label': 'invalid'}), 422)
    row = store.transition(id, ['1'], 'U1', 'resolve', {'label': 'fa_sitting', 'note': 'synthetic review'})
    assert (
        row['state'] == 'resolved'
        and app.state.store._db.execute(
            'SELECT review_status FROM events WHERE id=?', (receipt['event_id'],)
        ).fetchone()[0]
        == 'fa_sitting'
    )
    assert (
        store.transition(id, ['1'], 'U1', 'received', {'delivery_id': delivery, 'device_id': 'D1'})['state']
        == 'resolved'
    )
    # >500 durable records must be paged, never capped to a single page.
    with store.lock, store.db:
        for i in range(1101):
            store.db.execute(
                "INSERT INTO alert_log(alert_id,at,action,to_state) VALUES (?,?,'send_failed','resolved')",
                (id, time.time()),
            )
    cursor = 0
    seen = []
    while True:
        page = store.changes(['1'], cursor)
        if not page:
            break
        seen += page
        cursor = page[-1]['cursor']
    assert len(seen) == 1105 and len({r['cursor'] for r in seen}) == len(seen)
    assert not store.changes(['2'], 0)
    # Synthetic bytes test routing/ownership, not video decoding (covered by clip recorder tests).
    app.state.channels['1'].push_frame(b'\xff\xd8synthetic-frame')
    clip = root / 'clips' / f'fall_{receipt["event_id"]:08d}_synthetic.webm'
    clip.write_bytes(b'synthetic-clip-bytes')
    headers = {
        'X-Write-Token': 'hardware-test-write',
        'X-Alert-Actor': 'U1',
        'X-Alert-Role': 'responder',
        'X-Alert-Cameras': '1',
    }

    async def api():
        code, _ = await call(app, 'GET', '/api/alerts')
        assert code == 403
        code, data = await call(app, 'GET', '/api/alerts', headers=headers)
        assert code == 200 and len(data['alerts']) == 1
        code, _ = await call(app, 'GET', f'/api/alerts/{id}', headers={**headers, 'X-Alert-Cameras': '2'})
        assert code == 404
        code, _ = await call(
            app, 'POST', f'/api/alerts/{id}/ack', headers={**headers, 'X-Alert-Role': 'viewer'}, body={}
        )
        assert code == 403
        code, _ = await call(app, 'GET', '/api/events', headers={'X-Write-Token': ''})
        assert code == 403
        code, _ = await call(app, 'GET', '/api/events', headers={'X-Write-Token': 'hardware-test-write'})
        assert code == 200
        code, _ = await call(app, 'GET', f'/api/alerts/{id}/frame', headers={**headers, 'X-Alert-Cameras': '2'})
        assert code == 404
        code, _ = await call(app, 'GET', f'/api/alerts/{id}/clip', headers={**headers, 'X-Alert-Cameras': '2'})
        assert code == 404
        code, raw, _ = await call(app, 'GET', f'/api/alerts/{id}/frame', headers=headers, json_response=False)
        assert code == 200 and raw == b'\xff\xd8synthetic-frame'
        code, raw, _ = await call(app, 'GET', f'/api/alerts/{id}/clip', headers=headers, json_response=False)
        assert code == 200 and raw == b'synthetic-clip-bytes'
        code, raw, _ = await call(
            app, 'GET', f'/api/alerts/{id}/clip', headers={**headers, 'Range': 'bytes=0-8'}, json_response=False
        )
        assert code == 206 and raw == b'synthetic'
        code, _ = await call(app, 'GET', '/api/alerts/stream', query='device=D1&after=bad', headers=headers)
        assert code == 422
        code, _ = await call(
            app, 'GET', '/api/alerts/stream', query='device=D1', headers={**headers, 'X-Alert-Cameras': '*'}
        )
        assert code == 403

    asyncio.run(api())
    print(
        'PASS atomic first-ack, late receipts, resolve/review, 1105-log catch-up and camera/role/read/media authorization'
    )


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as d, Schema():
        main(Path(d))
