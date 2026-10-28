"""head_point.head_point reproduces the calibration definition in calibrate_head_point.py."""

import _paths  # noqa: F401  (edge on sys.path)
import numpy as np
from head_point import load_table, head_point
from calibrate_head_point import body_frame, face_raw

table = load_table()
rng = np.random.default_rng(0)
checked = 0
for _ in range(500):
    k = np.zeros((17, 3))
    k[:, :2] = rng.uniform(50, 600, (17, 2))
    k[:, 2] = rng.uniform(0, 1, 17)
    xy, conf = head_point(k, table)
    bf = body_frame(k)
    raw, vis = face_raw(k)
    if bf is None or raw is None:
        assert xy is None
        continue
    hip, u, v, torso = bf
    key = ''.join(map(str, vis))
    off = table['subsets'].get(key, table['pooled'])
    assert np.allclose(xy, raw + (off[0] * u + off[1] * v) * torso)
    checked += 1
upright = np.zeros((17, 3))
upright[:, 2] = 0.9
upright[11:13, :2] = [[300, 250], [340, 250]]
upright[5:7, :2] = [[295, 150], [345, 150]]
upright[0:5, :2] = [[320, 110], [312, 104], [328, 104], [304, 108], [336, 108]]
xy, _ = head_point(upright, table)
assert 90 < xy[1] < 125 and abs(xy[0] - 320) < 10
print(
    f'Passed: head_point matches calibration definition on {checked} random poses; upright head plausible {xy.round(1)}.'
)
