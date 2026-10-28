"""13th joint for the fallscope-pose13 contract: calibrated head point from YOLO COCO-17 face keypoints.

Shared by training data builders and the Pi runtime. The calibration table is
head_point_v2/head_point_v2.json (fit on train subjects; see calibrate_head_point.py).
head = mean(visible face points) + R(body) @ offset[visible subset] * torso_px, where the body
frame is u = hip-centre -> shoulder-centre (unit), v = u rotated +90 degrees.
Returns (xy, confidence) or (None, 0.0) when no face point is visible or the torso cannot be measured.
"""

import json
from pathlib import Path

import numpy as np

FACE, SHOULDERS, HIPS, CONF = (0, 1, 2, 3, 4), (5, 6), (11, 12), 0.2
TABLE_PATH = Path(__file__).resolve().parent / "head_point_v2" / "head_point_v2.json"


def load_table(path=TABLE_PATH):
    t = json.loads(Path(path).read_text())
    return {
        "pooled": np.asarray(t["pooled"], np.float64),
        "subsets": {k: np.asarray(v, np.float64) for k, v in t["subsets"].items()},
    }


def head_point(k17, table):
    k = np.asarray(k17, np.float64)
    ok = np.isfinite(k).all(1) & (k[:, 2] >= CONF)
    vis = [j for j in FACE if ok[j]]
    if not vis or not ok[list(HIPS)].all():
        return None, 0.0
    sh = [j for j in SHOULDERS if ok[j]]
    if not sh:
        return None, 0.0
    hip = k[list(HIPS), :2].mean(0)
    up = k[sh, :2].mean(0) - hip
    torso = float(np.hypot(*up))
    if torso < 5:
        return None, 0.0
    u = up / torso
    v = np.array([-u[1], u[0]])
    key = "".join("1" if j in vis else "0" for j in FACE)
    off = table["subsets"].get(key, table["pooled"])
    xy = k[vis, :2].mean(0) + (off[0] * u + off[1] * v) * torso
    return xy, float(k[vis, 2].mean())
