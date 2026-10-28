#!/usr/bin/env python3
"""Phase 2.3: 13-joint LPN (2D -> 3D) on GPU, with or without virtual-camera view augmentation.

Data: $FALLRELAY_WORK/lpn_v2/{train,val}.npz (build_lpn_pairs_v2.py). Test is never read.
Each epoch draws EPOCH_SAMPLES training rows:
  real  : actual YOLO inputs of valid paired train frames
  synth : (viewaug mode only) train Kinect poses re-viewed by lpn_augment_v2.synthesize with the
          TRAIN residual bank (pitch 0-50 deg, yaw +-90 deg, Pi/Kinect intrinsics)
Both get joint dropout (50 %) and mirror (50 %). Fall descent/post-impact frames are drawn 10x
more often (they are ~1 % of frames). Loss: masked MSE on trusted Kinect joints (ymask).
Selection: lowest mean of val-real MPJPE and val-synthetic MPJPE (pitch 0-50, Pi intrinsics,
val's own residuals). Reports MPJPE (mm) by view bin and fall phase; exports FP32 TFLite.
"""

import argparse, hashlib, json, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
import _paths  # noqa: E402

DATA = _paths.LPN_DATA
PROJ = ROOT / "lpn_v2_meta" / "kinect_projection_v2.json"
EPOCH_SAMPLES, BATCH, FALL_WEIGHT = 600_000, 4096, 10.0
KEYS = ("x", "valid", "y", "ymask", "kin_abs", "yolo_px", "yolo_conf", "fall_phase", "clip", "clip_ids", "subject")


def load(split):
    d = np.load(DATA / f"{split}.npz")
    return {k: d[k] for k in KEYS}


def create_lpn(width, blocks=2):
    import tensorflow as tf

    L = tf.keras.layers
    inp = L.Input((39,), name="xy26_mask13")
    x = L.ReLU()(L.BatchNormalization(name="input_bn")(L.Dense(width, name="input_dense")(inp)))
    for b in range(blocks):
        s = x
        x = L.Dropout(0.1)(L.ReLU()(L.BatchNormalization()(L.Dense(width, name=f"block{b}_dense0")(x))))
        x = L.ReLU()(L.Add()([s, L.BatchNormalization()(L.Dense(width, name=f"block{b}_dense1")(x))]))
    xyz = L.Reshape((13, 3))(L.Dense(39, name="xyz39")(x))
    xyz = L.Lambda(lambda v: v - tf.reduce_mean(v[:, 6:8, :], axis=1, keepdims=True), name="recenter_hips")(xyz)
    return tf.keras.Model(inp, xyz, name=f"lpn13_w{width}")


def mpjpe_mm(pred, y, m):
    e = np.linalg.norm(pred - y, axis=-1)[m]
    return float(e.mean() * 1000) if e.size else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "viewaug"], required=True)
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--width", type=int, default=256)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True)
    ap.add_argument("--train-subjects", help="comma list (OOF folds); default all train subjects")
    ap.add_argument("--syn-frac", type=float, default=0.5, help="fraction of each epoch that is synthetic (viewaug)")
    args = ap.parse_args()
    import tensorflow as tf
    from fit_kinect_projection_v2 import params_for, project
    from lpn_augment_v2 import residual_bank, synthesize, drop_joints, mirror

    for g in tf.config.list_physical_devices("GPU"):
        tf.config.experimental.set_memory_growth(g, True)
    tf.keras.utils.set_random_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tr, va = load("train"), load("val")
    proj = json.loads(PROJ.read_text())
    if args.train_subjects:
        keep = np.isin(tr["subject"], [int(s) for s in args.train_subjects.split(",")])
        tr = {k: (v[keep] if k != "clip_ids" else v) for k, v in tr.items()}
    real = np.flatnonzero(tr["valid"] & tr["ymask"][:, 6:8].all(1))
    w = np.where(np.isin(tr["fall_phase"][real], (2, 3)), FALL_WEIGHT, 1.0)
    w /= w.sum()
    bank_rows = real[tr["ymask"][real].sum(1) >= 10]
    ref = project(
        tr["kin_abs"][bank_rows].astype(np.float64),
        params_for({"clip_ids": tr["clip_ids"], "clip": tr["clip"][bank_rows]}, proj)[:, None, :],
    )
    bank = residual_bank({k: tr[k][bank_rows] for k in ("yolo_px", "yolo_conf")}, np.arange(len(bank_rows)), ref)

    # Fixed validation sets.
    vr = np.flatnonzero(va["valid"] & va["ymask"][:, 6:8].all(1))
    vbank_rows = vr[va["ymask"][vr].sum(1) >= 10]
    vref = project(
        va["kin_abs"][vbank_rows].astype(np.float64),
        params_for({"clip_ids": va["clip_ids"], "clip": va["clip"][vbank_rows]}, proj)[:, None, :],
    )
    vbank = residual_bank({k: va[k][vbank_rows] for k in ("yolo_px", "yolo_conf")}, np.arange(len(vbank_rows)), vref)
    vrng = np.random.default_rng(7)
    vsyn = {}
    for lo, hi in ((0, 15), (15, 30), (30, 50)):
        src = vrng.choice(vbank_rows, 60000)
        x, y, m, ok, _ = synthesize(
            va["kin_abs"][src].astype(np.float64), va["ymask"][src], vbank, vrng, pitch_deg=(lo, hi), intr_p=1.0
        )
        vsyn[f"pitch_{lo}_{hi}"] = (x[ok], y[ok], m[ok])

    def epoch_arrays():
        n_real = EPOCH_SAMPLES if args.mode == "baseline" else int(EPOCH_SAMPLES * (1 - args.syn_frac))
        i = rng.choice(real, n_real, p=w)
        xs, ys, ms = [tr["x"][i]], [tr["y"][i]], [tr["ymask"][i]]
        if args.mode == "viewaug":
            j = rng.choice(real, EPOCH_SAMPLES - n_real, p=w)
            x, y, m, ok, _ = synthesize(tr["kin_abs"][j].astype(np.float64), tr["ymask"][j], bank, rng)
            xs.append(x[ok])
            ys.append(y[ok])
            ms.append(m[ok])
        x, y, m = np.concatenate(xs), np.concatenate(ys), np.concatenate(ms)
        drop = rng.random(len(x)) < 0.5
        x[drop] = drop_joints(x[drop], rng, p_any=1.0)
        flip = rng.random(len(x)) < 0.5
        x[flip], y[flip], m[flip] = mirror(x[flip], y[flip], m[flip])
        return x.astype(np.float32), y.astype(np.float32), m

    model = create_lpn(args.width)
    sched = tf.keras.optimizers.schedules.CosineDecay(1e-3, args.epochs * (EPOCH_SAMPLES // BATCH))
    opt = tf.keras.optimizers.Adam(sched)

    @tf.function
    def step(x, y, m):
        with tf.GradientTape() as tape:
            p = model(x, training=True)
            mm = tf.cast(m, tf.float32)[..., None]
            loss = tf.reduce_sum(tf.square(p - y) * mm) / tf.maximum(tf.reduce_sum(mm) * 3.0, 1.0)
        opt.apply_gradients(zip(tape.gradient(loss, model.trainable_variables), model.trainable_variables))
        return loss

    def predict(x):
        return model.predict(x, batch_size=16384, verbose=0)

    def evaluate():
        res = {"val_real": mpjpe_mm(predict(va["x"][vr]), va["y"][vr], va["ymask"][vr])}
        p = predict(va["x"][vr])
        fp = va["fall_phase"][vr]
        for k, name in enumerate(("normal", "pre", "descent", "post_impact")):
            s = fp == k
            res[f"val_real_{name}"] = mpjpe_mm(p[s], va["y"][vr][s], va["ymask"][vr][s])
        for name, (x, y, m) in vsyn.items():
            res[f"val_syn_{name}"] = mpjpe_mm(predict(x), y, m)
        res["val_syn_all"] = float(np.mean([res[f"val_syn_{k}"] for k in vsyn]))
        res["select"] = (res["val_real"] + res["val_syn_all"]) / 2
        return res

    history, best, started = [], np.inf, time.monotonic()
    for epoch in range(1, args.epochs + 1):
        x, y, m = epoch_arrays()
        ds = (
            tf.data.Dataset.from_tensor_slices((x, y, m))
            .shuffle(len(x), seed=epoch)
            .batch(BATCH, drop_remainder=True)
            .prefetch(2)
        )
        losses = [float(step(a, b, c)) for a, b, c in ds]
        res = evaluate()
        res.update(epoch=epoch, loss=float(np.mean(losses)), minutes=(time.monotonic() - started) / 60)
        history.append(res)
        print(json.dumps({k: round(v, 2) if isinstance(v, float) else v for k, v in res.items()}), flush=True)
        if res["select"] < best:
            best = res["select"]
            model.save_weights(str(out / "best.weights.h5"))
        (out / "history.json").write_text(json.dumps(history, indent=1) + "\n")
    model.load_weights(str(out / "best.weights.h5"))
    final = evaluate()
    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS]
    blob = conv.convert()
    (out / "lpn13_fp32.tflite").write_bytes(blob)
    interp = tf.lite.Interpreter(model_content=blob)
    interp.allocate_tensors()
    i, o = interp.get_input_details()[0], interp.get_output_details()[0]
    errs = []
    for r in vr[:64]:
        interp.set_tensor(i["index"], va["x"][r : r + 1])
        interp.invoke()
        errs.append(
            float(np.abs(interp.get_tensor(o["index"]) - model(va["x"][r : r + 1], training=False).numpy()).max())
        )
    result = {
        "mode": args.mode,
        "width": args.width,
        "syn_frac": args.syn_frac if args.mode == "viewaug" else 0.0,
        "seed": args.seed,
        "epochs": args.epochs,
        "best_epoch": int(np.argmin([h["select"] for h in history]) + 1),
        "final": final,
        "train_real_frames": int(len(real)),
        "bank_frames": int(len(bank_rows)),
        "fall_weight": FALL_WEIGHT,
        "train_subjects": args.train_subjects or "all",
        "tflite_sha256": hashlib.sha256(blob).hexdigest(),
        "tflite_max_abs_diff": max(errs),
        "params": int(model.count_params()),
        "test_used": False,
    }
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("RESULT", json.dumps(result["final"]), flush=True)


if __name__ == "__main__":
    main()
