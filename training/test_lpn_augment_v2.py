"""lpn_augment_v2 conventions on real val pairs: per-group reprojection, identity targets, mirror,
runtime normalisation parity, joint dropout keeps validity, synthetic inputs are well-formed."""

import json, numpy as np
import _paths
from fit_kinect_projection_v2 import params_for, project
from lpn_augment_v2 import residual_bank, synthesize, mirror, normalise, drop_joints

d = np.load(_paths.LPN_DATA / 'val.npz')
sel = np.flatnonzero(d['ymask'].all(1))[:20000]
t = json.load(open('lpn_v2_meta/kinect_projection_v2.json'))
ref = project(d['kin_abs'][sel].astype(np.float64), params_for(d, t)[sel][:, None, :])
err = np.linalg.norm(ref - d['kin_px'][sel], axis=-1)
assert np.median(err) < 0.5, np.median(err)
sub = {k: d[k][sel] for k in ('yolo_px', 'yolo_conf')}
bank = residual_bank(sub, np.arange(len(sel)), ref)
rng = np.random.default_rng(0)
P = d['kin_abs'][sel].astype(np.float64)
x, y, ym, valid, _ = synthesize(P, d['ymask'][sel], bank, rng, pitch_deg=(0, 0), yaw_deg=(0, 0))
assert np.allclose(y, d['y'][sel], atol=1e-4)
x2, y2, m2 = mirror(*mirror(x, y, ym))
assert np.allclose(x2, x) and np.allclose(y2, y) and (m2 == ym).all()
xr, vr = normalise(d['yolo_px'][sel].astype(np.float64), d['yolo_conf'][sel] >= 0.2)
assert np.allclose(xr[vr], d['x'][sel][vr], atol=1e-5)
xd = drop_joints(d['x'][sel], rng)
vis = xd[:, 26:] > 0.5
assert (vis[:, 6] & vis[:, 7]).all() and (vis.sum(1) >= 8).all() and (xd[:, :26].reshape(-1, 13, 2)[~vis] == 0).all()
assert 0.4 < (vis.sum(1) < 13).mean() < 0.6
x3, y3, _, v3, meta = synthesize(P, d['ymask'][sel], bank, rng)
assert np.isfinite(x3).all() and v3.mean() > 0.5
print(
    f'Passed: per-group reprojection median {np.median(err):.2f}px; identity targets; mirror; normalisation parity '
    f'({vr.sum()} frames); dropout keeps validity ({(vis.sum(1) < 13).mean():.2f} rows with hidden joints); '
    f'synthetic valid rate {v3.mean():.2f}.'
)
