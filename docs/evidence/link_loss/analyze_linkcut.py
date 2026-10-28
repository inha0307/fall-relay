#!/usr/bin/env python3
"""Analyse a link-loss trial: what the gated device's outbox did while its server link was dead.

Reads the controller's cuts.jsonl, the device gate log, the device records (decisions, transport attempts, the
1-s resource log with the outbox status, the outbox database itself) and the server side (request log, events,
alerts, receipts, browser receipts). Writes aggregates only (JSON, CSV, a figure); no clip or image is touched.
"""

import argparse, collections, csv, json, math, sqlite3, sys, os
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'analysis_deps'))
os.environ.setdefault('MPLCONFIGDIR', '/tmp/fig6-matplotlib-cache')
import numpy as np
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt


def rows(path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def pct(v):
    v = [float(x) for x in v if x is not None and math.isfinite(x)]
    if not v:
        return {'n': 0}
    return {
        'n': len(v),
        'p50': float(np.percentile(v, 50)),
        'p95': float(np.percentile(v, 95)),
        'min': min(v),
        'max': max(v),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--trial', default='main_01_clips_off_linkcut')
    a = ap.parse_args()
    root, trial = a.root, a.root / a.trial
    out = root / 'analysis'
    out.mkdir(exist_ok=True)
    sched = json.loads((trial / 'schedule.json').read_text())
    ms, me = sched['measure_start'], sched['measure_end']
    cuts = [r for r in rows(trial / 'cuts.jsonl')]
    cut_windows = []
    for r in cuts:
        if r['kind'] == 'cut_start':
            cut_windows.append({'offset_s': r['offset_s'], 'duration_s': r['duration_s'], 'cut_at': r['cut_at']})
        elif r['kind'] == 'cut_end':
            for c in cut_windows:
                if c['offset_s'] == r['offset_s']:
                    c['restored_at'] = r['restored_at']
    gate = rows(next(iter(sorted(trial.glob('*-gate.jsonl'))), trial / 'pi4-gate.jsonl'))
    gate_summary = {
        'cut_start': [g for g in gate if g['kind'] == 'cut_start'],
        'cut_end': [g['wall'] for g in gate if g['kind'] == 'cut_end'],
        'held_connections': sum(g['kind'] == 'held' for g in gate),
        'held_seconds': pct([g['seconds'] for g in gate if g['kind'] == 'held']),
        'relayed_connections': sum(g['kind'] == 'relayed' for g in gate),
        'upstream_failed': sum(g['kind'] == 'upstream_failed' for g in gate),
    }

    import psycopg
    from psycopg.rows import dict_row
    from psycopg.conninfo import make_conninfo

    dbinfo = json.loads((root / 'database.json').read_text())
    with psycopg.connect(
        make_conninfo(dbinfo['conninfo'], options='-c search_path=' + dbinfo['schema']), row_factory=dict_row
    ) as db:
        events = {r['event_uid']: dict(r) for r in db.execute('SELECT * FROM events')}
        alerts = {r['event_id']: dict(r) for r in db.execute('SELECT * FROM alerts')}
        receipts = [
            dict(r)
            for r in db.execute(
                'SELECT event_uid,message_seq,kind,received_at FROM edge_event_receipts ORDER BY received_at'
            )
        ]
    first_browser = {}
    for r in rows(root / 'browser.jsonl'):
        if r.get('kind') != 'browser_receive':
            continue
        data = r.get('data') or {}
        for al in data.get('alerts', []) if r['event_type'] == 'snapshot' else [data.get('alert')]:
            if al and al.get('id') is not None:
                first_browser.setdefault(al['id'], r['browser_wall_ms'] / 1000)
    requests = rows(root / 'server_requests.jsonl')
    first_ok = {}
    for r in requests:
        if r['path'] == '/api/edge_event' and r.get('status') == 200:
            first_ok.setdefault((r.get('event_uid'), r.get('message_seq')), r)

    def in_cut(t):
        for i, c in enumerate(cut_windows):
            if c['cut_at'] <= t < c.get('restored_at', float('inf')):
                return i
        return None

    completed = json.loads((root / 'completed.json').read_text()) if (root / 'completed.json').exists() else {}
    gated = (completed.get('gated_devices') or ['pi4'])[0]
    control = 'pi5' if gated == 'pi4' else 'pi4'
    report = {
        'trial': a.trial,
        'schedule': sched,
        'cuts': cut_windows,
        'gated_device': gated,
        'control_device': control,
        'gate': gate_summary,
        'devices': {},
    }
    per_event = []
    series = {}
    for device, cam in (('pi4', '2'), ('pi5', '1')):
        local = trial / device
        if not (local / 'slots.jsonl').exists():
            continue
        slots = [
            r
            for r in rows(local / 'slots.jsonl')
            if r.get('phase') == 'measure' and r.get('segment') == 'source' and ms <= r['decision_wall'] < me
        ]
        videos = [r for r in rows(local / 'videos.jsonl') if r.get('phase') == 'measure']
        expected = sum(r['expected_slots'] for r in videos)
        source_s = sum(r['source_duration_s'] for r in videos)
        decisions = [r for r in rows(local / 'decisions.jsonl') if r['kind'] == 'open' and ms <= r['decided_at'] < me]
        transport = [
            r
            for r in rows(local / 'transport.jsonl')
            if r['path'].startswith('/api/edge_event') and 'context' not in r['path']
        ]
        t_meas = [r for r in transport if ms <= r['started_at'] < me + 300]
        failed = [r for r in t_meas if r['status'] != 'ok']
        failed_in_cut = [r for r in failed if in_cut(r['started_at']) is not None]
        dup = [r for r in t_meas if r['status'] == 'ok' and (r.get('reply') or {}).get('duplicate')]
        res = [
            r
            for r in rows(local / 'resources.jsonl')
            if ms - 60 <= r['wall'] < me + 300 and not r.get('error') and r.get('outbox')
        ]
        series[device] = [(r['wall'], r['outbox']['outbox_pending'], r['outbox'].get('outbox_oldest_s')) for r in res]
        summary = rows(local / 'summary.jsonl')
        summary = summary[-1] if summary else {}
        con = sqlite3.connect('file:' + str(local / 'outbox.sqlite3') + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        ob = [
            dict(r)
            for r in con.execute(
                'SELECT event_uid,message_seq,kind,created_at,acked_at,attempts,blocked_at,last_error FROM outbox WHERE created_at>=? AND created_at<?',
                (ms, me),
            )
        ]
        cohort = []
        for d in decisions:
            uid = d['event_uid']
            ev = events.get(uid)
            al = alerts.get(ev['id']) if ev else None
            rx = first_ok.get((uid, 1))
            clock = d.get('clock') or {}
            off = clock.get('offset_s')
            ci = in_cut(d['decided_at'])
            # receipts of this event, in the order the server stored them
            seqs = [r['message_seq'] for r in receipts if r['event_uid'] == uid]
            item = {
                'device': device,
                'event_uid': uid,
                'decided_at': d['decided_at'],
                'decided_in_cut': ci,
                'server_received': ev is not None,
                'server_open_received_at': rx['body_received_at'] if rx else None,
                'decision_to_server_s': (rx['body_received_at'] - d['decided_at'] - off)
                if rx and off is not None
                else None,
                'after_restore_s': (rx['body_received_at'] - cut_windows[ci]['restored_at'])
                if rx and ci is not None and 'restored_at' in cut_windows[ci]
                else None,
                'late_arrival': bool(ev and ev['late_arrival']),
                'notification_mode': al['notification_mode'] if al else None,
                'browser_received': (al['id'] in first_browser) if al else False,
                'browser_receipt_confirmed': bool(al and al['first_delivered_at']),
                'receipt_seqs': seqs,
                'receipts_in_order': seqs == sorted(seqs) and len(seqs) == len(set(seqs)),
                'messages': len([r for r in ob if r['event_uid'] == uid]),
                'max_attempts': max([r['attempts'] for r in ob if r['event_uid'] == uid] or [0]),
            }
            cohort.append(item)
        per_event.extend(cohort)
        in_cuts = [c for c in cohort if c['decided_in_cut'] is not None]
        pending_max = max([p for _, p, _ in series[device]] or [0])
        recovery = []
        for i, c in enumerate(cut_windows):
            if 'restored_at' not in c:
                continue
            after = [(w, p) for w, p, _ in series[device] if w >= c['restored_at']]
            zero = next((w for w, p in after if p == 0), None)
            peak = max([p for w, p, _ in series[device] if c['cut_at'] <= w <= c['restored_at'] + 120] or [0])
            msgs = [r for r in ob if c['cut_at'] <= r['created_at'] < c['restored_at']]
            recovery.append(
                {
                    'cut': i,
                    'peak_pending': peak,
                    'pending_zero_after_restore_s': (zero - c['restored_at']) if zero else None,
                    'messages_created_during_cut': len(msgs),
                    'messages_acked_after_restore_s': pct(
                        [r['acked_at'] - c['restored_at'] for r in msgs if r['acked_at']]
                    ),
                    'events_decided_during_cut': sum(1 for x in cohort if x['decided_in_cut'] == i),
                    'failed_attempts_during_cut': sum(1 for r in failed if in_cut(r['started_at']) == i),
                }
            )
        report['devices'][device] = {
            'run_id': slots[0]['run_id'] if slots else None,
            'processed_slots': len(slots),
            'expected_slots': expected,
            'missing_slots': max(0, expected - len(slots)),
            'achieved_hz': len(slots) / source_s if source_s else None,
            'processing_ms': pct([r['processing_ms'] for r in slots]),
            'events_decided': len(cohort),
            'events_decided_in_cuts': len(in_cuts),
            'server_events': sum(c['server_received'] for c in cohort),
            'browser_events': sum(c['browser_received'] for c in cohort),
            'late_arrival_events': sum(c['late_arrival'] for c in cohort),
            'notification_modes': dict(collections.Counter(c['notification_mode'] for c in cohort)),
            'receipts_in_order_all': all(c['receipts_in_order'] for c in cohort),
            'server_events_for_run': sum(
                1 for e in events.values() if e['run_id'] == (slots[0]['run_id'] if slots else None)
            ),
            'outbox_messages': len(ob),
            'outbox_retried_messages': sum(r['attempts'] > 1 for r in ob),
            'outbox_max_attempts': max([r['attempts'] for r in ob] or [0]),
            'outbox_attempts_total': sum(r['attempts'] for r in ob),
            'outbox_blocked': sum(r['blocked_at'] is not None for r in ob),
            'outbox_unacked': sum(r['acked_at'] is None for r in ob),
            'outbox_pending_end': summary.get('outbox_pending'),
            'created_to_ack_s': pct([r['acked_at'] - r['created_at'] for r in ob if r['acked_at']]),
            'created_to_ack_s_outside_cuts': pct(
                [r['acked_at'] - r['created_at'] for r in ob if r['acked_at'] and in_cut(r['created_at']) is None]
            ),
            'transport_attempts': len(t_meas),
            'transport_failed': len(failed),
            'transport_failed_in_cuts': len(failed_in_cut),
            'transport_failed_errors': dict(collections.Counter(r.get('error') for r in failed)),
            'duplicate_replies': len(dup),
            'outbox_pending_max': pending_max,
            'decision_to_server_s_outside_cuts': pct(
                [
                    c['decision_to_server_s']
                    for c in cohort
                    if c['decided_in_cut'] is None and c['decision_to_server_s'] is not None
                ]
            ),
            'cuts': recovery,
            'completed': bool(summary) and not summary.get('stopped'),
        }
    (out / 'linkcut_summary.json').write_text(json.dumps(report, indent=2, default=str) + '\n')
    with (out / 'linkcut_events.csv').open('w', newline='') as f:
        keys = list(dict.fromkeys(k for r in per_event for k in r))
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in per_event:
            w.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in r.items()})
    with (out / 'linkcut_outbox_series.csv').open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['device', 'wall', 'measure_s', 'outbox_pending', 'outbox_oldest_s'])
        for dev, s in series.items():
            for wall, p, o in s:
                w.writerow([dev, wall, wall - ms, p, o])
    # Figure: Pi 4 outbox backlog over the measurement with the cuts shaded and event decisions / server receipts marked.
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9})
    fig, ax = plt.subplots(figsize=(7.4, 3.2))
    for c in cut_windows:
        ax.axvspan(
            c['cut_at'] - ms,
            c.get('restored_at', me) - ms,
            color='#f3d9d9',
            label='link cut' if c is cut_windows[0] else None,
        )
    for dev, color in ((gated, '#25765f'), (control, '#28639e')):
        if dev in series:
            ax.step(
                [w - ms for w, _, _ in series[dev]],
                [p for _, p, _ in series[dev]],
                where='post',
                color=color,
                linewidth=1.4,
                label=dev.replace('pi', 'Pi ') + ' outbox pending' + ('' if dev == gated else ' (no cut)'),
            )
    ev4 = [c for c in per_event if c['device'] == gated]
    ax.plot(
        [c['decided_at'] - ms for c in ev4],
        [-1.2] * len(ev4),
        '|',
        color='#b17b19',
        markersize=8,
        label='event decided (' + gated.replace('pi', 'Pi ') + ')',
    )
    ax.plot(
        [c['server_open_received_at'] - ms for c in ev4 if c['server_open_received_at']],
        [-2.4] * sum(1 for c in ev4 if c['server_open_received_at']),
        '|',
        color='#28639e',
        markersize=8,
        label='open stored at server',
    )
    ax.set_xlim(0, me - ms + 120)
    ax.set_xlabel('Time since the start of the measurement (s)')
    ax.set_ylabel('Messages pending in the outbox')
    ax.legend(fontsize=7, loc='upper right')
    ax.set_title(
        root.name + ': ' + gated.replace('pi', 'Raspberry Pi ') + ' with two 180 s link cuts',
        loc='left',
        fontsize=9,
        fontweight='bold',
    )
    fig.tight_layout()
    for ext in ('png', 'pdf', 'svg'):
        fig.savefig(out / ('linkcut_outbox.' + ext), dpi=200)
    print(json.dumps({k: v for k, v in report.items() if k != 'devices'}, indent=1, default=str))
    for dev, d in report['devices'].items():
        print(dev, json.dumps({k: v for k, v in d.items() if k not in ('cuts', 'processing_ms')}, default=str))
        for c in d['cuts']:
            print('  cut', json.dumps(c, default=str))


if __name__ == '__main__':
    main()
