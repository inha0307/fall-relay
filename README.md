# Fall-Relay

[![Reproducibility (synthetic, no Pi)](https://github.com/inha0307/fall-relay/actions/workflows/reproducibility.yml/badge.svg)](https://github.com/inha0307/fall-relay/actions/workflows/reproducibility.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23719977.svg)](https://doi.org/10.5281/zenodo.23719977)

Fall-Relay is open-source edge–server software that carries fall events from camera devices to the people who
respond to them, and records how each alert is handled. A Raspberry Pi runs the detector and a latched decision
engine; every event message is stored in a local outbox before it is sent and is accepted by the server idempotently,
so retries, lost responses and restarts never lose an event or create a second alert. Detection events and responder
alerts have separate lifecycles: a responder acknowledges an alert and records the on-site outcome, while the event
keeps what the device observed.

- **Durable event delivery:** SQLite outbox on the device, ordered retries, transactional receipts on the server,
  restart recovery ([docs/EVENT_PROTOCOL.md](docs/EVENT_PROTOCOL.md)).
- **Alert handling:** live and manual-review alerts, acknowledgement, outcomes and an append-only handling log in a
  web application with Administrator, Responder and Viewer roles and per-camera access.
- **Data minimisation:** camera images stay on the device; the server receives decisions, the 13-joint skeleton the
  model used and, optionally, a short clip around each detected fall.
- **Device operation:** telemetry, health against per-device profiles, watchdog alerts for cameras that stop detecting
  and remote actions, on Raspberry Pi 5 and Raspberry Pi 4.
- **Replaceable detector:** the bundled pose-based detector ([edge/MODEL.md](edge/MODEL.md)) is a case study; any
  detector that emits the event contract can be used.

The bundled detector has known false positives during daily activities. The software is not clinically validated.

## Try the workflow without a Raspberry Pi

Tested on Linux x86_64, Python 3.10.12, Node.js 24.21.0 and PostgreSQL 14; the CI workflow runs the checks on PostgreSQL 14 and 16. Run from this directory.
The example needs no camera, trained weights or cloud account. It needs a PostgreSQL database in which
the connecting role may create schemas (see the PostgreSQL section of [server/bridge/INSTALL.md](server/bridge/INSTALL.md)
for a local setup): the demo creates a schema of its own, `demo_<random>`, and drops it when it stops, so an
existing database is never read or changed. Dependencies must be downloadable during installation.

Without a PostgreSQL installation, `docker compose -f server/bridge/postgres/docker-compose.yml up -d` starts a
throwaway one on port 54320 (connection string in that file).

```bash
git clone https://github.com/inha0307/fall-relay.git && cd fall-relay
python3 -m venv .venv
.venv/bin/python -m pip install -r server/bridge/requirements-test.lock.txt
.venv/bin/python -m pip check
export FALLRELAY_DATABASE_URL='host=127.0.0.1 port=54320 dbname=fall_relay user=fall_relay password=fall_relay_local'   # the docker-compose database
.venv/bin/python server/bridge/run_checks.py --report /tmp/fall-relay-checks.json
.venv/bin/python server/bridge/demo.py --output /tmp/fall-relay-demo
```

Every check creates a schema of its own in that database and drops it at exit; without the variable the checks fail
rather than pass silently.

`.venv/bin/python -m pytest` runs the same check scripts as `run_checks.py`, one test each. Code style is
checked with ruff for Python (`pyproject.toml`: `ruff format --check .`, `ruff check .`) and Prettier for the web
application (`server/frontend`: `npm run format:check`).

Use a **new output directory** for each run. The demo starts a loopback-only bridge
on the same PostgreSQL backend as the reference deployment, and two synthetic cameras that run the same post-inference code as the Pi
(v2 decision engine, explicit-event outbox, heartbeat with the 13-joint skeleton).
Camera 1 falls, lies down and gets up; camera 2 stays normal. The demo uploads a
generated fall clip the way the device does and takes the alert through
acknowledgement and an on-site outcome, checking authentication and per-camera
access on the way. It stops its server after completion. Inspect:

- `/tmp/fall-relay-demo/report.json`: event, alert timeline, clip and camera checks, and limitations.
- `/tmp/fall-relay-demo/fall_clip.webm`: the generated clip as served to the web (no real person).

Scores, keypoints and frames are generated: no pose, lifting or fall model runs, and
slots are replayed faster than real time, so nothing here measures accuracy or device speed.
See [the demo description](server/bridge/DEMO.md).

To put a different fall detector behind the same delivery and handling, see
[edge/examples/README.md](edge/examples/README.md): `any_detector_adapter.py` turns a stream of per-slot scores
into durable event messages and needs no camera or model.

## Try the web application with the synthetic run

In terminal 1, keep a fresh demo bridge open:

```bash
.venv/bin/python server/bridge/demo.py --output /tmp/fall-relay-demo-web --serve
```

Wait for `Isolated bridge:` in its output. In terminal 2:

```bash
.venv/bin/python server/bridge/verify_web_demo.py \
  --demo-output /tmp/fall-relay-demo-web \
  --output /tmp/fall-relay-web --serve
```

This copies frontend source into the new output folder (without `node_modules`,
builds or private `.env` files), runs `npm ci` and a production webpack build,
creates a throwaway account database and runs web API checks. Open the printed
`base_url` and sign in with `tadmin` / `admin-pass-123` **only in this isolated
synthetic demo**. The Alerts page shows the resolved synthetic fall with its clip.
While `--serve` runs, both synthetic cameras keep sending normal activity and two
synthetic devices report readings, so Status, Skeleton view and Devices have content;
the second device sends 7.3 decisions per second and appears as Degraded.
Ctrl+C in both terminals stops the demo processes. Production services and data are
not used. The generated `demo.env` holds private local demo tokens; do not publish it.

## Documentation

| Topic | Document |
|---|---|
| Components, data flow, security model | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Event messages, outbox, receipts, alert lifecycle | [docs/EVENT_PROTOCOL.md](docs/EVENT_PROTOCOL.md) |
| Server installation, checks, troubleshooting | [server/bridge/INSTALL.md](server/bridge/INSTALL.md) |
| Server configuration and services | [server/bridge/README.md](server/bridge/README.md) |
| Edge device installation | [edge/deploy/README.md](edge/deploy/README.md) |
| Bundled detector and its evaluation | [edge/MODEL.md](edge/MODEL.md), [training/README.md](training/README.md) |
| Raspberry Pi 5 / Pi 4 benchmark | [edge/ntu_benchmark/README.md](edge/ntu_benchmark/README.md) |
| Synthetic demo | [server/bridge/DEMO.md](server/bridge/DEMO.md) |
| Interface terms | [docs/UI_GLOSSARY.md](docs/UI_GLOSSARY.md) |
| Evidence behind the article: detector operating point, check conditions and results, replay measurements | [docs/evidence/README.md](docs/evidence/README.md) |
| Readable captures of the web application pages (hardware-free example) | [docs/screens/README.md](docs/screens/README.md) |

Repository layout:

| Folder | Contents |
|---|---|
| `server/bridge/` | Server API (FastAPI) with its stores, routers, PostgreSQL tools and SQL migrations; the modules the edge bundle copies (event contract, outbox, schema migrations, edge config loader, decision-log recorder); the device agent; demo, web verification, backup and install tools |
| `server/bridge/tests/` | Check scripts (each also runs on its own) and fixtures |
| `server/frontend/` | Next.js web application |
| `server/data/` | Runtime state, never versioned: databases, fall clips, decision logs |
| `edge/` | Edge runtime, frozen decision and feature modules, bundle builder, deployment files, NTU benchmark |
| `training/` | Training pipeline for the pose-lifting and fall models |
| `tests/` | pytest entry point for all check scripts |
| `docs/` | Architecture, event protocol, interface glossary, article evidence (`docs/evidence/`), web screen captures (`docs/screens/`) |

Running on a device additionally requires the camera packages of the OS, NCNN, LiteRT and the model files; the
hardware-free example does not supply them, and a successful example run does not establish a device installation.

## Checks, CI and local review bundle

The Python runner executes 29 bounded check scripts. The workflow at
[`.github/workflows/reproducibility.yml`](.github/workflows/reproducibility.yml)
adds the real HTTP demo, clean `npm ci`, production build and web API verification.
It needs no repository secrets or device access. The first hosted run passed on 28 October 2026 (see the badge
above and the Actions tab).

To build a **local review bundle**, which explicitly excludes operational databases,
credentials, captures and installed dependencies:

```bash
python3 server/bridge/package_demo.py --output /tmp/fall-relay-review.zip
```

This is a server/web reproduction package, not a complete Pi software release.

## Naming

The software was developed under the working names FallScope and Neulbom. Identifiers that devices and
stored records already carry keep those names and are frozen: the model contract
`fallscope-pose13-feature59-v2`, the event contract `fallscope-edge-event-v1`, bundle IDs such as
`fallscope-v2-20261019-ev`. Everything else uses the Fall-Relay name: the edge runtime `edge/fall_relay_edge_runtime.py`
(its former name `fallscope_edge_runtime_v2.py` remains as a shim for installed bundles), `FALLRELAY_*` environment variables, `fall-relay-*` systemd
units and `fall_relay_*` database files.

## License and data

The code is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0-only); see [LICENSE](LICENSE).
Model files are not in this repository and carry their own terms: the Ultralytics YOLO26n-pose model is AGPL-3.0,
and the fall models trained on NTU RGB+D are not redistributed; see [NOTICE](NOTICE). The demo draws artificial
points with OpenCV; it contains no NTU frames and no recording of a person.

Release v1.0.0 is archived on Zenodo under DOI [10.5281/zenodo.23719977](https://doi.org/10.5281/zenodo.23719977)
(all versions: [10.5281/zenodo.23719976](https://doi.org/10.5281/zenodo.23719976)). To cite the software, use
[CITATION.cff](CITATION.cff) or the "Cite this repository" button on GitHub.
