"""Registration acknowledgement, periodic refresh, lost registry, and failed delivery."""

import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from result_transport import ResultTransport
from video_source import VideoSource


class Reply:
    def __init__(self, status=200, **body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(self.status_code)


class Session:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def post(self, url, **kwargs):
        self.sent.append(kwargs['json'].copy())
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


identity = {'run_id': 'a' * 32}
ack = Reply(ok=True, model_run_id=identity['run_id'])
t = ResultTransport(identity)
s = Session([OSError('offline'), ack, ack, ack, Reply(409, code='model_identity_required'), ack, ack])
with patch('result_transport.time.monotonic', return_value=10):
    try:
        t.send(s, '', {}, {})
        raise AssertionError('network failure ignored')
    except OSError:
        pass
    assert t.registered_at is None
    t.send(s, '', {}, {})
    t.send(s, '', {}, {})
assert 'model_identity' in s.sent[1] and 'model_identity' not in s.sent[2]
with patch('result_transport.time.monotonic', return_value=71):
    t.send(s, '', {}, {})
assert 'model_identity' in s.sent[3]
with patch('result_transport.time.monotonic', return_value=72):
    t.send(s, '', {}, {})
    t.send(s, '', {}, {})
assert 'model_identity' not in s.sent[4] and 'model_identity' in s.sent[5]
assert 'model_identity' not in s.sent[6]
for reply in (Reply(ok=True), Reply(ok=True, model_run_id='b' * 32)):
    t = ResultTransport(identity)
    s = Session([reply, ack])
    t.send(s, '', {}, {})
    assert t.registered_at is None
    t.send(s, '', {}, {})
    assert all('model_identity' in body for body in s.sent)
t = ResultTransport(identity, full_identity=True)
s = Session([ack, ack])
t.send(s, '', {}, {})
t.send(s, '', {}, {})
assert all('model_identity' in body for body in s.sent)
print('PASS failed send, acknowledgement, refresh, lost registry recovery, legacy server and baseline')

# Exercise actual decoding, FPS timestamps, resize, EOF tail and cancellation with a tiny file.
import cv2
import numpy as np

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'source.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 20, (64, 48))
    assert writer.isOpened()
    for i in range(4):
        writer.write(np.full((48, 64, 3), 30 * i, np.uint8))
    writer.release()
    source = VideoSource(path, (128, 72), tail_s=0.1)
    stop = threading.Event()
    frames = list(source.frames(stop))
    assert source.frames_read == 4 and len(frames) == 6
    assert all(f[2].shape == (72, 128, 3) for f in frames)
    assert all(b[1] - a[1] == 50_000_000 for a, b in zip(frames, frames[1:]))
    assert np.array_equal(frames[-1][2], frames[-2][2])
    source.stop()
    source = VideoSource(path, (128, 72))
    stop.set()
    assert not list(source.frames(stop))
    source.stop()
print('PASS real video decode, timestamps, scaling, tail and cancellation')
