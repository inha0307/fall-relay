import base64
import binascii
import json
import re
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from edge_event_contract import CONTRACT
from edge_event_store import EventConflict


MAX_CLIP_BYTES = 8 * 1024 * 1024
MAX_CLIP_FRAMES = 120
RUN = re.compile('[a-f0-9]{32}')


def parse_clip(payload):
    """Device clip upload -> (camera, run, onset_sensor_ns, [(wall time, jpeg)]); raises ValueError."""
    camera, run, onset, frames = (payload.get(k) for k in ('camera_id', 'run_id', 'onset_sensor_ns', 'frames'))
    if not isinstance(camera, str) or not isinstance(run, str) or not RUN.fullmatch(run):
        raise ValueError('camera_id and run_id required')
    if isinstance(onset, bool) or not isinstance(onset, int) or onset < 0:
        raise ValueError('invalid onset_sensor_ns')
    if not isinstance(frames, list) or not 0 < len(frames) <= MAX_CLIP_FRAMES:
        raise ValueError('1 to 120 frames required')
    out = []
    for frame in frames:
        t = frame.get('t') if isinstance(frame, dict) else None
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not t > 0:
            raise ValueError('invalid frame time')
        try:
            jpeg = base64.b64decode(frame.get('jpeg', ''), validate=True)
        except (binascii.Error, TypeError):
            raise ValueError('invalid frame data') from None
        if not jpeg.startswith(b'\xff\xd8'):
            raise ValueError('frames must be JPEG')
        out.append((float(t), jpeg))
    if any(b[0] < a[0] for a, b in zip(out, out[1:])):
        raise ValueError('frames must be in time order')
    return camera, run, onset, out


def edge_router(service, authorize, schedule_clips, recorder=None, labels=None):
    router = APIRouter()

    @router.get('/api/edge_event/context')
    def context(request: Request, cam: str):
        refused = authorize(cam, request)
        if refused is not None:
            return refused
        return {'camera_id': cam, 'server_time': time.time(), 'contract': CONTRACT, 'event_protocol': 'edge_event'}

    @router.post('/api/edge_event')
    async def event(request: Request):
        raw = await request.body()
        if len(raw) > 32768:
            return JSONResponse({'ok': False, 'error': 'event exceeds 32 KiB'}, status_code=413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get('camera_id'), str):
                raise ValueError('event object and camera_id required')
        except (ValueError, TypeError):
            return JSONResponse({'ok': False, 'error': 'invalid event JSON'}, status_code=422)
        refused = authorize(payload['camera_id'], request)
        if refused is not None:
            return refused
        try:
            result = await run_in_threadpool(service.receive, payload)
        except EventConflict as exc:
            return JSONResponse({'ok': False, 'error': str(exc), 'code': exc.code}, status_code=409)
        except (ValueError, TypeError, OverflowError):
            return JSONResponse({'ok': False, 'error': 'invalid edge event contract'}, status_code=422)
        await run_in_threadpool(schedule_clips)
        return result

    @router.post('/api/edge_clip')
    async def clip(request: Request):
        """Frames around a detected fall from a skeleton-only device. 409 until the event itself is stored
        (the device retries); repeated uploads of a stored clip are acknowledged without re-encoding."""
        raw = await request.body()
        if len(raw) > MAX_CLIP_BYTES:
            return JSONResponse({'ok': False, 'error': 'clip exceeds 8 MiB'}, status_code=413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('clip object required')
            camera, run, onset, frames = parse_clip(payload)
        except (ValueError, TypeError):
            return JSONResponse({'ok': False, 'error': 'invalid clip upload'}, status_code=422)
        refused = authorize(camera, request)
        if refused is not None:
            return refused
        if recorder is None or not recorder.enabled:
            return JSONResponse({'ok': False, 'error': 'clip recording disabled'}, status_code=503)
        event = await run_in_threadpool(service.event_for_clip, camera, run, onset)
        if event is None:
            return JSONResponse(
                {'ok': False, 'error': 'event not stored yet', 'code': 'event_unknown'}, status_code=409
            )
        status = recorder.clip_status(event['id'])
        if status is None:
            label = (labels or {}).get(camera, '')
            if not recorder.submit(event['id'], camera, event['started_at'], frames, event['track_id'], label):
                return JSONResponse({'ok': False, 'error': 'clip could not be queued'}, status_code=503)
            status = 'recording'
        return {'ok': True, 'event_id': event['id'], 'clip_status': status, 'frames': len(frames)}

    return router
