"""Fall clip recording for the bridge.

Two frame sources. Skeleton-only edge devices keep the frames on the device and upload only the
window around a detected fall (`submit`). When a camera also pushes its JPEG stream (debug), the
bridge keeps a short ring buffer and cuts the window itself (`add_frame` / `trigger`). Either way the
frames from `pre_s` seconds before to `post_s` seconds after the episode start are written to a WebM
(VP8) file that browsers play directly, next to a JSON sidecar. Encoding uses OpenCV, which is already in the server venv, so no ffmpeg binary is
needed. Without OpenCV the bridge keeps running and only recording is disabled.

File layout in clip_dir:  fall_<event id:08d>_cam<camera>_<local YYYYmmdd_HHMMSS>.webm/.json
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

CLIP_SUFFIX = '.webm'
TMP_SUFFIX = '.tmp.webm'
MAX_GAP_REPEAT_S = 2.0  # a longer video gap is not stretched into frozen frames


class ClipRecorder:
    def __init__(
        self,
        clip_dir: str | Path,
        pre_s: float = 3.0,
        post_s: float = 2.0,
        fps: float = 10.0,
        retention_days: int = 30,
        max_workers: int = 2,
    ):
        self.clip_dir = Path(clip_dir)
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.pre_s = float(pre_s)
        self.post_s = float(post_s)
        self.fps = float(fps)
        self.retention_days = int(retention_days)
        self._lock = threading.Lock()
        self._buffers: dict[str, deque] = {}  # camera_id -> deque[(wall time, jpeg)]
        self._last_kept: dict[str, float] = {}
        self._pending: set[int] = set()  # event ids still collecting or encoding
        self._timers: set[threading.Timer] = set()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='clip')
        try:
            import cv2
            import numpy as np

            self._cv2, self._np, self.error = cv2, np, None
        except ImportError as exc:
            self._cv2 = self._np = None
            self.error = f'OpenCV unavailable: {exc}'
            print(f'[clips] recording disabled: {self.error}', flush=True)

    @property
    def enabled(self) -> bool:
        return self.error is None

    def close(self) -> None:
        with self._lock:
            timers, self._timers = self._timers, set()
        for timer in timers:
            timer.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def add_frame(self, camera_id: str, jpeg: bytes, now: float | None = None) -> None:
        """Keep at most `fps` frames per second and only as long as a clip can need them."""
        if not self.enabled:
            return
        now = time.time() if now is None else now
        with self._lock:
            if now - self._last_kept.get(camera_id, 0.0) < 0.9 / self.fps:
                return
            self._last_kept[camera_id] = now
            buffer = self._buffers.setdefault(camera_id, deque())
            buffer.append((now, jpeg))
            horizon = now - (self.pre_s + self.post_s + 5.0)
            while buffer and buffer[0][0] < horizon:
                buffer.popleft()

    def trigger(
        self, event_id: int, camera_id: str, fall_time: float, track_id: int | None = None, camera_label: str = ''
    ) -> bool:
        """Start a clip for a new episode. Pre-fall frames are copied now, post-fall frames later."""
        if not self.enabled:
            return False
        with self._lock:
            if not self._buffers.get(camera_id):
                return False  # no pushed video for this camera: a skeleton-only device uploads its own clip
            pre = [f for f in self._buffers.get(camera_id, ()) if fall_time - self.pre_s <= f[0] <= fall_time]
            self._pending.add(event_id)
            timer = threading.Timer(
                self.post_s, self._collect, args=(event_id, camera_id, fall_time, track_id, camera_label, pre)
            )
            timer.daemon = True
            self._timers.add(timer)
        timer.start()
        return True

    def submit(
        self,
        event_id: int,
        camera_id: str,
        fall_time: float,
        frames: list,
        track_id: int | None = None,
        camera_label: str = '',
    ) -> bool:
        """Encode a clip from frames an edge device uploaded: [(wall time, jpeg bytes)] in time order."""
        if not self.enabled or not frames:
            return False
        with self._lock:
            if event_id in self._pending:
                return False
            self._pending.add(event_id)
        try:
            self._executor.submit(self._encode, event_id, camera_id, fall_time, track_id, camera_label, list(frames))
        except RuntimeError:
            self._done(event_id)
            return False
        return True

    def _collect(self, event_id, camera_id, fall_time, track_id, camera_label, pre) -> None:
        with self._lock:
            self._timers = {t for t in self._timers if t.is_alive() and t is not threading.current_thread()}
            post = [f for f in self._buffers.get(camera_id, ()) if fall_time < f[0] <= fall_time + self.post_s]
        try:
            self._executor.submit(self._encode, event_id, camera_id, fall_time, track_id, camera_label, pre + post)
        except RuntimeError:  # executor already shut down
            self._done(event_id)

    def _done(self, event_id: int) -> None:
        with self._lock:
            self._pending.discard(event_id)

    def _encode(self, event_id, camera_id, fall_time, track_id, camera_label, frames) -> None:
        cv2, np = self._cv2, self._np
        stamp = datetime.fromtimestamp(fall_time).strftime('%Y%m%d_%H%M%S')
        stem = f'fall_{event_id:08d}_cam{camera_id}_{stamp}'
        final = self.clip_dir / f'{stem}{CLIP_SUFFIX}'
        tmp = self.clip_dir / f'{stem}{TMP_SUFFIX}'
        writer = None
        try:
            # Decode one frame at a time: holding a whole clip decoded costs ~2.7 MB per 720p frame.
            size = None
            source_frames = written = 0
            for index, (ts, jpeg) in enumerate(frames):
                image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    continue
                if writer is None:
                    size = (image.shape[1], image.shape[0])
                    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*'VP80'), self.fps, size)
                    if not writer.isOpened():
                        print(f'[clips] event {event_id}: VP8 writer could not be opened', flush=True)
                        return
                if (image.shape[1], image.shape[0]) != size:
                    image = cv2.resize(image, size)
                source_frames += 1
                # Repeat frames across short gaps so the clip plays back in real time.
                gap = frames[index + 1][0] - ts if index + 1 < len(frames) else 1.0 / self.fps
                for _ in range(max(1, round(min(gap, MAX_GAP_REPEAT_S) * self.fps))):
                    writer.write(image)
                    written += 1
            if writer is None:
                print(f'[clips] event {event_id} cam {camera_id}: no video frames buffered, clip skipped', flush=True)
                return
            writer.release()
            writer = None
            meta = {
                'event_id': event_id,
                'camera_no': camera_id,
                'camera_label': camera_label,
                'fall_time': datetime.fromtimestamp(fall_time, tz=timezone.utc).isoformat(),
                'track_id': track_id,
                'pre_s': self.pre_s,
                'post_s': self.post_s,
                'source_frames': source_frames,
                'frames': written,
            }
            sidecar = self.clip_dir / f'{stem}.json'
            sidecar_tmp = self.clip_dir / f'{stem}.json.tmp'
            sidecar_tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
            os.replace(sidecar_tmp, sidecar)
            os.replace(tmp, final)  # the clip becomes visible only once complete
            print(
                f'[clips] saved {final.name} ({source_frames} frames, {final.stat().st_size / 1e6:.1f} MB)', flush=True
            )
        except Exception as exc:  # recording must never take the bridge down
            print(f'[clips] event {event_id}: recording failed: {exc!r}', flush=True)
        finally:
            if writer is not None:
                writer.release()
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            self._done(event_id)

    def clip_name(self, event_id: int) -> str | None:
        for path in self.clip_dir.glob(f'fall_{event_id:08d}_*{CLIP_SUFFIX}'):
            if not path.name.endswith(TMP_SUFFIX):
                return path.name
        return None

    def clip_status(self, event_id: int) -> str | None:
        """'ready', 'recording' or None (no clip, e.g. no video was connected)."""
        with self._lock:
            if event_id in self._pending:
                return 'recording'
        return 'ready' if self.clip_name(event_id) else None

    def prune(self, now: float | None = None) -> int:
        """Delete clips (and sidecars) older than the retention window. Returns files removed."""
        cutoff = (time.time() if now is None else now) - self.retention_days * 86400
        removed = 0
        for path in self.clip_dir.glob('fall_*'):
            if path.suffix not in ('.webm', '.json') or not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed
