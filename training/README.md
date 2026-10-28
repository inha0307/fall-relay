# Training the Fall-Relay v2 fall models

These scripts produce the two models the Raspberry Pi runtime uses after pose estimation,
and the NTU RGB+D evaluation reported in the paper:

- **LPN v2** lifts 13 image joints to 3D (`lpn13_fp32.tflite`).
- **TCN v2** classifies a 20-slot window of 59 features (`event_tcn_v2_fp32.tflite`).

The trained models and every file derived from NTU RGB+D (labels, calibration tables,
extracted poses) are **not** in this repository; see [NOTICE](../NOTICE). Anyone who holds an
NTU RGB+D licence can regenerate them with the steps below. The hardware-free example in the
[root README](../README.md) needs none of this.

Training reuses the runtime modules in [`../edge`](../edge) (`features_v2.py`,
`head_point.py`, `pose17.py`, `shape_features_v2.py`, `causal_tracker.py`, `decision_engine.py`,
`edge_runtime_v2_core.py`); `_paths.py` puts that folder on the import path, so the features the
models are trained on are computed by the code the Pi runs.

## What you need

- **NTU RGB+D 60**, requested from the [ROSE Lab](https://rose1.ntu.edu.sg/dataset/actionRecognition/):
  the RGB archives `nturgbd_rgb_s001.zip` … `nturgbd_rgb_s017.zip` and the `*.skeleton` files.
  Its terms allow non-commercial research only and do not allow redistribution.
- **YOLO26n-pose exported to NCNN** (Ultralytics, AGPL-3.0). The published models used the
  export made with ultralytics 8.4.37; `extract_yolo17_v2.py` checks its hashes.
  ```bash
  yolo export model=yolo26n-pose.pt format=ncnn imgsz=320
  ```
- Two Python 3.10 environments on Linux x86_64: a CPU one for extraction and labels, and a GPU one
  for training. The published run used WSL2 and an NVIDIA RTX 4070 Ti SUPER.
  ```bash
  python3 -m venv ~/.venvs/fall-relay-extract && ~/.venvs/fall-relay-extract/bin/pip install -r requirements-extract.txt
  python3 -m venv ~/.venvs/fall-relay-train   && ~/.venvs/fall-relay-train/bin/pip install -r requirements-train.txt
  ```
- Environment variables (run every command from this folder):
  ```bash
  export FALLRELAY_NTU_RGB=/data/ntu/rgb              # folder with the RGB zips
  export FALLRELAY_NTU_SKELETONS=/data/ntu/skeletons  # folder with *.skeleton
  export FALLRELAY_POSE_MODEL=/models/yolo26n-pose_ncnn_model
  export FALLRELAY_WORK=~/fall-relay-work             # derived data and runs (about 10 GB)
  X=~/.venvs/fall-relay-extract/bin/python; G=~/.venvs/fall-relay-train/bin/python
  ```

`ntu60_official_missing.txt` is the list of samples with missing or incomplete skeletons that the dataset authors publish with NTU RGB+D (github.com/shahroudy/NTURGB-D); it contains sample identifiers only.

## Steps

The split is fixed: train and validation use the 20 official NTU60 cross-subject training
subjects (4 of them held out for validation, `ntu60_protocol.json`), and the test split is the
official test subjects. **Do not extract or evaluate test data until step 7.**

| Step | Command | Output |
|---|---|---|
| 1. Manifest | `$X audit_sources_v2.py manifest` (optional: `$X audit_sources_v2.py integrity`) | `source_inventory_v2/paired_manifest_v2.csv`; checked against the frozen `split_v2.json` |
| 2. 2D pose | `$X extract_yolo17_v2.py --splits train,val` (published run: 3 h 20 min with 16 workers) | `$FALLRELAY_WORK/yolo17_v2/` |
| 3. Tracking | `$X build_tracking_v2.py` (16 min) | `$FALLRELAY_WORK/tracking_v2/` |
| 4. Fall times | `$X annotation_v2/auto_labels.py build` then `$X annotation_v2/finalize_labels.py` | `annotation_v2/event_labels_v2.csv` |
| 5. Head point | `$X calibrate_head_point.py` | `../edge/head_point_v2/head_point_v2.json` |
| 6a. LPN pairs | `$X build_kinect_cache_v2.py`, `$X build_lpn_pairs_v2.py`, `$X fit_kinect_projection_v2.py` | `$FALLRELAY_WORK/kinect_v2/`, `lpn_v2/`, `lpn_v2_meta/kinect_projection_v2.json` |
| 6b. LPN | four out-of-fold models and one full model, see below | `$FALLRELAY_WORK/lpn_runs/final_v2/{fold0..3,full}/` |
| 6c. Features | `$X build_features_v2.py prep`, then `$G build_features_v2.py lift` | `$FALLRELAY_WORK/features_v2/{train,val}.npz` |
| 6d. TCN | `$G train_tcn_v2.py --seed S --out $FALLRELAY_WORK/tcn_runs/v2_sS` for S = 42, 43, 44 | `best.weights.h5`, `event_tcn_v2_fp32.tflite` |
| 6e. Validation events | `$G evaluate_events_v2.py --runs $FALLRELAY_WORK/tcn_runs/v2_s4{2,3,4} --out val_events.json` | operating-point grid on validation |
| 7. Test, once | `$X extract_yolo17_v2.py --splits test --allow-test`, `$X build_tracking_v2.py`, `$X build_features_v2.py prep --splits test`, `$G build_features_v2.py lift --splits test`, `$G evaluate_test_v2.py` | `tcn_v2_meta/test_result_v2.json` |

LPN commands (step 6b); the fold subjects are those in `lpn_v2_meta/oof_folds_v2.json`:

```bash
A="--mode viewaug --syn-frac 0.3 --width 256 --epochs 40 --seed 42"
R=$FALLRELAY_WORK/lpn_runs/final_v2
$G train_lpn_v2.py $A --train-subjects 1,4,5,9,14,15,16,17,18,19,28,35,38 --out $R/fold0
$G train_lpn_v2.py $A --train-subjects 8,14,15,16,17,18,27,28,31,35,38 --out $R/fold1
$G train_lpn_v2.py $A --train-subjects 1,4,5,8,9,15,16,19,27,31,35,38 --out $R/fold2
$G train_lpn_v2.py $A --train-subjects 1,4,5,8,9,14,17,18,19,27,28,31 --out $R/fold3
$G train_lpn_v2.py $A --out $R/full
```

The published models are `lpn_runs/final_v2/full` and `tcn_runs/v2_s43` (highest validation
window AP of the three seeds). The operating point (θ 0.95, 3 of the last 5 slots, at least 3
eligible) was chosen on validation before any test data were extracted and is frozen in
`tcn_v2_meta/frozen_operating_point_v2.json`.

## Fall times are rule-generated

All reference fall times come from a rule applied to the Kinect skeletons
(`annotation_v2/auto_labels.py`): onset is the start of the fastest head descent and impact is
the first floor contact of a body part other than the feet, taken as the median over the three
camera views. The rule labels 314 of the 316 fall capture groups (190 train, 33 validation,
91 test). The other two give no reliable descent and are excluded; this keeps the labels
regenerable from public code. The rule's parameters (`auto_labels_v2_params.json`) were chosen
on six development groups against AI-assisted visual estimates, which are not distributed.

The published models were trained with the same 314 rule labels; validation model selection
and the operating point used one additional AI-estimated validation group. A retrained model
can therefore differ slightly from the published weights; GPU training is not bit-exact either.
`evaluate_test_v2.py` reports whether the weights and labels it used match the published run.

## Checks

```bash
$X test_shape_features_v2.py && $X test_head_point.py && $X test_decision_engine.py && $X test_causal_tracker.py
$X test_lpn_augment_v2.py       # after step 6a
$G test_features_v2.py          # after step 6c: streaming runtime features == training features
```

## Results to expect

On the NTU60 cross-subject test split, with the 314 rule labels, the published models detected
241 of 272 fall sequences in 91 capture groups (recall 0.886, 95% CI 0.839–0.930 by capture
group), produced 1.38 false alarms per hour (16 in 11.58 h) and alerted a median 0.07 s after
impact. The evaluation used a 30 s cooldown; the deployed runtime uses 0 s.
