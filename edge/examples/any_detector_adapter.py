#!/usr/bin/env python3
"""Connect any fall detector to Fall-Relay: per-slot scores in -> durable event messages out.

The adapter is the smallest program that uses the event contract. It reads one row per decision slot
(time, score, eligible, upright, lying), runs the same latched k-of-n decision engine as the Raspberry Pi
runtime, and hands every open/update/close of a fall event to EventBridgeV2, which commits it to a local
SQLite outbox and delivers it to the bridge's /api/edge_event until the bridge acknowledges it with a receipt.
No camera, pose model or trained fall model is involved: the scores can come from any detector, a recording,
or a file such as examples/sample_scores.csv.

What it does not do: it sends no heartbeats (/api/edge_result), because a heartbeat carries the run identity
of the bundled model (pose model, LPN and TCN hashes) that a foreign detector cannot truthfully declare.
The camera therefore shows no skeleton or decision rate in the web application; events and alerts are
stored, delivered and handled exactly as for a device.

Usage (the bridge must know the camera and issue its token, see server/bridge/README.md):

    export FALLRELAY_CAMERA_TOKEN=<token for the camera>
    python3 edge/examples/any_detector_adapter.py --server http://127.0.0.1:5102 --camera 1 \
        --input edge/examples/sample_scores.csv --outbox /tmp/adapter_outbox.sqlite3

Input: CSV with a header. Columns: t_s (seconds, increasing), score (0-1), eligible (0/1: enough of the
person was visible for the score to count), upright (0/1 or empty), lying (0/1 or empty). `-` reads JSON
lines with the same keys from standard input. Rows are processed as fast as they arrive unless --realtime
is given, which sleeps according to t_s.
"""

import argparse
import csv
import json
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
sys.path.insert(0, str(EDGE))
sys.path.append(str(EDGE.parent / "server" / "bridge"))

from decision_engine import DecisionEngine, DecisionParams  # noqa: E402
from event_bridge_v2 import EventBridgeV2  # noqa: E402


def read_rows(source):
    if source == "-":
        for line in sys.stdin:
            if line.strip():
                yield json.loads(line)
        return
    with open(source, newline="") as fh:
        for row in csv.DictReader(fh):
            yield row


def flag(value):
    """'1'/'0'/'true'/'false'/'' -> True/False/None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return None
    return text in ("1", "true", "t", "yes", "y")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", required=True, help="bridge URL, e.g. http://127.0.0.1:5102")
    parser.add_argument("--camera", required=True, help="camera id registered on the bridge")
    parser.add_argument(
        "--token-env", default="FALLRELAY_CAMERA_TOKEN", help="environment variable with the camera token"
    )
    parser.add_argument(
        "--input", required=True, help="CSV file with t_s,score,eligible[,upright,lying] or - for JSON lines"
    )
    parser.add_argument("--outbox", type=Path, required=True, help="SQLite outbox file (kept across restarts)")
    parser.add_argument("--realtime", action="store_true", help="pace rows by t_s instead of as fast as possible")
    parser.add_argument("--track-id", type=int, default=1, help="track id written into the event messages")
    parser.add_argument("--theta", type=float, default=0.95, help="score threshold (deployed operating point: 0.95)")
    parser.add_argument("--k", type=int, default=3, help="high slots needed among the last n (deployed: 3)")
    parser.add_argument("--n", type=int, default=5, help="window length in slots (deployed: 5)")
    parser.add_argument("--m", type=int, default=3, help="eligible slots needed among the last n (deployed: 3)")
    parser.add_argument("--cooldown-s", type=float, default=0.0, help="cooldown after close (deployed: 0)")
    parser.add_argument("--drain-timeout", type=float, default=30.0, help="seconds to wait for receipts at the end")
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"set {args.token_env} to the camera token issued by the bridge")
    params = DecisionParams(theta=args.theta, k=args.k, n=args.n, m=args.m, cooldown_s=args.cooldown_s)
    engine = DecisionEngine(params)
    run_id = uuid.uuid4().hex
    headers = {"X-Camera-Token": token}
    bridge = EventBridgeV2(args.outbox, args.server, str(args.camera), run_id, engine, headers)
    base_ns = time.monotonic_ns()
    opened = closed = rows = 0
    started = time.monotonic()
    first_t = None
    try:
        for row in read_rows(args.input):
            t_s = float(row["t_s"])
            if first_t is None:
                first_t = t_s
            if args.realtime:
                wait = (t_s - first_t) - (time.monotonic() - started)
                if wait > 0:
                    time.sleep(wait)
            score = float(row["score"])
            eligible = bool(flag(row.get("eligible", "1")))
            sensor_ns = base_ns + int(t_s * 1e9)
            events = engine.step(t_s, score, eligible, flag(row.get("upright")), flag(row.get("lying")))
            bridge.step(sensor_ns, time.time(), score, eligible, args.track_id, events)
            opened += sum(ev["kind"] == "open" for ev in events)
            closed += sum(ev["kind"] == "close" for ev in events)
            rows += 1
        # An event still open at the end of the input is closed by the engine's end-of-stream rule.
        if first_t is not None:
            end_events = engine.finish(t_s)
            if end_events:
                bridge.step(sensor_ns, time.time(), 0.0, False, args.track_id, end_events)
                closed += 1
        deadline = time.monotonic() + args.drain_timeout
        while bridge.box.status()["outbox_pending"] and time.monotonic() < deadline:
            time.sleep(0.05)
        status = bridge.box.status()
    finally:
        bridge.close()
    report = {"run_id": run_id, "rows": rows, "events_opened": opened, "events_closed": closed, "outbox": status}
    print(json.dumps(report))
    return 0 if status["outbox_pending"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
