# Event delivery and alert handling

This document specifies how a fall decision on an edge device becomes an alert that a responder handles. It describes
the implemented behaviour; the code is the reference (`server/bridge/edge_event_contract.py`, `edge_outbox.py`,
`edge_event_runtime.py`, `edge_event_store.py`, `alert_store.py`, `notification_policy.py`, `watchdog.py`,
`routers/edge.py`, `routers/alerts.py`). The checks that exercise each rule are listed at the end.

Two records are kept apart on purpose:

- a **detection event** describes what the device observed: it opens, is updated and closes, driven by the decision
  engine on the device;
- an **alert** describes the work of a responder: it is delivered, acknowledged and resolved with an outcome.

Closing an event never resolves its alert, and resolving an alert never changes the event's timeline.

## 1. Identifiers and time

| Item | Rule |
|---|---|
| `camera_id` | 1–32 characters `[A-Za-z0-9_-]`; must be registered on the server |
| `run_id` | 32 hex characters, new for every start of the edge runtime |
| `event_uid` | `<run_id>:<event counter>`, created on the device; unique across runs |
| `message_seq` | 1 for `open`, then +1 for every `update` and the final `close` of the same event |
| Wall-clock times | Unix seconds (UTC). `decided_at` and `sent_at` are device clock, `received_at` is server clock |
| Sensor times | `onset_sensor_ns`, `decided_sensor_ns`: the camera's monotonic sensor timestamps |
| Clock | `clock.offset_s` = server time − device time, `clock.rtt_s` = round trip of the calibration request |

The contract string `fallscope-edge-event-v1` is carried in every message. (The identifier predates the project name and
is kept so that stored records stay comparable; see the README section "Naming".)

## 2. Event messages (`POST /api/edge_event`)

Authenticated with the camera's token (`X-Camera-Token`, issued on the Server page). Body limit 32 KiB. Exactly these
fields, all required:

```json
{
  "contract": "fallscope-edge-event-v1",
  "event_uid": "5139afe5daf0472e97e5c62c2d333a25:7",
  "message_seq": 1,
  "camera_id": "1",
  "run_id": "5139afe5daf0472e97e5c62c2d333a25",
  "kind": "open",
  "state": "ALERT",
  "onset_sensor_ns": 1765424159000,
  "decided_sensor_ns": 1765424161100,
  "decided_at": 1792357331.29,
  "sent_at": 1792357331.31,
  "clock": {"offset_s": 0.018, "rtt_s": 0.004},
  "max_score": 0.97,
  "confirm": {"k": 3, "n": 5, "m": 3},
  "track_id": 4,
  "close_reason": null,
  "attempt": 1
}
```

| Field | Constraint |
|---|---|
| `kind` / `state` | `open` → `ALERT`; `update` → `ALERT` or `POST_FALL`; `close` → `COOLDOWN` |
| `close_reason` | Required on `close`: `recovered`, `timeout`, `track_lost` or `runtime_stop`; `null` otherwise |
| `confirm` | The decision rule that opened the event: at least `k` of the last `n` slots scored above the threshold and at least `m` were eligible; integers with `1 ≤ k ≤ m ≤ n ≤ 300` |
| `decided_sensor_ns` | ≥ `onset_sensor_ns` |
| `max_score` | In [0, 1] |
| `attempt`, `sent_at` | Transport fields: they change on every retry |

The **semantic hash** of a message is the SHA-256 of its canonical JSON without `sent_at` and `attempt`. A retry must
have the same semantic hash as the first attempt.

### Server response

`200` with a receipt: `{"ok": true, "event_uid", "message_seq", "event_id", "alert_id", "duplicate"}`.
`duplicate` is `true` when this `(event_uid, message_seq)` was already stored; the stored receipt is returned and
nothing else changes.

Validation errors return `422`. Conflicts return `409` with a `code`; the device treats them as permanent:

| `code` | Cause |
|---|---|
| `message_changed` | A stored `(event_uid, message_seq)` arrives with a different semantic hash |
| `duplicate_open` | A second `open` for an event that exists |
| `sequence_gap` | An `update`/`close` before the `open`, or a sequence number that skips one |
| `event_closed` | A message for an event that is already closed |
| `identity_changed` | Camera, run, track or onset differ from the event's `open` |
| `event_regressed` | Decision time or maximum score decreases |
| `decision_changed` | `confirm` differs from the `open` or from the decision parameters the run declared in its identity |
| `run_camera_changed` | A run reports for more than one camera |

### What the server does in one transaction

For every accepted message the server writes, in a single database transaction:

- a receipt row keyed by `(event_uid, message_seq)` with the semantic hash and the full payload;
- for `open`: the event row, **one** fall alert (`pending`), the alert's first log entry, a dispatch job and, when the
  event is current, a clip job;
- for `update`/`close`: the new state, decision time and maximum score; `close` sets the end time and reason.

If the response is lost after the commit, the device resends the same message and receives the stored receipt, so a
retry never creates a second event or alert.

## 3. The device side

### Outbox

The runtime stores every message in a local SQLite outbox (`edge_events.sqlite3`) **before** sending it. The active
event and its next sequence number are written in the same transaction, so a crash cannot leave a message that was
decided but not stored. Only one runtime can own an outbox (file lock).

A background worker sends due messages:

- one message at a time, in sequence order per event: a later message of an event waits until the earlier one is
  acknowledged;
- a transport failure or a `5xx`, `408`, `425` or `429` response is retried after `min(60, 2^(attempt−1))` seconds;
- any other `4xx` marks the message as blocked: it is kept, not resent, and reported as a blocked count until an
  an administrator releases it (`python edge_outbox.py --db edge_events.sqlite3 --retry-blocked` on the device);
- an acknowledged message is kept for 7 days and then pruned.

The heartbeat (`POST /api/edge_result`, once per decision slot) reports the pending and blocked counts, the event
protocol, the current decision state and a display status (`NORMAL`, `WAIT` while the window is not eligible, `FALL`
while the state is `ALERT` or `POST_FALL`); it never creates events. The bridge refuses heartbeats that do not declare
`event_protocol: edge_event` (409).

### Restart

When the runtime starts and its outbox holds an event that was still open, it records a `close` with reason
`runtime_stop` for that event under its original `event_uid`, before any new event can open. A new run never adopts an
old run's event.

### Clock calibration

Every 2 s the worker calls `GET /api/edge_event/context` and computes `offset_s` from the server time and the round
trip. A calibration older than 10 s is replaced by `{"offset_s": 0, "rtt_s": 60}`, which the server treats as unusable.

### Fall clips (optional, `--clips`)

With clips enabled, the device keeps a few seconds of small JPEG frames in memory and, after an event opens, uploads
the frames from 3 s before to 2 s after the decision to `POST /api/edge_clip`. The server answers `409 event_unknown`
until the event's `open` is stored (the device retries) and acknowledges repeated uploads of a stored clip without
re-encoding. Without `--clips` no camera image leaves the device.

## 4. Notification policy

The server decides once, when it accepts the `open`, whether an alert is **live** or for **manual review**:

| Condition at the `open` | Mode | Reason recorded |
|---|---|---|
| `rtt_s > 2`, `|offset_s| > 86400`, or corrected decision time more than 2 s in the future | `manual_review` | `clock_unreliable` |
| Corrected decision time (`decided_at + offset_s`) older than 120 s | `manual_review` | `event_too_old` |
| Otherwise | `live` | — |

The decision is latched: later messages, retries or the passage of time never promote a manual-review alert to live
or silence a live alert. Live alerts are announced to responders (banner, sound, optional desktop notification);
manual-review alerts appear in the alert list without an alarm. Late events keep both their original decision time and
their arrival time.

## 5. Alerts

### Lifecycle

```
pending ──receipt from a signed-in browser──▶ delivered ──ack──▶ acknowledged ──resolve(outcome)──▶ resolved
   └──────────────────────── ack ────────────────────────────▲
```

| Action (`POST /api/alerts/{id}/{action}`) | Allowed from | Effect |
|---|---|---|
| `received` (`device_id`, `delivery_id`) | any state | Records the browser receipt; the first receipt moves `pending` to `delivered` |
| `ack` | `pending`, `delivered` | Records who acknowledged and when; a second acknowledgement returns `409` naming the first |
| `resolve` (`label`, optional `note` ≤ 2000 characters) | `acknowledged` | Records the outcome; for a fall alert the label is copied to the event's review status |

Fall outcomes: `fall`, `fa_sitting`, `fa_lying`, `fa_bending`, `fa_other`, `uncertain`. Device-alert outcomes:
`recovered`, `device_other`, `uncertain`. Every change is appended to the alert log with actor, time and old/new
state. Late receipts never move an alert backwards.

Only `admin` and `responder` roles may act on alerts; `viewer` reads. A responder sees only the cameras assigned to
the account. The web server checks the session and passes the actor, role and camera scope to the bridge on each
request together with the server token; the browser never holds the token.

### Alert stream (`GET /api/alerts/stream`)

Server-sent events. A connection starts with a `snapshot` (current alerts and a cursor), then sends `alert` changes
in log order and a heartbeat every 3 s; after about 20 s the server ends the response and the browser reconnects with
`Last-Event-ID` (or `?after=`) set to the last cursor, so no change is lost across reconnects. More than 500 pending
changes are sent in pages; a cursor the server does not know triggers a fresh snapshot. For signed-in admins and
responders each alert carries a `delivery_id`, which the browser returns with `received`.

### Device alerts (watchdog)

The bridge checks every camera twice per second:

- `camera_offline` opens when no decision has arrived for 29 s and resolves itself (`recovered`) after 3 s of fresh
  decisions;
- `device_warning` opens when the device agent reports 80 °C or more or an active power/thermal limit flag, or when no
  agent report has arrived for 29 s; it resolves itself after 600 s of fresh reports without a warning. (The Devices
  page applies the softer per-profile thresholds of `device_profiles.py` for its Degraded state.)

At most one open alert of each kind exists per camera.

## 6. Guarantees and limits

- **No duplicate alerts:** one fall alert per `event_uid`, however often a message is resent.
- **No lost events across restarts:** messages survive restarts of the runtime, the bridge or the network while the
  device's outbox file and the server database are retained.
- **Ordered history:** a gap, reuse or regression is rejected instead of being stored.
- **Not covered:** power loss before the outbox write reaches storage, storage failure, several bridge instances, and
  delivery to browsers that are closed or signed out (there is no push, SMS or escalation channel).

## 7. Checks

| Rule | Check |
|---|---|
| Contract validation, semantic hash | `server/bridge/tests/test_v2_foundation.py`, `test_edge_events.py` |
| Lost response, retries, duplicates, conflicts, restart recovery | `server/bridge/tests/test_edge_events.py`, `test_edge_event_http.py`, `edge/test_event_bridge_v2.py` |
| Notification policy (late, unusable clock) | `server/bridge/tests/test_notification_policy.py` |
| Alert lifecycle, roles, camera scope, stream catch-up | `server/bridge/tests/test_alerts.py`, `test_alert_stream.py` |
| Watchdog alerts | `server/bridge/tests/test_watchdog.py` |
| End to end over HTTP with the web application | `server/bridge/demo.py`, `server/bridge/verify_web_demo.py` |
