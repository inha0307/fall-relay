# NTU RGB real-time Pi replay (Table 4 of the article; evidence Figure E1)

This is a system performance replay of the existing NTU **test split**, not a new
independent model accuracy evaluation. Never substitute synthetic scores/keypoints.
The operational bridge, databases, credentials and device outboxes are not used.

## Frozen workload

`prepare.py` reads all eligible test rows of `paired_manifest_v2.csv`, checks RGB ZIP
membership, chooses one hash-ranked camera per capture group, then selects 12 A043,
8 A006, 8 A042 and one group from each of 20 other hash-ranked actions. Seed 20261021.
All 48 choices and their order are frozen before inference. Original full AVI members
are extracted without transcoding; FPS, frame count, duration, dimensions and SHA256
are recorded. The same list is used on both devices in all conditions.

The list is repeated as necessary for each fixed interval. Unique source videos,
unique capture groups, presentations and repetitions are separate counts. Failure to
detect an A043 presentation, load failures and missing slots remain in the logs. They
are not used to change the selection, decision threshold, models or operating point.

## Measurement schedule

One real Chromium browser on the WSL host uses the actual built Next.js application,
admin test account, both camera scopes, production AlertProvider/SSE/received route.
No human acknowledgement/resolve action is generated. Notification sound is off.
The isolated API, Next proxy, browser host/network and two-device concurrency stay
fixed across all runs. The frontend is a separately built source snapshot; production
`.next` and account DB are untouched.

Both Pis run simultaneously. Four paired trials alternate clip off/on, two repeats
each. Each trial loads the deployed models once, then runs 60 seconds of warmup
(excluded from every measurement), a marked 3-second phase boundary, and 1200 seconds
of measurement. The isolated bridge runs on PostgreSQL like the laboratory deployment:
the controller creates a schema of its own (`bench_<random>`) in the database named by
`BENCHMARK_PG_URL`, initialised empty from the current migrations, and records it in
`database.json`; a dedicated local PostgreSQL instance keeps the operational database
out of the experiment. Before the trials the controller records the uptime and load of
both devices and the server (`uptime_start.json`, `uptime_end.json`) and stops the
services listed as `unrelated_services` for a device in `setup.json`, restarting them
afterwards (`unrelated_services.json`). Within each
phase the playlist starts at its first video in the same order. Each original video
is played at its original FPS using deadline-based capture; overdue source frames
are skipped rather than extending the original video. A common 2.5-second marked
last-frame hold lets evidence clips finish; it **does not run model inference**.
Each sequence has a 1-second marked boundary. Original exposure, forced holds,
loading, phase gaps and boundaries are logged separately. A final truncated clip
can finish in a marked post-measurement hold, outside the measured wall interval.

At sequence boundaries the tracker, featurizer, history, eligibility, grid and decision
state reset. An open event is durably closed as runtime_stop. The model interpreter
and NCNN net handles remain in memory. The event outbox worker remains active;
undelivered records are retained and are not discarded to move to the next video.

The effective decision parameters come explicitly from the **device's deployed
manifest**: theta .95, k/n/m 3/5/3, cooldown 0 seconds; target grid 10 Hz. All model
and implementation hashes are verified and recorded. The manifest bundle ID may
have the deployed `-ev` suffix; artifact/decision hashes are compared, not inferred
from the label. Production compact result transport is reused unchanged: full run
identity registration/60-second refresh, otherwise camera-bound model_run_id.
Continuous JPEG streaming is never started. Evidence clip defaults are 3 seconds
before / 2 after the decision, 10 FPS, 640×360, JPEG quality 75; overrides are logged.

## Timing definitions

- Processing raw sample: monotonic_ns immediately before and after `core.process`.
  Includes pose, tracker, features, LPN, TCN and decision; excludes decode/resize,
  SQLite event serialization/outbox, HTTP and rendering. Distributions use **all
  original-source measured slots**, including ineligible/missed-person observations.
- Achieved source Hz: processed source slots / actual original sequence exposure.
  Short sequences each have a grid starting at zero; finite per-video slot density
  can slightly exceed 10 Hz. Missing slots use the explicit per-sequence expected
  grid count. Counts are never rescaled or imputed.
- Decision → server: passive ASGI complete request-body receipt minus recorded
  device decision time, with the outbox midpoint clock offset. Offset and RTT are
  retained. Half-RTT is a conservative alignment residual bound under that protocol;
  negative estimates and RTT >100 ms are retained but excluded from valid positive
  delay percentiles. Chrony tracking is also recorded, without changing clock setup.
- Server → browser: actual EventSource callback entry Date.now minus server body
  receipt. Server and Chromium share the WSL host clock. The production browser's
  received POST and its server confirmation are retained separately. This is not
  user acknowledgement, response time or render completion.
- Outbox creation → ACK: separate queue/round-trip elapsed wall time. Never label it
  one-way or add it unconditionally to the two delivery stages.
- Evidence readiness: distinct server-side final-file observation (0.1-second polling
  resolution) and time from decision; it is not alert delivery latency.

The processing-time distributions (evidence Figure E1; "Fig 6" in the tool output names) and the device table (Table 4 of the article) come from the **same measured trial intervals**.
The table's CPU/resources/bandwidth describe the complete sustained replay workload,
including explicitly marked holds and boundaries; original-video processing rates
exclude those intervals. Source-exposure seconds are reported next to wall duration.

CPU is process time divided by logical cores (100% = all device cores); one-core
scale is also logged. RSS MiB = bytes / 1048576. HTTP is exact request line, headers
and body bytes, separately for result/event/clip/health/context/etc. eth0 is the entire
device counter, including TCP/IP, SSH, the retained operational device agent and
other projects. Its rate uses the actual first/last sample span. Services named in
`setup.json` are stopped for the trials and restarted afterwards; the device's own
Fall-Relay fall detector service is stopped and restored by the remote supervisor. NTU has an
artificially enriched event rate;
these rates must not be presented as average field bandwidth.

## Execution / recovery

```bash
FALLRELAY_DATABASE_URL='...' server/.venv/bin/python edge/ntu_benchmark/prepare.py --training "$FALLRELAY_TRAINING" --out artifacts/fig6_ntu_benchmark/NEW_ID   # the audit reads the reference database read-only
BENCHMARK_PG_URL='host=/path/to/socket port=54331 dbname=fall_relay_bench user=<user>' \
  BENCHMARK_FRONTEND=/tmp/fig6-ntu-frontend-build server/.venv/bin/python edge/ntu_benchmark/controller.py --root artifacts/fig6_ntu_benchmark/NEW_ID
server/.venv/bin/python edge/ntu_benchmark/aggregate.py --root artifacts/fig6_ntu_benchmark/NEW_ID --ppt /path/to/latest_full_deck.pptx
server/.venv/bin/python edge/ntu_benchmark/export.py --root artifacts/fig6_ntu_benchmark/NEW_ID --out /path/to/supplement
```

`--training` is the folder holding the NTU-derived files of [training/README.md](../../training/README.md)
(`source_inventory_v2/`, `tcn_v2_meta/`). The controller reads the device hosts, SSH key and browser paths from `setup.json` (copy `setup.example.json`;
it is not versioned). It needs SSH access with that key, the installed
model runtimes on both Pis, the original RGB ZIPs, an isolated current frontend build,
Chromium and its runtime libraries (`BENCHMARK_BROWSER_LIBS` when the host lacks them). Analysis packages live in
`artifacts/fig6_ntu_benchmark/analysis_deps`; operation environments are not changed.
Preparation needs OpenCV from `server/.venv`. The test server binds loopback only and
is reached by a separate SSH reverse port 15992. Test tokens travel on SSH stdin into
owner-only remote job files, never command lines or reported logs.

The remote supervisor records the service's prior active state, stops only that Pi's
Fall-Relay fall detector service, and restores the state after success, child failure, controller
abort/disconnect or 90-second lease expiry. A controller-disconnect recovery was
exercised during preparation; both original services were verified active afterwards.
The improved signal path uses KeyboardInterrupt rather than InterruptedError, which
selector implementations can swallow. It also retrieves failed raw trials.

Preparation failures, invalid pilot versions and interrupted warmups remain under
separate run folders and are excluded from main measurements. Final aggregation
never fills absent fields. Pilot-only graphs are marked PARTIAL. Main graphs/PPT are
published only after actual data exist; complete reporting requires all four paired
runs and both services' restoration proofs.

Artifacts: selection_frozen.json, playlist CSV/JSON + original AVIs, code snapshot and
hashes, device jobs (tokens redacted), server/browser/device raw logs, the bridge schema
name, device outboxes, uptime and unrelated-service records, per-trial schedules, restoration
proofs, Fig 6 SVG/PNG/JPG, XLSX (per-condition, per-trial, event stages, raw resources,
HTTP requests, outbox ACKs, definitions), report and PPT verification. The revised PPT
contains native editable curves/markers/text; only the Fig 6 slide and notes change,
and all other ZIP parts are checked byte-for-byte identical to the latest full deck.
