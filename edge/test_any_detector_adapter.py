#!/usr/bin/env python3
"""examples/any_detector_adapter.py -> real loopback bridge: a foreign score stream becomes one event and one alert.

Checks: the sample CSV opens one event that closes as recovered; every message (open, updates, close) has a receipt;
the bridge raised one fall alert; the outbox is empty at the end; no heartbeat or run identity is involved.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from test_event_bridge_v2 import isolated_bridge  # noqa: E402
from _pg import Schema, url  # noqa: E402


def main(root):
    with isolated_bridge(root) as (server, headers):
        env = {**os.environ, "FALLRELAY_CAMERA_TOKEN": headers["X-Camera-Token"]}
        proc = subprocess.run(
            [
                sys.executable,
                str(HERE / "examples" / "any_detector_adapter.py"),
                "--server",
                server,
                "--camera",
                "1",
                "--input",
                str(HERE / "examples" / "sample_scores.csv"),
                "--outbox",
                str(root / "adapter_outbox.sqlite3"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["events_opened"] == 1 and report["events_closed"] == 1, report
        assert report["outbox"]["outbox_pending"] == 0, report

    db = psycopg.connect(url(), row_factory=dict_row)
    events = db.execute("SELECT id,source,close_reason,ended_at,last_message_seq FROM events").fetchall()
    assert len(events) == 1 and events[0]["source"] == "edge_event", events
    assert events[0]["close_reason"] == "recovered" and events[0]["ended_at"] is not None, events
    kinds = [
        r["kind"]
        for r in db.execute(
            "SELECT kind FROM edge_event_receipts WHERE event_uid=(SELECT event_uid FROM events WHERE id=%s) ORDER BY message_seq",
            (events[0]["id"],),
        )
    ]
    assert kinds[0] == "open" and kinds[-1] == "close" and len(kinds) == events[0]["last_message_seq"], kinds
    alerts = db.execute("SELECT event_id,type FROM alerts").fetchall()
    assert [(a["event_id"], a["type"]) for a in alerts] == [(events[0]["id"], "fall")], alerts
    runs = db.execute("SELECT COUNT(*) AS n FROM model_runs").fetchone()["n"]
    db.close()
    assert runs == 0, "the adapter must not register a model run identity"
    print(f"PASS any-detector adapter -> outbox -> bridge: 1 event ({kinds}), 1 fall alert, no run identity")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, Schema():
        main(Path(tmp))
