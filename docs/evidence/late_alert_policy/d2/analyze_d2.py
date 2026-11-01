#!/usr/bin/env python3
"""Analyse a D2 run (d2_controller.py): per case, what the device sent during and after the cut and how the bridge
classified the alert. Writes aggregates only (d2_summary.json, d2_messages.csv) into <root>/analysis."""

import argparse
import collections
import csv
import json
import sqlite3
import sys
from pathlib import Path

EXPECTED = {
    'lying_still': ('live', {'ONGOING', 'HOLD_TIMEOUT'}),
    'recovered_in_cut': ('manual_review', {'LATE_RECOVERED'}),
}


def rows(path):
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def rel(t, origin):
    return None if t is None or origin is None else round(t - origin, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    a = ap.parse_args()
    root = a.root.resolve()
    out = root / 'analysis'
    out.mkdir(exist_ok=True)
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    dbinfo = json.loads((root / 'database.json').read_text())
    completed = json.loads((root / 'completed.json').read_text())
    requests = [r for r in rows(root / 'server_requests.jsonl') if r['path'] == '/api/edge_event']
    report = {'root': root.name, 'cut': completed['cut'], 'cases': {}}
    messages = []
    with psycopg.connect(
        make_conninfo(dbinfo['conninfo'], options='-c search_path=' + dbinfo['schema']), row_factory=dict_row
    ) as db:
        for case, scores in completed['cases']:
            folder = root / case
            adapter = json.loads((folder / 'adapter.json').read_text())
            run_id = adapter['run_id']
            cuts = rows(folder / 'cuts.jsonl')
            started = next(c['host_started_at'] for c in cuts if c['kind'] == 'adapter_started')
            exited = next((c['host_exited_at'] for c in cuts if c['kind'] == 'adapter_exited'), None)
            cut_at = next((c['cut_at'] for c in cuts if c['kind'] == 'cut_start'), None)
            restored_at = next((c['restored_at'] for c in cuts if c['kind'] == 'cut_end'), None)
            events = db.execute('SELECT * FROM events WHERE run_id=%s ORDER BY id', (run_id,)).fetchall()
            assert len(events) == 1, (case, len(events))
            ev = events[0]
            al = db.execute('SELECT * FROM alerts WHERE event_id=%s', (ev['id'],)).fetchone()
            log = db.execute(
                'SELECT at,actor,action,from_state,to_state,detail FROM alert_log WHERE alert_id=%s ORDER BY id', (al['id'],)
            ).fetchall()
            receipts = db.execute(
                'SELECT message_seq,kind,received_at FROM edge_event_receipts WHERE event_uid=%s ORDER BY received_at',
                (ev['event_uid'],),
            ).fetchall()
            received = {r['message_seq']: r for r in receipts}
            con = sqlite3.connect('file:' + str(folder / 'outbox.sqlite3') + '?mode=ro', uri=True)
            con.row_factory = sqlite3.Row
            outbox = [
                dict(r)
                for r in con.execute(
                    'SELECT event_uid,message_seq,kind,created_at,acked_at,attempts,blocked_at,last_error FROM outbox ORDER BY message_seq'
                )
            ]
            con.close()
            served = collections.defaultdict(list)
            for r in requests:
                if r.get('run_id') == run_id or r.get('event_uid') == ev['event_uid']:
                    served[r.get('message_seq')].append(r)
            first_ok = {seq: next((r for r in lst if r.get('status') == 200), None) for seq, lst in served.items()}
            gate = [g for g in rows(folder / 'gate.jsonl') if started <= g['wall'] <= (exited or float('inf'))]
            in_cut = lambda t: cut_at is not None and cut_at <= t < (restored_at or float('inf'))  # noqa: E731
            for m in outbox:
                ok = first_ok.get(m['message_seq'])
                messages.append(
                    {
                        'case': case,
                        'message_seq': m['message_seq'],
                        'kind': m['kind'],
                        'created_s': rel(m['created_at'], started),
                        'created_in_cut': in_cut(m['created_at']),
                        'acked_s': rel(m['acked_at'], started),
                        'acked_after_restore_s': rel(m['acked_at'], restored_at) if m['acked_at'] and in_cut(m['created_at']) else None,
                        'attempts': m['attempts'],
                        'blocked': m['blocked_at'] is not None,
                        'server_received_s': rel(ok['body_received_at'], started) if ok else None,
                        'age_header_ms': ok.get('event_age_ms') if ok else None,
                        'backlog_header': ok.get('event_backlog') if ok else None,
                        'receipt_s': rel(received[m['message_seq']]['received_at'], started) if m['message_seq'] in received else None,
                    }
                )
            open_rx = first_ok.get(1)
            open_received_at = received[1]['received_at'] if 1 in received else None
            seqs = [r['message_seq'] for r in receipts]
            during = [m for m in outbox if in_cut(m['created_at'])]
            acked_after = [m['acked_at'] - restored_at for m in during if m['acked_at']]
            expected_mode, expected_reasons = EXPECTED[case]
            summary = {
                'scores': scores,
                'run_id': run_id,
                'event_uid': ev['event_uid'],
                'adapter': {k: adapter[k] for k in ('rows', 'events_opened', 'events_closed', 'outbox')},
                'cut': {'cut_at_s': rel(cut_at, started), 'restored_at_s': rel(restored_at, started)},
                'event': {
                    'decided_at_s': rel(ev['decided_at'], started),
                    'open_created_in_cut': in_cut(outbox[0]['created_at']) if outbox else None,
                    'open_received_s': rel(open_received_at, started),
                    'open_received_after_restore_s': rel(open_received_at, restored_at),
                    'open_age_header_ms': open_rx.get('event_age_ms') if open_rx else None,
                    'open_backlog_header': open_rx.get('event_backlog') if open_rx else None,
                    'event_age_on_arrival_ms': ev['event_age_on_arrival_ms'],
                    'age_source': ev['age_source'],
                    'late_arrival': ev['late_arrival'],
                    'clock_usable': ev['clock_usable'],
                    'close_reason': ev['close_reason'],
                    'ended_at_s': rel(ev['ended_at'], started),
                    'close_received_s': rel(next((r['received_at'] for r in receipts if r['kind'] == 'close'), None), started),
                },
                'alert': {
                    'id': al['id'],
                    'state': al['state'],
                    'notification_mode': al['notification_mode'],
                    'classification_reason': al['classification_reason'],
                    'suppression_reason': al['suppression_reason'],
                    'policy_version': al['policy_version'],
                    'created_s': rel(al['created_at'], started),
                    'hold_until_s': rel(al['hold_until'], started),
                    'classified_s': rel(al['classified_at'], started),
                    'classified_after_open_received_s': rel(al['classified_at'], open_received_at),
                    'log': [
                        {'at_s': rel(r['at'], started), 'actor': r['actor'], 'action': r['action'], 'to_state': r['to_state'], 'detail': r['detail']}
                        for r in log
                    ],
                },
                'messages': {
                    'total': len(outbox),
                    'kinds': dict(collections.Counter(m['kind'] for m in outbox)),
                    'created_during_cut': len(during),
                    'acked_after_restore_s': {'min': min(acked_after), 'max': max(acked_after), 'n': len(acked_after)} if acked_after else None,
                    'max_attempts': max([m['attempts'] for m in outbox] or [0]),
                    'retried': sum(m['attempts'] > 1 for m in outbox),
                    'blocked': sum(m['blocked_at'] is not None for m in outbox),
                    'unacked': sum(m['acked_at'] is None for m in outbox),
                    'receipts': len(receipts),
                    'receipts_in_order': seqs == sorted(seqs) and len(seqs) == len(set(seqs)) and len(seqs) == len(outbox),
                    'duplicate_server_rows': sum(len(lst) - 1 for lst in served.values() if len(lst) > 1),
                },
                'gate': {
                    'held_connections': sum(g['kind'] == 'held' for g in gate),
                    'relayed_connections': sum(g['kind'] == 'relayed' for g in gate),
                    'upstream_failed': sum(g['kind'] == 'upstream_failed' for g in gate),
                },
                'expected': {'notification_mode': expected_mode, 'classification_reason': sorted(expected_reasons)},
                # The case only counts when the open really was queued by the cut and the bridge saw it as late.
                'as_expected': al['notification_mode'] == expected_mode
                and al['classification_reason'] in expected_reasons
                and bool(outbox)
                and in_cut(outbox[0]['created_at'])
                and ev['late_arrival'] == 1,
            }
            report['cases'][case] = summary
    (out / 'd2_summary.json').write_text(json.dumps(report, indent=2, default=str) + '\n')
    with (out / 'd2_messages.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(messages[0]) if messages else ['case'])
        w.writeheader()
        w.writerows(messages)
    for case, s in report['cases'].items():
        print(
            case,
            'mode', s['alert']['notification_mode'],
            'reason', s['alert']['classification_reason'],
            'open age header ms', s['event']['open_age_header_ms'],
            'backlog', s['event']['open_backlog_header'],
            'open after restore s', s['event']['open_received_after_restore_s'],
            'classified after open s', s['alert']['classified_after_open_received_s'],
            'messages', s['messages']['total'], 'during cut', s['messages']['created_during_cut'],
            'in order', s['messages']['receipts_in_order'], 'unacked', s['messages']['unacked'],
            'AS EXPECTED' if s['as_expected'] else 'NOT AS EXPECTED',
        )
    return 0 if all(s['as_expected'] for s in report['cases'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
