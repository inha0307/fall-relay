# Hardware-free reproducibility example

See the [root quickstart](../../README.md) for exact commands. `demo.py` always creates
a new output directory and its own PostgreSQL schema (`demo_<random>`, in the database
named by `FALLRELAY_DATABASE_URL`), initialised empty and dropped when the demo stops;
the bridge runs on the same PostgreSQL backend as the reference deployment. It launches
the same `video_bridge.py` used by the deployed system; no mock endpoint implementation is
substituted. On the device side it imports the deployed v2 modules from `edge/`
(decision engine, `EventBridgeV2` with its SQLite outbox, and the runtime's
`edge_result`/`skeleton` builders), so the messages the bridge receives are produced
by the code the Pi runs after inference. Requests use a five-second timeout; startup
is bounded, and the owned bridge subprocess is stopped on completion, Ctrl+C or SIGTERM.

## API sequence

| Step | Requests | Verification |
|---|---|---|
| Setup | `POST /api/admin/cameras/{cam}/token` | Two cameras with explicit events and alerts enabled |
| Authentication | Unauthenticated `POST /api/edge_result`, `POST /api/edge_clip`, `GET /api/alerts` | Refused (403, 422 for an empty clip, 403) |
| Synthetic edge | `POST /api/edge_result` every slot, `POST /api/edge_event` from the outbox | Camera 1: open, updates, close (`recovered`) as one event; camera 2: no event |
| Skeleton | Heartbeat `skeleton`, `frame_size` | 13 joints per camera; no video reaches the server |
| Fall clip | `POST /api/edge_clip` with camera 2's token, then camera 1's | 403, then accepted; one WebM encoded and served by `GET /api/alerts/{id}/clip` |
| Alert scope | `GET /api/alerts` as responders of camera 1 and camera 2 | One fall alert for camera 1; none for camera 2 |
| Alert handling | `ack` as viewer, `resolve` before `ack`, `ack` twice, `resolve` | 403, 409, first wins / second 409, resolved with outcome `fall` |

Slots: camera 1 has 2 s of low scores, 0.5 s of high scores (the fall), 3 s lying and
4 s upright, at a nominal 10 Hz; camera 2 has low scores throughout. Decision
parameters are the deployed operating point (θ 0.95, k/n/m 3/5/3, cooldown 0).
Slots are replayed faster than real time on one host clock, so clock offset is zero.
Keypoints are a code-defined standing or lying figure, and clip frames are that
figure drawn on a plain background and labelled SYNTHETIC; no real person, NTU frame
or model output is used. Nothing here estimates detection accuracy or device speed.

## Web verification

`verify_web_demo.py` requires a completed report from a still-running `demo.py --serve`.
It copies only frontend source into a fresh directory, performs `npm ci`, builds
with webpack, seeds the test accounts into a new DB, starts Next.js on loopback,
then executes:

1. `scripts/check-demo-api.mjs`: CORS for the demo origin, bridge reads refused without
   the server token, alerts refused before sign-in, the resolved synthetic fall alert,
   its clip through the web route, and both cameras sending no video and a 13-joint skeleton.
2. `scripts/check-app-api.mjs`: accounts, roles, temporary passwords, deactivation and lockout.
3. `scripts/check-responder-api.mjs`: responder permissions and role-change session revocation.

The script bounds commands and startup, saves separate logs and a JSON report,
and stops its own process group. `--serve` leaves the tested web server available
for manual review until Ctrl+C. The web application's accounts are created in the demo's own schema.
The known test password is never installed into the operational account database.
