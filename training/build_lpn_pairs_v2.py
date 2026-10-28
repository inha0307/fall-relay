#!/usr/bin/env python3
"""Phase 2.1: 13-joint YOLO <-> Kinect paired frames for the LPN (train + val, all 30 fps frames).

Joint order (13): runtime ORDER over COCO-17 (12 body joints) + calibrated head point (head_point.py).
Kinect counterpart: ntu_io.JOINTS (12) + Head (3).
Per tracked frame (tracking_v2, phases 0/1/2 = every source frame once):
  input  x[39]   runtime normalisation: (xy - hip centre) / 360 for visible joints, then 13 masks
                 (conf >= 0.2, finite, inside 640x360); zero when a hip is missing
  valid          v2 rule: both hips visible and >= 8 of 13 joints visible
  target y[13,3] Kinect camera-space metres relative to the Kinect hip centre, y flipped (as v1)
  ymask[13]      Kinect tracking state 2 (and both Kinect hips state 2, else all False)
  aug inputs     kin_abs[13,3] camera-space metres, kin_root[3], kin_px[13,2] (640x360),
                 yolo_px[13,2], yolo_conf[13], body-frame torso length from YOLO (px)
The Kinect body is the one that pairs with the tracked YOLO primary (ncnn_pose.paired_actor,
smallest median pixel distance); frames with no paired body are not written.
Per-clip parts are merged into train.npz / val.npz. Test clips are never read.
"""

import argparse, csv, hashlib, json, sys, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)
import numpy as np  # noqa: E402

INV = ROOT / "source_inventory_v2"
TRACK = _paths.TRACKING / "clips"
KIN = _paths.KINECT / "clips"
OUT = _paths.LPN_DATA
LABELS = ROOT / "annotation_v2/event_labels_v2.csv"
W, H, CONF = 640, 360, 0.2
FIELDS = (
    "x",
    "valid",
    "y",
    "ymask",
    "kin_abs",
    "kin_root",
    "kin_px",
    "yolo_px",
    "yolo_conf",
    "frame",
    "phase",
    "fall_phase",
)


def build_clip(job):
    sid, onset, impact = job
    from ncnn_pose import ORDER, paired_actor
    from ntu_io import JOINTS
    from head_point import load_table, head_point

    target = OUT / "parts" / f"{sid}.npz"
    if target.exists():
        return sid, "skipped", 0
    table = load_table()
    K13 = list(JOINTS) + [3]
    tr = np.load(TRACK / f"{sid}.npz")
    kc = np.load(KIN / f"{sid}.npz")
    xyz, color, state = kc["xyz"], kc["color"], kc["state"]
    rows = defaultdict(list)
    for p in (0, 1, 2):
        for f, k17 in zip(tr[f"p{p}_frame"], tr[f"p{p}_kpts17"]):
            if f >= xyz.shape[1] or np.isnan(k17).all():
                continue
            k12 = k17[ORDER]
            hxy, hconf = head_point(k17, table)
            xy13 = np.vstack([k12[:, :2], hxy if hxy is not None else [np.nan, np.nan]])
            conf13 = np.r_[k12[:, 2], hconf]
            mask = (
                np.isfinite(xy13).all(1)
                & (conf13 >= CONF)
                & (xy13[:, 0] >= 0)
                & (xy13[:, 0] < W)
                & (xy13[:, 1] >= 0)
                & (xy13[:, 1] < H)
            )
            best, best_d = None, np.inf
            for b in range(xyz.shape[0]):
                st = state[b, f]
                if (st < 0).all():
                    continue
                px = color[b, f][K13] / 3.0
                present = (st[K13] >= 1) & np.isfinite(px).all(1)
                if not paired_actor(k12, px[:12], present[:12]):
                    continue
                good = present[:12] & mask[:12]
                d = np.median(np.linalg.norm(px[:12][good] - k12[good, :2], axis=1)) if good.any() else np.inf
                if d < best_d:
                    best, best_d = b, d
            if best is None:
                continue
            st = state[best, f][K13]
            abs13 = xyz[best, f][K13]
            root = abs13[6:8].mean(0)
            hips_ok = (st[6:8] == 2).all() and np.isfinite(abs13[6:8]).all()
            y = abs13 - root
            y[:, 1] *= -1
            ymask = (st == 2) & np.isfinite(abs13).all(1) & hips_ok
            x = np.zeros(39, np.float32)
            if mask[6:8].all():
                rel = (xy13 - xy13[6:8].mean(0)) / H
                x[:26] = np.where(mask[:, None], rel, 0).reshape(-1)
                x[26:] = mask
            t = f / 30.0
            fp = 0 if onset is None else (1 if t < onset else 2 if t <= impact else 3)
            for key, val in (
                ("x", x),
                ("valid", bool(mask[6:8].all() and mask.sum() >= 8)),
                ("y", np.nan_to_num(y)),
                ("ymask", ymask),
                ("kin_abs", np.nan_to_num(abs13)),
                ("kin_root", np.nan_to_num(root)),
                ("kin_px", np.nan_to_num(color[best, f][K13] / 3.0)),
                ("yolo_px", np.nan_to_num(xy13)),
                ("yolo_conf", conf13),
                ("frame", f),
                ("phase", p),
                ("fall_phase", fp),
            ):
                rows[key].append(val)
    n = len(rows["x"])
    dtypes = {
        "x": np.float32,
        "valid": bool,
        "y": np.float32,
        "ymask": bool,
        "kin_abs": np.float32,
        "kin_root": np.float32,
        "kin_px": np.float32,
        "yolo_px": np.float32,
        "yolo_conf": np.float32,
        "frame": np.int16,
        "phase": np.int8,
        "fall_phase": np.int8,
    }
    arrays = {k: np.asarray(rows[k], dtypes[k]) if n else np.zeros((0,), dtypes[k]) for k in FIELDS}
    partial = target.with_name(target.stem + ".partial.npz")
    np.savez(partial, **arrays)
    partial.replace(target)
    return sid, "ok", n


def merge(rows):
    meta = {r["sample_id"]: r for r in rows}
    for split in ("train", "val"):
        acc = defaultdict(list)
        ids = []
        for sid in sorted(s for s, r in meta.items() if r["split"] == split):
            d = np.load(OUT / "parts" / f"{sid}.npz")
            if len(d["x"]) == 0:
                continue
            for k in FIELDS:
                acc[k].append(d[k])
            n = len(d["x"])
            r = meta[sid]
            acc["subject"].append(np.full(n, int(r["person"]), np.int16))
            acc["action"].append(np.full(n, int(r["action"]), np.int16))
            acc["clip"].append(np.full(n, len(ids), np.int32))
            ids.append(sid)
        arrays = {k: np.concatenate(v) for k, v in acc.items()}
        np.savez(OUT / f"{split}.npz", **arrays, clip_ids=np.array(ids))
        print(
            split,
            {
                "frames": len(arrays["x"]),
                "clips": len(ids),
                "valid_rate": round(float(arrays["valid"].mean()), 3),
                "supervised_joint_rate": round(float(arrays["ymask"].mean()), 3),
                "fall_phase_counts": np.bincount(arrays["fall_phase"], minlength=4).tolist(),
            },
            flush=True,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    rows = [
        r
        for r in csv.DictReader((INV / "paired_manifest_v2.csv").open())
        if r["eligible"] == "1" and r["split"] in ("train", "val")
    ]
    labels = {r["capture_group"]: r for r in csv.DictReader(LABELS.open())}
    jobs = []
    for r in rows[: args.limit] if args.limit else rows:
        lab = labels.get(r["capture_group"]) if r["action"] == "43" else None
        jobs.append((r["sample_id"], lab and float(lab["onset_s"]), lab and float(lab["impact_s"])))
    (OUT / "parts").mkdir(parents=True, exist_ok=True)
    started, status, frames = time.monotonic(), defaultdict(int), 0
    with ProcessPoolExecutor(args.workers) as pool:
        futures = [pool.submit(build_clip, j) for j in jobs]
        for k, fut in enumerate(as_completed(futures), 1):
            sid, st, n = fut.result()
            status[st] += 1
            frames += n
            if k % 2000 == 0 or k == len(jobs):
                print(
                    f"{k}/{len(jobs)} {dict(status)} frames {frames} {(time.monotonic() - started) / 60:.1f} min",
                    flush=True,
                )
    if not args.limit:
        merge(rows)
        sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
        (OUT / "contract.json").write_text(
            json.dumps(
                {
                    "version": "lpn-pairs-13-v2",
                    "joints": "ORDER(12) + head_point",
                    "input": "39 = 26 xy/360 rel hip centre + 13 masks",
                    "target": "Kinect metres rel hip centre, y flipped",
                    "valid_rule": "both hips + >=8/13 visible",
                    "code_sha256": {
                        f: sha(_paths.source(f)) for f in ("build_lpn_pairs_v2.py", "head_point.py", "ncnn_pose.py")
                    },
                    "head_table_sha256": sha(_paths.EDGE / "head_point_v2/head_point_v2.json"),
                    "test_used": False,
                },
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
