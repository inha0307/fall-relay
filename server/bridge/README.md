# Server bridge: configuration and services

This folder holds the bridge (`video_bridge.py`, FastAPI), its stores, routers, SQL migrations and PostgreSQL tools;
the modules the edge bundle copies from here (`edge_event_contract.py`, `edge_outbox.py`, `edge_event_runtime.py`,
`schema_migrations.py`, `edge_config.py`, `decision_log_runtime.py`, `db_backend.py`); the device agent
(`pi_hardware_agent.py`); and the demo, verification, backup and installation tools. Checks are in `tests/`; runtime
state lives in `../data/`. The design is described in [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) and the
event and alert rules in [docs/EVENT_PROTOCOL.md](../../docs/EVENT_PROTOCOL.md).

For a clean installation, PostgreSQL setup and the checks, see [INSTALL.md](INSTALL.md); for the hardware-free
example, [DEMO.md](DEMO.md). Model identity is enforced by `model_contract.py` and `model_run_store.py`; device
telemetry and remote actions are served by `hardware_store.py`, `device_profiles.py` and `device_commands.py`.

## Configuration (`server_config.json`)

The bridge reads `server_config.json` from this folder (or `--config PATH`, or `FALLRELAY_SERVER_CONFIG`). Relative
paths are resolved from the configuration file's folder. The reference file in the repository is the configuration
of the laboratory deployment.

| Key | Meaning |
|---|---|
| `host`, `port`, `cors_origins` | Listening address (default port 5001) and allowed browser origins. A non-loopback `host` needs the server token; reads always need it once it is set |
| `cameras` | Initial camera list (`id`, `label`, `location`); cameras can also be added on the web's Server page. Unknown camera IDs are rejected |
| `require_camera_tokens` | Devices must send their camera token (required for alerts) |
| `require_media_token` | Clips and the debugging video stream require a media token from the web server |
| `write_token_env` | Name of the environment variable holding the server token (`FALLRELAY_WRITE_TOKEN`) |
| `features` | `alerts` (create and serve alerts), `watchdog` (camera-offline and device-warning alerts); explicit events are always accepted |
| `edge_event_max_live_age_s` | Older events become manual-review alerts (default 120) |
| `edge_stale_after_s`, `video_stale_after_s` | A decision or frame older than this is not shown as live (default 3 s and 5 s) |
| `database_url_env` | Name of the environment variable holding the PostgreSQL connection string (default `FALLRELAY_DATABASE_URL`) |
| `archive_dir` | Folder for collected decision-log archives (their index is in the database); the reference uses `../data/decision_logs` |
| `clip_dir`, `clip_pre_s`, `clip_post_s`, `clip_fps`, `clip_retention_days` | Fall clips: folder, window around the decision, frame rate, retention |
| `metrics_interval_s`, `metrics_retention_days`, `hardware_retention_days`, `hardware_stale_after_s` | Telemetry sampling and retention |
| `device_profiles` | Per-device profile and threshold overrides for the Devices page (see `device_profiles.py`) |

The schema is the baseline script `migrations/postgres/0001_baseline.sql`; later changes are versioned scripts in the
same folder, applied offline with `python -m postgres.upgrade` (the bridge refuses to start while one is pending). The table `push_subscriptions` and the `subscription_id` columns of
`alert_deliveries` and `alert_dispatch_jobs` are reserved for a push channel that is not implemented; they stay empty.

Settings of earlier versions (`database_backend`, `db_path`, `v2_features`, `edge_event_camera_ids`, `episode_gap_s` and the
research-capture keys) are refused with an error that names them, so a stale configuration cannot start silently.

## Cameras and devices

Each camera is served by one edge runtime process that reports under its `camera_id`; the bridge keeps decisions,
connection state and staleness per camera. Register the camera (Server page), issue its token there and save the shown
line as `camera_token_cam<N>.env` on the device. Install the device as described in
[edge/deploy/README.md](../../edge/deploy/README.md).

- **One camera per device:** give every device its own `camera_id` and either list it in `RPI_TARGETS` (reverse
  tunnel, below) or set its `server_url` to the server's address.
- **Two cameras on one Raspberry Pi 5:** install the runtime twice with `camera_index` 0 and 1 and lower
  `pose_threads` (for example 2 each). The 10 Hz budget with two pipelines on one Pi 5 has not been measured.

## Services

Settings live in `deploy.env` in this folder (copy `deploy.env.example`; mode 600, never committed):
`FALLRELAY_SERVER_ROOT` (the `server/` folder with `.venv` and `frontend/`), `FALLRELAY_SERVER_CONFIG`,
`FALLRELAY_WRITE_TOKEN`, `FALLRELAY_DATABASE_URL` (PostgreSQL), `NEXT_PUBLIC_PIPELINE_URL` (the bridge address as the
browser sees it), `RPI_TARGETS` (space-separated `user@host`, one reverse tunnel each), `RPI_SSH_KEY` and
`RPI_TUNNEL_PORT`. The bridge and the web unit both load this file; environment variables override it.

Node.js and npm must be installed. From this folder, with ports 3000 and 5001 free:

```bash
bash install_server_services.sh            # builds the web application, installs and starts the units
bash install_server_services.sh --no-build # keeps an existing build in ../frontend/.next
```

The script installs `fall-relay-server.service` (bridge), `fall-relay-web.service` (port 3000) and one
`fall-relay-tunnel-<user>-<host>.service` per `RPI_TARGETS` entry, which makes the bridge reachable on the device at
`127.0.0.1:15001`. The SSH key stays where `RPI_SSH_KEY` points.

```bash
systemctl --user status fall-relay-server.service fall-relay-web.service 'fall-relay-tunnel-*'
journalctl --user -u fall-relay-server.service -f
loginctl enable-linger "$USER"   # keep the units running after logout
```

Under WSL, systemd must be enabled. The daily database backup timer is installed with
`python -m postgres.install_backup_timer`.

Create the first administrator before signing in (the password is typed in the terminal and never echoed; the accounts
live in the schema `FALLRELAY_APP_SCHEMA`, default `app`, of the same PostgreSQL database):

```bash
cd ../frontend && node scripts/set-password.mjs admin --create-admin
```

Further accounts, roles (Administrator, Responder, Viewer) and camera access are managed on the Accounts page.

## Checks

```bash
.venv/bin/python run_checks.py --report checks.json        # all 29 check scripts, each in its own process
.venv/bin/python tests/smoke_test_edge_api.py              # one check on its own
```

`python -m pytest` from the repository root runs the same scripts. They use temporary databases and synthetic
inputs; they do not measure model accuracy, device speed or the live network. For a deployment check, confirm that
the Status page shows every camera detecting and that `/api/connection` (through the web server) reports each device
as connected.

## Limits

The bridge is one process; it is not safe to run several workers or instances against one database. Our fall
detector has known false positives during daily activities; do not describe the system as clinically validated.
