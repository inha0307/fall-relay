#!/usr/bin/env python3
"""Phase 1.4: head point for the 13-joint contract, calibrated against Kinect v2 Head.

Head point (runtime-computable from YOLO COCO-17 only):
  raw   = mean of face keypoints (nose 0, eyes 1-2, ears 3-4) with confidence >= 0.2
  final = raw + R(torso) @ offset[subset] * torso_px
where subset = which of the 5 face points are visible, torso_px = |shoulder-centre - hip-centre|
in pixels and R(torso) maps the body frame (u = hip->shoulder unit vector, v = u rotated +90 deg)
to image coordinates, so the correction rotates with the body (upright or lying).
offset[subset] = median over TRAIN frames of the body-frame residual (Kinect Head pixel - raw)
/ torso_px; subsets with fewer than MIN_FRAMES fall back to the pooled median.
Frames are used only when the tracked primary is the Kinect actor (ncnn_pose.paired_actor),
Kinect Head is tracked (state 2) and both hips and >=1 shoulder are visible.
Fit: train subjects. Report: val subjects, by subset and by fall phase (event_labels_v2).
Writes head_point_v2.json (the table the runtime and training both load) and a report.
"""

import csv, hashlib, json, random, sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)
import numpy as np  # noqa: E402

TRACK = _paths.TRACKING / "clips"
INV = ROOT / "source_inventory_v2"
LABELS = ROOT / "annotation_v2/event_labels_v2.csv"
OUT = _paths.EDGE / "head_point_v2"  # read by edge/head_point.py
FACE, CONF, MIN_FRAMES = (0, 1, 2, 3, 4), 0.2, 200
KINECT_HEAD = 3
COCO_SHOULDERS, COCO_HIPS = (5, 6), (11, 12)
SEED, NORMAL_TRAIN, NORMAL_VAL = 0, 1500, 300


def body_frame(k):
    """Hip centre, unit u (hip->shoulder), unit v, torso length px; None if not measurable."""
    ok = k[:, 2] >= CONF
    if not ok[list(COCO_HIPS)].all():
        return None
    sh = [j for j in COCO_SHOULDERS if ok[j]]
    if not sh:
        return None
    hip = k[list(COCO_HIPS), :2].mean(0)
    up = k[sh, :2].mean(0) - hip
    torso = float(np.hypot(*up))
    if torso < 5:
        return None
    u = up / torso
    v = np.array([-u[1], u[0]])
    return hip, u, v, torso


def face_raw(k):
    vis = tuple(int(k[j, 2] >= CONF) for j in FACE)
    if not any(vis):
        return None, vis
    return k[[j for j, s in zip(FACE, vis) if s], :2].mean(0), vis


def clip_rows(args):
    """Paired (subset, residual body-frame / torso, time, phase tag) rows for one clip (phase-0 grid)."""
    sid, skel_path, onset, impact = args
    from ntu_io import read_skeleton, choose_track, JOINTS
    from ncnn_pose import ORDER, paired_actor

    t = np.load(TRACK / f"{sid}.npz")
    frames = read_skeleton(skel_path)
    actor, _ = choose_track(frames)
    rows = []
    for f, k in zip(t["p0_frame"], t["p0_kpts17"]):
        if f >= len(frames) or actor not in frames[f] or np.isnan(k).all():
            continue
        j = frames[f][actor]
        if j[KINECT_HEAD, 11] != 2:
            continue
        px = j[:, 5:7] * np.array([640 / 1920, 360 / 1080])
        present = (j[JOINTS, 11] >= 1) & np.isfinite(px[JOINTS]).all(1)
        if not paired_actor(k[ORDER], px[JOINTS], present):
            continue
        bf = body_frame(k)
        raw, vis = face_raw(k)
        if bf is None or raw is None:
            rows.append((vis, None, f / 30))
            continue
        hip, u, v, torso = bf
        res = px[KINECT_HEAD] - raw
        rows.append((vis, (float(res @ u / torso), float(res @ v / torso), float(np.hypot(*res)), torso), f / 30))
    phase = lambda s: (
        "normal" if onset is None else ("pre" if s < onset else "descent" if s <= impact else "post_impact")
    )
    return [(vis, r, phase(s)) for vis, r, s in rows]


def jobs(split, n_normal, labels, rng):
    rows = [
        r
        for r in csv.DictReader((INV / "paired_manifest_v2.csv").open())
        if r["eligible"] == "1" and r["split"] == split
    ]
    falls = [r for r in rows if r["action"] == "43"]
    normal = rng.sample([r for r in rows if r["action"] != "43"], n_normal)
    out = []
    for r in falls + normal:
        lab = labels.get(r["capture_group"]) if r["action"] == "43" else None
        out.append((r["sample_id"], r["skeleton_path"], lab and float(lab["onset_s"]), lab and float(lab["impact_s"])))
    return out


def collect(job_list, workers):
    missing = [j[0] for j in job_list if not (TRACK / f"{j[0]}.npz").exists()]
    if missing:
        raise SystemExit(f"{len(missing)} tracking files missing (e.g. {missing[:3]}); run build_tracking_v2.py first")
    with ProcessPoolExecutor(workers) as pool:
        return [row for rows in pool.map(clip_rows, job_list, chunksize=8) for row in rows]


def apply(table, vis, res_uv):
    off = table["subsets"].get("".join(map(str, vis)), table["pooled"])
    return res_uv[0] - off[0], res_uv[1] - off[1]


def main():
    rng = random.Random(SEED)
    labels = {r["capture_group"]: r for r in csv.DictReader(LABELS.open())}
    train = collect(jobs("train", NORMAL_TRAIN, labels, rng), 12)
    fit = [(vis, r) for vis, r, _ in train if r is not None]
    by = defaultdict(list)
    for vis, r in fit:
        by["".join(map(str, vis))].append(r[:2])
    pooled = np.median(np.array([r[:2] for _, r in fit]), axis=0).tolist()
    table = {
        "version": "head-point-v2",
        "conf": CONF,
        "face_coco": FACE,
        "units": "torso length, body frame (u=hip->shoulder, v=u+90deg)",
        "pooled": pooled,
        "min_frames": MIN_FRAMES,
        "subsets": {s: np.median(np.array(v), axis=0).tolist() for s, v in by.items() if len(v) >= MIN_FRAMES},
        "subset_counts_train": {s: len(v) for s, v in sorted(by.items(), key=lambda x: -len(x[1]))},
        "fit_split": "train",
        "fit_frames": len(fit),
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "head_point_v2.json").write_text(json.dumps(table, indent=2) + "\n")

    val = collect(jobs("val", NORMAL_VAL, labels, rng), 12)
    report = {"table_sha256": hashlib.sha256((OUT / "head_point_v2.json").read_bytes()).hexdigest(), "val": {}}
    groups = defaultdict(lambda: {"frames": 0, "face_visible": 0, "raw": [], "cal": []})
    for vis, r, ph in val:
        for key in (ph, "all"):
            g = groups[key]
            g["frames"] += 1
            if r is None:
                continue
            g["face_visible"] += 1
            cu, cv = apply(table, vis, r[:2])
            g["raw"].append(np.hypot(*r[:2]))
            g["cal"].append(np.hypot(cu, cv))
    for key, g in groups.items():
        raw, cal = np.array(g["raw"]), np.array(g["cal"])
        report["val"][key] = {
            "paired_frames": g["frames"],
            "face_visible_rate": round(g["face_visible"] / max(g["frames"], 1), 3),
            "err_raw_torso_median": round(float(np.median(raw)), 3) if len(raw) else None,
            "err_cal_torso_median": round(float(np.median(cal)), 3) if len(cal) else None,
            "err_cal_torso_p90": round(float(np.percentile(cal, 90)), 3) if len(cal) else None,
        }
    px = np.array([r[2] for _, r, _ in val if r is not None])
    report["val"]["all"]["err_raw_px_median_640x360"] = round(float(np.median(px)), 2)
    (OUT / "head_point_v2_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "subsets_fitted": len(table["subsets"]),
                "top_subsets": dict(list(table["subset_counts_train"].items())[:6]),
                "val": report["val"],
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
