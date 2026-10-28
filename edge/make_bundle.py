#!/usr/bin/env python3
"""Assemble a Pi deploy bundle for the v2 runtime: code from this repository, weights from given files.

  python edge/make_bundle.py --out DIR --bundle-id fall-relay-v2-20261022-ev \
      --lpn .../lpn13_fp32.tflite --tcn .../event_tcn_v2_fp32.tflite \
      [--install-dir /home/<user>/fall-relay] [--python PATH] [--camera 1] [--clips]

The decision block, pose-model hashes and provenance are kept from edge/bundle_manifest.json; file hashes are
recomputed for everything copied, including the shared event modules (edge_outbox, edge_event_runtime,
edge_event_contract, schema_migrations, migrations/edge/*.sql) the runtime uses to deliver events.
The bundle is self-contained: the runtime imports nothing from outside it except installed packages (requirements-edge.txt
and the system Picamera2). The systemd unit is rendered from deploy/fall-relay-edge.service.template for the given
install directory and camera.
The runtime refuses to start when any listed file differs from its hash. See deploy/README.md.
"""

import argparse, hashlib, json, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "server" / "bridge"
CODE = [
    "fall_relay_edge_runtime.py",
    "edge_runtime_v2_core.py",
    "decision_engine.py",
    "features_v2.py",
    "shape_features_v2.py",
    "head_point.py",
    "pose17.py",
    "causal_tracker.py",
    "head_point_v2/head_point_v2.json",
    "result_transport.py",
    "video_source.py",
]
SHARED_CODE = ["edge_config.py", "decision_log_runtime.py"]  # from server/bridge, needed in every mode
EVENT_CODE = ["event_bridge_v2.py"]
EVENT_SHARED = [
    "edge_outbox.py",
    "edge_event_runtime.py",
    "edge_event_contract.py",
    "schema_migrations.py",
    "db_backend.py",
    "migrations/edge/0001_outbox.sql",
    "migrations/edge/0002_runtime_recovery.sql",
]
TEMPLATE = HERE / "deploy" / "fall-relay-edge.service.template"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--bundle-id", required=True)
    ap.add_argument("--lpn", type=Path, required=True)
    ap.add_argument("--tcn", type=Path, required=True)
    ap.add_argument("--install-dir", default="/home/<user>/fall-relay", help="bundle folder on the device")
    ap.add_argument("--python", default="/usr/bin/python3", help="Python on the device (system packages for Picamera2)")
    ap.add_argument("--camera", default="1", help="camera id; names the unit fall-relay-edge-cam<id>.service")
    ap.add_argument("--clips", action="store_true", help="upload short event clips (contain camera images)")
    args = ap.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise SystemExit(f"{args.out} is not empty")
    manifest = json.loads((HERE / "bundle_manifest.json").read_text())
    files = {name: HERE / name for name in CODE}
    files.update({name: SHARED / name for name in SHARED_CODE})
    files.update({"lpn13_fp32.tflite": args.lpn, "event_tcn_v2_fp32.tflite": args.tcn})
    files.update({name: HERE / name for name in EVENT_CODE})
    files.update({name: SHARED / name for name in EVENT_SHARED})
    for name, src in files.items():
        dst = args.out / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for weight in ("lpn13_fp32.tflite", "event_tcn_v2_fp32.tflite"):  # the models must stay the frozen ones
        if sha(args.out / weight) != manifest["sha256"][weight]:
            raise SystemExit(f"{weight} differs from the frozen model hash")
    extra = f" --outbox {args.install_dir}/edge_events.sqlite3" + (" --clips" if args.clips else "")
    unit = f"fall-relay-edge-cam{args.camera}.service"
    (args.out / unit).write_text(
        TEMPLATE.read_text().format(
            install_dir=args.install_dir, python=args.python, camera=args.camera, extra_args=extra
        )
    )
    hashed = [n for n in files if n != "fall_relay_edge_runtime.py"]
    manifest.update(
        bundle_id=args.bundle_id,
        event_protocol="edge_event",
        sha256={n: sha(args.out / n) for n in sorted(hashed)},
    )
    (args.out / "bundle_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "bundle_id": args.bundle_id,
                "event_protocol": manifest["event_protocol"],
                "files": len(files) + 2,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
