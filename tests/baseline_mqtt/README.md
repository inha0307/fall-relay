# Broker-based baseline (MQTT)

This folder compares Fall-Relay with a baseline built on a message broker, under the same kind of injected faults as
the tests A1 and A2 of the article (Table 2); its results are Table 3 of the article. The baseline uses MQTT QoS 1 with persistent sessions: the device
publishes each fall event with Eclipse Paho (Python), Eclipse Mosquitto forwards it, and a subscriber stores the event
and its alert in PostgreSQL and acknowledges the message only after its transaction has committed. The subscriber
runs in two modes: `naive` creates one alert per received message; `idempotent` uses the event identifier as the
primary key, as Fall-Relay's server does. The device has no local outbox.

| Scenario | Injected fault | Fall-Relay counterpart |
|---|---|---|
| S1 | The broker's PUBACK to the device is dropped and the connection closed (`fault_proxy.py`); the device resends | A1 (lost confirmation) |
| S2 | The subscriber commits the event and the alert, then is killed (SIGKILL) before its PUBACK; it restarts with the same session | A1 (server restarted after its commit) |
| S3 | The device process is killed (SIGKILL) after `publish()` while the broker is unreachable; it restarts later | A2 (process killed after a local commit) |
| S4 | The broker acknowledges the message while the subscriber is offline, then is killed (SIGKILL); `S4-control` stops it with SIGTERM | none: Fall-Relay's server confirms only after its own commit |

Each scenario is one deterministic run with one fall event. `run_baseline.py` writes `baseline_results.json` with the
event rows and alerts found in PostgreSQL after the fault, and the logs of every process.

## Run

Requirements: Mosquitto 2.0 (`mosquitto` binary), PostgreSQL with an empty database, Python 3.10 or later.

```
python3 -m venv .venv
.venv/bin/pip install -r tests/baseline_mqtt/requirements.txt
export BASELINE_DATABASE_URL='host=127.0.0.1 port=54320 dbname=baseline user=fall_relay'
.venv/bin/python tests/baseline_mqtt/run_baseline.py --mosquitto "$(command -v mosquitto)" --out /tmp/baseline-mqtt
```

The script uses the local ports 18830 (broker) and 18831 (fault proxy) and recreates the tables `events` and
`alerts` in the given database. The broker of S4 runs with `persistence true` and the default `autosave_interval`
(1800 s); a shorter interval narrows the window of S4 but does not close it.
