# Fall detection model (default detector)

The default detector of Fall-Relay is the one whose NTU RGB+D results are reported in the article; this repository holds its code and training scripts, not its trained weights;
it is a component behind the event contract, not a contribution in itself. Any detector can replace it as long as the runtime emits the event contract
([docs/EVENT_PROTOCOL.md](../docs/EVENT_PROTOCOL.md)). Training code and the full evaluation procedure are in
[`training/README.md`](../training/README.md). The trained weights are not distributed with the source; see
[NOTICE](../NOTICE).

## Pipeline

```text
camera 1280×720, 30 fps
 └ every 100 ms slot: newest frame
   → 640×360 → YOLO26n-pose (NCNN, fp32, 320 letterbox, CPU) → 17 COCO keypoints per person
   → causal tracker: one primary person (body joints only, no appearance or identity features)
   → 13 joints: 12 body joints + a calibrated head point, hip-centred, visibility mask
   → LPN (LiteRT, [1,39] → [1,13,3]): 2D-to-3D lifting, on valid slots only
   → 59 features per slot: 39 3D coordinates + 13 mask values + 7 dynamic features
   → window of 20 slots (2 s) → TCN (LiteRT, [1,20,59] → [1,1]) → score, on eligible slots only
   → decision engine → events (open / update / close) → outbox
```

| Item | Definition |
|---|---|
| Model contract | `fallscope-pose13-feature59-v2` (`edge/features_v2.py`) |
| Valid slot | Both hips and at least 8 of the 13 joints visible |
| Eligible slot | The current slot is valid and at least 10 of the last 20 slots are valid |
| Dynamic features | Vertical and horizontal hip velocity, trunk angular velocity, 1 s hip drop, 1 s trunk tilt change, two validity flags; normalised by body length |
| Head point | Computed from the face keypoints and calibrated to the NTU Kinect head joint (`head_point_v2.json`, derived from NTU RGB+D, not distributed) |

## Decision rule

Frozen with the test evaluation and declared by every run in its identity (`edge/bundle_manifest.json`, `decision`).

| Parameter | Value | Meaning |
|---|---|---|
| `theta` | 0.95 | Score threshold |
| `k` / `n` / `m` | 3 / 5 / 3 | Open an event when, of the last 5 slots, at least 3 score ≥ `theta` and at least 3 are eligible; ineligible slots count as neither |
| Latch | on | While an event is open, later high scores update it instead of opening a new one |
| `post_fall_after_s` | 5 | State `POST_FALL` 5 s after opening, or earlier when lying still is detected |
| `recover_hold_s` | 3 | Close as `recovered` after 3 s upright (trunk tilt < 35° and person box height/width ≥ 1.3) |
| `track_lost_s` | 10 | Close as `track_lost` after 10 s without an eligible slot |
| `max_event_s` | 600 | Close as `timeout` after 10 min |
| `cooldown_s` | 0 | A new event may open right after a close |

Two settings were changed after the test evaluation (recorded in `post_test_runtime_changes` of the bundle manifest):
the upright condition gained the box aspect requirement and `cooldown_s` went from 30 to 0. The NTU test sequences are
shorter than the cooldown and offline replays carry no posture flags, so the test figures below do not depend on them.

## Evaluation on NTU RGB+D

| Item | Value |
|---|---|
| Data | NTU RGB+D 60, actions A001–A049, cross-subject split (train 16 subjects, validation 4, test 20); pose re-extracted from the RGB videos with the same NCNN model as the device |
| Reference fall times | Rule-generated from the Kinect skeletons (`training/annotation_v2/auto_labels.py`): onset at the start of the fastest head descent, impact at the first non-foot floor contact. 314 of 316 fall capture groups; the 2 without a reliable descent are excluded |
| Detection | An alert in a fall (A043) sequence that opens after onset − 0.3 s; every other alert is a false alarm |
| Procedure | Operating point chosen on validation and frozen (`training/tcn_v2_meta/frozen_operating_point_v2.json`) before the test pose was extracted; test evaluated once, 10 Hz, single camera view per sequence |

| Metric (test; 272 fall and 13,184 non-fall sequences) | Value |
|---|---|
| Recall (sensitivity) | 0.886 (241 of 272 fall sequences, 91 capture groups; 95% CI 0.839–0.930, bootstrap over capture groups) |
| False alarms | 15 of 13,184 non-fall sequences (16 events); specificity 99.9 % |
| Per-sequence accuracy, precision, F1 | 99.66 %, 94.1 %, 91.3 % (with a 1:1 balanced negative set the accuracy would be 94.2 %) |
| Alert time | Median 0.07 s after impact |

The rule parameters were chosen and audited against AI-assisted visual estimates on development groups, not against
human annotation, and the validation model selection used one additional AI-estimated group. The `provenance` field of
`edge/bundle_manifest.json` keeps the first test report (recall 0.884), which used reference times that included the
two AI-estimated groups; the figures above use rule-generated times only.

## On the devices

NTU RGB+D replay through the deployed bundle on the released software, clip upload off (2 × 20 min per device; median / 95th percentile; `docs/evidence/replay`):

| Device | Decision rate | Processing per slot | CPU | Peak temperature |
|---|---|---|---|---|
| Raspberry Pi 5 (BCM2712 at 2.0 GHz as configured, 4 GB, active cooler), Camera Module 3 | 10.11 Hz | 47.6 / 61.0 ms | 26 % | 62.2 °C |
| Raspberry Pi 4 Model B (BCM2711 at 1.8 GHz, 8 GB, armour case with two fans), Arducam IMX519 | 7.32 Hz | 129.4 / 146.6 ms | 39 % | 52.1 °C |

The Pi 4 misses 27.6 % of the 10 Hz slots; missed slots enter the window as invalid, so it raises fewer events
from the same input. The benchmark procedure is in [`edge/ntu_benchmark/README.md`](ntu_benchmark/README.md).

## Limitations

1. **False alarms in daily activities.** On NTU most false alarms came from picking up an object (A006) and staggering
   (A042); lying down and long stillness are absent from NTU, so behaviour on them is known only from operation.
2. **NTU sequences are short clips, not monitoring time.** False alarms are counted per sequence of a few seconds.
3. **Late detections are not counted.** A clip ends about 0.9 s after impact, which bounds the detection time.
4. **Reference times are rule-generated**, not human annotations.
5. **No field evaluation.** The software has not been evaluated with responders in a field setting and is not
   clinically validated.
