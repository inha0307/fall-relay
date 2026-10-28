#!/usr/bin/env python3
"""Phase 4: event TCN on fallscope-pose13-feature59-v2 windows (20 slots x 59, 10 Hz).

Windows end at every slot of every sequence, exactly as the runtime builds them: the last 20 slots,
cut at the most recent primary reset (history cleared), left zero-padded. A window is scored only
when eligible: current slot valid and >= MIN_VALID valid slots (runtime rule).
Labels by window end time t (A043 capture groups, event_labels_v2):
  t <  onset - 0.3        negative          onset - 0.3 <= t < onset    ignore (boundary)
  onset <= t <= impact+D  positive          t > impact + D               ignore (NTU clips end ~0.9 s
                                                                         after impact; every such window
                                                                         still contains the impact)
  all other actions: negative.
Batches: 25 % positives (capture group uniform), 37.5 % hard-negative actions, 37.5 % other negatives.
Augmentation: mirror (50 %), 1-2 dropped slots (30 %). Model: causal TCN (dilations 1,2,4,8,
receptive field 31 >= 20) with LAST-STEP output (no global average pooling).
Checkpoint selection: val window-level average precision (natural prior, ignores excluded).
"""

import argparse, hashlib, json, time
from pathlib import Path

import numpy as np

import _paths  # noqa: E402

FEAT = _paths.FEATURES
SLOTS, WIDTH, MIN_VALID, DELTA, PRE_IGNORE = 20, 59, 10, 0.5, 0.3
HARD_NEG = (5, 6, 8, 9, 24, 26, 27, 42, 48)  # drop, pick up, sit down, stand up, kick, hop, jump, stagger, vomit
PAIRS = ((0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (10, 11))


def load(split):
    d = np.load(FEAT / f"{split}.npz")
    return {k: d[k] for k in d.files}


def windows(d):
    """Eligible windows: end e, first allowed slot f, label (1/0/-1), clip, t_end."""
    E, Fs, C = [], [], []
    for a, b, c in zip(d["seq_start"], d["seq_stop"], d["seq_clip"]):
        last = a
        for e in range(a, b):
            if d["reset"][e]:
                last = e
            E.append(e)
            Fs.append(max(last, e - SLOTS + 1))
            C.append(c)
    E, Fs, C = np.asarray(E), np.asarray(Fs), np.asarray(C)
    cv = np.concatenate([[0], np.cumsum(d["valid"])])
    nvalid = cv[E + 1] - cv[Fs]
    ok = d["valid"][E] & (nvalid >= MIN_VALID)
    E, Fs, C = E[ok], Fs[ok], C[ok]
    t = d["t"][E]
    on, im = d["clip_onset_s"][C], d["clip_impact_s"][C]
    fall = np.isfinite(on)
    y = np.zeros(len(E), np.int8)
    y[fall & (t >= on - PRE_IGNORE) & (t < on)] = -1
    y[fall & (t >= on) & (t <= im + DELTA)] = 1
    y[fall & (t > im + DELTA)] = -1
    return {
        "e": E,
        "f": Fs,
        "y": y,
        "clip": C,
        "t": t,
        "action": d["clip_action"][C],
        "group": d["clip_capture_group"][C],
    }


def gather(feat, e, f):
    idx = e[:, None] - SLOTS + 1 + np.arange(SLOTS)[None]
    x = feat[np.clip(idx, 0, None)]
    x[idx < f[:, None]] = 0
    return x


def mirror(x):
    x = x.copy()
    xyz = x[..., :39].reshape(*x.shape[:-1], 13, 3).copy()
    m = x[..., 39:52].copy()
    for a, b in PAIRS:
        xyz[..., [a, b], :] = xyz[..., [b, a], :]
        m[..., [a, b]] = m[..., [b, a]]
    xyz[..., 0] *= -1
    x[..., :39] = xyz.reshape(*x.shape[:-1], 39)
    x[..., 39:52] = m
    x[..., 53] *= -1  # hip_vx flips; vertical motion, tilt changes and validity flags are mirror-invariant
    return x


def build_model(seed):
    import tensorflow as tf

    L = tf.keras.layers
    tf.keras.utils.set_random_seed(seed)
    inp = L.Input((SLOTS, WIDTH), name="pose13_feature59")
    x = inp
    for i, (ch, dil) in enumerate(((32, 1), (32, 2), (64, 4), (64, 8))):
        x = L.Dropout(0.1)(
            L.Conv1D(ch, 3, padding="causal", dilation_rate=dil, activation="relu", name=f"causal_{i}")(x)
        )
    x = L.Lambda(lambda v: v[:, -1, :], name="last_step")(x)
    return tf.keras.Model(inp, L.Dense(1, name="fall_event_logit")(x), name="event_tcn_v2")


def average_precision(y, s):
    o = np.argsort(-s, kind="stable")
    ys = y[o]
    tp = np.cumsum(ys)
    prec = tp / np.arange(1, len(ys) + 1)
    return float((prec * ys).sum() / max(ys.sum(), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    import tensorflow as tf

    for g in tf.config.list_physical_devices("GPU"):
        tf.config.experimental.set_memory_growth(g, True)
    tf.config.experimental.enable_tensor_float_32_execution(False)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    tr, va = load("train"), load("val")
    wt, wv = windows(tr), windows(va)
    pos = np.flatnonzero(wt["y"] == 1)
    neg = np.flatnonzero(wt["y"] == 0)
    hard = neg[np.isin(wt["action"][neg], HARD_NEG)]
    other = neg[~np.isin(wt["action"][neg], HARD_NEG)]
    pgroups = {g: pos[wt["group"][pos] == g] for g in np.unique(wt["group"][pos])}
    gkeys = list(pgroups)
    vl = np.flatnonzero(wv["y"] >= 0)
    stats = {
        "train_windows": int(len(wt["e"])),
        "train_pos": int(len(pos)),
        "train_pos_groups": len(gkeys),
        "train_hard_neg": int(len(hard)),
        "val_windows": int(len(wv["e"])),
        "val_labeled": int(len(vl)),
        "val_pos": int((wv["y"] == 1).sum()),
    }
    print(json.dumps(stats), flush=True)

    def batch():
        nb = args.batch
        npos = nb // 4
        nh = (nb - npos) // 2
        g = rng.choice(len(gkeys), npos)
        p = np.array([rng.choice(pgroups[gkeys[i]]) for i in g])
        idx = np.r_[p, rng.choice(hard, nh), rng.choice(other, nb - npos - nh)]
        y = np.r_[np.ones(npos), np.zeros(nb - npos)]
        x = gather(tr["feat"], wt["e"][idx], wt["f"][idx])
        flip = rng.random(nb) < 0.5
        x[flip] = mirror(x[flip])
        drop = np.flatnonzero(rng.random(nb) < 0.3)
        for r in drop:
            x[r, rng.choice(SLOTS - 1, rng.integers(1, 3), replace=False)] = 0
        return x.astype(np.float32), y.astype(np.float32)

    model = build_model(args.seed)
    sched = tf.keras.optimizers.schedules.CosineDecay(1e-3, args.epochs * args.steps)
    opt = tf.keras.optimizers.Adam(sched)

    @tf.function
    def step(x, y):
        with tf.GradientTape() as tape:
            loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(y, tf.reshape(model(x, training=True), [-1])))
        opt.apply_gradients(zip(tape.gradient(loss, model.trainable_variables), model.trainable_variables))
        return loss

    def score(d, w, rows):
        s = np.empty(len(rows), np.float32)
        for i in range(0, len(rows), 16384):
            r = rows[i : i + 16384]
            s[i : i + len(r)] = (
                tf.sigmoid(model(gather(d["feat"], w["e"][r], w["f"][r]), training=False)).numpy().ravel()
            )
        return s

    history, best, started = [], -1.0, time.monotonic()
    for epoch in range(1, args.epochs + 1):
        losses = [float(step(*batch())) for _ in range(args.steps)]
        s = score(va, wv, vl)
        y = (wv["y"][vl] == 1).astype(int)
        apv = average_precision(y, s)
        bce = float(-np.mean(y * np.log(np.clip(s, 1e-7, 1)) + (1 - y) * np.log(np.clip(1 - s, 1e-7, 1))))
        row = {
            "epoch": epoch,
            "loss": float(np.mean(losses)),
            "val_ap": apv,
            "val_bce_natural": bce,
            "minutes": (time.monotonic() - started) / 60,
        }
        history.append(row)
        print(json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()}), flush=True)
        if apv > best:
            best = apv
            model.save_weights(str(out / "best.weights.h5"))
        (out / "history.json").write_text(json.dumps(history, indent=1) + "\n")
    model.load_weights(str(out / "best.weights.h5"))
    allv = np.arange(len(wv["e"]))
    sv = score(va, wv, allv)
    np.savez(out / "val_scores.npz", e=wv["e"], f=wv["f"], y=wv["y"], clip=wv["clip"], t=wv["t"], score=sv)
    exp = tf.keras.Model(model.input, tf.keras.layers.Activation("sigmoid")(model.output))
    conv = tf.lite.TFLiteConverter.from_keras_model(exp)
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS]
    blob = conv.convert()
    (out / "event_tcn_v2_fp32.tflite").write_bytes(blob)
    it = tf.lite.Interpreter(model_content=blob)
    it.allocate_tensors()
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    x = gather(va["feat"], wv["e"][:64], wv["f"][:64]).astype(np.float32)
    errs = []
    for r in range(len(x)):
        it.set_tensor(i["index"], x[r : r + 1])
        it.invoke()
        errs.append(abs(float(it.get_tensor(o["index"]).ravel()[0]) - float(sv[r])))
    res = {
        "seed": args.seed,
        "best_epoch": int(np.argmax([h["val_ap"] for h in history]) + 1),
        "best_val_ap": best,
        **stats,
        "delta_s": DELTA,
        "pre_ignore_s": PRE_IGNORE,
        "min_valid": MIN_VALID,
        "hard_negative_actions": HARD_NEG,
        "tflite_sha256": hashlib.sha256(blob).hexdigest(),
        "tflite_max_abs_diff": max(errs),
        "params": int(model.count_params()),
        "tflite_io": [i["shape"].tolist(), o["shape"].tolist()],
        "test_used": False,
    }
    (out / "result.json").write_text(json.dumps(res, indent=2) + "\n")
    print("RESULT", json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
