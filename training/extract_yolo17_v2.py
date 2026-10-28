#!/usr/bin/env python3
"""Phase 1.3: NCNN CPU YOLO26n-pose extraction of every 30 fps frame, all 17 COCO joints.

Per clip (sample_id.npz):
  fps, width, height, frames            source video metadata / decoded frame count
  det_ptr[frames+1]                     detections of frame f are det_ptr[f]:det_ptr[f+1]
  det_box[N,4], det_score[N], det_kpts[N,17,3]   640x360 coordinates, highest score first
Preprocessing is identical to the Pi runtime (pose17.prepare_frame -> detect17). No
tracking, selection, lifting or labels happen here. Resumable; writes are atomic.
Test-split clips are refused unless --allow-test (Phase 7 only, after val freezes choices).
"""

import argparse, csv, hashlib, json, os, sys, tempfile, time, zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)

os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
import cv2  # noqa: E402
import numpy as np  # noqa: E402

INV = ROOT / "source_inventory_v2"
OUT = _paths.YOLO17
MODEL = _paths.POSE_MODEL
EXPECTED = {
    "model.ncnn.param": "189c56e17dc2cc7773806dcad7334f2d66ca2effbac4f39245ed643db8ca1de2",
    "model.ncnn.bin": "54f59d78974b26ab634bc3358f4d41f279b37a2e89425d076b69b4797c8ee00c",
}
NET = None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _init():
    global NET
    import ncnn

    cv2.setNumThreads(1)
    NET = ncnn.Net()
    NET.opt.num_threads = 1
    NET.opt.use_vulkan_compute = False
    NET.opt.use_fp16_storage = NET.opt.use_fp16_packed = NET.opt.use_fp16_arithmetic = False
    assert NET.load_param(str(MODEL / "model.ncnn.param")) == 0 and NET.load_model(str(MODEL / "model.ncnn.bin")) == 0


def extract(row):
    from pose17 import prepare_frame, detect17

    target = OUT / "clips" / f"{row['sample_id']}.npz"
    if target.exists():
        return row["sample_id"], "skipped", 0
    with zipfile.ZipFile(row["rgb_archive"]) as z, tempfile.NamedTemporaryFile(suffix=".avi", dir="/tmp") as tmp:
        tmp.write(z.read(row["rgb_member"]))
        tmp.flush()
        cap = cv2.VideoCapture(tmp.name)
        if not cap.isOpened():
            return row["sample_id"], "open_failed", 0
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        ptr, boxes, scores, kpts = [0], [], [], []
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            dets = detect17(NET, prepare_frame(frame))
            boxes += [d["box"] for d in dets]
            scores += [d["score"] for d in dets]
            kpts += [d["keypoints"] for d in dets]
            ptr.append(ptr[-1] + len(dets))
        cap.release()
    n = len(ptr) - 1
    partial = target.with_name(target.stem + ".partial.npz")
    np.savez_compressed(
        partial,
        fps=np.float32(fps),
        width=np.int32(width),
        height=np.int32(height),
        frames=np.int32(n),
        det_ptr=np.asarray(ptr, np.int32),
        det_box=np.asarray(boxes, np.float32).reshape(-1, 4),
        det_score=np.asarray(scores, np.float32),
        det_kpts=np.asarray(kpts, np.float32).reshape(-1, 17, 3),
    )
    partial.replace(target)
    return row["sample_id"], "ok", n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", default="train,val")
    ap.add_argument("--allow-test", action="store_true")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--actions", help="comma list, e.g. 43 (default all eligible)")
    args = ap.parse_args()
    splits = set(args.splits.split(","))
    if "test" in splits and not args.allow_test:
        raise SystemExit("test extraction requires --allow-test (Phase 7)")
    for name, digest in EXPECTED.items():
        if sha(MODEL / name) != digest:
            raise SystemExit(f"{name} differs from the deployed pose model")
    split = json.loads((INV / "split_v2.json").read_text())
    if split["manifest_fingerprint"] != _paths.manifest_fingerprint(INV / "paired_manifest_v2.csv"):
        raise SystemExit("manifest differs from the frozen split")
    rows = [
        r
        for r in csv.DictReader((INV / "paired_manifest_v2.csv").open())
        if r["eligible"] == "1" and r["split"] in splits
    ]
    if args.actions:
        rows = [r for r in rows if int(r["action"]) in {int(a) for a in args.actions.split(",")}]
    rows.sort(key=lambda r: (r["rgb_archive"], r["sample_id"]))
    if args.limit:
        rows = rows[: args.limit]
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    contract = {
        "version": "yolo17-30fps-v2",
        "preprocess": "decoded BGR -> 640x360 INTER_LINEAR -> JPEG95 round trip -> 320 letterbox",
        "detector": "YOLO26n-pose NCNN fp32 CPU 1 thread",
        "det_conf": 0.25,
        "nms_iou": 0.45,
        "coordinates": "640x360 pixels",
        "joint_order": "raw COCO-17 (runtime 12-joint ORDER = [6,5,8,7,10,9,12,11,14,13,16,15])",
        "model_sha256": EXPECTED,
        "code_sha256": {f: sha(_paths.source(f)) for f in ("extract_yolo17_v2.py", "pose17.py")},
        "split_sha256": sha(INV / "split_v2.json"),
        "splits": sorted(splits),
    }
    (OUT / "contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    started, done, frames, status = time.monotonic(), 0, 0, {}
    with ProcessPoolExecutor(args.workers, initializer=_init) as pool:
        for fut in as_completed([pool.submit(extract, r) for r in rows]):
            sid, st, n = fut.result()
            status[st] = status.get(st, 0) + 1
            done += 1
            frames += n
            if st not in ("ok", "skipped"):
                print("FAIL", sid, st, flush=True)
            if done % 200 == 0 or done == len(rows):
                el = time.monotonic() - started
                print(
                    f"{done}/{len(rows)} clips, {frames} new frames, {frames / el:.0f} fps, {el / 60:.1f} min, {status}",
                    flush=True,
                )
    (OUT / f"progress_{'_'.join(sorted(splits))}.json").write_text(
        json.dumps(
            {
                "clips": len(rows),
                "status": status,
                "new_frames": frames,
                "seconds": time.monotonic() - started,
                "test_used": "test" in splits,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
