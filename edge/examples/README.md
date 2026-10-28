# Connecting another detector

`any_detector_adapter.py` is the smallest program that speaks Fall-Relay's event contract. It turns a stream of
per-slot scores from *any* fall detector into durable event messages: the same latched decision engine as the
Raspberry Pi runtime opens and closes events, `EventBridgeV2` commits every open/update/close to a local SQLite
outbox and delivers it to the bridge's `/api/edge_event` until a receipt comes back. The bridge then raises,
delivers and lets responders handle the alert exactly as for a device. No camera, pose model or trained fall
model is needed, so this is the path for a reader who has no NTU RGB+D licence and cannot regenerate the
bundled models, and for anyone who wants to put a different detector behind the same delivery and handling.

```bash
# 1. a bridge with camera 1 (the hardware-free example starts one: server/bridge/demo.py --serve prints the URL)
# 2. the camera token issued by the bridge (demo: see the report; deployment: /api/admin/cameras/<id>/token)
export FALLRELAY_CAMERA_TOKEN=<token>
python3 edge/examples/any_detector_adapter.py --server http://127.0.0.1:<port> --camera 1 \
    --input edge/examples/sample_scores.csv --outbox /tmp/adapter_outbox.sqlite3
```

`sample_scores.csv` is a hand-written sequence: 2 s of low scores, a rise above the 0.95 threshold, lying for a
few seconds, then upright again. With the deployed operating point (θ 0.95, 3 of 5 slots, cooldown 0) it opens one
event at 2.2–2.4 s, updates it while the person lies, and closes it with reason `recovered` after 3 s upright. The
adapter prints one JSON line with the run id, the number of events opened and closed and the outbox status; a
non-empty `outbox_pending` at the end means the bridge never acknowledged some message (exit status 1).

Input columns: `t_s` (seconds, increasing), `score` (0–1), `eligible` (1 when enough of the person was visible for
the score to count), `upright` and `lying` (1/0, or empty when the detector cannot say). `--input -` reads JSON
lines with the same keys from standard input, so a live detector can pipe into the adapter. `--realtime` paces the
rows by `t_s`; otherwise they are processed as fast as the bridge accepts them.

What the adapter does not do: it sends no heartbeats (`/api/edge_result`), because a heartbeat declares the run
identity of the bundled model (pose, LPN and TCN hashes) that a foreign detector cannot truthfully claim. The
camera therefore shows no skeleton or decision rate in the web application; events, receipts, alerts,
acknowledgements and outcomes are stored as for a device. Restarting the adapter with the same `--outbox` replays
unacknowledged messages in order and closes an event left open by the previous run with reason `runtime_stop`.

The check `edge/test_any_detector_adapter.py` (run by `server/bridge/run_checks.py`) starts a loopback bridge,
runs the adapter on the sample file and verifies the stored event, its receipts and the alert.
