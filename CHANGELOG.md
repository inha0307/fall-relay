# Changelog

Each release is archived on Zenodo; the DOI 10.5281/zenodo.23719976 resolves to the latest version.

## 1.2.0 — 2026-11-24

- Broker-based baseline: [tests/baseline_mqtt](tests/baseline_mqtt) injects the faults of the tests A1 and A2 into a
  baseline built on MQTT QoS 1 with persistent sessions (Mosquitto 2.0.11, Eclipse Paho 2.1.0), with a subscriber that
  creates one alert per message or deduplicates by event identifier. Results of two runs and the 30 checks on the same
  host are in [docs/evidence/baseline_mqtt](docs/evidence/baseline_mqtt).
- [training/rescore_test_scores.py](training/rescore_test_scores.py) recomputes the NTU RGB+D test results from the
  per-time-step fall scores with the runtime's own decision code; it needs only numpy. The scores are derived from
  NTU RGB+D and are available from the corresponding author on reasonable request and with permission of the ROSE Lab.
- The Raspberry Pi 4 is described as an experimental platform (about 7 Hz; the decision threshold has not been
  re-validated at that rate).
- `CITATION.cff`: title and abstract aligned with the article.
- No change to the edge runtime, the bridge or the web application.

## 1.1.0 — 2026-11-03

- Notification policy version 2: an alert is classified from the latest state of its event (live, manual review or
  outcome unknown) instead of its arrival time; the device sends the event age, the outbox backlog and a
  clock-synchronised flag as HTTP headers ([docs/EVENT_PROTOCOL.md](docs/EVENT_PROTOCOL.md) §4). Server migration
  `0002`, edge outbox migrations `0003` and `0004`.
- Outbox durability: a thread of the outbox syncs the write-ahead log to storage every second and checkpoints it
  outside commits, so a power loss costs at most the last second of committed messages and never stalls the
  inference loop.
- Contract strings renamed to `fall-relay-edge-event-v1` and `fall-relay-pose13-feature59-v2`; the server still
  accepts messages and run identities that carry the earlier strings.
- Web proxy forwards the alert policy version, so manual-review alerts appear on the Alerts page.
- Evidence of the link-loss, late-arrival and power-loss trials on the Raspberry Pi 5
  ([docs/evidence/README.md](docs/evidence/README.md)); web page captures retaken from one run of this version.
- Code of conduct and issue templates.

## 1.0.1 — 2026-10-29

- Code defaults and documentation aligned with the article; the measured behaviour is unchanged.
- `Licence.txt`, a copy of `LICENSE` under the file name the SoftwareX template asks for.
- CI runs the synthetic workflow on PostgreSQL 14 and 16.

## 1.0.0 — 2026-10-28

- First public release.
