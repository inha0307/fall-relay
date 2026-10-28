#!/usr/bin/env python3
"""Phase 2.2 helper: per (setup, camera) Kinect depth->colour projection, fit on TRAIN pairs only.

u = fx * X/Z + fx*tx / Z + cx,  v = fy * Y/Z + fy*ty / Z + cy   (640x360 pixels, Kinect camera space)
Linear least squares on trusted joints. A single global model leaves a setup-dependent vertical bias
(S008/S009 about -5 px, S002/S015 about +4 px); per-group fits remove it, so YOLO residuals can be
defined against the projection of the 3D joint, which is how synthetic views are generated.
"""

import json
from pathlib import Path

import numpy as np

import _paths  # noqa: E402

DATA = _paths.LPN_DATA
OUT = Path(__file__).resolve().parent / "lpn_v2_meta" / "kinect_projection_v2.json"


def groups(d):
    ids = d["clip_ids"]
    g = np.array([f"{s[0:4]}{s[4:8]}" for s in ids])
    return g[d["clip"]]


def project(P, prm):
    """P[...,3] with prm[...,6] = (fx, fx*tx, cx, fy, fy*ty, cy) -> pixels[...,2]."""
    X, Y, Z = P[..., 0], P[..., 1], np.maximum(P[..., 2], 1e-3)
    return np.stack(
        [prm[..., 0] * X / Z + prm[..., 1] / Z + prm[..., 2], prm[..., 3] * Y / Z + prm[..., 4] / Z + prm[..., 5]], -1
    )


def params_for(d, table):
    g = groups(d)
    keys = sorted(table["groups"])
    lut = np.array([table["groups"].get(k, table["global"]) for k in keys] + [table["global"]], np.float64)
    pos = {k: i for i, k in enumerate(keys)}
    return lut[np.array([pos.get(x, len(keys)) for x in g])]


def main():
    d = np.load(DATA / "train.npz")
    g = groups(d)
    P, px, ok = d["kin_abs"].astype(np.float64), d["kin_px"].astype(np.float64), d["ymask"]
    fit = {}
    report = {}
    for key in ["__global__"] + sorted(set(g)):
        rows = ok if key == "__global__" else ok & (g == key)[:, None]
        X, Y, Z = P[rows].T
        uv = px[rows]
        if len(Z) < 500:
            continue
        cu, *_ = np.linalg.lstsq(np.c_[X / Z, 1 / Z, np.ones_like(Z)], uv[:, 0], rcond=None)
        cv, *_ = np.linalg.lstsq(np.c_[Y / Z, 1 / Z, np.ones_like(Z)], uv[:, 1], rcond=None)
        prm = np.r_[cu, cv]
        err = np.linalg.norm(project(P[rows], prm) - uv, axis=-1)
        fit[key] = prm.tolist()
        report[key] = {
            "points": int(len(Z)),
            "median_px": round(float(np.median(err)), 2),
            "p90_px": round(float(np.percentile(err, 90)), 2),
        }
    table = {
        "fit_split": "train",
        "global": fit.pop("__global__"),
        "groups": fit,
        "report": report,
        "form": "u = p0*X/Z + p1/Z + p2, v = p3*Y/Z + p4/Z + p5 (640x360)",
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(table, indent=1) + "\n")
    errs = [v["median_px"] for k, v in report.items() if k != "__global__"]
    print(
        "groups",
        len(fit),
        "global median",
        report["__global__"]["median_px"],
        "per-group median of medians",
        round(float(np.median(errs)), 2),
        "worst group",
        max(errs),
    )


if __name__ == "__main__":
    main()
