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

### Delivery context (headers)

Beside the body, every send carries two headers that describe the delivery, not the event. They change from one send
to the next and are therefore kept out of the body and of the semantic hash; a server that does not know them ignores
them.

| Header | Meaning |
|---|---|
| `X-Fall-Relay-Event-Age-Ms` | Milliseconds since the event opened, by the device's monotonic clock (`CLOCK_BOOTTIME`, which keeps counting through suspend). Omitted when the device cannot know it: the event opened before the last reboot (the outbox stores the boot identifier of the open) |
| `X-Fall-Relay-Event-Backlog` | How many later messages of the same event are still pending in the outbox (0 = this message is the event's latest state) |
| `X-Fall-Relay-Decided-Clock` | `synced` when the device's wall clock was synchronised (kernel `adjtimex`, STA_UNSYNC clear) at the moment the event opened; absent otherwise. The outbox records it with the open |

The age needs neither the wall clock nor its calibration, so it is right after a reboot before the clock was set, and
after a link outage during which the calibration failed. The third header covers the remaining case: a message
queued before a power loss and sent after the reboot while the wall clock is still unset. Its `decided_at` was taken
by a synchronised clock and is used as is; the calibration measured at send time, which would be off by the clock's
error, is ignored.

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
trip. A calibration older than 10 s is replaced by `{"offset_s": 0, "rtt_s": 60}`, which the server treats as
untrusted. Since policy version 2 the calibration only dates the event for display when the device sent no age
(an older device, or a message queued before a reboot); it never decides the notification mode.

### Fall clips (optional, `--clips`)

With clips enabled, the device keeps a few seconds of small JPEG frames in memory and, after an event opens, uploads
the frames from 3 s before to 2 s after the decision to `POST /api/edge_clip`. The server answers `409 event_unknown`
until the event's `open` is stored (the device retries) and acknowledges repeated uploads of a stored clip without
re-encoding. Without `--clips` no camera image leaves the device.

## 4. Notification policy (version 2)

An alert is **live** (it sounds until a responder acknowledges it) or for **manual review** (listed without sound).
The server classifies from the **latest state of the event it knows**, not from the time of arrival alone:

| Latest message of the event | Event age | Mode | `classification_reason` |
|---|---|---|---|
| `open` or `update` (the event is still open) | any | `live` | `ONGOING` |
| `close`, reason `recovered` | ≤ 120 s | `live` | `RECOVERED_RECENT` |
| `close`, reason `recovered` | > 120 s or unknown | `manual_review` | `LATE_RECOVERED` |
| `close`, reason `track_lost`, `timeout` or `runtime_stop` | ≤ 3600 s or unknown | `live` | `OUTCOME_UNKNOWN` |
| `close`, reason `track_lost`, `timeout` or `runtime_stop` | > 3600 s | `manual_review` | `STALE_UNKNOWN` |
| the latest state did not arrive within the hold | — | `live` | `HOLD_TIMEOUT` |

**When.** The `open` is classified at once when the event is at most 120 s old or when the device reports no later
message pending (`X-Fall-Relay-Event-Backlog: 0`): the open is then the latest state, so the alert is live
(`ONGOING`). Otherwise the alert is stored as `classifying` with a hold of 5 s (`edge_event_classify_hold_s`); the
message that arrives with backlog 0, or any `close`, settles it in the same transaction, and the bridge's periodic tick
settles an expired hold as `live` / `HOLD_TIMEOUT`. A classifying alert is already visible in the browser, without
sound.

**Age.** `X-Fall-Relay-Event-Age-Ms` when present; else `decided_at` as sent when `X-Fall-Relay-Decided-Clock:
synced` is present (and the time is plausible: at most 30 days old, at most 2 s in the future); else the corrected
wall clock when the calibration is trusted (`rtt_s ≤ 2`, `|offset_s| ≤ 86400`, not more than 2 s in the future); else
unknown. The event's `started_at` is the arrival time minus the age, so the detection time shown to responders does
not depend on the device's wall clock at send time. `events.event_age_on_arrival_ms` and `events.age_source`
(`device_boot`, `decided_wall` or `wall_clock`) record what was used.

**Once settled, a classification never changes**: a late `close` with `recovered` does not demote a live alert, and
nothing promotes a manual-review alert. The decision is recorded on the alert (`notification_mode`,
`suppression_reason` = `late_recovered` or `stale_unknown`, `classification_reason`, `classified_at`, `policy_version`)
and in `alert_log` as `classified`. A manual-review alert that nobody has acknowledged after 600 s
(`alert_review_reminder_s`) gets one quiet reminder (`review_reminded_at`, `alert_log` action `review_reminded`); the
browser shows a notification without a tone.

**Thresholds** are server settings: `edge_event_max_live_age_s` (120), `edge_event_stale_age_s` (3600),
`edge_event_classify_hold_s` (5), `alert_review_reminder_s` (600).

**Why.** A fall reported minutes late may already have ended, or the person may still be on the floor; a long lie is
itself a hazard. Only the first case calls for review rather than an urgent sound. Version 1 classified from the
arrival time and the clock calibration alone, so a fall that started during a link outage was listed without sound;
rows written under version 1 keep their `event_too_old` / `clock_unreliable` reasons.

**Clients.** The stream request carries `policy=<version>`. A client of another version receives settled live alerts
only (`live_only`), which is the one set every version understands.

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
