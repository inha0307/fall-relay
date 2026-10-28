#!/usr/bin/env python3
"""Real-time continuous NTU replay. Models load once; all mutable sequence state resets.

Uses deployed core, event bridge/outbox, compact result transport and evidence clip code.
This module is staged beside the deployed bundle; it does not modify production files.
"""

import argparse, json, math, os, queue, signal, subprocess, sys, threading, time, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cv2, requests, ncnn
import fall_relay_edge_runtime as runtime
from edge_runtime_v2_core import EdgeRuntimeV2Core
from decision_engine import DecisionParams
from causal_tracker import CausalTracker
from features_v2 import SlotFeaturizer, ORDER
from event_bridge_v2 import EventBridgeV2
from edge_event_runtime import request_json
from result_transport import ResultTransport
from pi_hardware_agent import Sampler
from edge_config import load_edge_config


def reset_sequence(core):
    core.tracker = CausalTracker()
    core.featurizer = SlotFeaturizer()
    core.engine.reset()
    core.origin = None
    core.last_grid = -1
    core.history = []
    core.history_valid = []
    assert core.engine.state == 'IDLE' and not core.engine.window and not core.history


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', required=True)
    args = ap.parse_args()
    job = json.loads(Path(args.job).read_text())
    out = Path(job['output'])
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_edge_config(job['config'])
    for extra in cfg.get('extra_python_paths', []):  # e.g. the LiteRT wheel of a system-Python device
        if extra not in sys.path:
            sys.path.append(extra)
    manifest = json.loads((HERE / 'bundle_manifest.json').read_text())
    d = manifest['decision']
    assert {k: d[k] for k in ('theta', 'k', 'n', 'm', 'cooldown_s')} == {
        'theta': 0.95,
        'k': 3,
        'n': 5,
        'm': 3,
        'cooldown_s': 0.0,
    }
    base = {k: job[k] for k in ('experiment_id', 'run_id', 'device', 'clips', 'trial', 'replicate')}
    lock = threading.Lock()
    streams = {}
    context = {'phase': 'loading', 'video_id': None, 'repeat': None, 'segment': None}
    stop = threading.Event()

    def log(stream_name, **data):
        item = {**base, **context, 'wall': time.time(), 'mono_ns': time.monotonic_ns(), **data}
        with lock:
            if stream_name not in streams:
                streams[stream_name] = (out / (stream_name + '.jsonl')).open('a', buffering=1)
            streams[stream_name].write(json.dumps(item, allow_nan=False) + '\n')

    def sig(*_):
        stop.set()

    signal.signal(signal.SIGTERM, sig)
    signal.signal(signal.SIGINT, sig)
    headers = {'X-Camera-Token': job['camera_token']}
    server = cfg['server_url'].rstrip('/')
    cam = str(cfg['camera_id'])
    # Verify the original deployment files and models before running any inference.
    for name, digest in manifest['sha256'].items():
        assert runtime.sha(HERE / name) == digest, ('bundle mismatch', name)
    pose = (Path(job['config']).parent / cfg['pose_model_dir']).resolve()
    for name, key in [('model.ncnn.param', 'pose_param'), ('model.ncnn.bin', 'pose_weights')]:
        assert runtime.sha(pose / name) == manifest['pose_sha256'][key]
    actual_hashes = {p.name: runtime.sha(p) for p in HERE.glob('*.py')}
    log(
        'lifecycle',
        kind='load_start',
        manifest=manifest,
        hashes=actual_hashes,
        config_sha256=runtime.sha(job['config']),
        clip_settings={**runtime.CLIP_DEFAULTS, **cfg.get('event_clip', {})},
        cpu_cores=os.cpu_count(),
    )
    cv2.setNumThreads(1)
    net = ncnn.Net()
    net.opt.num_threads = cfg['pose_threads']
    net.opt.use_vulkan_compute = False
    net.opt.use_fp16_storage = net.opt.use_fp16_packed = net.opt.use_fp16_arithmetic = False
    assert net.load_param(str(pose / 'model.ncnn.param')) == 0 and net.load_model(str(pose / 'model.ncnn.bin')) == 0
    params = DecisionParams(theta=d['theta'], k=d['k'], n=d['n'], m=d['m'], cooldown_s=d['cooldown_s'])
    core = EdgeRuntimeV2Core(
        net, HERE / 'lpn13_fp32.tflite', HERE / 'event_tcn_v2_fp32.tflite', params, hz=10, slots=20, min_valid=10
    )

    def event_request(url, h, payload=None):
        start = time.time()
        mono = time.monotonic_ns()
        try:
            reply = request_json(url, h, payload)
            log(
                'transport',
                path=url.split(server)[-1],
                started_at=start,
                elapsed_ms=(time.monotonic_ns() - mono) / 1e6,
                status='ok',
                event_uid=payload.get('event_uid') if payload else None,
                message_seq=payload.get('message_seq') if payload else None,
                reply=reply,
            )
            return reply
        except Exception as exc:
            log(
                'transport',
                path=url.split(server)[-1],
                started_at=start,
                elapsed_ms=(time.monotonic_ns() - mono) / 1e6,
                status='failed',
                error=type(exc).__name__,
                event_uid=payload.get('event_uid') if payload else None,
                message_seq=payload.get('message_seq') if payload else None,
            )
            raise

    bridge = EventBridgeV2(
        out / 'outbox.sqlite3', server, cam, job['run_id'], core.engine, headers, request_fn=event_request
    )
    settings = {
        'inference_hz': 10,
        'window_slots': 20,
        'min_valid_pose_slots': 10,
        'fall_threshold': params.theta,
        'fall_confirm_slots': params.k,
    }
    identity = runtime.run_identity(job['run_id'], manifest, job['config'], runtime.__file__, settings, params, bridge)
    (out / 'identity.json').write_text(json.dumps(identity, indent=2) + '\n')
    transport = ResultTransport(identity)
    latest = {'f': None}
    cond = threading.Condition()
    result_q = queue.Queue(maxsize=1)
    clips = (
        runtime.EventClips(cfg.get('event_clip', {}), server, cam, job['run_id'], headers, latest, cond, stop)
        if job['clips']
        else None
    )
    sender_errors = 0
    result_dropped = 0

    def sender():
        nonlocal sender_errors
        with requests.Session() as session:
            while not stop.is_set():
                try:
                    payload, meta = result_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                t = time.monotonic_ns()
                try:
                    r = transport.send(session, server + '/api/edge_result', payload, headers)
                    log('result_send', **meta, status=r.status_code, elapsed_ms=(time.monotonic_ns() - t) / 1e6)
                except Exception as exc:
                    sender_errors += 1
                    log('result_send', **meta, status='failed', error=str(exc))
                finally:
                    result_q.task_done()

    sampler = Sampler({'device_id': 'benchmark-' + job['device'], 'camera_id': cam, 'interval_s': 5, 'services': []})

    def resources():
        previous = None
        hz = os.sysconf('SC_CLK_TCK')
        cores = os.cpu_count() or 1
        last_health = 0
        with requests.Session() as session:
            while not stop.is_set():
                try:
                    now = time.monotonic()
                    fields = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
                    ticks = int(fields[11]) + int(fields[12])
                    rss = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
                    one = 100 * (ticks - previous[1]) / hz / (now - previous[0]) if previous else None
                    previous = (now, ticks)
                    tx = int(Path('/sys/class/net/eth0/statistics/tx_bytes').read_text())
                    rx = int(Path('/sys/class/net/eth0/statistics/rx_bytes').read_text())
                    temperature = float(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
                    data = {
                        'cpu_pct_one_core': one,
                        'cpu_pct_device': one / cores if one is not None else None,
                        'rss_bytes': rss,
                        'eth0_tx_bytes': tx,
                        'eth0_rx_bytes': rx,
                        'temperature_c': temperature,
                    }
                    if now - last_health >= 5:
                        last_health = now
                        report = sampler.sample()
                        data['throttle'] = report['throttle']
                        report['processes'] = [
                            {
                                'service': 'benchmark',
                                'pid': os.getpid(),
                                'running': True,
                                'rss_bytes': rss,
                                'cpu_pct': data['cpu_pct_device'],
                            }
                        ]
                        r = session.post(server + '/api/device_health', json=report, headers=headers, timeout=3)
                        data['health_status'] = r.status_code
                    log('resources', **data)
                except Exception as exc:
                    log('resources', error=type(exc).__name__ + ': ' + str(exc))
                stop.wait(1)

    threads = [threading.Thread(target=sender, daemon=True), threading.Thread(target=resources, daemon=True)]
    if clips:
        threads += [
            threading.Thread(target=clips.sampler, daemon=True),
            threading.Thread(target=clips.uploader, daemon=True),
        ]
    for t in threads:
        t.start()
    log(
        'lifecycle',
        kind='ready',
        model_loads=1,
        clock=subprocess.run(['chronyc', 'tracking'], capture_output=True, text=True).stdout,
    )
    (out / 'ready.json').write_text(json.dumps({'ready_at': time.time(), **base}))
    # Host releases both devices with an absolute UTC start after both models are ready.
    release = Path(job['release_file'])
    while not release.exists() and not stop.wait(0.1):
        pass
    if stop.is_set():
        raise RuntimeError('stopped before coordinated release')
    for _ in range(100):  # the supervisor replaces the file atomically; tolerate a partial read
        try:
            start_at = json.loads(release.read_text())['start_at']
            break
        except (ValueError, OSError):
            time.sleep(0.05)
    else:
        raise RuntimeError('release file unreadable')
    warmup = float(job['warmup_s'])
    duration = float(job['measure_s'])
    playlist = json.loads(Path(job['playlist']).read_text())
    expected_total = processed_total = 0
    video_summaries = []
    global_frame_seq = [0]

    def close_active():
        if bridge.active and bridge.last_sensor is not None:
            bridge._record('close', 'COOLDOWN', bridge.last_sensor, time.time(), 'runtime_stop')
            bridge.active = None

    def check_status():
        if bridge.worker.stop.is_set():
            raise RuntimeError('outbox worker stopped')

    try:
        for phase, phase_start, length in [
            ('warmup', start_at, warmup),
            ('measure', start_at + warmup + 3.0, duration),
        ]:
            context.update(phase=phase, video_id=None, repeat=0, segment='boundary')
            while time.time() < phase_start and not stop.is_set():
                stop.wait(min(0.1, phase_start - time.time()))
            reset_sequence(core)
            close_active()
            bridge.slots.clear()
            log('lifecycle', kind='phase_start', scheduled_at=phase_start, scheduled_end=phase_start + length)
            (out / ('phase_' + phase + '.json')).write_text(
                json.dumps(
                    {
                        'scheduled_start': phase_start,
                        'scheduled_end': phase_start + length,
                        'actual_start': time.time(),
                        **base,
                    }
                )
            )
            offset = 0
            repeat = 0
            while offset < length and not stop.is_set():
                for video in playlist:
                    if offset >= length or stop.is_set():
                        break
                    source_start = phase_start + offset
                    source_duration = min(float(video['duration_s']), length - offset)
                    tail = min(2.5, max(0, length - offset - source_duration))
                    end_at = source_start + source_duration + tail
                    context.update(video_id=video['sample_id'], repeat=repeat, segment='loading')
                    close_active()
                    reset_sequence(core)
                    bridge.slots.clear()
                    bridge.last_sensor = None
                    with cond:
                        latest['f'] = None
                        if clips:
                            if clips.marks:
                                log('failures', kind='unfinished_clip_at_boundary', marks=clips.marks.copy())
                                clips.failed += len(clips.marks)
                            clips.frames = []
                            clips.marks = []
                    log(
                        'boundaries',
                        kind='reset',
                        models_retained=True,
                        history_len=len(core.history),
                        engine_state=core.engine.state,
                        planned_start=source_start,
                        source_duration=source_duration,
                        video=video,
                    )
                    cap = cv2.VideoCapture(str(Path(job['video_dir']) / video['file']))
                    if not cap.isOpened():
                        log('failures', kind='video_load_failure')
                        video_summaries.append({**context, 'load_failed': True})
                        offset += source_duration + tail + 1
                        continue
                    loaded = time.time()
                    log('boundaries', kind='loaded', late_s=max(0, loaded - source_start))
                    local_stop = threading.Event()
                    done = threading.Event()
                    capture_errors = []
                    info = {'decoded': 0, 'skipped_decode': 0, 'tail_frames': 0}
                    frame_meta = {}
                    counter = global_frame_seq
                    origin_mono = time.monotonic() + source_start - time.time()
                    origin_ns = round(origin_mono * 1e9)

                    def capture():
                        index = 0
                        last = None
                        try:
                            while not stop.is_set() and not local_stop.is_set():
                                elapsed = time.monotonic() - origin_mono
                                if elapsed >= source_duration:
                                    break
                                if elapsed < 0:
                                    local_stop.wait(min(0.02, -elapsed))
                                    continue
                                target = min(video['frames'] - 1, int(elapsed * video['fps']))
                                if index > target:
                                    local_stop.wait(min(0.01, max(0, (index / video['fps']) - elapsed)))
                                    continue
                                while index < target:
                                    if not cap.grab():
                                        break
                                    index += 1
                                    info['skipped_decode'] += 1
                                ok, image = cap.read()
                                if not ok:
                                    break
                                stamp = index / video['fps']
                                index += 1
                                info['decoded'] += 1
                                last = cv2.resize(image, (cfg['width'], cfg['height']))
                                counter[0] += 1
                                with cond:
                                    frame_meta[counter[0]] = {
                                        'segment': 'source',
                                        'frame_index': index - 1,
                                        'source_s': stamp,
                                    }
                                    latest['f'] = (counter[0], origin_ns + round(stamp * 1e9), last)
                                    cond.notify_all()
                            # Identical marked tail in both conditions; never counted as original video inference.
                            tail_index = 0
                            while last is not None and not stop.is_set() and not local_stop.is_set():
                                when = origin_mono + source_duration + tail_index * 0.1
                                if when >= origin_mono + source_duration + tail:
                                    break
                                if local_stop.wait(max(0, when - time.monotonic())):
                                    break
                                counter[0] += 1
                                info['tail_frames'] += 1
                                with cond:
                                    frame_meta[counter[0]] = {
                                        'segment': 'forced_tail',
                                        'frame_index': None,
                                        'source_s': source_duration + tail_index * 0.1,
                                    }
                                    latest['f'] = (
                                        counter[0],
                                        origin_ns + round((source_duration + tail_index * 0.1) * 1e9),
                                        last.copy(),
                                    )
                                    cond.notify_all()
                                tail_index += 1
                        except Exception as exc:
                            capture_errors.append(str(exc))
                        finally:
                            cap.release()
                            with cond:
                                done.set()
                                cond.notify_all()

                    producer = threading.Thread(target=capture, daemon=True)
                    producer.start()
                    seen = -1
                    source_slots = set()
                    source_opens = tail_opens = 0
                    while not stop.is_set():
                        with cond:
                            cond.wait_for(
                                lambda: done.is_set() or (latest['f'] is not None and latest['f'][0] != seen),
                                timeout=0.2,
                            )
                            if latest['f'] is None or latest['f'][0] == seen:
                                if done.is_set():
                                    break
                                continue
                            seen, sensor, image = latest['f']
                            fm = frame_meta.pop(seen)
                            for old in list(frame_meta):
                                if old < seen:
                                    frame_meta.pop(old, None)
                        context['segment'] = fm['segment']
                        if fm['segment'] == 'forced_tail':
                            log('excluded_frames', **fm, sensor_ns=sensor)
                            continue
                        tick = time.monotonic_ns()
                        r = core.process(image, sensor)
                        tock = time.monotonic_ns()
                        decided = time.time()
                        if r is None:
                            continue
                        if fm['segment'] == 'source':
                            source_slots.add(r['grid'])
                        bridge.step(sensor, decided, r['score'], r['eligible'], r['track_id'], r['events'])
                        for ev in r['events']:
                            if ev['kind'] == 'open':
                                if fm['segment'] == 'source':
                                    source_opens += 1
                                else:
                                    tail_opens += 1
                                with bridge.box.lock:
                                    active = bridge.box.db.execute(
                                        'SELECT event_uid FROM edge_active_events WHERE camera_id=?', (cam,)
                                    ).fetchone()
                                log(
                                    'decisions',
                                    kind='open',
                                    event_uid=active[0],
                                    decided_at=decided,
                                    sensor_ns=sensor,
                                    clock=bridge.worker.clock(),
                                    score=r['score'],
                                )
                                if clips and bridge.active:
                                    clips.open(bridge.active['onset'], sensor)
                            else:
                                log('decisions', kind='close', transition=ev, decided_at=decided, sensor_ns=sensor)
                        payload = runtime.edge_result(
                            r,
                            core,
                            params,
                            cam,
                            seen,
                            identity,
                            (cfg['width'] / 640, cfg['height'] / 360),
                            (tock - tick) / 1e6,
                            bridge,
                            ORDER,
                        )
                        try:
                            result_q.get_nowait()
                            result_q.task_done()
                            result_dropped += 1
                        except queue.Empty:
                            pass
                        result_q.put_nowait((payload, {**context}))
                        log(
                            'slots',
                            **fm,
                            grid=r['grid'],
                            processing_start_ns=tick,
                            processing_end_ns=tock,
                            processing_ms=(tock - tick) / 1e6,
                            decision_wall=decided,
                            sensor_ns=sensor,
                            score=r['score'],
                            valid=r['valid'],
                            eligible=r['eligible'],
                            state=r['state'],
                            people=r['people'],
                            history_slots=len(core.history),
                            history_valid_slots=sum(core.history_valid),
                            track_id=r['track_id'],
                            clock=bridge.worker.clock(),
                        )
                        check_status()
                    local_stop.set()
                    producer.join(timeout=3)
                    if producer.is_alive():
                        raise RuntimeError('video producer failed to stop')
                    expected = math.ceil(source_duration * 10 - 1e-9)
                    processed = len(source_slots)
                    summary = {
                        **context,
                        'source_duration_s': source_duration,
                        'expected_slots': expected,
                        'processed_slots': processed,
                        'missing_slots': max(0, expected - processed),
                        'source_opens': source_opens,
                        'tail_opens': tail_opens,
                        'missed_A043': int(video['action']) == 43 and source_opens == 0,
                        'truncated': source_duration < video['duration_s'],
                        'capture_errors': capture_errors,
                        **info,
                        'ended_at': time.time(),
                        'planned_end': end_at,
                    }
                    video_summaries.append(summary)
                    log('videos', **summary)
                    if phase == 'measure':
                        expected_total += expected
                        processed_total += processed
                    context['segment'] = 'boundary'
                    close_active()
                    offset += float(video['duration_s']) + 2.5 + 1.0
                    next_at = phase_start + min(offset, length)
                    while time.time() < next_at and not stop.is_set():
                        stop.wait(min(0.05, next_at - time.time()))
                repeat += 1
            # A phase may truncate a final source/tail. Complete evidence in a marked gap,
            # without feeding repeated frames back into inference or extending source exposure.
            if clips and clips.marks:
                context['segment'] = 'boundary_tail'
                deadline = time.monotonic() + 2.8
                while clips.marks and time.monotonic() < deadline and not stop.is_set():
                    with cond:
                        if latest['f'] is None:
                            break
                        _, sensor, image = latest['f']
                        global_frame_seq[0] += 1
                        latest['f'] = (global_frame_seq[0], sensor + 100_000_000, image)
                        cond.notify_all()
                    log('excluded_frames', segment='boundary_tail', sensor_ns=sensor + 100_000_000)
                    stop.wait(0.1)
                if clips.marks:
                    raise RuntimeError('phase boundary evidence did not finish')
            log('lifecycle', kind='phase_end', scheduled_at=phase_start + length)
        context.update(phase='drain', segment='drain')
        close_active()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not stop.is_set():
            if not (
                result_q.unfinished_tasks
                or bridge.box.status()['outbox_pending']
                or (clips and clips.uploads.unfinished_tasks)
            ):
                break
            stop.wait(0.1)
        status = bridge.box.status()
        log(
            'summary',
            expected_slots=expected_total,
            processed_slots=processed_total,
            missing_slots=expected_total - processed_total,
            result_send_failures=sender_errors,
            result_queue_replaced=result_dropped,
            clips_stored=clips.stored if clips else 0,
            clips_failed=clips.failed if clips else 0,
            clip_marks_pending=len(clips.marks) if clips else 0,
            **status,
            stopped=stop.is_set(),
        )
    finally:
        interrupted = stop.is_set()
        close_active()
        bridge.close(wait_s=5)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        log('lifecycle', kind='finished', interrupted=interrupted)
        for f in streams.values():
            f.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
