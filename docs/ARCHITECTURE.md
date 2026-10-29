# Architecture

Fall-Relay has three parts: an **edge runtime** on each camera device, a **bridge** (the server API) and a **web
application** for responders and administrators. The edge decides; the bridge stores and delivers; people handle
alerts in the web application.

```
 Edge device (Raspberry Pi 5 / Pi 4)           Server                                       Browser
┌──────────────────────────────────┐        ┌───────────────────────────────────┐        ┌──────────────┐
│ camera → pose → 13 joints → 3D   │        │ bridge (FastAPI, :5001)           │        │ web (Next.js,│
│ lifting → TCN → decision engine  │ events │  · event receipts, alerts         │  HTTP  │  :3000)      │
│        │                         │ ─────▶ │  · watchdog, device telemetry     │ ◀────▶ │ Status       │
│        ▼                         │ heart- │  · clips (WebM), skeleton view    │  via   │ Alerts       │
│ SQLite outbox ── sender ─────────│  beat  │  · PostgreSQL                     │  web   │ Skeleton view│
│ device agent (telemetry, remote  │ ─────▶ │                                   │  proxy │ Devices      │
│ actions)                         │        │ web server: sessions, roles,      │        │ Reports      │
└──────────────────────────────────┘        │ camera grants (PostgreSQL)        │        │ Accounts     │
                                            └───────────────────────────────────┘        └──────────────┘
```

## Edge device (`edge/`)

`fall_relay_edge_runtime.py` runs one camera. Every 100 ms slot it takes the newest frame, estimates 2D pose
(YOLO26n-pose, NCNN), selects one tracked person, forms 13 joints (12 body joints and a calibrated head point), lifts
them to 3D (LPN, LiteRT), builds 59 features per slot and scores a window of 20 slots with a temporal convolutional
network (TCN, LiteRT). The decision engine (`decision_engine.py`) turns scores into latched fall events. The model,
features and decision rule are described in [`edge/MODEL.md`](../edge/MODEL.md).

Each decision slot produces one heartbeat (`/api/edge_result`): the decision state, the skeleton the model used and
outbox counters. Events go through the outbox described in [EVENT_PROTOCOL.md](EVENT_PROTOCOL.md). Camera images stay
on the device unless short fall clips are enabled (`--clips`) or the debugging video stream is switched on (`--video`).

The device agent (`server/bridge/pi_hardware_agent.py`, installed once per device) reports CPU, memory, temperature,
clock and power/thermal limit flags every 5 s and executes the remote actions an administrator queues on the Devices
page (restart/stop/start detection, fetch the log, decision-log recording). The device pulls commands; the server
never connects to the device.

A device is installed from a **bundle** built by `edge/make_bundle.py`: the runtime and every module it imports, the
two model files and a manifest of SHA-256 hashes the runtime verifies at start. See
[`edge/deploy/README.md`](../edge/deploy/README.md).

## Bridge (`server/bridge/`)

`video_bridge.py` is a single FastAPI process. It

- accepts heartbeats, event messages, clips and telemetry from registered cameras with per-camera tokens;
- stores events, alerts, the alert log, model run identities, device telemetry and commands;
- runs the watchdog that opens camera-offline and device-warning alerts;
- encodes uploaded clip frames into WebM files and deletes them after `clip_retention_days` (3 in the reference
  configuration);
- serves the alert stream and the read APIs to the web server.

Storage is PostgreSQL (`server/bridge/postgres/`); the schema is the baseline script and versioned migrations in
`server/bridge/migrations/postgres/`. Runtime state lives outside the code in `server/data/`.

The bridge itself does not judge falls: it never re-applies a threshold to scores. It enforces the event contract,
records which model and decision parameters each run declared, and rejects messages that disagree with them.

## Web application (`server/frontend/`)

A Next.js application with its own account schema (`app`) in the same PostgreSQL database: accounts, hashed
passwords, sessions and camera grants. Its server-side routes check the session and forward requests to the bridge
with the server token, the actor, the role and the camera scope; the browser never receives the token.

| Page | Purpose |
|---|---|
| Status | What needs attention now: alerts waiting for acknowledgement, cameras not detecting, devices with warnings |
| Alerts | Live and manual-review alerts, acknowledgement, outcome, clip and handling timeline |
| Skeleton view | The 13-joint skeleton each camera's model is using, per slot (no video) |
| Devices | Device list and per-device detail (readings, decision rate, remote actions) driven by device profiles |
| Reports | Statistics over a chosen period: alerts per day and by hour, on-site outcomes, per-camera detection |
| Server / Accounts | Camera registration and tokens, model runs, accounts, roles and camera access |

Roles: **Administrator** (everything), **Responder** (handles alerts for the cameras assigned to the account),
**Viewer** (read-only). Terms used in the interface are defined in [UI_GLOSSARY.md](UI_GLOSSARY.md).

## Security model

| Credential | Held by | Used for |
|---|---|---|
| Camera token (`FALLRELAY_CAMERA_TOKEN`) | One edge device per camera | Writing heartbeats, events, clips and telemetry for that camera |
| Server token (`FALLRELAY_WRITE_TOKEN`) | Bridge and web server | Every bridge read and every alert request from the web server |
| Session cookie | Signed-in browser | Web pages and API routes; 12 h, extended while in use |
| Media token | Signed-in browser | Access to clips and the debugging video stream; valid for 1 h |

Whenever a server token is configured, every bridge read requires it, so only the web server can read; browsers
reach the bridge directly only for clips and the debugging video stream, with a media token. The bridge refuses to
listen on a non-loopback address without a server token. Allowed CORS origins (`cors_origins`) therefore grant no
access by themselves.

In the reference setup each device reaches the bridge through a reverse SSH tunnel opened by the server
(`fall-relay-tunnel-*` units), so the bridge port is not exposed on the network.

## Deployment

`server/bridge/install_server_services.sh` installs three kinds of systemd user units on the server:
`fall-relay-server` (bridge), `fall-relay-web` and one `fall-relay-tunnel-<device>` per device. Both server units read
`server/bridge/deploy.env`. On each device the bundle's `fall-relay-edge-cam<N>.service` and
`fall-relay-device-agent.service` run the runtime and the agent.

## Scope

One bridge process per installation (connection state is held in memory); browsers must be open and signed in to
receive alerts; there is no push, SMS or escalation channel. Our fall detector has known false positives and is not
clinically validated.
