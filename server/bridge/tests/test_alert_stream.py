#!/usr/bin/env python3
"""SSE production iterator: snapshot, >500 logs, resume, scope and restart."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from _pg import Schema
import asyncio
from pathlib import Path
import tempfile
import time
from fastapi import Request
from test_hardware import config
from test_edge_events import messages
from video_bridge import create_app
from routers.alerts import alerts_router
from alert_store import AlertStore


async def run(root):
    cfg = config(root)
    cfg['features'] = {'alerts': True}
    cfg['require_media_token'] = True
    cfg['require_camera_tokens'] = True
    app = create_app(cfg)
    receipt = app.state.edge_events.receive(messages(time.time())[0])
    id = receipt['alert_id']
    db = app.state.store._db
    with app.state.store._lock, db:
        for _ in range(1101):
            db.execute(
                "INSERT INTO alert_log(alert_id,at,action,to_state) VALUES (?,?,'send_failed','pending')",
                (id, time.time()),
            )

    async def open_stream(app, query, scope='1'):
        async def receive():
            await asyncio.Event().wait()

        request = Request(
            {
                'type': 'http',
                'method': 'GET',
                'path': '/api/alerts/stream',
                'query_string': query.encode(),
                'headers': [
                    (b'x-write-token', b'hardware-test-write'),
                    (b'x-alert-actor', b'U1'),
                    (b'x-alert-role', b'responder'),
                    (b'x-alert-cameras', scope.encode()),
                ],
            },
            receive,
        )
        router = alerts_router(
            AlertStore(app.state.store), lambda req: req.headers.get('x-write-token') == 'hardware-test-write', True
        )
        endpoint = next(r.endpoint for r in router.routes if getattr(r, 'path', '') == '/api/alerts/stream')
        response = await endpoint(request)
        assert response.status_code == 200
        return response.body_iterator

    iterator = await open_stream(app, 'device=D1')
    snapshot = await anext(iterator)
    assert 'event: snapshot' in snapshot and 'delivery_id' in snapshot
    await iterator.aclose()
    iterator = await open_stream(app, 'device=D1&after=0')
    cursors = []
    async for chunk in iterator:
        if 'event: alert' in chunk:
            cursors.append(int(chunk.split('\n')[0].split(': ')[1]))
            if len(cursors) == 507:
                break
    await iterator.aclose()
    assert len(cursors) == 507 and len(set(cursors)) == 507
    # A reconnect after a slow/interrupted consumer gets every remaining row, then a scoped resync.
    app.state.store.close()
    app = create_app(cfg)
    iterator = await open_stream(app, f'device=D1&after={cursors[-1]}')
    remaining = []
    async for chunk in iterator:
        if 'event: alert' in chunk:
            remaining.append(int(chunk.split('\n')[0].split(': ')[1]))
        if 'event: snapshot' in chunk:
            break
    await iterator.aclose()
    assert len(remaining) == 595 and remaining[0] == 508 and remaining[-1] == 1102
    iterator = await open_stream(app, 'device=D1&after=0', '2')
    chunk = await anext(iterator)
    assert 'event: snapshot' in chunk and '"alerts": []' in chunk
    await iterator.aclose()
    iterator = await open_stream(app, 'device=D1&after=999999')
    chunk = await anext(iterator)
    assert 'event: snapshot' in chunk
    await iterator.aclose()
    app.state.store.close()
    print(
        'PASS SSE initial snapshot, delivery IDs, 1102-log pagination, interrupted resume, restart, camera isolation and future-cursor resync'
    )


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as d, Schema():
        asyncio.run(run(Path(d)))
