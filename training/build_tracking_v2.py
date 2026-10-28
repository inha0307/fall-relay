#!/usr/bin/env python3
"""Phase 1.5: deployment-identical primary-person tracking on 10 Hz grids from yolo17_v2.

For each clip and each 10 Hz phase p in {0,1,2} (source frames p, p+3, p+6, ...), the
deployed CausalTracker (sha256 verified against the Pi run record) is run from scratch on the
stored detections, fed exactly as at runtime: 12 joints in ncnn_pose.ORDER, box, score,
highest score first. The chosen detection's full COCO-17 keypoints are kept.
Per phase p the clip npz stores
  p{p}_frame[S] source frame, p{p}_det[S] index into that frame's detections (-1 none),
  p{p}_track[S], p{p}_reset[S], p{p}_valid12[S] (runtime ncnn_pose.inputs validity),
  p{p}_kpts17[S,17,3] (NaN when no primary), p{p}_box[S,4], p{p}_score[S].
Timestamps are frame/30 s (all clips verified 30 fps). No actor association, lifting or labels.
"""

import argparse, hashlib, json, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)
import numpy as np  # noqa: E402

SRC = _paths.YOLO17 / "clips"
OUT = _paths.TRACKING
# The tracker must be the deployed one: same executed code as recorded in edge/code_provenance.json.
PROVENANCE = json.loads((_paths.EDGE / "code_provenance.json").read_text())["modules"]
TRACKER = "causal_tracker.py"
PHASES = (0, 1, 2)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def track_clip(src):
    from causal_tracker import CausalTracker
    from ncnn_pose import ORDER, inputs

    target = OUT / "clips" / src.name
    if target.exists():
        return src.stem, "skipped"
    d = np.load(src)
    ptr, box, score, kp = d["det_ptr"], d["det_box"], d["det_score"], d["det_kpts"]
    n = int(d["frames"])
    out = {"frames": np.int32(n)}
    for p in PHASES:
        tracker = CausalTracker()
        rows = {k: [] for k in ("frame", "det", "track", "reset", "valid12", "kpts17", "box", "score")}
        for f in range(p, n, 3):
            dets = [
                {"box": box[i], "keypoints": kp[i][ORDER], "score": float(score[i]), "_i": i - ptr[f]}
                for i in range(ptr[f], ptr[f + 1])
            ]
            chosen, identity, reset = tracker.update(dets)
            rows["frame"].append(f)
            rows["track"].append(identity)
            rows["reset"].append(bool(reset))
            if chosen is None:
                rows["det"].append(-1)
                rows["valid12"].append(False)
                rows["kpts17"].append(np.full((17, 3), np.nan, np.float32))
                rows["box"].append(np.full(4, np.nan, np.float32))
                rows["score"].append(np.nan)
            else:
                i = ptr[f] + chosen["_i"]
                rows["det"].append(chosen["_i"])
                rows["valid12"].append(inputs(chosen["keypoints"])[1])
                rows["kpts17"].append(kp[i])
                rows["box"].append(box[i])
                rows["score"].append(score[i])
        for k, dtype in (
            ("frame", np.int16),
            ("det", np.int16),
            ("track", np.int32),
            ("reset", bool),
            ("valid12", bool),
            ("kpts17", np.float32),
            ("box", np.float32),
            ("score", np.float32),
        ):
            out[f"p{p}_{k}"] = np.asarray(rows[k], dtype)
    partial = target.with_name(target.stem + ".partial.npz")
    np.savez_compressed(partial, **out)
    partial.replace(target)
    return src.stem, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    from code_identity import ast_sha256

    if ast_sha256(_paths.source(TRACKER)) != PROVENANCE[TRACKER]["ast_sha256"]:
        raise SystemExit(f"{TRACKER} differs from the deployed runtime")
    files = sorted(SRC.glob("S*.npz"))
    files = [f for f in files if not f.name.endswith(".partial.npz")][: args.limit]
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    (OUT / "contract.json").write_text(
        json.dumps(
            {
                "version": "tracking-10hz-3phase-v2",
                "source": str(SRC),
                "source_contract_sha256": sha(SRC.parent / "contract.json"),
                "phases": "source frames p, p+3, ... for p in 0,1,2; timestamps frame/30 s",
                "tracker_sha256": {TRACKER: sha(_paths.source(TRACKER))},
                "tracker_frozen_sha256": {TRACKER: PROVENANCE[TRACKER]["frozen_sha256"]},
                "ncnn_pose_note": "ORDER/inputs identical to deployed (only ROOT path line differs)",
                "code_sha256": sha(Path(__file__)),
            },
            indent=2,
        )
        + "\n"
    )
    started, status = time.monotonic(), {}
    with ProcessPoolExecutor(args.workers) as pool:
        futures = [pool.submit(track_clip, f) for f in files]
        for k, fut in enumerate(as_completed(futures), 1):
            sid, st = fut.result()
            status[st] = status.get(st, 0) + 1
            if k % 1000 == 0 or k == len(files):
                print(f"{k}/{len(files)} {status} {(time.monotonic() - started) / 60:.1f} min", flush=True)
    (OUT / "progress.json").write_text(
        json.dumps({"clips": len(files), "status": status, "seconds": time.monotonic() - started}, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
