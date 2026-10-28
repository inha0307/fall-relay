"""Fall-Relay edge inference core (v2): one call per camera frame, no camera or network code.

The Pi runtime wraps this with Picamera2 capture and HTTP upload; replay tools feed recorded frames.
Pipeline per 10 Hz slot (latest frame of the slot):
  prepare_frame -> detect17 (YOLO26n-pose NCNN) -> CausalTracker (12 joints, deployed) ->
  SlotFeaturizer (13 joints, dynamic7-v2) -> LPN v2 (TFLite, valid slots) -> 20-slot history
  (cleared on primary reset, missed slots as invalid zeros) -> TCN v2 when eligible ->
  posture flags (torso tilt) -> DecisionEngine (k-of-n with latch).
"""

import math
from pathlib import Path  # noqa: F401  (unused, kept: the frozen code identity includes this import)

import numpy as np

from causal_tracker import CausalTracker
from decision_engine import DecisionEngine, DecisionParams
from features_v2 import SlotFeaturizer, assemble, ORDER
from pose17 import prepare_frame, detect17
from shape_features_v2 import _measure

NS = 1_000_000_000


def load_tflite(path):
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        from tensorflow.lite import Interpreter
    it = Interpreter(model_path=str(path), num_threads=1)
    it.allocate_tensors()
    return it, it.get_input_details()[0]["index"], it.get_output_details()[0]["index"]


class EdgeRuntimeV2Core:
    def __init__(
        self,
        net,
        lpn_path,
        tcn_path,
        params=None,
        hz=10.0,
        slots=20,
        min_valid=10,
        upright_max_deg=35.0,
        lying_min_deg=60.0,
        still_dy=0.1,
        upright_min_aspect=1.3,
    ):
        self.net = net
        self.period_ns = round(NS / hz)
        self.slots, self.min_valid = slots, min_valid
        self.lpn = load_tflite(lpn_path)
        self.tcn = load_tflite(tcn_path)
        self.engine = DecisionEngine(params or DecisionParams())
        self.tracker = CausalTracker()
        self.featurizer = SlotFeaturizer()
        self.upright_max, self.lying_min, self.still_dy = (
            math.radians(upright_max_deg),
            math.radians(lying_min_deg),
            still_dy,
        )
        # Upright (used to close an event as recovered) also needs a tall person box: a squatting or seated person
        # keeps a vertical torso (session 6: stand 1.85-2.49, squat 1.21, sit 0.78, lie 0.29-0.42 box height/width).
        self.upright_min_aspect = upright_min_aspect
        self.origin = None
        self.last_grid = -1
        self.history = []
        self.history_valid = []

    def _lift(self, x):
        it, i, o = self.lpn
        it.set_tensor(i, x[None].astype(np.float32))
        it.invoke()
        return it.get_tensor(o)[0]

    def _classify(self, w):
        it, i, o = self.tcn
        it.set_tensor(i, w[None].astype(np.float32))
        it.invoke()
        return float(it.get_tensor(o).ravel()[0])

    def process(self, image_bgr, sensor_ns):
        """Returns None when the frame does not start a new slot, else a result dict."""
        if self.origin is None:
            self.origin = sensor_ns
        grid = (sensor_ns - self.origin) // self.period_ns
        if grid <= self.last_grid:
            return None
        t = sensor_ns / NS
        dets = detect17(self.net, prepare_frame(image_bgr))
        tracked = [
            {"box": d["box"], "keypoints": d["keypoints"][ORDER], "score": d["score"], "_k17": d["keypoints"]}
            for d in dets
        ]
        chosen, identity, reset = self.tracker.update(tracked)
        k17 = chosen["_k17"] if chosen is not None else np.full((17, 3), np.nan, np.float32)
        x, valid, dyn = self.featurizer.step(k17, t, identity, reset)
        xyz = self._lift(x) if valid else np.zeros((13, 3), np.float32)
        feat = assemble(xyz[None], x[None], [valid], dyn[None])[0]
        if reset:
            self.history, self.history_valid = [], []
        elif self.last_grid >= 0:
            missing = min(self.slots, max(0, int(grid - self.last_grid - 1)))
            self.history += [np.zeros(59, np.float32)] * missing
            self.history_valid += [False] * missing
        self.history.append(feat)
        self.history_valid.append(valid)
        self.history, self.history_valid = self.history[-self.slots :], self.history_valid[-self.slots :]
        self.last_grid = int(grid)
        eligible = bool(valid and sum(self.history_valid) >= self.min_valid)
        score = 0.0
        if eligible:
            w = np.zeros((self.slots, 59), np.float32)
            w[-len(self.history) :] = self.history
            score = self._classify(w)
        upright = lying = None
        if valid:
            _, tilt, _ = _measure(k17[ORDER], 0.0)
            if math.isfinite(tilt):
                box = chosen["box"]
                aspect = float(box[3] - box[1]) / max(float(box[2] - box[0]), 1.0)
                upright = bool(tilt < self.upright_max and aspect >= self.upright_min_aspect)
                lying = bool(tilt > self.lying_min and abs(float(dyn[4])) < self.still_dy and dyn[6] > 0)
        events = self.engine.step(t, score, eligible, upright, lying)
        return {
            "t": t,
            "grid": int(grid),
            "valid": bool(valid),
            "eligible": eligible,
            "score": score,
            "state": self.engine.state,
            "events": events,
            "track_id": int(identity),
            "reset": bool(reset),
            "upright": upright,
            "lying": lying,
            "people": len(dets),
            "k17": k17,
            "box": None if chosen is None else [float(v) for v in chosen["box"]],  # 640x360 pose coordinates
            "detector_score": None if chosen is None else float(chosen["score"]),
        }
