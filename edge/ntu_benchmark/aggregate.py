#!/usr/bin/env python3
"""Aggregate paired NTU replay trials without filling missing observations.

Fig 6: original-source measured slots, event-open cohort, stage-specific delays.
Table 2: same trial's fixed measured wall interval; tails/boundaries remain explicitly
part of sustained replay load, never part of original-video inference throughput.
"""

import argparse, collections, csv, hashlib, json, math, sqlite3, statistics, sys, time, os
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
DEPS = HERE.parents[1] / 'artifacts/fig6_ntu_benchmark/analysis_deps'
sys.path.insert(0, str(DEPS))
os.environ.setdefault("MPLCONFIGDIR", "/tmp/fig6-matplotlib-cache")
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from zipfile import ZipFile, ZIP_DEFLATED


def rows(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            continue
    return result


def percentiles(v):
    v = [float(x) for x in v if x is not None and math.isfinite(x)]
    return {
        'n': len(v),
        'p50': float(np.percentile(v, 50)) if v else None,
        'p95': float(np.percentile(v, 95)) if v else None,
        'min': min(v) if v else None,
        'max': max(v) if v else None,
    }


def scalar(v, default='NA'):
    return f'{v:.2f}' if isinstance(v, (int, float)) else default


def writecsv(path, data):
    if not data:
        return
    keys = list(dict.fromkeys(k for r in data for k in r))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in data:
            w.writerow(
                {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in r.items()}
            )


def kind(path):
    for prefix, label in [
        ('/api/edge_result', 'result'),
        ('/api/edge_event/context', 'context'),
        ('/api/edge_event', 'event'),
        ('/api/edge_clip', 'clip'),
        ('/api/device_health', 'health'),
        ('/api/device_commands', 'command'),
        ('/push_frame', 'video'),
    ]:
        if path.startswith(prefix):
            return label
    return None


def collect(root, include_pilot=False):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.conninfo import make_conninfo

    dbinfo = json.loads((root / 'database.json').read_text())
    with psycopg.connect(
        make_conninfo(dbinfo['conninfo'], options='-c search_path=' + dbinfo['schema']), row_factory=dict_row
    ) as db:
        events = {r['event_uid']: dict(r) for r in db.execute('SELECT * FROM events')}
        alerts = {r['id']: dict(r) for r in db.execute('SELECT * FROM alerts')}
    alert_by_event = {r['event_id']: r for r in alerts.values()}
    first_browser = {}
    browser = rows(root / 'browser.jsonl')
    for r in browser:
        if r.get('kind') != 'browser_receive':
            continue
        data = r.get('data') or {}
        aa = data.get('alerts', []) if r['event_type'] == 'snapshot' else [data.get('alert')]
        for alert in aa:
            if alert and alert.get('id') is not None:
                first_browser[alert['id']] = min(
                    first_browser.get(alert['id'], float('inf')), r['browser_wall_ms'] / 1000
                )
    requests = rows(root / 'server_requests.jsonl')
    ready = {r['event_id']: r for r in rows(root / 'clip_ready.jsonl') if r.get('event_id') is not None}
    received = {}
    for r in requests:
        if r['path'] == '/api/edge_event' and r.get('kind') == 'open' and r.get('status') == 200:
            uid = r.get('event_uid')
            if uid not in received or r['body_received_at'] < received[uid]['body_received_at']:
                received[uid] = r
    trials = []
    slot_data = {}
    delays = []
    video_data = []
    ack_data = []
    raw_resources = []
    all_http = []
    proofs = []
    folders = sorted(root.glob('main_*')) + ([root / 'pilot_clips_on'] if include_pilot else [])
    for folder in folders:
        if not (folder / 'schedule.json').exists():
            continue
        sched = json.loads((folder / 'schedule.json').read_text())
        start, end = sched['measure_start'], sched['measure_end']
        for device, cam in [('pi5', '1'), ('pi4', '2')]:
            local = folder / device
            if not (local / 'slots.jsonl').exists():
                continue
            slots = [
                r
                for r in rows(local / 'slots.jsonl')
                if r.get('phase') == 'measure' and r.get('segment') == 'source' and start <= r['decision_wall'] < end
            ]
            videos = [r for r in rows(local / 'videos.jsonl') if r.get('phase') == 'measure']
            video_data.extend(videos)
            raw_resources.extend(rows(local / 'resources.jsonl'))
            resources = [r for r in rows(local / 'resources.jsonl') if start <= r['wall'] < end and not r.get('error')]
            summary = rows(local / 'summary.jsonl')
            summary = summary[-1] if summary else {}
            complete = bool(summary) and not summary.get('stopped')
            lifecycle = rows(local / 'lifecycle.jsonl')
            last_observed = max([r['wall'] for r in lifecycle + rows(local / 'resources.jsonl')] + [start])
            actual_measure_s = end - start if complete else max(0, min(end, last_observed) - start)
            source_seconds = sum(r['source_duration_s'] for r in videos)
            expected = sum(r['expected_slots'] for r in videos)
            selected = [
                r
                for r in requests
                if start <= (r.get('body_received_at') or r['entered_at']) < end
                and (str(r.get('camera_id') or r.get('cam_id') or '') == cam)
                and kind(r['path'])
            ]
            http = collections.defaultdict(lambda: {'bytes': 0, 'requests': 0, 'failed_responses': 0})
            for r in selected:
                k = kind(r['path'])
                http[k]['bytes'] += r['http_bytes']
                http[k]['requests'] += 1
                http[k]['failed_responses'] += int((r.get('status') or 0) >= 400)
                all_http.append({'trial': folder.name, 'device': device, 'clips': sched['clips'], **r})
            network_seconds = resources[-1]['wall'] - resources[0]['wall'] if len(resources) > 1 else None
            tx = (resources[-1]['eth0_tx_bytes'] - resources[0]['eth0_tx_bytes']) if len(resources) > 1 else None
            cpu = [r['cpu_pct_device'] for r in resources if r.get('cpu_pct_device') is not None]
            mem = [r['rss_bytes'] / 1024**2 for r in resources]
            temp = [r['temperature_c'] for r in resources]
            decisions = [
                r
                for r in rows(local / 'decisions.jsonl')
                if r['kind'] == 'open'
                and r['phase'] == 'measure'
                and r['segment'] == 'source'
                and start <= r['decided_at'] < end
            ]
            item = {
                'trial': folder.name,
                'device': device,
                'clips': sched['clips'],
                'replicate': sched['replicate'],
                'run_id': slots[0]['run_id'] if slots else None,
                'scheduled_measure_s': end - start,
                'actual_measure_s': actual_measure_s,
                'original_source_s': source_seconds,
                'processing_samples': len(slots),
                'expected_slots': expected,
                'missing_slots': max(0, expected - len(slots)),
                'achieved_source_hz': len(slots) / source_seconds if source_seconds else None,
                'video_presentations': len(videos),
                'unique_videos': len({r['video_id'] for r in videos}),
                'capture_errors': sum(bool(r.get('capture_errors') or r.get('load_failed')) for r in videos),
                'missed_A043_presentations': sum(r.get('missed_A043', False) for r in videos),
                'source_events': len(decisions),
                'http_bytes': sum(x['bytes'] for x in http.values()),
                'http_kbps': sum(x['bytes'] for x in http.values()) * 8 / (end - start) / 1000,
                'http_by_kind': dict(http),
                'http_failed_responses': sum(x['failed_responses'] for x in http.values()),
                'eth0_tx_bytes': tx,
                'eth0_observed_s': network_seconds,
                'eth0_tx_kbps': tx * 8 / network_seconds / 1000
                if network_seconds and tx is not None and tx >= 0
                else None,
                'cpu_pct_device_mean': statistics.mean(cpu) if cpu else None,
                'rss_MiB_p50': percentiles(mem)['p50'],
                'rss_MiB_p95': percentiles(mem)['p95'],
                'rss_MiB_max': max(mem) if mem else None,
                'temperature_c_p50': percentiles(temp)['p50'],
                'temperature_c_max': max(temp) if temp else None,
                'resource_samples': len(resources),
                'throttle_current_nonzero_samples': sum(
                    bool((r.get('throttle') or {}).get('raw', 0) & 0xF)
                    for r in resources
                    if (r.get('throttle') or {}).get('raw') is not None
                ),
                'result_send_failures_all_trial': summary.get('result_send_failures'),
                'clips_failed_all_trial': summary.get('clips_failed'),
                'outbox_pending_end': summary.get('outbox_pending'),
                'outbox_blocked_end': summary.get('outbox_blocked'),
                'completed': bool(summary) and not summary.get('stopped'),
            }
            item.update({'processing_ms_' + k: v for k, v in percentiles([r['processing_ms'] for r in slots]).items()})
            for decision in decisions:
                uid = decision['event_uid']
                event = events.get(uid)
                rx = received.get(uid)
                alert = alert_by_event.get(event['id']) if event else None
                browser_at = first_browser.get(alert['id']) if alert else None
                clock = decision.get('clock') or {}
                rtt = clock.get('rtt_s')
                offset = clock.get('offset_s')
                one = (
                    (rx['body_received_at'] - decision['decided_at'] - offset) * 1000
                    if rx and rx.get('body_received_at') is not None and offset is not None
                    else None
                )
                valid = one is not None and one >= 0 and rtt is not None and 0 <= rtt <= 0.1
                server_browser = (
                    (browser_at - rx['body_received_at']) * 1000
                    if rx and browser_at is not None and rx.get('body_received_at') is not None
                    else None
                )
                clip = ready.get(event['id']) if event else None
                delays.append(
                    {
                        k: decision[k]
                        for k in (
                            'experiment_id',
                            'run_id',
                            'device',
                            'clips',
                            'trial',
                            'replicate',
                            'video_id',
                            'repeat',
                        )
                    }
                    | {
                        'event_uid': uid,
                        'event_id': event['id'] if event else None,
                        'decided_at': decision['decided_at'],
                        'server_body_received_at': rx.get('body_received_at') if rx else None,
                        'server_db_received_at': event['received_at'] if event else None,
                        'browser_received_at': browser_at,
                        'server_received': event is not None,
                        'browser_received': browser_at is not None,
                        'browser_receipt_confirmed': bool(alert and alert['first_delivered_at'] is not None),
                        'late_arrival': bool(event and event['late_arrival']),
                        'device_server_ms_raw': one,
                        'device_server_clock_valid': valid,
                        'clock_offset_s': offset,
                        'clock_rtt_s': rtt,
                        'clock_half_rtt_bound_ms': rtt * 500 if rtt is not None else None,
                        'server_browser_ms': server_browser,
                        'server_receipt_confirmation_ms': (alert['first_delivered_at'] - rx['body_received_at']) * 1000
                        if alert and alert['first_delivered_at'] and rx
                        else None,
                        'clip_ready': clip is not None,
                        'clip_ready_after_decision_s': clip['observed_at'] - decision['decided_at'] - offset
                        if clip and offset is not None
                        else None,
                    }
                )
            cohort = [d for d in delays if d['trial'] == folder.name and d['device'] == device]
            item.update(
                server_events=sum(d['server_received'] for d in cohort),
                browser_events=sum(d['browser_received'] for d in cohort),
                browser_receipt_confirmed=sum(d['browser_receipt_confirmed'] for d in cohort),
                late_events=sum(d['late_arrival'] for d in cohort),
                saved_clips_for_source_events=sum(d['clip_ready'] for d in cohort),
            )
            if (local / 'outbox.sqlite3').exists():
                ed = sqlite3.connect('file:' + str(local / 'outbox.sqlite3') + '?mode=ro', uri=True)
                ed.row_factory = sqlite3.Row
                for r in ed.execute(
                    'SELECT event_uid,message_seq,kind,created_at,acked_at,attempts,blocked_at,last_error FROM outbox WHERE created_at>=? AND created_at<?',
                    (start, end),
                ):
                    ack_data.append(
                        dict(r)
                        | {
                            'trial': folder.name,
                            'device': device,
                            'clips': sched['clips'],
                            'created_to_ack_ms': (r['acked_at'] - r['created_at']) * 1000 if r['acked_at'] else None,
                        }
                    )
            if (local / 'service_after.json').exists():
                proofs.append(
                    json.loads((local / 'service_after.json').read_text()) | {'trial': folder.name, 'device': device}
                )
            trials.append(item)
            slot_data[(folder.name, device)] = slots
    conditions = []
    for device, clips in [('pi5', False), ('pi5', True), ('pi4', False), ('pi4', True)]:
        tt = [r for r in trials if r['device'] == device and r['clips'] == clips]
        ss = [s for r in tt for s in slot_data[(r['trial'], device)]]
        dd = [r for r in delays if r['device'] == device and r['clips'] == clips]
        cpu = [r['cpu_pct_device_mean'] for r in tt if r['cpu_pct_device_mean'] is not None]
        http = [r['http_kbps'] for r in tt]
        eth = [r['eth0_tx_kbps'] for r in tt if r['eth0_tx_kbps'] is not None]
        rss_p50 = [r['rss_MiB_p50'] for r in tt if r['rss_MiB_p50'] is not None]
        rss_p95 = [r['rss_MiB_p95'] for r in tt if r['rss_MiB_p95'] is not None]
        rss_max = [r['rss_MiB_max'] for r in tt if r['rss_MiB_max'] is not None]
        temp_p50 = [r['temperature_c_p50'] for r in tt if r['temperature_c_p50'] is not None]
        temp_max = [r['temperature_c_max'] for r in tt if r['temperature_c_max'] is not None]
        conditions.append(
            {
                'device': device,
                'clips': clips,
                'trials': len(tt),
                'complete_trials': sum(r['completed'] for r in tt),
                'measure_s': sum(r['actual_measure_s'] for r in tt),
                'planned_measure_s': sum(r['scheduled_measure_s'] for r in tt),
                'original_source_s': sum(r['original_source_s'] for r in tt),
                'processing': percentiles([r['processing_ms'] for r in ss]),
                'achieved_source_hz': len(ss) / sum(r['original_source_s'] for r in tt)
                if tt and sum(r['original_source_s'] for r in tt) > 0
                else None,
                'expected_slots': sum(r['expected_slots'] for r in tt),
                'missing_slots': sum(r['missing_slots'] for r in tt),
                'video_presentations': sum(r['video_presentations'] for r in tt),
                'unique_videos': len({s['video_id'] for s in ss}),
                'unique_capture_groups': len({s['video_id'][:4] + s['video_id'][8:] for s in ss}),
                'source_events': len(dd),
                'server_events': sum(d['server_received'] for d in dd),
                'browser_events': sum(d['browser_received'] for d in dd),
                'late_events': sum(d['late_arrival'] for d in dd),
                'clock_excluded': sum(not d['device_server_clock_valid'] for d in dd),
                'device_server': percentiles([d['device_server_ms_raw'] for d in dd if d['device_server_clock_valid']]),
                'server_browser': percentiles(
                    [
                        d['server_browser_ms']
                        for d in dd
                        if d['server_browser_ms'] is not None and d['server_browser_ms'] >= 0
                    ]
                ),
                'clock_half_rtt_bound_ms': percentiles([d['clock_half_rtt_bound_ms'] for d in dd]),
                'missed_A043_presentations': sum(r['missed_A043_presentations'] for r in tt),
                'cpu_pct_device_mean': statistics.mean(cpu) if cpu else None,
                'cpu_pct_device_SD_trials': statistics.stdev(cpu) if len(cpu) > 1 else None,
                'rss_MiB_p50_mean': statistics.mean(rss_p50) if rss_p50 else None,
                'rss_MiB_p95_mean': statistics.mean(rss_p95) if rss_p95 else None,
                'rss_MiB_max': max(rss_max) if rss_max else None,
                'temperature_c_p50_mean': statistics.mean(temp_p50) if temp_p50 else None,
                'temperature_c_max': max(temp_max) if temp_max else None,
                'resource_samples': sum(r['resource_samples'] for r in tt),
                'throttle_current_nonzero_samples': sum(r['throttle_current_nonzero_samples'] for r in tt),
                'http_kbps_mean': statistics.mean(http) if http else None,
                'http_kbps_SD_trials': statistics.stdev(http) if len(http) > 1 else None,
                'eth0_kbps_mean': statistics.mean(eth) if eth else None,
                'eth0_kbps_SD_trials': statistics.stdev(eth) if len(eth) > 1 else None,
                'saved_clips': sum(d['clip_ready'] for d in dd),
            }
        )
    return (
        {
            'root': str(root),
            'trials': trials,
            'conditions': conditions,
            'browser_page_errors': [r for r in browser if r['kind'] == 'pageerror'],
            'service_restoration': proofs,
        },
        slot_data,
        delays,
        video_data,
        ack_data,
        raw_resources,
        all_http,
    )


def plot(out, report, slot_data, partial):
    plt.rcParams.update(
        {
            'font.family': 'DejaVu Sans',
            'font.size': 9,
            'svg.fonttype': 'none',
            'axes.spines.top': False,
            'axes.spines.right': False,
        }
    )
    colors = ['#28639e', '#63a3cf', '#25765f', '#78b899']
    labels = ['Pi 5 · clip off', 'Pi 5 · clip on', 'Pi 4 · clip off', 'Pi 4 · clip on']
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 4.7), gridspec_kw={'width_ratios': [1, 1]})
    ax = axes[0]
    for cond, label, color in zip(report['conditions'], labels, colors):
        vals = sorted(
            s['processing_ms']
            for (trial, device), ss in slot_data.items()
            for s in ss
            if device == cond['device'] and s['clips'] == cond['clips']
        )
        if vals:
            ax.plot(
                vals,
                np.arange(1, len(vals) + 1) / len(vals),
                color=color,
                label=f"{label}  {scalar(cond['achieved_source_hz'])} Hz; miss {cond['missing_slots']:,}  n={len(vals):,}",
                linewidth=1.8,
            )
    ax.axhline(0.5, color='#929da8', linewidth=0.7, linestyle=':')
    ax.axhline(0.95, color='#929da8', linewidth=0.7, linestyle=':')
    ax.set_xscale('log')
    ax.set_ylim(0, 1.02)
    ax.set_xlabel('Pose → LPN → TCN → decision processing (ms; log scale)')
    ax.set_ylabel('Empirical cumulative proportion')
    ax.set_title('(a) Original-video decision slots', loc='left', fontweight='bold')
    ax.legend(fontsize=7.4, loc='lower right')
    ax = axes[1]
    stagecolors = ['#28639e', '#b17b19']
    limits = []
    for index, cond in enumerate(report['conditions']):
        for stage, key in enumerate(('device_server', 'server_browser')):
            stats = cond[key]
            y = index + (stage - 0.5) * 0.22
            if stats['n']:
                limits.append(stats['p95'])
                ax.plot([stats['p50'], stats['p95']], [y, y], color=stagecolors[stage], linewidth=2)
                ax.plot(stats['p50'], y, 'o', color=stagecolors[stage], markersize=5)
                ax.plot(stats['p95'], y, '|', color=stagecolors[stage], markersize=10)
                ax.annotate(
                    f"n={stats['n']}",
                    (stats['p95'], y),
                    xytext=(5, 0),
                    textcoords='offset points',
                    va='center',
                    fontsize=7,
                )
    ax.set_yticks(range(4), labels)
    ax.invert_yaxis()
    ax.set_xlim(left=0, right=(max(limits) * 1.35 if limits else 1))
    ax.set_xlabel('Stage delay (ms): dot p50, bar end p95')
    ax.set_title('(b) Actual event delivery stages', loc='left', fontweight='bold')
    from matplotlib.lines import Line2D

    ax.legend(
        handles=[
            Line2D([0], [0], color=stagecolors[0], label='Decision → server (clock aligned)'),
            Line2D([0], [0], color=stagecolors[1], label='Server → browser JS receipt'),
        ],
        loc='lower right',
        fontsize=7,
    )
    status = (
        'PARTIAL — completed observations only'
        if partial
        else '2 × 20 min per condition; 1 min warm-up before each paired trial'
    )
    fig.suptitle('Fig 6. NTU RGB replay on Raspberry Pi devices', fontsize=13, fontweight='bold', y=0.99)
    fig.text(
        0.03,
        0.012,
        status
        + '\nOriginal test split reused for system performance; repeated videos are not independent accuracy cases. Browser receipt is not user acknowledgement.\nHz = processed slots / source exposure; each video restarts the 10 Hz grid, so per-video slot rounding can put this ratio slightly above 10. Misses use the fixed expected-slot count.',
        fontsize=6.6,
        color='#4c6074',
    )
    fig.tight_layout(rect=[0, 0.11, 1, 0.95])
    for ext in ('svg', 'png', 'jpg'):
        fig.savefig(out / ('Fig6_NTU_replay.' + ext), dpi=300, facecolor='white')
    plt.close(fig)


def workbook(out, report, delays, videos, acks, resources, http):
    wb = Workbook()
    wb.remove(wb.active)

    def sheet(name, data):
        ws = wb.create_sheet(name)
        if not data:
            ws.append(['No observations'])
            return
        keys = list(dict.fromkeys(k for r in data for k in r))
        ws.append(keys)
        for r in data:
            ws.append(
                [
                    json.dumps(r.get(k), ensure_ascii=False) if isinstance(r.get(k), (dict, list)) else r.get(k)
                    for k in keys
                ]
            )
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor='28639E')
            c.alignment = Alignment(wrap_text=True)
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(48, max(16, len(str(col[0].value)) + 2))

    flat = []
    for c in report['conditions']:
        flat.append(
            {k: v for k, v in c.items() if not isinstance(v, dict)}
            | {f'{k}_{kk}': vv for k, v in c.items() if isinstance(v, dict) for kk, vv in v.items()}
        )
    sheet('Table2_conditions', flat)
    sheet('Per_trial', report['trials'])
    sheet('Event_stages', delays)
    sheet('Video_presentations', videos)
    sheet('Outbox_creation_to_ACK', acks)
    sheet('Resource_raw_1s', resources)
    sheet('HTTP_requests', http)
    sheet('Service_restoration', report['service_restoration'])
    sheet(
        'Definitions',
        [
            {
                'metric': 'Achieved decision Hz',
                'definition': 'Processed source decision slots / original source exposure seconds. The 10 Hz grid restarts for each video and expected slots are rounded per video; short-sequence rounding can put this ratio slightly above 10 Hz. Missed slots = fixed expected slots - processed slots.',
            },
            {
                'metric': 'CPU',
                'definition': 'Process CPU / logical cores: 100% means all device cores; one-core scale is also in raw logs. CPU includes benchmark instrumentation.',
            },
            {'metric': 'Memory', 'definition': 'Process RSS, MiB = bytes / 1048576; not whole-device RAM.'},
            {
                'metric': 'Processing time',
                'definition': 'monotonic_ns immediately around core.process; pose+tracking+features+LPN+TCN+decision; excludes decode/resize, event serialization, outbox and network.',
            },
            {
                'metric': 'Latency (decision to server)',
                'definition': 'ASGI full request-body receipt - device decision wall time - outbox midpoint clock offset. Valid if nonnegative and calibration RTT <=100 ms; raw negatives and exclusions retained. Residual bound RTT/2 recorded.',
            },
            {
                'metric': 'Latency (server to browser)',
                'definition': 'Actual Chromium EventSource callback entry Date.now - server full-body receive time; same WSL host clock. Browser receipt confirmation POST is separately recorded. Not render completion or human response.',
            },
            {
                'metric': 'Outbox ACK',
                'definition': 'acked_at-created_at, separate round-trip/queue metric, never added to one-way stages.',
            },
            {
                'metric': 'Time window',
                'definition': 'Table 2 fixed 2400 s interval per trial. Includes marked replay tails and boundaries in workload; source inference distribution and achieved Hz exclude them and warmup. eth0 uses actual first-last resource sample span.',
            },
            {
                'metric': 'HTTP vs eth0',
                'definition': 'HTTP bytes request line+headers+body, by message kind. eth0 entire device including TCP/IP, SSH, existing device agent and other projects. NTU-enriched event frequency is not field bandwidth.',
            },
            {
                'metric': 'Units',
                'definition': 'kbit/s decimal (1000 bit/s), memory MiB binary. Three trial summaries are repeated measurements, not independent capture-group samples.',
            },
        ],
    )
    wb.save(out / 'Table2_NTU_replay.xlsx')


def editable_ppt(out, report, slot_data, source):
    prs = Presentation(source)
    index = None
    for i, s in enumerate(prs.slides):
        text = ' '.join(sh.text for sh in s.shapes if sh.has_text_frame)
        if any(t in text for t in ('Fig 6', 'Fig. 6', 'Figure 6')):
            index = i
    if index is None:
        raise ValueError('Fig 6 slide not found')
    slide = prs.slides[index]
    for sh in list(slide.shapes):
        slide.shapes._spTree.remove(sh._element)
    W, H = prs.slide_width / 914400, prs.slide_height / 914400

    def text(t, x, y, w, h, size=12, bold=False, color='182D43'):
        b = slide.shapes.add_textbox(Inches(x * W), Inches(y * H), Inches(w * W), Inches(h * H))
        tf = b.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = t
        p.font.name = 'Arial'
        p.font.size = Pt(size)
        p.font.bold = bold
        p.font.color.rgb = RGBColor.from_string(color)
        return b

    def line(x1, y1, x2, y2, color='A9BDCF', width=1):
        sh = slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT, Inches(x1 * W), Inches(y1 * H), Inches(x2 * W), Inches(y2 * H)
        )
        sh.line.color.rgb = RGBColor.from_string(color)
        sh.line.width = Pt(width)

    def dot(x, y, color):
        sh = slide.shapes.add_shape(
            MSO_SHAPE.OVAL, Inches(x * W - 0.025), Inches(y * H - 0.025), Inches(0.05), Inches(0.05)
        )
        sh.fill.solid()
        sh.fill.fore_color.rgb = RGBColor.from_string(color)
        sh.line.fill.background()

    colors = ['28639E', '63A3CF', '25765F', '78B899']
    labels = ['Pi 5 / off', 'Pi 5 / on', 'Pi 4 / off', 'Pi 4 / on']
    text('Fig 6. NTU RGB replay: device processing and actual alert delivery', 0.025, 0.015, 0.95, 0.07, 20, True)
    text('(a) Original-video processing time', 0.035, 0.105, 0.44, 0.055, 16, True)
    text('(b) Delivery stages: p50 to p95', 0.535, 0.105, 0.43, 0.055, 16, True)
    allvals = [s['processing_ms'] for ss in slot_data.values() for s in ss]
    lo = math.floor(math.log10(min(allvals))) if allvals else 0
    hi = math.ceil(math.log10(max(allvals))) if allvals else 3
    for prob in (0, 0.5, 0.95, 1):
        y = 0.69 - prob * 0.45
        line(0.075, y, 0.47, y)
        text(str(prob), 0.025, y - 0.012, 0.045, 0.03, 9)
    for power in range(lo, hi + 1):
        x = 0.075 + (power - lo) / max(1, hi - lo) * 0.395
        line(x, 0.69, x, 0.70)
        text(str(10**power), x - 0.02, 0.71, 0.06, 0.03, 9)
    for c, label, color in zip(report['conditions'], labels, colors):
        vals = sorted(
            s['processing_ms']
            for (trial, device), ss in slot_data.items()
            for s in ss
            if device == c['device'] and s['clips'] == c['clips']
        )
        if not vals:
            continue
        pts = [(float(np.percentile(vals, q)), q / 100) for q in np.linspace(0, 100, 101)]
        for (a, p), (b, q) in zip(pts, pts[1:]):
            line(
                0.075 + (math.log10(a) - lo) / max(1, hi - lo) * 0.395,
                0.69 - p * 0.45,
                0.075 + (math.log10(b) - lo) / max(1, hi - lo) * 0.395,
                0.69 - q * 0.45,
                color,
                1.5,
            )
    for i, (c, label, color) in enumerate(zip(report['conditions'], labels, colors)):
        text(
            f"{label}: p50 {scalar(c['processing']['p50'])}, p95 {scalar(c['processing']['p95'])} ms; {scalar(c['achieved_source_hz'])} Hz, miss {c['missing_slots']}",
            0.055,
            0.79 + i * 0.036,
            0.46,
            0.04,
            9,
            False,
            color,
        )
    max_delay = (
        max([c[k]['p95'] or 0 for c in report['conditions'] for k in ('device_server', 'server_browser')] + [1]) * 1.25
    )
    for i, (c, label) in enumerate(zip(report['conditions'], labels)):
        y = 0.27 + i * 0.13
        text(label, 0.52, y - 0.025, 0.12, 0.045, 11, True)
        for k, offset, color in [('device_server', -0.005, '28639E'), ('server_browser', 0.035, 'B17B19')]:
            s = c[k]
            if s['n']:
                x1 = 0.65 + s['p50'] / max_delay * 0.28
                x2 = 0.65 + s['p95'] / max_delay * 0.28
                line(x1, y + offset, x2, y + offset, color, 2)
                dot(x1, y + offset, color)
                line(x2, y + offset - 0.01, x2, y + offset + 0.01, color, 1.5)
                text(f"n={s['n']}", 0.94, y + offset - 0.015, 0.05, 0.035, 8)
    line(0.65, 0.76, 0.94, 0.76)
    for v in np.linspace(0, max_delay, 5):
        x = 0.65 + v / max_delay * 0.28
        line(x, 0.76, x, 0.77)
        text(f'{v:.0f}', x - 0.02, 0.775, 0.06, 0.04, 9)
    text('Decision → server (clock aligned)', 0.57, 0.84, 0.4, 0.04, 10, False, '28639E')
    text('Server → actual browser JS receipt', 0.57, 0.89, 0.4, 0.04, 10, False, 'B17B19')
    text('Processing: ms, log scale; empirical cumulative distribution', 0.05, 0.745, 0.45, 0.04, 9)
    text(
        '2 × 20 min per condition; 1 min warm-up per trial. NTU test split reused for system performance. Browser receipt is not user acknowledgement.',
        0.025,
        0.955,
        0.95,
        0.035,
        9,
    )
    slide.notes_slide.notes_text_frame.text = (
        'Source experiment '
        + report['root']
        + '. Full measurement definitions and valid sample counts are in summary.json, event_stages.csv and Table2_NTU_replay.xlsx. Achieved Hz is processed slots divided by original source exposure; each video restarts the 10 Hz grid, so per-video slot rounding can put this ratio slightly above 10. Missed slots are counted against the fixed expected-slot total. Native editable lines, percentile markers and text. Models and operating point fixed. Only this figure slide and its notes are replaced.'
    )
    candidate = out / '_editable_candidate.pptx'
    prs.save(candidate)
    dest = out / 'Fall-Relay_Figures_Fig6_NTU_Editable.pptx'
    allowed = {f'ppt/slides/slide{index + 1}.xml', f'ppt/notesSlides/notesSlide{index + 1}.xml'}
    with ZipFile(source) as original, ZipFile(candidate) as updated, ZipFile(dest, 'w', ZIP_DEFLATED) as output:
        for info in original.infolist():
            output.writestr(
                info,
                updated.read(info.filename)
                if info.filename in allowed and info.filename in updated.namelist()
                else original.read(info.filename),
            )
    with ZipFile(source) as original, ZipFile(dest) as changed:
        diffs = [name for name in original.namelist() if original.read(name) != changed.read(name)]
    assert set(diffs) <= allowed and f'ppt/slides/slide{index + 1}.xml' in diffs, diffs
    candidate.unlink()
    (out / 'ppt_verification.json').write_text(
        json.dumps(
            {
                'source': str(source),
                'source_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                'changed_parts': diffs,
                'other_parts_byte_identical': True,
                'native_editable_shapes': len(slide.shapes),
            },
            indent=2,
        )
        + '\n'
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--out', type=Path)
    p.add_argument('--include-pilot', action='store_true')
    p.add_argument('--ppt', type=Path)
    a = p.parse_args()
    out = a.out or a.root / 'results'
    out.mkdir(parents=True, exist_ok=True)
    report, slot_data, delays, videos, acks, resources, http = collect(a.root, a.include_pilot)
    partial = (
        not (a.root / 'completed.json').exists()
        or not json.loads((a.root / 'completed.json').read_text()).get('main_completed')
        or any(c['complete_trials'] != 2 for c in report['conditions'])
    )
    report.update(
        generated_at=time.time(),
        partial=partial,
        measurement_definitions={
            'processing': 'core.process monotonic duration; decoder/resize/outbox/network excluded',
            'browser': 'actual Chromium EventSource callback; same-host server clock; confirmed by production /received path',
            'source': 'warm-up, forced tail, loading and boundary excluded from inference timing and achieved Hz',
            'achieved_rate': 'processed source decision slots / original source exposure seconds; grid restarts per video and expected slots round per video, so short-sequence rounding can put the ratio slightly above the nominal 10Hz',
            'missing_slots': 'fixed expected slot count minus recorded processed source slots; expected slots are rounded up per video',
            'Table2': 'fixed measured wall interval, source/tail/boundary workload identified separately',
            'clock': 'outbox midpoint offset; half-RTT residual bound, valid RTT <=100ms, nonnegative observed delay; excluded raw values retained',
        },
    )
    (out / 'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    writecsv(out / 'trials.csv', report['trials'])
    writecsv(out / 'event_stages.csv', delays)
    writecsv(out / 'video_presentations.csv', videos)
    writecsv(out / 'outbox_creation_to_ack.csv', acks)
    plot(out, report, slot_data, partial)
    workbook(out, report, delays, videos, acks, resources, http)
    if a.ppt and not partial:
        editable_ppt(out, report, slot_data, a.ppt)
    lines = [
        '# NTU RGB+D replay on the edge devices',
        '',
        f"Experiment: `{a.root.name}`",
        '',
        'Status: ' + ('partial result, measurement not complete' if partial else 'measurement complete'),
        '',
        '| Device / clips | Complete trials | Measured min | Source shown s | Processed / missed slots | Achieved Hz | Events / browser | Processing p50 / p95 ms |',
        '|---|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for c in report['conditions']:
        lines.append(
            f"| {c['device']} / {'on' if c['clips'] else 'off'} | {c['complete_trials']} | {c['measure_s'] / 60:.1f} | {c['original_source_s']:.1f} | {c['processing']['n']} / {c['missing_slots']} | {scalar(c['achieved_source_hz'])} | {c['source_events']} / {c['browser_events']} | {scalar(c['processing']['p50'])} / {scalar(c['processing']['p95'])} |"
        )
    lines += [
        '',
        '| Device / clips | Process CPU % | RSS p95 MiB | Peak temp. °C | Throttled samples | HTTP upload kbit/s | eth0 upload kbit/s | Stored clips |',
        '|---|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for c in report['conditions']:
        lines.append(
            f"| {c['device']} / {'on' if c['clips'] else 'off'} | {scalar(c['cpu_pct_device_mean'])} | {scalar(c['rss_MiB_p95_mean'])} | {scalar(c['temperature_c_max'])} | {c['throttle_current_nonzero_samples']} / {c['resource_samples']} | {scalar(c['http_kbps_mean'])} | {scalar(c['eth0_kbps_mean'])} | {c['saved_clips']} |"
        )
    lines += [
        '',
        'A fixed list of 48 unique videos from 48 capture groups was repeated; repetitions are not independent cases. The existing NTU test split is reused to measure system performance; this is not a new, independent accuracy evaluation.',
        '',
        'Processing time runs from just before core.process to just after it returns: pose, tracking, features, LPN, TCN and decision, excluding decoding, resizing, outbox and network. Frames held after a video ends are not inferred and are excluded from the source rate. Hz is processed slots per second of source shown; the 10 Hz grid restarts with every video and the expected slots per video are rounded up, so the rate can be slightly above 10 Hz. Missed slots are the fixed expected slots minus the processed slots.',
        '',
        'The two delivery stages and outbox creation to acknowledgement are separate raw data. One-way latencies must be read together with the recorded midpoint clock alignment and its RTT/2 residual. Browser receipt is the callback of a real Chromium EventSource, not rendering, acknowledgement or response time.',
        '',
        'The resource table covers the same fixed measurement windows. CPU is the process CPU divided by all logical cores (100% = every core of the device); RSS is in MiB. HTTP and eth0 upload are separate: eth0 includes SSH/TCP/IP, the device agent and, on the Pi 4, unrelated software. Upload measured at the artificial event rate of NTU replay does not generalise to average field bandwidth.',
        '',
        'Missed slots, undetected A043 videos, late deliveries, values excluded by clock validity, failed responses and outbox states are kept in the XLSX raw data. Missing values are not imputed. Warm-up and failed preliminary runs are not merged with the measurement.',
        '',
        f"Browser page errors: {len(report['browser_page_errors'])}. Service restorations: {len(report['service_restoration'])}, failed {sum(not r['restored'] for r in report['service_restoration'])}.",
    ]
    (out / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'output': str(out), 'partial': partial, 'conditions': report['conditions']}, indent=2))


if __name__ == '__main__':
    main()
