"""NTU RGB+D readers shared by the training scripts (skeleton files and AVI headers)."""

import struct
from collections import defaultdict
from pathlib import Path

import numpy as np

# The 12 body joints of the model, as 0-based NTU joint indices:
# shoulders, elbows, wrists, hips, knees, ankles (left, right).
JOINTS = np.array([4, 8, 5, 9, 6, 10, 12, 16, 13, 17, 14, 18])


def read_skeleton(path):
    """Return frame dictionaries indexed by persistent NTU body ID."""
    frames = []
    with Path(path).open() as stream:
        count = int(stream.readline())
        for _ in range(count):
            bodies = {}
            for _ in range(int(stream.readline())):
                metadata = stream.readline().split()
                if len(metadata) != 10:
                    raise ValueError('Invalid body metadata')
                identity = metadata[0]  # Preserve 64-bit ID as a string.
                if identity in bodies:
                    raise ValueError('Duplicate body ID within frame')
                n = int(stream.readline())
                if n != 25:
                    raise ValueError(f'Expected25joints, got{n}')
                values = [list(map(float, stream.readline().split())) for _ in range(n)]
                if any(len(v) != 12 for v in values):
                    raise ValueError('Invalid joint fields')
                bodies[identity] = np.asarray(values, dtype=np.float64)
            frames.append(bodies)
        if stream.read().strip():
            raise ValueError('Trailing skeleton content')
    return frames


def choose_track(frames):
    """Coverage heuristic only; not proof of actor identity or RGB association."""
    scores = defaultdict(int)
    for frame in frames:
        for identity, values in frame.items():
            joints = values[JOINTS]
            scores[identity] += int(np.sum((joints[:, 11] == 2) & np.isfinite(joints[:, :3]).all(axis=1)))
    if not scores:
        raise ValueError('No bodies')
    identity = max(scores, key=lambda key: (scores[key], key))
    return identity, dict(scores)


def avi_metadata(data):
    if data[:4] != b'RIFF' or data[8:12] != b'AVI ':
        raise ValueError('Expected RIFF AVI')
    result = {}

    def chunks(start, end):
        cursor = start
        while cursor + 8 <= min(end, len(data)):
            kind = data[cursor : cursor + 4]
            size = struct.unpack_from('<I', data, cursor + 4)[0]
            payload = cursor + 8
            if kind == b'LIST' and data[payload : payload + 4] in [b'hdrl', b'strl']:
                chunks(payload + 4, min(payload + size, len(data)))
            elif kind == b'avih' and size >= 56 and payload + 56 <= len(data):
                values = struct.unpack_from('<14I', data, payload)
                result.update(
                    frame_count=values[4], width=values[8], height=values[9], microseconds_per_frame=values[0]
                )
            elif (
                kind == b'strh' and size >= 48 and payload + 48 <= len(data) and data[payload : payload + 4] == b'vids'
            ):
                scale, rate = struct.unpack_from('<II', data, payload + 20)
                if not scale:
                    raise ValueError('Zero stream time scale')
                result.update(
                    fps=rate / scale,
                    stream_scale=scale,
                    stream_rate=rate,
                    stream_frame_count=struct.unpack_from('<I', data, payload + 32)[0],
                )
            cursor = payload + size + (size % 2)

    chunks(12, len(data))
    if not all(result.get(k, 0) > 0 for k in ['width', 'height', 'fps', 'frame_count']):
        raise ValueError('Missing or invalid AVI metadata in header prefix')
    if result['frame_count'] != result['stream_frame_count']:
        raise ValueError('AVI main/stream frame counts differ')
    return result
