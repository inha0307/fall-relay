"""Phase 2.2: virtual-camera view augmentation for the 13-joint LPN (numpy, vectorised).

Coordinates follow the fitted Kinect colour projection: camera space X right, Y up, Z forward;
u = fx X/Z + cx, v = -fy Y/Z + cy. Targets are camera-frame metres relative to the hip centre
with Y flipped (y down), identical to the v1 convention, so real and synthetic targets mix.
Synthetic sample from a real Kinect pose P (13x3):
  1. Q = P - hip centre; yaw about the vertical (Y) axis, then camera pitch-down about X
  2. place the root at a random image location and depth, project with Pi Camera Module 3
     standard or Kinect intrinsics (+-5 %)
  3. add a real YOLO-vs-Kinect residual (body frame, torso units) and visibility pattern drawn
     from the TRAIN residual bank in the same torso-tilt bucket
  4. normalise exactly like the runtime (hip centre, /360, masks) and apply the v2 validity rule
Mirror flip swaps left/right index pairs and negates image x / camera X.
"""

import numpy as np

W, H, CONF = 640, 360, 0.2
PAIRS = ((0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (10, 11))
FLIP = np.arange(13)
for a, b in PAIRS:
    FLIP[a], FLIP[b] = b, a
INTRINSICS = {"pi_cm3_standard": (493.0, 481.0, 320.0, 180.0), "kinect_v2": (351.6, 352.6, 325.3, 183.7)}
TILT_EDGES = np.radians([30.0, 60.0])


def body_frame(px):
    """px[...,13,2] -> hip centre, u (hip->shoulder unit), v, torso length, tilt (rad from image up)."""
    hip = px[..., 6:8, :].mean(-2)
    sh = px[..., 0:2, :].mean(-2)
    up = sh - hip
    torso = np.linalg.norm(up, axis=-1)
    u = up / np.maximum(torso, 1e-6)[..., None]
    v = np.stack([-u[..., 1], u[..., 0]], -1)
    tilt = np.arctan2(np.abs(u[..., 0]), -u[..., 1])
    return hip, u, v, torso, tilt


def tilt_bucket(tilt):
    return np.digitize(tilt, TILT_EDGES)


def residual_bank(d, idx, ref_px):
    """Body-frame residuals (torso units) and YOLO visibility from paired TRAIN frames idx.

    ref_px[idx] must be the projection of the 3D Kinect joints with the per-(setup, camera) model
    (fit_kinect_projection_v2), so a residual means 'YOLO keypoint minus projected 3D joint', the
    same quantity that synthesize() adds to its own projections.
    """
    kin, yolo, conf = np.asarray(ref_px[idx], np.float64), d["yolo_px"][idx].astype(np.float64), d["yolo_conf"][idx]
    hip, u, v, torso, tilt = body_frame(kin)
    r = yolo - kin
    ok = torso > 5
    rb = np.stack([(r * u[:, None]).sum(-1), (r * v[:, None]).sum(-1)], -1) / np.maximum(torso, 1e-6)[:, None, None]
    vis = (conf >= CONF) & np.isfinite(yolo).all(-1) & (yolo != 0).any(-1)
    rb[~vis] = 0
    return {"r": rb[ok].astype(np.float32), "vis": vis[ok], "bucket": tilt_bucket(tilt[ok])}


def normalise(kp, vis):
    """Runtime input: 26 relative coords /360 + 13 masks; valid = both hips and >=8 visible."""
    vis = vis & np.isfinite(kp).all(-1) & (kp[..., 0] >= 0) & (kp[..., 0] < W) & (kp[..., 1] >= 0) & (kp[..., 1] < H)
    hips = vis[:, 6] & vis[:, 7]
    centre = np.where(hips[:, None], kp[:, 6:8].mean(1), 0)
    rel = np.where(vis[..., None], (kp - centre[:, None]) / H, 0) * hips[:, None, None]
    x = np.concatenate([rel.reshape(len(kp), -1), (vis & hips[:, None]).astype(np.float32)], 1).astype(np.float32)
    return x, hips & (vis.sum(1) >= 8)


def synthesize(P, ymask, bank, rng, pitch_deg=(0, 50), yaw_deg=(-90, 90), depth=(1.8, 5.0), intr_p=0.7, bank_draw=None):
    """P[N,13,3] Kinect camera-space poses -> (x[N,39], y[N,13,3], ymask[N,13], valid[N], meta)."""
    N = len(P)
    Q = P - P[:, 6:8].mean(1, keepdims=True)
    yaw = np.radians(rng.uniform(*yaw_deg, N))
    pitch = np.radians(rng.uniform(*pitch_deg, N))
    cy_, sy_ = np.cos(yaw), np.sin(yaw)
    cp, sp = np.cos(pitch), np.sin(pitch)
    X, Y, Z = Q[..., 0], Q[..., 1], Q[..., 2]
    X, Z = cy_[:, None] * X + sy_[:, None] * Z, -sy_[:, None] * X + cy_[:, None] * Z  # yaw about Y
    Y, Z = cp[:, None] * Y - sp[:, None] * Z, sp[:, None] * Y + cp[:, None] * Z  # camera pitch-down
    Qc = np.stack([X, Y, Z], -1)
    use_pi = rng.random(N) < intr_p
    fx, fy, cx, cy = (np.where(use_pi, INTRINSICS["pi_cm3_standard"][i], INTRINSICS["kinect_v2"][i]) for i in range(4))
    jit = rng.uniform(0.95, 1.05, N)
    fx, fy = fx * jit, fy * jit
    d = rng.uniform(*depth, N)
    u0 = rng.uniform(80, W - 80, N)
    v0 = rng.uniform(70, H - 70, N)
    T = np.stack([(u0 - cx) * d / fx, -(v0 - cy) * d / fy, d], -1)
    Pc = Qc + T[:, None]
    Zs = np.maximum(Pc[..., 2], 0.2)
    kp = np.stack([fx[:, None] * Pc[..., 0] / Zs + cx[:, None], -fy[:, None] * Pc[..., 1] / Zs + cy[:, None]], -1)
    hip, u, v, torso, tilt = body_frame(kp)
    j = bank_draw if bank_draw is not None else np.empty(N, np.int64)
    if bank_draw is None:
        b = tilt_bucket(tilt)
        for k in range(3):
            pool = np.flatnonzero(bank["bucket"] == k)
            if not len(pool):
                pool = np.arange(len(bank["bucket"]))
            sel = b == k
            j[sel] = rng.choice(pool, sel.sum())
    r = bank["r"][j]
    kp = kp + (r[..., 0:1] * u[:, None] + r[..., 1:2] * v[:, None]) * torso[:, None, None]
    x, valid = normalise(kp, bank["vis"][j] & (Pc[..., 2] > 0.2))
    y = Qc.copy()
    y[..., 1] *= -1
    meta = {"pitch_deg": np.degrees(pitch), "yaw_deg": np.degrees(yaw), "pi_intrinsics": use_pi}
    return x, y.astype(np.float32), ymask, valid, meta


def drop_joints(x, rng, p_any=0.5, max_drop=5):
    """Hide random non-hip joints in normalised inputs (NTU pairs are ~99 % fully visible; field
    data is not). Keeps the runtime validity rule: hips stay, at least 8 of 13 remain visible."""
    x = x.copy()
    n = len(x)
    vis = x[:, 26:] > 0.5
    k = np.where(rng.random(n) < p_any, rng.integers(1, max_drop + 1, n), 0)
    k = np.minimum(k, np.maximum(vis.sum(1) - 8, 0))
    cand = np.array([j for j in range(13) if j not in (6, 7)])
    score = rng.random((n, len(cand)))
    score[~vis[:, cand]] = 2.0  # hidden joints are never "re-hidden"
    rank = np.argsort(np.argsort(score, axis=1), axis=1)
    hide = np.zeros((n, 13), bool)
    hide[:, cand] = rank < k[:, None]
    x[:, 26:][hide] = 0
    xy = x[:, :26].reshape(n, 13, 2)
    xy[hide] = 0
    return x


def mirror(x, y, ymask):
    """Left/right mirror of normalised inputs and camera-frame targets."""
    xy = x[:, :26].reshape(-1, 13, 2)[:, FLIP].copy()
    xy[..., 0] *= -1
    m = x[:, 26:][:, FLIP]
    yy = y[:, FLIP].copy()
    yy[..., 0] *= -1
    return np.concatenate([xy.reshape(len(x), -1), m], 1), yy, ymask[:, FLIP]
