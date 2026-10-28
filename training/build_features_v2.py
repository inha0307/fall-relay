#!/usr/bin/env python3
"""Phase 3.1: fallscope-pose13-feature59-v2 slot sequences for TCN training (train + val).

prep (CPU, multiprocess):  tracking_v2 clip -> for each 10 Hz phase p: SlotFeaturizer.step per slot
                           (identical call order to the Pi runtime) -> x39, valid, dyn7, t, track, reset
lift (GPU, TF venv):       LPN on valid slots -> xyz13; train subjects use the OOF fold model that
                           never saw them, val uses the full model; features_v2.assemble -> [N,59]
Output $FALLRELAY_WORK/features_v2/{train,val}.npz:
  feat[N,59] valid[N] t[N] track[N] reset[N] frame[N]
  seq_* per (clip, phase) sequence: start, stop, clip, phase
  clip_* per clip: sample_id, subject, action, capture_group, onset_s, impact_s (NaN if not A043),
         label_source, label_flags
Window labels are NOT assigned here (Phase 4 decides delta/ignore zones on val).
"""

import argparse, csv, hashlib, json, sys, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)

INV = ROOT / "source_inventory_v2"
TRACK = _paths.TRACKING / "clips"
OUT = _paths.FEATURES
LPN = _paths.LPN_RUNS / "final_v2"
LABELS = ROOT / "annotation_v2/event_labels_v2.csv"


def prep_clip(sid):
    from features_v2 import SlotFeaturizer
    from head_point import load_table

    target = OUT / "parts" / f"{sid}.npz"
    if target.exists():
        return sid, "skipped"
    tr = np.load(TRACK / f"{sid}.npz")
    table = load_table()
    out = {}
    for p in (0, 1, 2):
        fz = SlotFeaturizer(table)
        xs, vs, ds = [], [], []
        frames, tracks, resets = tr[f"p{p}_frame"], tr[f"p{p}_track"], tr[f"p{p}_reset"]
        for f, k17, tid, rs in zip(frames, tr[f"p{p}_kpts17"], tracks, resets):
            x, v, d = fz.step(k17, f / 30.0, int(tid), bool(rs))
            xs.append(x)
            vs.append(v)
            ds.append(d)
        out[f"p{p}_x"] = np.asarray(xs, np.float32).reshape(-1, 39)
        out[f"p{p}_valid"] = np.asarray(vs, bool)
        out[f"p{p}_dyn"] = np.asarray(ds, np.float32).reshape(-1, 7)
        out[f"p{p}_frame"] = frames.astype(np.int16)
        out[f"p{p}_track"] = tracks.astype(np.int32)
        out[f"p{p}_reset"] = resets.astype(bool)
    partial = target.with_name(target.stem + ".partial.npz")
    np.savez(partial, **out)
    partial.replace(target)
    return sid, "ok"


def manifest_rows(splits=("train", "val")):
    return [
        r
        for r in csv.DictReader((INV / "paired_manifest_v2.csv").open())
        if r["eligible"] == "1" and r["split"] in splits
    ]


def prep(workers, splits):
    rows = manifest_rows(splits)
    (OUT / "parts").mkdir(parents=True, exist_ok=True)
    started, status = time.monotonic(), defaultdict(int)
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(prep_clip, r["sample_id"]) for r in rows]
        for k, fut in enumerate(as_completed(futures), 1):
            status[fut.result()[1]] += 1
            if k % 4000 == 0 or k == len(rows):
                print(f"prep {k}/{len(rows)} {dict(status)} {(time.monotonic() - started) / 60:.1f} min", flush=True)


def lift(splits):
    import tensorflow as tf
    from train_lpn_v2 import create_lpn
    from features_v2 import assemble, CONTRACT

    for g in tf.config.list_physical_devices("GPU"):
        tf.config.experimental.set_memory_growth(g, True)
    tf.config.experimental.enable_tensor_float_32_execution(False)  # TF32 matmuls differ from Pi TFLite by ~1e-3
    folds = json.loads((ROOT / "lpn_v2_meta/oof_folds_v2.json").read_text())["folds"]
    subject_model = {s: f"fold{k}" for k, f in enumerate(folds) for s in f["heldout"]}
    models = {}

    def model(name):
        if name not in models:
            m = create_lpn(256)
            m.load_weights(str(LPN / name / "best.weights.h5"))
            models[name] = m
        return models[name]

    labels = {r["capture_group"]: r for r in csv.DictReader(LABELS.open())}
    rows = manifest_rows(splits)
    for split in splits:
        sel = [r for r in rows if r["split"] == split]
        acc = defaultdict(list)
        seq = defaultdict(list)
        clip = defaultdict(list)
        n = 0
        owner = []
        for ci, r in enumerate(sel):
            d = np.load(OUT / "parts" / f"{r['sample_id']}.npz")
            subj = int(r["person"])
            name = subject_model[subj] if split == "train" else "full"
            for p in (0, 1, 2):
                x = d[f"p{p}_x"]
                for k in ("x", "valid", "dyn", "track", "reset", "frame"):
                    acc[k].append(d[f"p{p}_{k}"])
                owner.append(np.full(len(x), name))
                seq["start"].append(n)
                n += len(x)
                seq["stop"].append(n)
                seq["clip"].append(ci)
                seq["phase"].append(p)
            lab = labels.get(r["capture_group"]) if r["action"] == "43" else None
            clip["sample_id"].append(r["sample_id"])
            clip["subject"].append(subj)
            clip["action"].append(int(r["action"]))
            clip["capture_group"].append(r["capture_group"])
            clip["lpn_model"].append(name)
            clip["onset_s"].append(float(lab["onset_s"]) if lab else np.nan)
            clip["impact_s"].append(float(lab["impact_s"]) if lab else np.nan)
            clip["label_source"].append(lab["source"] if lab else "")
            clip["label_flags"].append(lab["flags"] if lab else "")
            if (ci + 1) % 5000 == 0:
                print(f"load {split} {ci + 1}/{len(sel)}", flush=True)
        cat = {k: np.concatenate(v) for k, v in acc.items()}
        owner = np.concatenate(owner)
        xyz = np.zeros((len(cat["x"]), 13, 3), np.float32)
        for name in sorted(set(owner)):  # one batched pass per LPN model
            idx = np.flatnonzero((owner == name) & cat["valid"])
            xyz[idx] = model(name).predict(cat["x"][idx], batch_size=16384, verbose=0)
            print(f"lift {split} {name}: {len(idx)} valid slots", flush=True)
        arrays = {
            "feat": assemble(xyz, cat["x"], cat["valid"], cat["dyn"]),
            "valid": cat["valid"],
            "t": cat["frame"].astype(np.float32) / 30.0,
            "track": cat["track"],
            "reset": cat["reset"],
            "frame": cat["frame"],
        }
        arrays.update({f"seq_{k}": np.asarray(v) for k, v in seq.items()})
        arrays.update({f"clip_{k}": np.asarray(v) for k, v in clip.items()})
        np.savez(OUT / f"{split}.npz", **arrays)
        print(
            split,
            {
                "slots": len(arrays["feat"]),
                "valid": round(float(arrays["valid"].mean()), 3),
                "sequences": len(seq["start"]),
                "clips": len(sel),
                "fall_clips": int(np.isfinite(arrays["clip_onset_s"]).sum()),
            },
            flush=True,
        )
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    (OUT / ("contract.json" if set(splits) == {"train", "val"} else f"contract_{'_'.join(splits)}.json")).write_text(
        json.dumps(
            {
                "contract": CONTRACT,
                "layout": {"xyz": [0, 38], "mask": [39, 51], "dynamic7_v2": [52, 58]},
                "splits": list(splits),
                "lpn": {
                    "train": "OOF fold model per subject (lpn_v2_meta/oof_folds_v2.json)",
                    "val/test": "full",
                    "weights_sha256": {
                        k: sha(LPN / k / "best.weights.h5") for k in ("full", "fold0", "fold1", "fold2", "fold3")
                    },
                },
                "code_sha256": {
                    f: sha(_paths.source(f))
                    for f in ("features_v2.py", "head_point.py", "shape_features_v2.py", "build_features_v2.py")
                },
                "labels": {"file": str(LABELS), "sha256": sha(LABELS)},
                "test_used": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prep", "lift"])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--splits", default="train,val", help="test only after the operating point is frozen")
    a = ap.parse_args()
    sp = tuple(a.splits.split(","))
    if "test" in sp and not (ROOT / "tcn_v2_meta/frozen_operating_point_v2.json").exists():
        raise SystemExit("freeze the operating point before building test features")
    prep(a.workers, sp) if a.stage == "prep" else lift(sp)
