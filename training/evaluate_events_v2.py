#!/usr/bin/env python3
"""Phase 4.4: event-level validation on VAL sequences with the deployed decision logic.

Every val sequence (clip x 10 Hz phase) is replayed slot by slot through decision_engine, single-camera like
the device.
  fall sequence (A043): TP if an event opens at t >= onset - 0.3 s; earlier opens are false alarms;
                        latency = open time - impact
  other actions       : every open is a false alarm; FA/hour uses the sequence duration (10 Hz slots)
Primary numbers use phase-0 sequences (phases 1/2 are the same clips with shifted sampling).
"""

import argparse, json, sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import _paths  # noqa: E402  (also puts edge on sys.path)

FEAT = _paths.FEATURES
TRACK = _paths.TRACKING / "clips"
HARD_NEG = (5, 6, 8, 9, 24, 26, 27, 42, 48)
GRID_THETA = (0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99)
GRID_KNM = ((2, 3, 2), (3, 5, 3), (4, 6, 4), (5, 8, 5), (6, 8, 6), (6, 10, 6), (8, 10, 8))


def slot_scores(d, scores_npz):
    s = np.zeros(len(d["t"]), np.float32)
    elig = np.zeros(len(d["t"]), bool)
    z = np.load(scores_npz)
    s[z["e"]] = z["score"]
    elig[z["e"]] = True
    return s, elig


def simulate(d, s, elig, params, phases=(0,)):
    from decision_engine import DecisionEngine

    rows = []
    for a, b, c, p in zip(d["seq_start"], d["seq_stop"], d["seq_clip"], d["seq_phase"]):
        if p not in phases:
            continue
        opens = []
        eng = DecisionEngine(params)
        for i in range(a, b):
            for ev in eng.step(float(d["t"][i]), float(s[i]), bool(elig[i])):
                if ev["kind"] == "open":
                    opens.append(float(d["t"][i]))
        rows.append((c, opens, (b - a) * 0.1))
    return rows


def summarise(d, rows):
    on, im, act = d["clip_onset_s"], d["clip_impact_s"], d["clip_action"]
    tp = fn = 0
    lat = []
    fa_fall = 0
    fa = 0
    hours = 0.0
    fa_hard = 0
    hard_hours = 0.0
    for c, opens, dur in rows:
        if np.isfinite(on[c]):
            ok = [t for t in opens if t >= on[c] - 0.3]
            fa_fall += len(opens) - len(ok)
            if ok:
                tp += 1
                lat.append(ok[0] - im[c])
            else:
                fn += 1
            hours += dur / 3600
        else:
            fa += len(opens)
            hours += dur / 3600
            if act[c] in HARD_NEG:
                fa_hard += len(opens)
                hard_hours += dur / 3600
    total_fa = fa + fa_fall
    return {
        "fall_sequences": tp + fn,
        "recall": round(tp / max(tp + fn, 1), 3),
        "false_alarms": total_fa,
        "fa_per_hour": round(total_fa / hours, 2),
        "hours": round(hours, 2),
        "fa_per_hour_hard_actions": round(fa_hard / max(hard_hours, 1e-9), 2),
        "hard_hours": round(hard_hours, 2),
        "latency_vs_impact_s_median": round(float(np.median(lat)), 2) if lat else None,
        "latency_vs_impact_s_p90": round(float(np.percentile(lat, 90)), 2) if lat else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="*", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from decision_engine import DecisionParams

    d = np.load(FEAT / "val.npz")
    d = {k: d[k] for k in d.files}
    report = {"split": "val", "phases": [0], "models": {}}
    for run in args.runs:
        s, e = slot_scores(d, Path(run) / "val_scores.npz")
        grid = []
        for th in GRID_THETA:
            for k, n, m in GRID_KNM:
                r = summarise(d, simulate(d, s, e, DecisionParams(theta=th, k=k, n=n, m=m)))
                r.update(theta=th, k=k, n=n, m=m)
                grid.append(r)
        report["models"][Path(run).name] = grid
        print(Path(run).name, "grid done", flush=True)
    Path(args.out).write_text(json.dumps(report, indent=1) + "\n")


if __name__ == "__main__":
    main()
