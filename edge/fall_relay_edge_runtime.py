#!/usr/bin/env python3
"""Fall-Relay edge runtime (v2, production): camera -> v2 edge core -> server.

Inference uses EdgeRuntimeV2Core (13 joints, feature59-v2, LPN v2, TCN v2, latched k-of-n decision). Every
open/update/close of a fall event is written to a local outbox (SQLite) and delivered to /api/edge_event until the
bridge acknowledges it (event_bridge_v2, docs/EVENT_PROTOCOL.md). Once per decision slot a heartbeat goes to
/api/edge_result with the decision state, the outbox counters and the chosen person's 13 model joints (stream-frame
pixels) for the skeleton view; it never creates events. Heartbeats register and refresh a run identity with contract
fallscope-pose13-feature59-v2 (model_contract.validate_identity), also written to run_identity.json; a separate
latest-wins sender thread posts them, so network stalls never block inference.

Camera images stay on the device. With --clips the device keeps the last few seconds as small JPEGs in memory and
uploads only the window around each detected fall (event_clip: pre_s before and post_s after the open decision) to
/api/edge_clip, retrying until stored. --video pushes JPEG frames to /push_frame (debugging only). --metrics PATH
appends one JSON line per decision slot (sensor time, slot index, latency, state) for benchmarks.
"""

import argparse, hashlib, json, logging, os, queue, signal, sys, threading, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Bundle modules first. A bundle (make_bundle.py) contains every module it needs; run from the repository, the shared
# modules come from server/bridge (appended, so the bundle's own copies always win). Device-specific package
# folders (system Picamera2, extra site-packages) are listed in edge_config.json "extra_python_paths".
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent / "server" / "bridge"))
import numpy as np  # noqa: E402

V2_MODULES = (
    "pose17",
    "causal_tracker",
    "features_v2",
    "head_point",
    "shape_features_v2",
    "decision_engine",
    "edge_runtime_v2_core",
)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def event_decision(decision):
    """event_decision identity block: the decision parameters and every module that turns them into bridge events."""
    import decision_engine, edge_event_contract, edge_event_runtime, edge_outbox, schema_migrations

    files = {
        m.__name__: Path(m.__file__)
        for m in (decision_engine, edge_outbox, edge_event_runtime, edge_event_contract, schema_migrations)
    }
    sql = Path(edge_outbox.__file__).parent / "migrations" / "edge"
    files.update(edge_schema_1=sql / "0001_outbox.sql", edge_schema_2=sql / "0002_runtime_recovery.sql")
    return {
        "event_protocol": "edge_event",
        "declared": dict(decision),
        "effective": dict(decision),
        "overridden": False,
        "implementation_sha256": {k: sha(p) for k, p in files.items()},
    }


def run_identity(run_id, manifest, config_path, runtime_path, settings, params, events):
    """Run identity in the bridge's schema. Settings must equal the per-result threshold / confirm_slots /
    window_min_valid_slots fields; event_decision declares the decision block every event must match."""
    identity = {
        "schema_version": 1,
        "run_id": run_id,
        "bundle_id": manifest["bundle_id"],
        "bundle_sha256": sha(HERE / "bundle_manifest.json"),
        "config_sha256": sha(config_path),
        "runtime_sha256": sha(runtime_path),
        "artifacts_sha256": {
            "pose_param": manifest["pose_sha256"]["pose_param"],
            "pose_weights": manifest["pose_sha256"]["pose_weights"],
            "lpn": manifest["sha256"]["lpn13_fp32.tflite"],
            "tcn": manifest["sha256"]["event_tcn_v2_fp32.tflite"],
        },
        "implementation_sha256": {m: manifest["sha256"][f"{m}.py"] for m in V2_MODULES},
        "settings": settings,
        "contract_version": manifest["contract"],
        "started_at": time.time(),
        "verification": f"bundle manifest file hashes checked at start-up; decision k/n/m={params.k}/{params.n}/{params.m} (latched)"
        "; explicit edge events via local outbox",
        "event_decision": event_decision(events.decision),
    }
    return identity


def skeleton(k17, table, scale):
    """The 13 joints the model sees (ORDER: R/L shoulder, elbow, wrist, hip, knee, ankle, then the calibrated head
    point) as [x, y, confidence] in stream-frame pixels. A joint the LPN input masks out (features_v2.lpn_input rule:
    confidence below 0.2 or outside the 640x360 pose frame) is null, so face keypoints never leave the device."""
    from features_v2 import ORDER, W, H, CONF
    from head_point import head_point

    k = np.asarray(k17, dtype=np.float64)
    if not np.isfinite(k).any():
        return None
    k12 = k[ORDER]
    hxy, hconf = head_point(k, table)
    xy = np.vstack([k12[:, :2], hxy if hxy is not None else [np.nan, np.nan]])
    conf = np.r_[k12[:, 2], hconf]
    vis = np.isfinite(xy).all(1) & (conf >= CONF) & (xy[:, 0] >= 0) & (xy[:, 0] < W) & (xy[:, 1] >= 0) & (xy[:, 1] < H)
    if not vis.any():
        return None
    return [
        [round(float(x) * scale[0], 1), round(float(y) * scale[1], 1), round(float(c), 3)] if v else None
        for (x, y), c, v in zip(xy, conf, vis)
    ]


# The clip is evidence for the person handling the alert, not an input of the fall detector (detection uses the 10 Hz slots).
# 10 fps keeps 4-5 frames of a ~0.45 s fall descent; the bridge encodes at 10 fps, so no frames are repeated.
CLIP_DEFAULTS = {"pre_s": 3.0, "post_s": 2.0, "fps": 10.0, "width": 640, "height": 360, "jpeg_quality": 75}
CLIP_RETRY_S, CLIP_GIVE_UP_S = 5.0, 600.0


class EventClips:
    """Fall-window clips from a skeleton-only device. A sampler thread keeps small JPEGs of the last
    pre_s + post_s seconds; open() marks a detected fall; once post_s of frames exist after it, the window is
    queued and an uploader posts it (409 = event not stored yet, network errors: retried for 10 min)."""

    def __init__(self, cfg, server, camera, run_id, headers, latest, cond, stop):
        import cv2, requests

        self.cv2, self.requests = cv2, requests
        self.c = {**CLIP_DEFAULTS, **cfg}
        self.server, self.camera, self.run_id, self.headers = server, camera, run_id, headers
        self.latest, self.cond, self.stop = latest, cond, stop
        self.frames = []  # (sensor_ns, wall, jpeg)
        self.marks = []  # (onset_sensor_ns, decided_sensor_ns)
        self.uploads = queue.Queue()
        self.stored = self.failed = 0

    def open(self, onset_ns, decided_ns):
        with self.cond:
            self.marks.append((int(onset_ns), int(decided_ns)))

    def sample_frame(self, sensor, wall, img):
        """Shared clip-window path for camera sampling and deterministic recorded-frame checks."""
        keep_ns = (self.c["pre_s"] + self.c["post_s"] + 2.0) * 1e9
        size = (self.c["width"], self.c["height"])
        quality = [self.cv2.IMWRITE_JPEG_QUALITY, self.c["jpeg_quality"]]
        ok, jpg = self.cv2.imencode(".jpg", self.cv2.resize(img, size, interpolation=self.cv2.INTER_AREA), quality)
        if not ok:
            return
        with self.cond:
            self.frames.append((sensor, wall, jpg.tobytes()))
            self.frames = [x for x in self.frames if x[0] >= sensor - keep_ns]
            due = [m for m in self.marks if sensor >= m[1] + self.c["post_s"] * 1e9]
            self.marks = [m for m in self.marks if m not in due]
            windows = [
                (m, [x for x in self.frames if m[1] - self.c["pre_s"] * 1e9 <= x[0] <= m[1] + self.c["post_s"] * 1e9])
                for m in due
            ]
        for (onset, _), frames in windows:
            if frames:
                self.uploads.put((time.monotonic(), onset, frames))

    def sampler(self):
        period, last = 1.0 / self.c["fps"], -1
        while not self.stop.wait(period):
            with self.cond:
                f = self.latest["f"]
            if f is None or f[0] == last:
                continue
            last, sensor, img = f
            self.sample_frame(sensor, time.time(), img)

    def uploader(self):
        import base64

        with self.requests.Session() as s:
            while not self.stop.is_set():
                try:
                    first, onset, frames = self.uploads.get(timeout=1)
                except queue.Empty:
                    continue
                body = {
                    "camera_id": self.camera,
                    "run_id": self.run_id,
                    "onset_sensor_ns": onset,
                    "frames": [{"t": wall, "jpeg": base64.b64encode(j).decode()} for _, wall, j in frames],
                }
                while not self.stop.is_set():
                    try:
                        r = s.post(self.server + "/api/edge_clip", json=body, headers=self.headers, timeout=(3, 20))
                        if r.ok:
                            self.stored += 1
                            logging.info("event clip stored (%d frames, onset %d)", len(frames), onset)
                            break
                        if r.status_code not in (409, 429) and r.status_code < 500:
                            self.failed += 1
                            logging.warning("event clip refused %s: %s", r.status_code, r.text[:200])
                            break
                        reason = f"HTTP {r.status_code}"
                    except self.requests.RequestException as e:
                        reason = str(e)
                    if time.monotonic() - first > CLIP_GIVE_UP_S:
                        self.failed += 1
                        logging.warning("event clip dropped after %.0f s: %s", CLIP_GIVE_UP_S, reason)
                        break
                    self.stop.wait(CLIP_RETRY_S)
                self.uploads.task_done()


def edge_result(r, core, params, cam_id, seq, identity, scale, latency_ms, events, order=None):
    """/api/edge_result heartbeat for one decision slot (display, liveness, outbox state)."""
    high = sum(1 for _, h in core.engine.window if h)
    valid_slots = int(sum(core.history_valid))
    state = r["state"]
    status = (
        "FALL"
        if state in ("ALERT", "POST_FALL")
        else ("WAIT" if (not r["eligible"] or state == "SUSPECT") else "NORMAL")
    )
    k12 = np.asarray(r["k17"])[order]  # visibility over the 12 body joints
    vis = (
        np.isfinite(k12).all(1)
        & (np.nan_to_num(k12[:, 2]) >= 0.2)
        & (k12[:, 0] >= 0)
        & (k12[:, 0] < 640)
        & (k12[:, 1] >= 0)
        & (k12[:, 1] < 360)
    )
    bbox = None if r["box"] is None else [v * s for v, s in zip(r["box"], scale * 2)]
    result = {
        "cam_id": cam_id,
        "seq": int(seq),
        "input_valid": r["valid"],
        "window_valid_slots": valid_slots,
        "window_min_valid_slots": core.min_valid,
        "window_eligible": r["eligible"],
        "people": r["people"],
        "track_id": r["track_id"],
        "bbox": bbox,
        "score": float(r["score"]),
        "threshold": params.theta,
        "consecutive_high": int(high),
        "confirm_slots": params.k,
        "status": status,
        "detector_score": r["detector_score"],
        "visible_joints": int(vis.sum()),
        "mean_keypoint_confidence": float(k12[vis, 2].mean()) if vis.any() else 0.0,
        "latency_ms": round(latency_ms, 2),
        "skeleton": skeleton(r["k17"], core.featurizer.table, scale),
        "frame_size": [round(640 * scale[0]), round(360 * scale[1])],
        "model_identity": identity,
        **events.heartbeat(),
    }
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=HERE / "edge_config.json")
    ap.add_argument("--outbox", type=Path, default=HERE / "edge_events.sqlite3")
    ap.add_argument("--video", action="store_true", help="debug: also push JPEG camera frames to the server")
    ap.add_argument("--metrics", type=Path, help="append one JSON line per decision slot (benchmarks)")
    ap.add_argument("--clips", action="store_true", help="upload a short clip around each detected fall")
    ap.add_argument("--input-video", type=Path, help="replay a recorded clip in real time instead of the camera")
    ap.add_argument("--full-identity", action="store_true", help="baseline: send the full identity with every result")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd stop: run the finally block (close open event)
    import cv2
    import requests
    from edge_config import load_edge_config, select_sensor_mode

    cfg = load_edge_config(args.config)
    for extra in cfg.get("extra_python_paths", []):  # e.g. the system Picamera2/libcamera
        if extra not in sys.path:
            sys.path.append(extra)
    import ncnn
    from edge_runtime_v2_core import EdgeRuntimeV2Core
    from decision_engine import DecisionParams
    from features_v2 import ORDER

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    manifest = json.loads((HERE / "bundle_manifest.json").read_text())
    bad = [f for f, h in manifest["sha256"].items() if sha(HERE / f) != h]
    if bad:
        raise SystemExit(f"bundle files changed: {bad}")
    d = manifest["decision"]
    params = DecisionParams(theta=d["theta"], k=d["k"], n=d["n"], m=d["m"], cooldown_s=d.get("cooldown_s", 0.0))
    cv2.setNumThreads(1)
    net = ncnn.Net()
    net.opt.num_threads = cfg["pose_threads"]
    net.opt.use_vulkan_compute = False
    net.opt.use_fp16_storage = net.opt.use_fp16_packed = net.opt.use_fp16_arithmetic = False
    pose_dir = (args.config.parent / cfg["pose_model_dir"]).resolve()
    for name, key in (("model.ncnn.param", "pose_param"), ("model.ncnn.bin", "pose_weights")):
        if sha(pose_dir / name) != manifest["pose_sha256"][key]:
            raise SystemExit(f"pose model {name} differs from bundle")
    assert (
        net.load_param(str(pose_dir / "model.ncnn.param")) == 0
        and net.load_model(str(pose_dir / "model.ncnn.bin")) == 0
    )
    core = EdgeRuntimeV2Core(net, HERE / "lpn13_fp32.tflite", HERE / "event_tcn_v2_fp32.tflite", params)
    run_id = uuid.uuid4().hex
    server = cfg["server_url"].rstrip("/")
    cam_id = cfg["camera_id"]
    token = os.environ.get("FALLRELAY_CAMERA_TOKEN", "").strip()
    headers = {"X-Camera-Token": token} if token else {}
    if not token:
        raise SystemExit("FALLRELAY_CAMERA_TOKEN is required (camera_token_cam<N>.env)")
    from event_bridge_v2 import EventBridgeV2

    events = EventBridgeV2(args.outbox, server, cam_id, run_id, core.engine, headers)
    settings = {
        "inference_hz": cfg["inference_hz"],
        "window_slots": core.slots,
        "min_valid_pose_slots": core.min_valid,
        "fall_threshold": params.theta,
        "fall_confirm_slots": params.k,
    }
    identity = run_identity(run_id, manifest, args.config, Path(__file__), settings, params, events)
    (HERE / "run_identity.json").write_text(json.dumps(identity, indent=1) + "\n")
    logging.info("Fall-Relay edge runtime identity %s", json.dumps(identity))
    scale = (
        cfg["width"] / 640.0,
        cfg["height"] / 360.0,
    )  # pose input (640x360) -> stream-frame pixels for the Skeleton view
    logging.info(
        "camera frames %s", "pushed to the server (--video)" if args.video else "stay on the device; skeleton only"
    )

    latest = {"f": None}
    cond = threading.Condition()
    stop = threading.Event()
    results = queue.Queue(maxsize=1)
    capture_done = threading.Event()
    capture_errors = []
    if args.input_video:
        from video_source import VideoSource

        cam = VideoSource(
            args.input_video,
            (cfg["width"], cfg["height"]),
            tail_s=cfg.get("event_clip", {}).get("post_s", CLIP_DEFAULTS["post_s"]) + 0.5,
        )
        logging.info("replay input=%s fps=%.3f; final frame held for post-clip window", args.input_video, cam.fps)
    else:
        from picamera2 import Picamera2
        from libcamera import controls

        tuning = cfg.get("camera_tuning_file")
        tuning = json.loads((args.config.parent / tuning).read_text()) if tuning else None
        cam = Picamera2(cfg["camera_index"], tuning=tuning)
        mode = select_sensor_mode(cam.sensor_modes, cfg["sensor_mode"])
        fps = min(cfg["capture_fps"], float(mode["fps"]))
        cam.configure(
            cam.create_video_configuration(
                main={"size": (cfg["width"], cfg["height"]), "format": "RGB888"},
                controls={"FrameRate": fps},
                buffer_count=6,
                queue=False,
                sensor={"output_size": mode["size"], "bit_depth": mode["bit_depth"]},
            )
        )
        if "AfMode" in cam.camera_controls:
            cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        cam.start()

    def capture():
        try:
            if args.input_video:
                for frame in cam.frames(stop):
                    with cond:
                        latest["f"] = frame
                        cond.notify_all()
            else:
                seq = 0
                while not stop.is_set():
                    req = cam.capture_request()
                    try:
                        sensor = int(req.get_metadata()["SensorTimestamp"])
                        img = req.make_array("main").copy()
                    finally:
                        req.release()
                    seq += 1
                    with cond:
                        latest["f"] = (seq, sensor, img)
                        cond.notify_all()
        except Exception as exc:
            capture_errors.append(exc)
            logging.exception("capture failed")
        finally:
            with cond:
                capture_done.set()
                cond.notify_all()

    def stream():
        seen = -1
        with requests.Session() as s:
            while not stop.is_set():
                with cond:
                    cond.wait_for(
                        lambda: stop.is_set() or (latest["f"] is not None and latest["f"][0] != seen), timeout=1
                    )
                    if latest["f"] is None or latest["f"][0] == seen:
                        continue
                    seen, _, img = latest["f"]
                ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, cfg["stream_jpeg_quality"]])
                if not ok:
                    continue
                try:
                    s.post(
                        server + "/push_frame",
                        params={"cam": cam_id, "run_id": run_id, "seq": seen},
                        data=jpg.tobytes(),
                        headers={"Content-Type": "image/jpeg", **headers},
                        timeout=(2, 3),
                    ).raise_for_status()
                except requests.RequestException as e:
                    logging.warning("Video send failed: %s", e)

    from result_transport import ResultTransport

    transport = ResultTransport(identity, full_identity=args.full_identity)
    send_failures = []

    def sender():
        with requests.Session() as s:
            while not stop.is_set():
                try:
                    r = results.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    transport.send(s, server + "/api/edge_result", r, headers)
                except (requests.RequestException, ValueError) as e:
                    if args.input_video:
                        send_failures.append(str(e))
                    logging.warning("Result send failed: %s", e)
                finally:
                    results.task_done()

    clips = None
    if args.clips:
        clips = EventClips(cfg.get("event_clip", {}), server, cam_id, run_id, headers, latest, cond, stop)
        logging.info("event clips on: %s", json.dumps(clips.c))
    threads = (capture, sender) + ((stream,) if args.video else ()) + ((clips.sampler, clips.uploader) if clips else ())
    for fn in threads:
        threading.Thread(target=fn, daemon=True).start()
    metrics = args.metrics.open("a", buffering=1) if args.metrics else None
    from decision_log_runtime import SlotRecorder

    try:
        diagnostic_logs = SlotRecorder(HERE / 'decision_logs', cam_id, identity)
    except OSError:
        diagnostic_logs = None
        logging.exception('Optional decision logging unavailable')
    seen, last_log, last_state = -1, 0.0, None
    try:
        while True:
            if diagnostic_logs:
                diagnostic_logs.tick()
            with cond:
                cond.wait_for(
                    lambda: capture_done.is_set() or (latest["f"] is not None and latest["f"][0] != seen), timeout=1
                )
                if capture_done.is_set() and (latest["f"] is None or latest["f"][0] == seen):
                    break
                if latest["f"] is None or latest["f"][0] == seen:
                    continue
                seen, sensor, img = latest["f"]
            t0 = time.monotonic_ns()
            r = core.process(img, sensor)
            if r is None:
                continue
            ms = (time.monotonic_ns() - t0) / 1e6
            events.step(sensor, time.time(), r["score"], r["eligible"], r["track_id"], r["events"])
            if clips and events.active and any(ev["kind"] == "open" for ev in r["events"]):
                clips.open(events.active["onset"], sensor)
            result = edge_result(r, core, params, cam_id, seen, identity, scale, ms, events, ORDER)
            try:
                results.get_nowait()
                results.task_done()
            except queue.Empty:
                pass
            results.put_nowait(result)
            metric_row = {
                "wall": round(time.time(), 3),
                "sensor_s": round(r["t"], 4),
                "grid": r["grid"],
                "seq": seen,
                "latency_ms": round(ms, 2),
                "valid": r["valid"],
                "eligible": r["eligible"],
                "score": round(r["score"], 4),
                "state": r["state"],
                "people": r["people"],
            }
            if metrics:
                metrics.write(json.dumps(metric_row) + "\n")
            if diagnostic_logs:
                diagnostic_logs.tick(metric_row)
            for ev in r["events"]:
                logging.info("EVENT %s", json.dumps(ev))
            now = time.monotonic()
            state = r["state"]
            if state != last_state or now - last_log >= 5:
                outbox = f" outbox_pending={result['outbox_pending']}"
                logging.info(
                    "v2 state=%s status=%s score=%.3f eligible=%s valid_slots=%d people=%d latency=%.1fms%s",
                    state,
                    result["status"],
                    r["score"],
                    r["eligible"],
                    result["window_valid_slots"],
                    r["people"],
                    ms,
                    outbox,
                )
                last_state, last_log = state, now
        if capture_errors:
            raise RuntimeError("capture failed") from capture_errors[0]
        if args.input_video:
            # Let the sampler consume the final frame, then drain bounded HTTP/outbox work.
            time.sleep(0.3)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pending = results.unfinished_tasks or (clips and clips.uploads.unfinished_tasks)
                pending = pending or events.box.status()["outbox_pending"]
                if not pending:
                    break
                time.sleep(0.1)
            if pending or send_failures or (clips and (clips.failed or clips.marks)):
                raise RuntimeError("replay finished with failed or undelivered results, clips or events")
            logging.info("replay complete: %d decoded frames", cam.frames_read)
    finally:
        stop.set()
        cam.stop()
        if metrics:
            metrics.close()
        if diagnostic_logs:
            try:
                diagnostic_logs.close()
            except OSError:
                logging.exception('Optional decision log finalization failed')
        events.close()


if __name__ == "__main__":
    main()
