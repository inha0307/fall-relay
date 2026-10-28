#!/usr/bin/env python3
"""Automatic A043 onset/impact labels from Kinect v2 skeletons (rule v2); the definitions are in training/README.md.

  onset   start of the downward head run containing the fastest descent: walk back from the
          fastest descent while descent speed > ONSET_FRACTION x peak speed (5-frame smoothing)
  impact  first non-foot floor contact: first frame at/after onset where any of knees, hands,
          hips, elbows or head is within CONTACT_M of the floor for CONTACT_FRAMES consecutive
          frames. Floor = median lowest foot height over the first 10 frames; heights are along
          the per-view 'up' axis (SpineBase->Neck over the first 0.5 s, actors start upright).
Per group: median over camera views (synchronised); spread is reported as a quality flag.
`dev` mode compares candidate parameters with reference onset/impact frames for the six DEV
groups (camera 2), read from a CSV given with --reference (capture_group,onset_frame,impact_frame).
The released parameters were chosen against AI-assisted visual estimates, which are not
distributed (they are NTU-derived annotations); `build` needs no reference.
"""

import argparse, csv, json, sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from ntu_io import read_skeleton, choose_track  # noqa: E402

INV = ROOT / "source_inventory_v2"
FPS = 30.0
SPINE_BASE, NECK, HEAD, FEET = 0, 2, 3, (15, 19)
CONTACT_JOINTS = (13, 17, 7, 11, 12, 16, 5, 9, 3)  # knees, hands, hips, elbows, head
PARAMS = {"onset_fraction": 0.20, "contact_m": 0.08, "contact_frames": 2, "min_drop_m": 0.50}
# Groups used to choose PARAMS; they are flagged in the output (dev_group) and kept out of audits.
DEV = (
    "S017P007R002A043",
    "S008P033R001A043",
    "S001P002R001A043",
    "S013P007R002A043",
    "S013P025R001A043",
    "S004P008R002A043",
)


def smooth(x, k=5):
    return np.convolve(np.pad(x, (k // 2, k // 2), mode="edge"), np.ones(k) / k, mode="valid")


def interpolate(x):
    ok = np.isfinite(x)
    return np.interp(np.arange(len(x)), np.flatnonzero(ok), x[ok]) if ok.sum() >= 2 else x


def view_heights(skeleton_path):
    frames = read_skeleton(skeleton_path)
    n = len(frames)
    actor, _ = choose_track(frames)
    joints = np.full((n, 25, 3), np.nan)
    for t, f in enumerate(frames):
        if actor in f and (f[actor][:, 11] >= 1).any():
            joints[t] = f[actor][:, :3]
    up = np.nanmean(joints[:15, NECK] - joints[:15, SPINE_BASE], axis=0)
    if not np.isfinite(up).all() or np.linalg.norm(up) < 0.2:
        return None, "no_upright_start"
    heights = joints @ (up / np.linalg.norm(up))
    floor = np.nanmedian(np.nanmin(heights[:10][:, list(FEET)], axis=1))
    if not np.isfinite(floor):
        return None, "no_floor"
    return heights - floor, "ok"


def label_view(rel, p):
    n = len(rel)
    head = rel[:, HEAD]
    if np.isnan(head).mean() > 0.3:
        return {"status": "head_missing"}
    h = smooth(interpolate(head))
    v = np.gradient(h) * FPS
    drop = np.median(h[:10]) - h.min()
    if drop < p["min_drop_m"]:
        return {"status": "small_drop", "drop_m": round(float(drop), 3)}
    peak = int(np.argmin(v))
    onset = peak
    while onset > 0 and -v[onset - 1] > p["onset_fraction"] * -v[peak]:
        onset -= 1
    low = np.nanmin(rel[:, list(CONTACT_JOINTS)], axis=1) <= p["contact_m"]
    k = p["contact_frames"]
    impact = next((t for t in range(onset, n - k + 1) if low[t : t + k].all()), None)
    if impact is None:
        return {"status": "no_contact", "onset_frame": onset}
    return {"status": "ok", "onset_frame": onset, "impact_frame": impact, "drop_m": round(float(drop), 3)}


def dev(rows, reference):
    ref = {
        r["capture_group"]: (int(r["onset_frame"]), int(r["impact_frame"]))
        for r in csv.DictReader(open(reference))
        if r["capture_group"] in DEV
    }
    by = {(r["capture_group"], r["camera"]): r for r in rows}
    cache = {g: view_heights(by[(g, "2")]["skeleton_path"])[0] for g in DEV}
    print("onset_fraction contact_m | onset err (frames) | impact err (frames)")
    for frac in (0.15, 0.20, 0.25, 0.30):
        for cm in (0.05, 0.08, 0.10, 0.12):
            p = dict(PARAMS, onset_fraction=frac, contact_m=cm)
            eo, ei = [], []
            for g, (on, im) in ref.items():
                r = label_view(cache[g], p)
                eo.append(r.get("onset_frame", np.nan) - on)
                ei.append(r.get("impact_frame", np.nan) - im)
            print(
                f"{frac:.2f} {cm:.2f} | {eo} mean|e| {np.nanmean(np.abs(eo)):.1f} | {ei} mean|e| {np.nanmean(np.abs(ei)):.1f}"
            )


def build(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r["capture_group"]].append(r)
    out = []
    for g, views in sorted(groups.items()):
        per = {}
        for r in sorted(views, key=lambda r: r["camera"]):
            rel, status = view_heights(r["skeleton_path"])
            per[r["camera"]] = label_view(rel, PARAMS) if rel is not None else {"status": status}
        ok = [x for x in per.values() if x["status"] == "ok"]
        rec = {
            "capture_group": g,
            "split": views[0]["split"],
            "views_ok": len(ok),
            "view_status": json.dumps({c: x["status"] for c, x in per.items()}),
            "dev_group": int(g in DEV),
        }
        if ok:
            on = [x["onset_frame"] for x in ok]
            im = [x["impact_frame"] for x in ok]
            rec.update(
                onset_frame=int(np.median(on)),
                impact_frame=int(np.median(im)),
                onset_s=round(float(np.median(on)) / FPS, 3),
                impact_s=round(float(np.median(im)) / FPS, 3),
                onset_spread_frames=max(on) - min(on),
                impact_spread_frames=max(im) - min(im),
                per_view=json.dumps({c: [x.get("onset_frame"), x.get("impact_frame")] for c, x in per.items()}),
            )
        out.append(rec)
    fields = [
        "capture_group",
        "split",
        "views_ok",
        "onset_frame",
        "impact_frame",
        "onset_s",
        "impact_s",
        "onset_spread_frames",
        "impact_spread_frames",
        "per_view",
        "view_status",
        "dev_group",
    ]
    with (HERE / "auto_labels_v2.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    (HERE / "auto_labels_v2_params.json").write_text(
        json.dumps(
            {
                "rule": "auto_labels.py v2",
                "params": PARAMS,
                "fps": FPS,
                "contact_joints": CONTACT_JOINTS,
                "dev_groups": sorted(DEV),
            },
            indent=2,
        )
        + "\n"
    )
    status = defaultdict(int)
    for r in out:
        status["ok" if r["views_ok"] else "no_label"] += 1
    print(
        dict(status),
        "groups;",
        sum(r.get("impact_spread_frames", 0) > 6 for r in out),
        "groups with impact spread > 6 frames",
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["dev", "build"])
    ap.add_argument("--reference", help="dev mode: CSV with capture_group,onset_frame,impact_frame (camera 2)")
    args = ap.parse_args()
    if args.mode == "dev" and not args.reference:
        ap.error("dev mode needs --reference")
    rows = [
        r
        for r in csv.DictReader((INV / "paired_manifest_v2.csv").open())
        if r["eligible"] == "1" and r["action"] == "43"
    ]
    dev(rows, args.reference) if args.mode == "dev" else build(rows)
