"""Explicit edge events for the v2 runtime (bridge contract fallscope-edge-event-v1).

The fall decision is unchanged: decision_engine.DecisionEngine with the frozen operating point decides.
This module only turns its transitions into durable, ordered bridge messages:
  engine 'open'                 -> open   (state ALERT)
  ALERT -> POST_FALL, and every update_interval_s while the event is open -> update
  engine 'close' (recovered | timeout | track_lost), or runtime shutdown    -> close (state COOLDOWN)
Messages go through the shared outbox and transport from server/bridge (edge_outbox,
edge_event_runtime.OutboxWorker): written to SQLite before sending, resent until the bridge acknowledges,
and an event left open by a previous run is closed as runtime_stop on start-up.
"""

from collections import deque
import time

from edge_event_contract import CLOSE_REASONS, CONTRACT
from edge_event_runtime import OutboxWorker, request_json
from edge_outbox import EdgeOutbox

UPDATE_INTERVAL_S = 2.0


def decision_block(params, update_interval_s=UPDATE_INTERVAL_S):
    """The engine parameters in the bridge's decision-block form (model_contract.validate_decision)."""
    return {
        "theta": params.theta,
        "k": params.k,
        "n": params.n,
        "m": params.m,
        "post_fall_after_s": params.post_fall_after_s,
        "recover_hold_s": params.recover_hold_s,
        "cooldown_s": params.cooldown_s,
        "max_event_s": params.max_event_s,
        "track_lost_s": params.track_lost_s,
        "update_interval_s": update_interval_s,
    }


class EventBridgeV2:
    def __init__(
        self,
        outbox_path,
        server,
        camera,
        run_id,
        engine,
        headers,
        update_interval_s=UPDATE_INTERVAL_S,
        request_fn=request_json,
        start_worker=True,
    ):
        self.camera, self.run_id, self.engine = camera, run_id, engine
        self.decision = decision_block(engine.params, update_interval_s)
        self.box = EdgeOutbox(outbox_path)
        self.box.recover(camera, time.time(), self.decision["cooldown_s"])
        self.worker = OutboxWorker(self.box, server, camera, headers, request_fn)
        self.slots = deque(maxlen=engine.params.n)  # (sensor_ns, high) of the decision window -> event onset
        self.active = None
        self.last_sensor = None
        self.heartbeat_seq = 0
        self.started = start_worker
        if start_worker:
            self.worker.start()

    def _record(self, kind, state, sensor_ns, wall, reason=None):
        a = self.active
        self.box.record(
            self.camera,
            self.run_id,
            {
                "kind": kind,
                "state": state,
                "sensor_ns": sensor_ns,
                "decided_at": wall,
                "onset_sensor_ns": a["onset"],
                "track_id": a["track"],
                "max_score": a["max_score"],
                "close_reason": reason,
            },
            self.decision,
            self.worker.clock(),
            self.decision["cooldown_s"],
        )

    def step(self, sensor_ns, wall, score, eligible, track_id, events):
        """Call once per decision slot, after engine.step(); `events` is what that step returned."""
        if self.started and self.worker.stop.is_set():
            raise RuntimeError("outbox worker failed; refusing unrecorded monitoring")
        self.last_sensor = sensor_ns
        self.slots.append((sensor_ns, bool(eligible and score >= self.engine.params.theta)))
        for ev in events:
            if ev["kind"] == "open":
                # Onset = first high slot of the window that confirmed the event (sensor clock).
                onset = min((t for t, high in self.slots if high), default=sensor_ns)
                self.active = {
                    "onset": onset,
                    "track": max(0, int(track_id)),
                    "max_score": float(ev["max_score"]),
                    "state": "ALERT",
                    "last_update": sensor_ns,
                }
                self._record("open", "ALERT", sensor_ns, wall)
            elif ev["kind"] == "close" and self.active:
                self.active["max_score"] = max(self.active["max_score"], float(ev["max_score"]))
                self._record(
                    "close",
                    "COOLDOWN",
                    sensor_ns,
                    wall,
                    ev["reason"] if ev["reason"] in CLOSE_REASONS else "runtime_stop",
                )
                self.active = None
        a = self.active
        if a and self.engine.state in ("ALERT", "POST_FALL") and not any(ev["kind"] == "open" for ev in events):
            a["max_score"] = max(a["max_score"], float(self.engine.max_score))
            if (
                self.engine.state != a["state"]
                or (sensor_ns - a["last_update"]) / 1e9 >= self.decision["update_interval_s"]
            ):
                a["state"], a["last_update"] = self.engine.state, sensor_ns
                self._record("update", a["state"], sensor_ns, wall)

    def heartbeat(self, now=None):
        """Fields the bridge requires on /api/edge_result from an explicit-event camera."""
        self.heartbeat_seq += 1
        window = list(self.engine.window)
        return {
            "event_protocol": "edge_event",
            "contract": CONTRACT,
            "run_id": self.run_id,
            "decision_state": self.engine.state,
            "heartbeat_seq": self.heartbeat_seq,
            "decision_high_slots": sum(h for _, h in window),
            "decision_valid_slots": sum(e for e, _ in window),
            "decision_overridden": False,
            **self.box.status(now),
        }

    def close(self, wait_s=1.5):
        """Close an open event as runtime_stop, give the sender a moment, then release the outbox."""
        try:
            if self.active and self.last_sensor is not None:
                self._record("close", "COOLDOWN", self.last_sensor, time.time(), "runtime_stop")
                self.active = None
        finally:
            if self.started:
                deadline = time.monotonic() + wait_s
                while (
                    not self.worker.stop.is_set()
                    and time.monotonic() < deadline
                    and self.box.status()["outbox_pending"]
                ):
                    self.worker.stop.wait(0.05)
                self.worker.close()
            self.box.close()
