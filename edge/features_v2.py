"""fallscope-pose13-feature59-v2 per-slot features, shared by the Pi runtime and training.

Per 10 Hz slot of the tracked primary (COCO-17 keypoints in 640x360 pixels):
  x39    LPN input: 13 joints (runtime ORDER + head_point) relative to the hip centre / 360,
         then 13 visibility masks (conf >= 0.2, finite, inside the image); zero without both hips
  valid  both hips visible and >= 8 of 13 joints visible
  dyn7   shape_features_v2 dynamic features from the 12 body joints (camera-invariant, zero at rest)
Feature vector (59) = [LPN xyz 13x3 (0-38), masks 13 (39-51), dyn7 (52-58)], all zero for an
invalid slot. The LPN itself runs outside this module (batch in training,
per slot on the Pi) on x39 of valid slots only.
"""

import numpy as np

from head_point import head_point, load_table
from shape_features_v2 import DynamicFeatureState

ORDER = np.array([6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15])
W, H, CONF, WIDTH = 640, 360, 0.2, 59
CONTRACT = "fallscope-pose13-feature59-v2"


def lpn_input(k17, table):
    """COCO-17 keypoints -> (x39, valid, kp12) with the runtime normalisation."""
    k = np.asarray(k17, np.float64)
    if not np.isfinite(k).any():
        return np.zeros(39, np.float32), False, np.full((12, 3), np.nan)
    k12 = k[ORDER]
    hxy, hconf = head_point(k, table)
    xy = np.vstack([k12[:, :2], hxy if hxy is not None else [np.nan, np.nan]])
    conf = np.r_[k12[:, 2], hconf]
    vis = np.isfinite(xy).all(1) & (conf >= CONF) & (xy[:, 0] >= 0) & (xy[:, 0] < W) & (xy[:, 1] >= 0) & (xy[:, 1] < H)
    x = np.zeros(39, np.float32)
    if vis[6] and vis[7]:
        rel = (xy - xy[6:8].mean(0)) / H
        x[:26] = np.where(vis[:, None], rel, 0).reshape(-1)
        x[26:] = vis
    return x, bool(vis[6] and vis[7] and vis.sum() >= 8), k12


class SlotFeaturizer:
    """Streaming per-slot featuriser for one camera's primary track (runtime and training)."""

    def __init__(self, table=None, roll_deg=0.0):
        self.table = table if table is not None else load_table()
        self.dynamic = DynamicFeatureState(roll_deg=roll_deg)

    def reset(self):
        self.dynamic.reset()

    def step(self, k17, t, track_id, reset=False):
        """-> (x39, valid, dyn7). `reset` mirrors the runtime clearing history on a new primary."""
        if reset:
            self.dynamic.reset()
        x, valid, k12 = lpn_input(k17, self.table)
        dyn = self.dynamic.update(k12, t, valid, track_id if track_id is not None else -1)
        return x, valid, dyn


def assemble(xyz13, x39, valid, dyn7):
    """Feature rows [N,59]; invalid rows are all zero."""
    xyz13, x39, dyn7 = np.asarray(xyz13), np.asarray(x39), np.asarray(dyn7)
    valid = np.asarray(valid, bool)
    f = np.concatenate([xyz13.reshape(len(x39), 39), x39[:, 26:39], dyn7], 1).astype(np.float32)
    f[~valid] = 0
    return f
