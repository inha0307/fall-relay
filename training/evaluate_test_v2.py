#!/usr/bin/env python3
"""Phase 7 (NTU part): single, pre-registered evaluation on the NTU60 cross-subject TEST split.

Uses only what tcn_v2_meta/frozen_operating_point_v2.json fixed before any test pose was extracted:
TCN v2_s43, LPN final_v2/full, theta 0.95, k/n/m 3/5/3, the same matching rule as validation.
Primary: phase-0 sequences, single camera. Secondary: phases 1/2, per camera, excluding flagged labels.
"""

import hashlib, json, math, sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)

FREEZE = ROOT / "tcn_v2_meta/frozen_operating_point_v2.json"
FREEZE_SHA = "64775bb8a4fdc902456d997ce287424a85019c0e4aca7cdacf71b6105f452084"
FEAT = _paths.FEATURES
RUNS = _paths.WORK
OUT = ROOT / "tcn_v2_meta/test_result_v2.json"
FLAGS_EXCLUDE = ("weak_fall_performance", "low_confidence")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def poisson_ci(k, alpha=0.05):
    """Exact (Garwood) two-sided CI for a Poisson count k, by bisection on the Poisson CDF."""

    def cdf(n, lam):
        if n < 0:
            return 0.0
        return sum(math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1)) for i in range(n + 1)) if lam > 0 else 1.0

    def bisect(too_small, hi):
        lo = 0.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if too_small(mid):
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    hi = 10.0 * k + 20
    upper = bisect(lambda lam: cdf(k, lam) > alpha / 2, hi)  # P(X <= k) = alpha/2
    lower = 0.0 if k == 0 else bisect(lambda lam: 1 - cdf(k - 1, lam) < alpha / 2, hi)  # P(X >= k) = alpha/2
    return lower, upper


def evaluate(d, s, elig, params, phases, keep_clip=None, rng_seed=0):
    from evaluate_events_v2 import simulate

    rows = [r for r in simulate(d, s, elig, params, phases) if keep_clip is None or keep_clip[r[0]]]
    on, im, grp = d["clip_onset_s"], d["clip_impact_s"], d["clip_capture_group"]
    det, lat, fa, hours = {}, [], 0, 0.0
    for c, opens, dur in rows:
        hours += dur / 3600
        if np.isfinite(on[c]):
            ok = [t for t in opens if t >= on[c] - 0.3]
            fa += len(opens) - len(ok)
            det.setdefault(grp[c], []).append(bool(ok))
            if ok:
                lat.append(ok[0] - im[c])
        else:
            fa += len(opens)
    seqs = [x for v in det.values() for x in v]
    g = np.array([np.mean(v) for v in det.values()])
    rng = np.random.default_rng(rng_seed)
    bs = [rng.choice(g, len(g)).mean() for _ in range(5000)]
    lo, hi = poisson_ci(fa)
    return {
        "fall_sequences": len(seqs),
        "fall_groups": len(g),
        "recall": round(float(np.mean(seqs)), 3),
        "recall_group_ci95": [round(float(np.percentile(bs, 2.5)), 3), round(float(np.percentile(bs, 97.5)), 3)],
        "false_alarms": fa,
        "hours": round(hours, 2),
        "fa_per_hour": round(fa / hours, 3),
        "fa_per_hour_ci95": [round(lo / hours, 3), round(hi / hours, 3)],
        "latency_vs_impact_s_median": round(float(np.median(lat)), 2) if lat else None,
        "latency_vs_impact_s_iqr": [round(float(np.percentile(lat, 25)), 2), round(float(np.percentile(lat, 75)), 2)]
        if lat
        else None,
    }


def main():
    if sha(FREEZE) != FREEZE_SHA:
        raise SystemExit("freeze file changed")
    fz = json.loads(FREEZE.read_text())
    # Retrained weights and regenerated labels are compared with the published run, not required to match:
    # GPU training is not bit-exact, and the released labels are rule-only (see README).
    used = {
        "tcn_weights": sha(RUNS / "tcn_runs/v2_s43/best.weights.h5"),
        "lpn_weights": sha(RUNS / "lpn_runs/final_v2/full/best.weights.h5"),
        "labels": sha(ROOT / "annotation_v2/event_labels_v2.csv"),
    }
    published = {
        "tcn_weights": fz["tcn"]["weights_sha256"],
        "lpn_weights": fz["lpn"]["weights_sha256"],
        "labels": fz["labels"]["sha256"],
    }
    for k in used:
        if used[k] != published[k]:
            print(f"note: {k} differ from the published run", flush=True)
    import tensorflow as tf
    from train_tcn_v2 import build_model, windows, gather, average_precision
    from decision_engine import DecisionParams

    tf.config.experimental.enable_tensor_float_32_execution(False)
    d = np.load(FEAT / "test.npz")
    d = {k: d[k] for k in d.files}
    w = windows(d)
    model = build_model(43)
    model.load_weights(str(RUNS / "tcn_runs/v2_s43/best.weights.h5"))
    sw = np.empty(len(w["e"]), np.float32)
    for i in range(0, len(w["e"]), 16384):
        sw[i : i + 16384] = (
            tf.sigmoid(model(gather(d["feat"], w["e"][i : i + 16384], w["f"][i : i + 16384]), training=False))
            .numpy()
            .ravel()
        )
    s = np.zeros(len(d["t"]), np.float32)
    elig = np.zeros(len(d["t"]), bool)
    s[w["e"]] = sw
    elig[w["e"]] = True
    lab = w["y"] >= 0
    P = DecisionParams(**{k: fz["decision"][k] for k in ("theta", "k", "n", "m")})
    flags = d["clip_label_flags"]
    keep = np.array([not any(f in str(x) for f in FLAGS_EXCLUDE) for x in flags])
    cams = np.array([sid[4:8] for sid in d["clip_sample_id"]])
    rep = {
        "freeze_sha256": FREEZE_SHA,
        "split": "test",
        "operating_point": fz["decision"],
        "inputs_sha256": used,
        "matches_published_run": used == published,
        "window_ap": round(average_precision((w["y"][lab] == 1).astype(int), sw[lab]), 4),
        "primary_v2": evaluate(d, s, elig, P, (0,)),
        "secondary": {
            "v2_phase1": evaluate(d, s, elig, P, (1,)),
            "v2_phase2": evaluate(d, s, elig, P, (2,)),
            "v2_excluding_flagged_labels": evaluate(d, s, elig, P, (0,), keep),
            **{f"v2_camera_{c}": evaluate(d, s, elig, P, (0,), cams == c) for c in sorted(set(cams))},
        },
    }
    OUT.write_text(json.dumps(rep, indent=2) + "\n")
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
