"""Camera-invariant dynamic body features: the seven dyn7 values (features 52-58 of the 59-feature contract).

An earlier feature set used absolute image position and pose extent normalised by image
height. Field diagnostics showed that the extent/aspect pair was far outside the NTU range at
a close, level camera and alone drove persistent fall scores while a person lay still on the
floor.

These seven features replace it with quantities that are
  * independent of camera distance, image size and field of view (ratios of image lengths
    measured on the same person; no division by image height),
  * zero for a person who is not moving, whatever the posture (no absolute shape, aspect
    or image position), so static posture information reaches the TCN only through the
    lifted 3D block,
  * causal and O(1) per frame on the edge: one ring buffer for the primary track.

Layout (index within dyn7; add 52 for the index in the 59-feature vector):
  0 hip_vy_bl           vertical hip-centre velocity, body lengths/s, + = moving down
  1 hip_vx_bl           horizontal hip-centre velocity, body lengths/s
  2 torso_omega         torso tilt angular velocity, rad/s, + = tilting toward horizontal
  3 short_valid         1 when 0-2 come from the previous slot of the same track
  4 hip_dy_1s_bl        hip-centre vertical displacement over the last ~1 s, body lengths
  5 torso_dtilt_1s      torso tilt change over the last ~1 s, rad
  6 long_valid          1 when 4-5 have a ~1 s reference on the same track

Body length is a causal per-track reference: the maximum, over the last ``ref_seconds``,
of torso length plus mean thigh length in pixels. The maximum keeps the upright length
while the body is transiently foreshortened. Torso tilt is the unsigned angle between
the hip-to-shoulder vector and image up (0 upright, pi/2 horizontal). ``roll_deg`` rotates
keypoints about the image centre before any measurement; set it to the negative of the
installed camera's measured image roll (0 for a level camera).
"""

import math
from collections import deque

import numpy as np

VERSION = "dynamic7-v2"
WIDTH = 7
NAMES = ["hip_vy_bl", "hip_vx_bl", "torso_omega", "short_valid", "hip_dy_1s_bl", "torso_dtilt_1s", "long_valid"]
# Joint order follows ncnn_pose.ORDER: R/L shoulder, elbow, wrist, hip, knee, ankle.
SHOULDERS, HIPS, KNEES = (0, 1), (6, 7), (8, 9)
CONF, IMAGE_W, IMAGE_H = 0.2, 640, 360
CLIP = {"velocity": 5.0, "omega": 10.0, "displacement": 3.0}
VY, VX, OMEGA, SHORT_VALID, DY_1S, DTILT_1S, LONG_VALID = range(WIDTH)


def _measure(keypoints, roll_rad):
    """Per-frame geometry: hip centre (px), torso tilt (rad) or nan, body length (px) or nan."""
    k = np.asarray(keypoints, np.float64)
    ok = np.isfinite(k).all(axis=1) & (k[:, 2] >= CONF)
    ok &= (k[:, 0] >= 0) & (k[:, 0] < IMAGE_W) & (k[:, 1] >= 0) & (k[:, 1] < IMAGE_H)
    xy = k[:, :2].copy()
    if roll_rad:
        c, s = math.cos(roll_rad), math.sin(roll_rad)
        centre = np.array([IMAGE_W / 2, IMAGE_H / 2])
        xy = (xy - centre) @ np.array([[c, -s], [s, c]]).T + centre
    hip = xy[list(HIPS)].mean(axis=0)
    shoulders = [j for j in SHOULDERS if ok[j]]
    if not shoulders:
        return hip, math.nan, math.nan
    up = xy[shoulders].mean(axis=0) - hip
    torso = float(np.hypot(*up))
    tilt = math.atan2(abs(up[0]), -up[1]) if torso > 1e-6 else math.nan
    thighs = [float(np.hypot(*(xy[k_] - xy[h]))) for h, k_ in zip(HIPS, KNEES) if ok[h] and ok[k_]]
    length = torso + float(np.mean(thighs)) if thighs and torso > 1e-6 else math.nan
    return hip, tilt, length


class DynamicFeatureState:
    """Streaming v2 feature computer for one camera's primary track."""

    def __init__(self, roll_deg=0.0, ref_seconds=5.0, lag_seconds=1.0, lag_tolerance=0.2, max_step_dt=0.15):
        self.roll = math.radians(roll_deg)
        self.ref_seconds, self.lag, self.tol, self.max_step = ref_seconds, lag_seconds, lag_tolerance, max_step_dt
        self.reset()

    def reset(self):
        self.track = None
        self.history = deque()  # (t, hip, tilt, length) of valid frames on the current track
        self.last_seen = None  # (t, hip, tilt) of the immediately preceding valid slot

    def update(self, keypoints, timestamp, available, track_id):
        out = np.zeros(WIDTH, np.float32)
        if not available or track_id is None or track_id < 0:
            self.last_seen = None
            return out
        if track_id != self.track:
            self.reset()
            self.track = track_id
        t = float(timestamp)
        hip, tilt, length = _measure(keypoints, self.roll)
        while self.history and t - self.history[0][0] > max(self.ref_seconds, self.lag + self.tol):
            self.history.popleft()
        lengths = [h[3] for h in self.history if t - h[0] <= self.ref_seconds and math.isfinite(h[3])]
        if math.isfinite(length):
            lengths.append(length)
        scale = max(lengths) if lengths else math.nan

        if math.isfinite(scale) and self.last_seen is not None:
            t0, hip0, tilt0 = self.last_seen
            dt = t - t0
            if 0 < dt <= self.max_step:
                v = (hip - hip0) / dt / scale
                out[VY] = np.clip(v[1], -CLIP["velocity"], CLIP["velocity"])
                out[VX] = np.clip(v[0], -CLIP["velocity"], CLIP["velocity"])
                if math.isfinite(tilt) and math.isfinite(tilt0):
                    out[OMEGA] = np.clip((tilt - tilt0) / dt, -CLIP["omega"], CLIP["omega"])
                out[SHORT_VALID] = 1.0
        if math.isfinite(scale):
            target = t - self.lag
            ref = min(self.history, key=lambda h: abs(h[0] - target), default=None)
            if ref is not None and abs(ref[0] - target) <= self.tol:
                out[DY_1S] = np.clip((hip[1] - ref[1][1]) / scale, -CLIP["displacement"], CLIP["displacement"])
                if math.isfinite(tilt) and math.isfinite(ref[2]):
                    out[DTILT_1S] = tilt - ref[2]
                out[LONG_VALID] = 1.0
        self.history.append((t, hip, tilt, length))
        self.last_seen = (t, hip, tilt)
        return out


def dynamic_features(keypoints, timestamps, available, track_ids, roll_deg=0.0):
    """Batch form for training/export. Uses the streaming state so both paths are identical."""
    state = DynamicFeatureState(roll_deg=roll_deg)
    return (
        np.stack([state.update(k, t, a, i) for k, t, a, i in zip(keypoints, timestamps, available, track_ids)])
        if len(keypoints)
        else np.zeros((0, WIDTH), np.float32)
    )
