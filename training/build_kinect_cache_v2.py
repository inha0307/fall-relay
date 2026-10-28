#!/usr/bin/env python3
"""Phase 2.0: parse NTU Kinect v2 skeletons once into a fast local cache (train+val only).

Per clip npz (Linux filesystem, fast re-reads for LPN data building):
  frames                     number of skeleton frames (equals video frames; verified in 1.1)
  body_ids[B]                up to two bodies with the most tracked joints (choose_track order)
  xyz[B,n,25,3]  float32     Kinect camera-space metres, NaN where the body is absent
  color[B,n,25,2] float32    Kinect colour-image pixels (1920x1080)
  state[B,n,25]  int8        tracking state 0/1/2, -1 where absent
  bodies_per_frame[n] int8
Which body is the actor is decided later by pairing with the tracked YOLO primary (2.1).
Test clips are not read.
"""

import argparse, csv, json, sys, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)

INV = ROOT / "source_inventory_v2"
OUT = _paths.KINECT
MAX_BODIES = 2


def cache_clip(row):
    from ntu_io import read_skeleton

    target = OUT / "clips" / f"{row['sample_id']}.npz"
    if target.exists():
        return row["sample_id"], "skipped"
    frames = read_skeleton(row["skeleton_path"])
    n = len(frames)
    score = defaultdict(int)
    for f in frames:
        for b, j in f.items():
            score[b] += int((j[:, 11] == 2).sum())
    ids = sorted(score, key=lambda b: (-score[b], b))[:MAX_BODIES]
    B = max(len(ids), 1)
    xyz = np.full((B, n, 25, 3), np.nan, np.float32)
    color = np.full((B, n, 25, 2), np.nan, np.float32)
    state = np.full((B, n, 25), -1, np.int8)
    per = np.zeros(n, np.int8)
    for t, f in enumerate(frames):
        per[t] = min(len(f), 127)
        for k, b in enumerate(ids):
            if b in f:
                j = f[b]
                xyz[k, t] = j[:, :3]
                color[k, t] = j[:, 5:7]
                state[k, t] = j[:, 11]
    partial = target.with_name(target.stem + ".partial.npz")
    np.savez(
        partial,
        frames=np.int32(n),
        body_ids=np.array(ids, dtype="U32"),
        xyz=xyz,
        color=color,
        state=state,
        bodies_per_frame=per,
    )
    partial.replace(target)
    return row["sample_id"], "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    split = json.loads((INV / "split_v2.json").read_text())
    if split["manifest_fingerprint"] != _paths.manifest_fingerprint(INV / "paired_manifest_v2.csv"):
        raise SystemExit("manifest differs from the frozen split")
    rows = [
        r
        for r in csv.DictReader((INV / "paired_manifest_v2.csv").open())
        if r["eligible"] == "1" and r["split"] in ("train", "val")
    ]
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    started, status = time.monotonic(), defaultdict(int)
    with ProcessPoolExecutor(args.workers) as pool:
        futures = [pool.submit(cache_clip, r) for r in rows]
        for k, fut in enumerate(as_completed(futures), 1):
            status[fut.result()[1]] += 1
            if k % 2000 == 0 or k == len(rows):
                print(f"{k}/{len(rows)} {dict(status)} {(time.monotonic() - started) / 60:.1f} min", flush=True)
    (OUT / "progress.json").write_text(
        json.dumps(
            {"clips": len(rows), "status": status, "test_used": False, "seconds": time.monotonic() - started}, indent=2
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
