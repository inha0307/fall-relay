"""Trusted web-server API. Actor and scope must be supplied by the session proxy."""

import asyncio
import json
import re
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response, FileResponse
from alert_store import AlertError
from starlette.concurrency import run_in_threadpool
from notification_policy import VERSION


def alerts_router(store, authorized, enabled, channels=None, recorder=None, clip_dir=None, watchdog=None):
    router = APIRouter()

    def context(request):
        if not enabled:
            raise AlertError(503, 'explicit alerts disabled')
        if not authorized(request):
            raise AlertError(403, 'server token required')
        actor = request.headers.get('x-alert-actor', '')
        role = request.headers.get('x-alert-role', '')
        scope = request.headers.get('x-alert-cameras', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', actor) or role not in ('admin', 'responder', 'viewer'):
            raise AlertError(403, 'trusted actor required')
        cameras = None if scope == '*' and role != 'responder' else scope.split(',') if scope else []
        if cameras is not None and (
            len(cameras) > 100 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', v) for v in cameras)
        ):
            raise AlertError(403, 'invalid camera scope')
        return actor, role, cameras

    def error(exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)

    @router.get('/api/alerts/status')
    def status(request: Request):
        if not authorized(request):
            return JSONResponse({'error': 'server token required'}, status_code=403)
        return {'enabled': enabled, 'notification_policy_version': VERSION}

    @router.get('/api/alerts')
    def listing(request: Request):
        try:
            _, _, scope = context(request)
            return store.snapshot(scope)
        except AlertError as exc:
            return error(exc)

    @router.get('/api/alerts/metrics')
    def metrics(request: Request):
        try:
            _, _, scope = context(request)
            with store.lock:
                clause, args = store.scope(scope)
                counts = [
                    dict(r)
                    for r in store.db.execute(
                        'SELECT a.state,COUNT(*) AS count FROM alerts a WHERE ' + clause + ' GROUP BY a.state', args
                    )
                ]
                queue = [
                    dict(r)
                    for r in store.db.execute(
                        'SELECT j.state,COUNT(*) AS count FROM alert_dispatch_jobs j JOIN alerts a ON a.id=j.alert_id WHERE '
                        + clause
                        + ' GROUP BY j.state',
                        args,
                    )
                ]
            return {
                'server_time': time.time(),
                'states': counts,
                'dispatch': queue,
                'watchdog_enabled': watchdog is not None,
                'watchdog_error': watchdog.error if watchdog else None,
                'devices': [
                    row
                    for row in (watchdog.last_status if watchdog else [])
                    if scope is None or row['camera_id'] in scope
                ],
            }
        except AlertError as exc:
            return error(exc)

    @router.get('/api/alerts/stream')
    async def stream(request: Request):
        try:
            actor, role, scope = context(request)
            raw = request.headers.get('last-event-id') or request.query_params.get('after')
            if raw is not None and (not raw.isdigit() or len(raw) > 18):
                raise AlertError(422, 'invalid cursor')
            device = request.query_params.get('device', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device):
                raise AlertError(422, 'device required')
            after = int(raw) if raw else None
            # Old tabs do not know how to suppress manual-review reminders. Their
            # stream omits these cases; capable clients still get them for review.
            live_only = request.query_params.get('policy') != str(VERSION)
        except AlertError as exc:
            return error(exc)

        def present(alert):
            if role in ('admin', 'responder'):
                alert['delivery_id'] = store.delivery(alert['id'], scope, actor, device)
            return alert

        def encode(event, data, cursor):
            return f'id: {cursor}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

        async def generate():
            nonlocal after
            deadline = time.monotonic() + 20
            snapshot = await run_in_threadpool(store.snapshot, scope, live_only)
            # Snapshots also repair future cursors after DB restore. Logs are retained without pruning.
            if after is None or after > snapshot['cursor']:
                after = snapshot['cursor']
                snapshot['alerts'] = await run_in_threadpool(lambda: [present(a) for a in snapshot['alerts']])
                yield encode('snapshot', snapshot, after)
            resync = raw is not None
            ping = 0
            while time.monotonic() < deadline and not await request.is_disconnected():
                changes = await run_in_threadpool(store.changes, scope, after, live_only)
                for change in changes:
                    if time.monotonic() >= deadline or await request.is_disconnected():
                        return
                    after = change['cursor']
                    change['alert'] = await run_in_threadpool(present, change['alert'])
                    yield encode('alert', change, after)
                if len(changes) == 500:
                    continue
                if resync:
                    snapshot = await run_in_threadpool(store.snapshot, scope, live_only)
                    after = snapshot['cursor']
                    snapshot['alerts'] = await run_in_threadpool(lambda: [present(a) for a in snapshot['alerts']])
                    yield encode('snapshot', snapshot, after)
                    resync = False
                if time.monotonic() - ping >= 3:
                    yield ': heartbeat\n\nevent: heartbeat\ndata: {}\n\n'
                    ping = time.monotonic()
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}
        )

    @router.get('/api/alerts/{id}/frame')
    def frame(id: int, request: Request):
        try:
            _, _, scope = context(request)
            alert = store.get(id, scope)
            channel = channels.get(alert['camera_id']) if channels else None
            jpeg = channel.latest_frame()[1] if channel else None
            if not jpeg:
                raise AlertError(404, 'live frame unavailable')
            return Response(jpeg, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
        except AlertError as exc:
            return error(exc)

    @router.get('/api/alerts/{id}/clip')
    def clip(id: int, request: Request):
        try:
            _, _, scope = context(request)
            alert = store.get(id, scope)
            name = (
                recorder.clip_name(alert['event_id'])
                if recorder and alert['event_id'] and recorder.clip_status(alert['event_id']) == 'ready'
                else None
            )
            if not name or not clip_dir:
                raise AlertError(404, 'clip unavailable')
            from pathlib import Path

            path = (Path(clip_dir) / name).resolve()
            if path.parent != Path(clip_dir).resolve() or not path.is_file():
                raise AlertError(404, 'clip unavailable')
            return FileResponse(path, headers={'Cache-Control': 'no-store'})
        except AlertError as exc:
            return error(exc)

    @router.get('/api/alerts/{id}')
    def detail(id: int, request: Request):
        try:
            _, _, scope = context(request)
            return store.get(id, scope)
        except AlertError as exc:
            return error(exc)

    @router.post('/api/alerts/{id}/{action}')
    async def transition(id: int, action: str, request: Request):
        try:
            actor, role, scope = context(request)
            if role not in ('admin', 'responder'):
                raise AlertError(403, 'administrator or responder role required')
            raw = await request.body()
            if len(raw) > 8192:
                raise AlertError(413, 'body too large')
            try:
                body = json.loads(raw)
            except ValueError:
                raise AlertError(422, 'invalid JSON')
            if not isinstance(body, dict):
                raise AlertError(422, 'object required')
            return await run_in_threadpool(store.transition, id, scope, actor, action, body)
        except AlertError as exc:
            return error(exc)

    return router
