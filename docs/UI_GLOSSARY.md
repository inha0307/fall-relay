# Fall-Relay UI glossary

Every screen, the documentation and the paper use these terms. Internal keys are shown where
they differ from the label.

## Product, navigation and roles

| Item | Label | Key |
|---|---|---|
| Product | Fall-Relay | — |
| Navigation (Operations) | Status · Alerts · Skeleton view · Devices · Reports | `/overview`, `/alerts`, `/skeleton`, `/devices`, `/reports` |
| Navigation (Administration) | Server · Camera access · Accounts | `/system`, `/manage/assignments`, `/manage` |
| Roles | Administrator · Viewer · Responder | `admin`, `viewer`, `responder` |

- Administrator: manages accounts, cameras and devices; can handle alerts.
- Viewer: read-only access to status and records.
- Responder: sees and handles alerts for assigned cameras.
- Status: what needs attention now (waiting alerts, cameras not detecting, devices needing attention).
- Reports: statistics over a chosen period (handling times, outcomes, false alarms, detection availability).
- Skeleton view shows the live skeletons; Devices shows device readings; Alerts keeps the alert history.
- A refused alert action says "Administrator or responder role required".
- The UI says "server", never "bridge".

## Detection (device side)

| Term | Meaning |
|---|---|
| Edge device | The computer that runs detection for one or more cameras |
| Decision | One per-slot result from the device (10 Hz design rate on the Raspberry Pi profile) |
| Decision rate | Decisions per second received from the device (Hz) |
| Processing time | Pose to decision on the device (ms) |
| Valid slots (last 20) | Slots in the model window with enough visible joints |
| Detection event | One detected fall on the device: open, update, close |
| Device health | Detecting · Degraded · Not detecting · Offline |
| Status board | Detecting · Fall event open · Not detecting · Device warning |
| Commands | Restart detection · Stop detection · Start detection · Fetch log · Reboot device |

## Alerts (responder side)

| Term | Meaning |
|---|---|
| Fall alert | The responder's task created from a detection event |
| Timeline | Detected on device → Received by server → Delivered to browser → Acknowledged → Resolved |
| States | Pending · Delivered · Acknowledged · Resolved · Expired |
| Delivered | A browser stored a receipt; it does not mean a person saw it |
| Manual review | Arrived late or with an unreliable device clock; no sound or notification |
| Outcomes | Fall confirmed · False alarm (sitting / lying down / bending / other) · Uncertain |

## Writing rules

- Do not use: caregiver, patient, care, elderly, protect, safety, or an unqualified "real-time".
- Dates: `2026-10-22 18:30:05 GMT+9` (24-hour clock, the browser's time zone shown).
- Units: a space between number and unit (`10.0 Hz`, `46 ms`, `64 °C`); durations `48 s`, `2.3 min`.
- English only.
