# Changelog

Each release is archived on Zenodo; the DOI 10.5281/zenodo.23719976 resolves to the latest version.

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
