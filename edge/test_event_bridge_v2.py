#!/usr/bin/env python3
"""v2 decision engine -> EventBridgeV2 -> real loopback bridge (edge_events + alerts on); no camera or models.

Checks: open/update/close reach the bridge as one event and one fall alert; heartbeats with the v2 run identity
and event_decision block are accepted; a runtime stopped mid-event closes it as runtime_stop and the next run
starts a new event; resending a delivered message is a duplicate; heartbeats never create events.
"""

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / "server" / "bridge"
sys.path.insert(0, str(HERE))
sys.path.append(str(BRIDGE))
sys.path.append(str(BRIDGE / "tests"))

from decision_engine import DecisionEngine, DecisionParams  # noqa: E402  (v2 engine, bundle copy)
from edge_event_runtime import request_json  # noqa: E402
from event_bridge_v2 import EventBridgeV2  # noqa: E402
import fall_relay_edge_runtime as runtime  # noqa: E402
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from _pg import Schema, url  # noqa: E402


class FakeCore:
    """The attributes edge_result() reads from EdgeRuntimeV2Core."""

    def __init__(self, engine):
        import numpy as np
        from features_v2 import SlotFeaturizer
        from head_point import TABLE_PATH, load_table

        self.engine, self.history_valid, self.min_valid = engine, [True] * 20, 10
        # The head-point table draws the skeleton's 13th joint. The calibrated table is NTU-derived and
        # not distributed (NOTICE); without it a neutral table (no offset) gives the same message shape.
        table = load_table() if TABLE_PATH.exists() else {"pooled": np.zeros(2), "subsets": {}}
        self.featurizer = SlotFeaturizer(table)


def wait_drained(bridge, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and bridge.box.status()["outbox_pending"]:
        time.sleep(0.02)
    assert bridge.box.status()["outbox_pending"] == 0, bridge.box.status()


def run_slots(bridge, engine, core, identity, params, server, headers, slots, t0):
    """slots: list of (score, eligible, upright). One slot per 0.1 s; heartbeat every slot."""
    for i, (score, eligible, upright) in enumerate(slots):
        sensor = t0 + i * 100_000_000
        events = engine.step(sensor / 1e9, score, eligible, upright, None)
        bridge.step(sensor, time.time(), score, eligible, 7, events)
        r = {
            "state": engine.state,
            "eligible": eligible,
            "valid": eligible,
            "k17": [[100.0, 100.0, 0.9]] * 17,
            "box": [10.0, 10.0, 60.0, 160.0],
            "people": 1,
            "track_id": 7,
            "score": score,
            "detector_score": 0.9,
        }
        payload = runtime.edge_result(r, core, params, "1", i, identity, (2.0, 2.0), 50.0, bridge, list(range(12)))
        reply = request_json(server + "/api/edge_result", headers, payload)
        assert reply.get("ok") is True, reply
    return t0 + len(slots) * 100_000_000


@contextmanager
def isolated_bridge(root, python=sys.executable, clip_dir=None):
    """Loopback bridge on a temporary DB with explicit events and alerts for camera 1; yields (url, camera headers)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = f"http://127.0.0.1:{port}"
    secret = secrets.token_urlsafe(32)
    config = {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": port,
        "cameras": [{"id": "1"}],
        "database_url_env": "FALLRELAY_V2_TEST_DATABASE_URL",
        "archive_dir": str(root / "decision_logs"),
        "clip_dir": str(clip_dir) if clip_dir else None,
        "write_token_env": "FALLRELAY_V2_TEST_WRITE_TOKEN",
        "require_camera_tokens": True,
        "require_media_token": True,
        "features": {"alerts": True},
    }
    (root / "config.json").write_text(json.dumps(config))
    log = (root / "bridge.log").open("w")
    proc = subprocess.Popen(
        [os.path.abspath(python), str(BRIDGE / "video_bridge.py"), "--config", str(root / "config.json")],
        env={**os.environ, "FALLRELAY_V2_TEST_WRITE_TOKEN": secret, "FALLRELAY_V2_TEST_DATABASE_URL": url()},
        stdout=log,
        stderr=log,
        cwd=BRIDGE,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            assert proc.poll() is None, "bridge exited: " + (root / "bridge.log").read_text()[-2000:]
            try:
                request_json(server + "/api/edge_event/context?cam=1", {})
                break
            except OSError as exc:
                if getattr(exc, "code", None) == 403:
                    break  # up, token required
                assert time.monotonic() < deadline, "bridge start timed out"
                time.sleep(0.1)
        token = request_json(server + "/api/admin/cameras/1/token", {"X-Write-Token": secret}, {})["token"]
        yield server, {"X-Camera-Token": token}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


def exercise(root, server, headers, bridges):
    manifest = json.loads((HERE / "bundle_manifest.json").read_text())
    params = DecisionParams(theta=0.95, k=3, n=5, m=3, cooldown_s=0.0)
    edge_config = root / "edge_config.json"
    edge_config.write_text("{}")
    settings = {
        "inference_hz": 10,
        "window_slots": 20,
        "min_valid_pose_slots": 10,
        "fall_threshold": 0.95,
        "fall_confirm_slots": 3,
    }

    def start_run():
        engine = DecisionEngine(params)
        run_id = uuid.uuid4().hex
        bridge = EventBridgeV2(root / "outbox.sqlite3", server, "1", run_id, engine, headers)
        bridges.append(bridge)
        identity = runtime.run_identity(run_id, manifest, edge_config, runtime.__file__, settings, params, bridge)
        return engine, bridge, identity, FakeCore(engine)

    # Run 1: normal -> fall (5 high slots) -> lying 6 s (POST_FALL, updates) -> standing 3.5 s -> recovered.
    engine, bridge, identity, core = start_run()
    slots = (
        [(0.01, True, True)] * 20 + [(0.99, True, False)] * 5 + [(0.02, True, False)] * 60 + [(0.01, True, True)] * 36
    )
    t = run_slots(bridge, engine, core, identity, params, server, headers, slots, 10**12)
    # Second fall in the same run, then the runtime stops while that event is open.
    t = run_slots(
        bridge,
        engine,
        core,
        identity,
        params,
        server,
        headers,
        [(0.01, True, True)] * 10 + [(0.99, True, False)] * 5 + [(0.02, True, False)] * 10,
        t,
    )
    wait_drained(bridge)
    delivered = json.loads(
        bridge.box.db.execute("SELECT payload FROM outbox ORDER BY created_at LIMIT 1").fetchone()[0]
    )
    bridge.close()
    bridges.remove(bridge)
    engine, bridge, identity, core = start_run()  # new run: previous outbox state already closed
    run_slots(bridge, engine, core, identity, params, server, headers, [(0.01, True, True)] * 20, t)
    wait_drained(bridge)
    dup = request_json(server + "/api/edge_event", headers, delivered)
    assert dup["duplicate"] is True, dup


def main(root):
    bridges = []
    with isolated_bridge(root) as (server, headers):
        try:
            exercise(root, server, headers, bridges)
        finally:
            for b in bridges:
                b.close()

    db = psycopg.connect(url(), row_factory=dict_row)
    events = db.execute(
        "SELECT id,source,close_reason,ended_at,max_score,last_message_seq FROM events ORDER BY id"
    ).fetchall()
    assert [e["source"] for e in events] == ["edge_event", "edge_event"], events
    assert [e["close_reason"] for e in events] == ["recovered", "runtime_stop"], events
    assert all(e["ended_at"] is not None for e in events) and events[0]["last_message_seq"] >= 4, events
    alerts = db.execute("SELECT event_id,type FROM alerts ORDER BY id").fetchall()
    assert [(a["event_id"], a["type"]) for a in alerts] == [(events[0]["id"], "fall"), (events[1]["id"], "fall")], (
        alerts
    )
    kinds = [
        r["kind"]
        for r in db.execute(
            "SELECT kind FROM edge_event_receipts WHERE event_uid=(SELECT event_uid FROM events WHERE id=%s) ORDER BY message_seq",
            (events[0]["id"],),
        )
    ]
    assert kinds[0] == "open" and kinds[-1] == "close" and set(kinds[1:-1]) == {"update"}, kinds
    runs = [json.loads(r["identity_json"]) for r in db.execute("SELECT identity_json FROM model_runs")]
    db.close()
    assert len(runs) == 2 and all(r["event_decision"]["effective"]["cooldown_s"] == 0.0 for r in runs)
    print(
        f"PASS v2 engine -> outbox -> bridge: events {[(e['close_reason'], e['last_message_seq']) for e in events]}, "
        f"alerts {len(alerts)}, messages of event 1 {kinds}, runs {len(runs)}, duplicate resend acknowledged"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, Schema():
        main(Path(tmp))
