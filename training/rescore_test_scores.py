#!/usr/bin/env python3
"""Recompute the NTU RGB+D test results of the article from the exported per-time-step fall scores,
without the trained models, TensorFlow or the NTU RGB+D data.

The scores (test_scores_v2.npz) were exported from the single test evaluation (training/evaluate_test_v2.py);
the event rule is the runtime's own edge/decision_engine.py with the frozen operating point
(theta 0.95, k/n/m 3/5/3). Usage, from the repository root:
  python training/rescore_test_scores.py path/to/test_scores_v2.npz
Needs only numpy."""

import json
import sys
from pathlib import Path

import numpy as np

TRAINING = Path(__file__).resolve().parent
sys.path.insert(0, str(TRAINING))
import evaluate_test_v2 as E  # noqa: E402  (imports _paths, which puts ../edge on sys.path)
from decision_engine import DecisionParams  # noqa: E402
from evaluate_events_v2 import simulate  # noqa: E402

d = np.load(sys.argv[1])
d = {k: d[k] for k in d.files}
fz = json.loads(E.FREEZE.read_text())
P = DecisionParams(**{k: fz["decision"][k] for k in ("theta", "k", "n", "m")})
flags = d["clip_label_flags"]
keep = np.array([not any(f in str(x) for f in E.FLAGS_EXCLUDE) for x in flags])

# Sequence-level counts as defined in the article: a fall sequence is detected when an event opens between
# 0.3 s before the fall onset and the end of the sequence; a non-fall sequence is a false positive when any event opens.
tp = fn = fp = tn = 0
for clip, opens, _ in simulate(d, d["score"], d["eligible"], P, (0,)):
    if not keep[clip]:
        continue
    onset = d["clip_onset_s"][clip]
    if np.isfinite(onset):
        if any(t >= onset - 0.3 for t in opens):
            tp += 1
        else:
            fn += 1
    elif opens:
        fp += 1
    else:
        tn += 1
precision, recall = tp / (tp + fp), tp / (tp + fn)
report = {
    "fall_sequences": tp + fn,
    "detected": tp,
    "sensitivity": round(recall, 3),
    "non_fall_sequences": fp + tn,
    "false_positive_sequences": fp,
    "specificity": round(tn / (tn + fp), 4),
    "accuracy": round((tp + tn) / (tp + fn + fp + tn), 4),
    "f1": round(2 * precision * recall / (precision + recall), 3),
    "event_level": E.evaluate(d, d["score"], d["eligible"], P, (0,), keep),
    "excluded_fall_sequences_with_flagged_labels": int(sum(np.isfinite(d["clip_onset_s"][~keep]))),
}
print(json.dumps(report, indent=1))
