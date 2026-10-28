#!/usr/bin/env python3
"""Phase 2.4: compare LPN v2 runs on the same VAL frames (model selection for step 6b).

Inputs use the runtime normalisation (hip centre, /360), so the exported TFLite models run unchanged.
Metrics: MPJPE (mm) over trusted Kinect joints, 12 body joints (head reported separately), on real val
frames by fall phase and on synthetic val views by pitch bin
(Pi Camera Module 3 standard intrinsics, val's own YOLO residuals, fixed seed).
"""

import argparse, json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
import _paths  # noqa: E402

DATA = _paths.LPN_DATA
BODY = list(range(12))


def run_tflite(path, x):
    import tensorflow as tf

    it = tf.lite.Interpreter(
        model_path=str(path),
        experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES,
    )
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    x = x.astype(np.float32)
    try:
        it.resize_tensor_input(i["index"], [len(x), x.shape[1]])
        it.allocate_tensors()
        it.set_tensor(i["index"], x)
        it.invoke()
        return it.get_tensor(o["index"]).reshape(len(x), -1, 3)
    except RuntimeError:  # a model exported with a fixed batch of 1: run row by row
        it = tf.lite.Interpreter(
            model_path=str(path),
            experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES,
        )
        it.allocate_tensors()
        out = []
        for r in x:
            it.set_tensor(i["index"], r[None])
            it.invoke()
            out.append(it.get_tensor(o["index"]).reshape(-1, 3))
        return np.stack(out)


def mpjpe(p, y, m):
    e = np.linalg.norm(p - y, axis=-1)[m]
    return round(float(e.mean() * 1000), 1) if e.size else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from fit_kinect_projection_v2 import params_for, project
    from lpn_augment_v2 import residual_bank, synthesize

    d = np.load(DATA / "val.npz")
    proj = json.loads((ROOT / "lpn_v2_meta/kinect_projection_v2.json").read_text())
    rows = np.flatnonzero(d["valid"] & d["ymask"][:, 6:8].all(1))
    sets = {
        f"real_{n}": rows[d["fall_phase"][rows] == k] for k, n in enumerate(("normal", "pre", "descent", "post_impact"))
    }
    sets["real_all"] = rows
    data = {k: (d["x"][v], d["y"][v], d["ymask"][v]) for k, v in sets.items()}
    brow = rows[d["ymask"][rows].sum(1) >= 10]
    ref = project(
        d["kin_abs"][brow].astype(np.float64),
        params_for({"clip_ids": d["clip_ids"], "clip": d["clip"][brow]}, proj)[:, None, :],
    )
    bank = residual_bank({k: d[k][brow] for k in ("yolo_px", "yolo_conf")}, np.arange(len(brow)), ref)
    rng = np.random.default_rng(11)
    for lo, hi in ((0, 15), (15, 30), (30, 50)):
        src = rng.choice(brow, 60000)
        x, y, m, ok, _ = synthesize(
            d["kin_abs"][src].astype(np.float64), d["ymask"][src], bank, rng, pitch_deg=(lo, hi), intr_p=1.0
        )
        data[f"syn_pitch_{lo}_{hi}"] = (x[ok], y[ok], m[ok])
    models = {Path(r).name: Path(r) / "lpn13_fp32.tflite" for r in args.runs}
    report = {
        "frames": {k: int(len(v[0])) for k, v in data.items()},
        "unit": "MPJPE mm, 12 body joints (head separate)",
        "models": {},
    }
    for name, path in models.items():
        res = {}
        for k, (x, y, m) in data.items():
            p = run_tflite(path, x)
            res[k] = mpjpe(p[:, :12], y[:, BODY], m[:, BODY])
            res[k + "_head"] = mpjpe(p[:, 12:13], y[:, 12:13], m[:, 12:13])
        report["models"][name] = res
        print(name, json.dumps({k: v for k, v in res.items() if not k.endswith("_head")}), flush=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
