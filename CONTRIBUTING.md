# Contributing to Fall-Relay

Thank you for considering a contribution. Fall-Relay is research software; the rules below keep it reproducible.

## Reporting a problem

Open an issue with: what you ran (command line, configuration without secrets), what happened, what you expected,
and the versions (`python --version`, `node --version`, PostgreSQL version, device model for edge problems).
For delivery or alert-handling problems attach the relevant lines of `bridge.log` and, for a device, the outbox
status printed by the runtime. Never attach event databases, clips or camera images: they may contain personal
data.

## Proposing a change

1. Fork and branch from `main`.
2. Run the checks before and after your change (they need a PostgreSQL database in which your role may create
   schemas; `server/bridge/postgres/docker-compose.yml` provides one):

   ```bash
   python3 -m venv .venv && .venv/bin/python -m pip install -r server/bridge/requirements-test.lock.txt
   export FALLRELAY_DATABASE_URL='host=127.0.0.1 port=54320 dbname=fall_relay user=fall_relay password=fall_relay_local'
   .venv/bin/python server/bridge/run_checks.py
   .venv/bin/python server/bridge/demo.py --output /tmp/fall-relay-demo
   ```

3. Keep the code style: `ruff format .` and `ruff check .` for Python (`pyproject.toml`), `npm run format` in
   `server/frontend` for the web application.
4. A change to the event contract (`server/bridge/edge_event_contract.py`, `docs/EVENT_PROTOCOL.md`) or to the
   decision engine needs a check that creates the failure or the behaviour it changes, in the style of the scripts
   in `server/bridge/tests/` and `edge/`.
5. Open a pull request that says what changed, why, and which checks cover it. The continuous-integration
   workflow (`.github/workflows/reproducibility.yml`) must pass.

## What is out of scope

The trained fall models and the data derived from NTU RGB+D are not part of this repository (see `NOTICE`);
pull requests must not add them. Contributions that only change our fall detector should come with the training
provenance expected by `edge/test_code_provenance.py`.

## Licence

By contributing you agree that your contribution is licensed under the GNU Affero General Public License v3.0,
the licence of this repository (`LICENSE`).
