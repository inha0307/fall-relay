# Fall-Relay installation and verification

## Server bridge: clean environment

Validated on Linux x86_64 with CPython 3.10.12. These commands run from
`server/bridge/`. They do not require a Raspberry Pi or private model weights.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.lock.txt
.venv/bin/python -m pip check
.venv/bin/python run_checks.py --report checks.json
```

`requirements-server.txt` pins direct API dependencies. `requirements-test.txt`
adds NumPy and headless OpenCV for recording and all current Python checks.
`requirements-test.lock.txt` also pins transitive dependencies from the verified
clean environment. This snapshot is not a cross-platform lock or a Pi installer.
For API installations, use `requirements-server.txt`. Fall-clip encoding requires OpenCV. Install only one OpenCV distribution in an environment.

The runner invokes 27 standalone check scripts (`tests/` here and three in `edge/`: alert, explicit-event, clip,
device-command, reports, hardware, model-management and edge-bundle checks), each in its own process with a
60-second timeout. It returns nonzero for failures/timeouts, continues through the
remaining scripts, and optionally saves stdout/stderr and timings as JSON.
`python -m pytest` from the repository root runs the same scripts. To run a subset:

```bash
.venv/bin/python run_checks.py --timeout 20 tests/smoke_test_edge_api.py tests/test_alerts.py
```

The scripts use temporary or in-memory databases and synthetic inputs. They do
not establish model accuracy, Pi performance or clinical suitability. Run Python
without `-O`, since existing checks use assertions.

### Restricted execution environments

The bridge has synchronous routes dispatched through AnyIO worker threads.
During verification, both a minimal AnyIO thread example and the ASGI smoke test
stalled inside the execution sandbox but completed outside it, with the same
existing interpreter. This did not reproduce as an application defect outside
the sandbox. If a check times out, inspect its saved output and the environment's
thread/event-loop restrictions; do not treat a partial PASS log as overall success.
The smoke test additionally bounds each ASGI request at five seconds.

### Start an isolated local bridge

Do not use an operational configuration/database for a trial run. Point `FALLRELAY_DATABASE_URL` at a throwaway
database initialised with `python -m postgres.bootstrap`, then create a local configuration with a loopback bind and no
recording:

```bash
.venv/bin/python -c 'import json; from pathlib import Path; Path("server_config.local.json").write_text(json.dumps({"schema_version":1,"host":"127.0.0.1","port":5102,"cors_origins":[],"archive_dir":"local-data/decision_logs","clip_dir":None,"cameras":[{"id":"1","label":"Demo camera","location":"Local"}]}))'
.venv/bin/python video_bridge.py --config server_config.local.json
```

Use `http://127.0.0.1:5102/api/cameras` to check startup. This example has no camera
producer and is loopback-only; for the complete synthetic workflow, use [demo.py](DEMO.md). This minimal
configuration is not a public deployment configuration. Configure authentication and deployment settings before
connecting actual devices. Keep the generated config/database out of a release.

## Web application

The web application is in `../frontend/`; use Node.js 24 (tested with 24.21.0) and its `package-lock.json`:

```bash
cd ../frontend
npm ci
npm run build
npm start
```

Its account store (accounts, sessions, camera grants) is the schema `FALLRELAY_APP_SCHEMA` (default `app`) of the
same PostgreSQL database as the bridge, created on first start. Server-side settings (`FALLRELAY_WRITE_TOKEN`,
`FALLRELAY_DATABASE_URL`, `NEXT_PUBLIC_PIPELINE_URL`, `PIPELINE_SERVER_URL`) come from the environment; the installed unit reads them from `deploy.env` (see
[README.md](README.md)). The server token is used only by server-side routes and never reaches browser code.
`verify_web_demo.py` builds a clean copy and runs the web checks against the synthetic demo; see [DEMO.md](DEMO.md).

## PostgreSQL

The bridge stores events, alerts, receipts and dispatch jobs in PostgreSQL, and the web application keeps its accounts in
a schema of the same database; the only SQLite file is the outbox on the device. For a throwaway local database use `server/bridge/postgres/docker-compose.yml`
(role `fall_relay`, port 54320). For a system installation, as a database administrator:

```sh
sudo -u postgres psql -X -f /path/to/server/bridge/postgres/init_local.sql
sudo -u postgres psql -X -c '\password "fall-relay"'
```

Then, as the user running the bridge, run `python postgres/configure_local.py`: it prompts for the password without
echo, verifies the login and writes `.postgres.pgpass` (mode 0600). Neither that file nor `deploy.env` may enter a
public repository. Export the connection string with a passfile, never with a password on the command line:

```sh
export FALLRELAY_DATABASE_URL='host=127.0.0.1 port=5432 dbname=fall-relay user=fall-relay passfile=/absolute/path/.postgres.pgpass'
```

A new installation initialises its schema with `python -m postgres.bootstrap --report bootstrap.json`; the server
configuration names the connection variable with `{"database_url_env": "FALLRELAY_DATABASE_URL"}`. A database that was
initialised by a version before the baseline script adopts it once, with the bridge stopped, through
`python -m postgres.upgrade --adopt-baseline --report adopt.json`. Missing credentials or pending schema migrations
make the bridge fail at start.
The pool leases one connection per request (1–8 connections); event, alert, dispatch job and receipt commit in one
transaction before the receipt is returned, and transaction-scoped advisory locks serialise only the messages of the
same event, run-identity registration, command pickup and alert transitions. The bridge is still one application
process: several Uvicorn workers are not supported. A daily backup timer can be installed with
`python -m postgres.install_backup_timer`.

## Edge device

Do not install the server lock on a device. The edge runtime imports Picamera2/libcamera (system packages), NCNN,
OpenCV, NumPy, Requests and `ai_edge_litert`; the versions used on the reference devices are pinned in
[`edge/requirements-edge.txt`](../../edge/requirements-edge.txt). A device is installed from a bundle built by
`edge/make_bundle.py`, which contains the runtime and every module it imports (checked by `edge/test_bundle.py`) and
verifies that the two model files match the frozen hashes. The YOLO26n-pose NCNN export and the trained model files
are not in this repository (see [NOTICE](../../NOTICE)). Step-by-step installation:
[edge/deploy/README.md](../../edge/deploy/README.md).

At start the runtime verifies every bundled file against `bundle_manifest.json` and the pose model against the
manifest's pose hashes, and refuses to run on a mismatch. Record the device model and memory, OS and kernel, package
versions, camera, cooling and power supply with any device measurement; the reference devices are described in
[edge/ntu_benchmark/README.md](../../edge/ntu_benchmark/README.md).

OpenCV may print a VP80/WebM tag diagnostic during the clip checks; the checks still verify that the files are
complete and decodable.
