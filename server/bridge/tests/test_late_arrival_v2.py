#!/usr/bin/env python3
"""Late-arrival policy, version 2: classification from the event's latest state (checks C1-C11).

C1 late, recovered -> manual review (LATE_RECOVERED), no dispatch, one quiet reminder after the review interval
C2 late, still open (POST_FALL) -> live (ONGOING)
C3 late, closed by input loss / maximum age / runtime stop -> live (OUTCOME_UNKNOWN)
C4 opened right after a restore, before the clock was re-calibrated -> live (ONGOING), clock untrusted for display
C5 late open, later messages never arrive -> live (HOLD_TIMEOUT) when the hold runs out
C6 the same message resent with another age and backlog -> the stored receipt, no conflict
C7 bridge restarted while an alert was classifying -> settled by the same rules after the restart
C8 a live alert never becomes manual review when its close (recovered) arrives later
C9 age unknown after a reboot, closed with recovery -> manual review (LATE_RECOVERED)
C10 opened after a reboot before the wall clock was set -> live, detection time taken from the device age
C11 queued before a power loss, sent after the reboot: runtime_stop -> live; recovered -> manual review
C12 the same with the device's word that its clock was synchronised at the open: decided_at used as is
Also: an outcome unknown older than the stale threshold -> manual review (STALE_UNKNOWN); a recent open is classified
at once even with a backlog; an old device without transport headers is held and settled by its close.
"""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import copy
import json
import uuid

from _pg import Schema, url
from alert_store import AlertStore
from camera_registry import CameraRegistry
from edge_event_store import EdgeEventStore, EventConflict
from event_store import EventStore
from notification_policy import (
    CLASSIFYING,
    HOLD_TIMEOUT,
    LATE_RECOVERED,
    LIVE,
    MANUAL_REVIEW,
    ONGOING,
    OUTCOME_UNKNOWN,
    STALE_UNKNOWN,
    VERSION,
)

GOOD = {'offset_s': 0.0, 'rtt_s': 0.01}
FAILED = {'offset_s': 0.0, 'rtt_s': 60.0}  # the device's value before its first calibration


class Device:
    """Builds the messages of one event the way the device does, with a chosen clock sample per message."""

    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.run = uuid.uuid4().hex

    def message(self, kind, seq, state, decided_at, close_reason=None, clock=GOOD, sensor_ns=None):
        base = {'open': 0, 'update': 1, 'close': 3}[kind]
        row = copy.deepcopy(self.fixtures[base])
        row.update(
            run_id=self.run,
            event_uid=self.run + ':1',
            message_seq=seq,
            kind=kind,
            state=state,
            decided_at=decided_at,
            sent_at=decided_at,
            clock=dict(clock),
            close_reason=close_reason,
            decided_sensor_ns=sensor_ns if sensor_ns is not None else 1_000_000_000 + seq * 1_000_000_000,
        )
        return row


def expect(alerts, id, mode, reason=None):
    row = alerts.get(id, None)
    assert row['notification_mode'] == mode, (row['notification_mode'], row['classification_reason'])
    if reason is not None:
        assert row['classification_reason'] == reason, row['classification_reason']
    assert row['policy_version'] == VERSION
    return row


def dispatch(events, id):
    return events._db.execute('SELECT state FROM alert_dispatch_jobs WHERE alert_id=?', (id,)).fetchone()[0]


def log_actions(events, id):
    return [r[0] for r in events._db.execute('SELECT action FROM alert_log WHERE alert_id=? ORDER BY id', (id,))]


def scenarios(events, fixtures):
    alerts = AlertStore(events)
    edge = EdgeEventStore(events, 120, delivery_enabled=True, stale_age_s=3600, hold_s=5, review_reminder_s=600)
    t = 50_000_000.0  # well after 30 days of epoch time, for the unset-wall-clock case
    results = {}

    # Recent open: classified at once, even with a backlog, and the later recovered close does not change it (C8).
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 10), now=t, transport={'event_age_ms': 10_000, 'event_backlog': 3})['alert_id']
    expect(alerts, id, LIVE, ONGOING)
    assert dispatch(events, id) == 'queued'
    edge.receive(d.message('update', 2, 'POST_FALL', t - 5), now=t + 0.1, transport={'event_age_ms': 15_100, 'event_backlog': 1})
    edge.receive(d.message('close', 3, 'COOLDOWN', t - 1, 'recovered'), now=t + 0.2, transport={'event_age_ms': 19_200, 'event_backlog': 0})
    row = expect(alerts, id, LIVE, ONGOING)
    assert row['close_reason'] == 'recovered' and row['event_ended_at'] > row['event_decided_at']
    assert log_actions(events, id) == ['created']
    results['C8'] = id

    # C1: late and recovered -> manual review, dispatch cancelled, one reminder after 600 s.
    d = Device(fixtures)
    first = d.message('open', 1, 'ALERT', t - 200)
    id = edge.receive(first, now=t, transport={'event_age_ms': 200_000, 'event_backlog': 2})['alert_id']
    row = expect(alerts, id, CLASSIFYING)
    assert row['hold_until'] == t + 5 and row['event_decided_at'] == t - 200 and row['event_age_on_arrival_ms'] == 200_000
    edge.receive(d.message('update', 2, 'POST_FALL', t - 190), now=t + 0.1, transport={'event_age_ms': 200_100, 'event_backlog': 1})
    expect(alerts, id, CLASSIFYING)
    edge.receive(d.message('close', 3, 'COOLDOWN', t - 100, 'recovered'), now=t + 0.2, transport={'event_age_ms': 200_200, 'event_backlog': 0})
    row = expect(alerts, id, MANUAL_REVIEW, LATE_RECOVERED)
    assert row['suppression_reason'] == 'late_recovered' and row['hold_until'] is None and row['classified_at'] == t + 0.2
    assert dispatch(events, id) == 'cancelled'
    assert log_actions(events, id) == ['created', 'classified']
    assert edge.tick(now=t + 500) == {'settled': 0, 'reminded': 0}
    assert edge.tick(now=t + 601) == {'settled': 0, 'reminded': 1}
    assert edge.tick(now=t + 700) == {'settled': 0, 'reminded': 0}, 'the review reminder is sent once'
    assert log_actions(events, id) == ['created', 'classified', 'review_reminded']
    assert alerts.get(id, None)['review_reminded_at'] == t + 601
    # C6: the open resent with another age and backlog is the stored receipt, not a conflict.
    again = edge.receive(first, now=t + 1, transport={'event_age_ms': 201_000, 'event_backlog': 0})
    assert again['duplicate'] and again['alert_id'] == id
    results['C1'] = id

    # C2: late but still open -> live.
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 200), now=t, transport={'event_age_ms': 200_000, 'event_backlog': 1})['alert_id']
    expect(alerts, id, CLASSIFYING)
    edge.receive(d.message('update', 2, 'POST_FALL', t - 190), now=t + 0.1, transport={'event_age_ms': 200_100, 'event_backlog': 0})
    expect(alerts, id, LIVE, ONGOING)
    assert dispatch(events, id) == 'queued' and alerts.get(id, None)['late_arrival'] == 1
    results['C2'] = id

    # C3: late and closed for another reason -> live (OUTCOME_UNKNOWN); beyond the stale threshold -> manual review.
    for reason, age, mode, code in (
        ('track_lost', 400, LIVE, OUTCOME_UNKNOWN),
        ('timeout', 900, LIVE, OUTCOME_UNKNOWN),
        ('runtime_stop', 3000, LIVE, OUTCOME_UNKNOWN),
        ('runtime_stop', 3601, MANUAL_REVIEW, STALE_UNKNOWN),
    ):
        d = Device(fixtures)
        id = edge.receive(d.message('open', 1, 'ALERT', t - age), now=t, transport={'event_age_ms': age * 1000, 'event_backlog': 1})['alert_id']
        edge.receive(d.message('close', 2, 'COOLDOWN', t - age + 30, reason), now=t + 0.1, transport={'event_age_ms': age * 1000 + 100, 'event_backlog': 0})
        row = expect(alerts, id, mode, code)
        assert row['suppression_reason'] == ('stale_unknown' if mode == MANUAL_REVIEW else None)
        results[f'C3:{reason}:{age}'] = id

    # C4: opened right after a restore, before re-calibration: the clock sample is the failure value.
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 1, clock=FAILED), now=t, transport={'event_age_ms': 1_200, 'event_backlog': 0})['alert_id']
    row = expect(alerts, id, LIVE, ONGOING)
    assert row['clock_usable'] == 0 and row['event_decided_at'] == t - 1.2 and row['age_source'] == 'device_boot'
    results['C4'] = id

    # C5: late open and nothing more arrives -> live when the hold runs out.
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 300), now=t, transport={'event_age_ms': 300_000, 'event_backlog': 2})['alert_id']
    expect(alerts, id, CLASSIFYING)
    assert edge.tick(now=t + 4.9)['settled'] == 0
    assert edge.tick(now=t + 5)['settled'] == 1
    row = expect(alerts, id, LIVE, HOLD_TIMEOUT)
    assert row['classified_at'] == t + 5 and dispatch(events, id) == 'queued'
    # The late close that finally arrives does not demote the live alert.
    edge.receive(d.message('close', 2, 'COOLDOWN', t - 100, 'recovered'), now=t + 9, transport={'event_age_ms': 309_000, 'event_backlog': 0})
    expect(alerts, id, LIVE, HOLD_TIMEOUT)
    results['C5'] = id

    # C9: age unknown (no age header after a reboot) and recovered -> manual review; clock sample from before the
    # reboot is reused by the device, so the wall clock says 200 s: that fallback also gives LATE_RECOVERED.
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 200, clock=FAILED), now=t, transport={'event_backlog': 1})['alert_id']
    row = expect(alerts, id, CLASSIFYING)
    assert row['age_source'] is None and row['event_decided_at'] is None and row['late_arrival'] == 1
    edge.receive(d.message('close', 2, 'COOLDOWN', t - 150, 'recovered', clock=FAILED), now=t + 0.1, transport={'event_backlog': 0})
    expect(alerts, id, MANUAL_REVIEW, LATE_RECOVERED)
    results['C9'] = id

    # C10: opened after a reboot before the wall clock was set (device time in the past, no calibration yet).
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 86_400 * 30, clock=FAILED), now=t, transport={'event_age_ms': 3_000, 'event_backlog': 0})['alert_id']
    row = expect(alerts, id, LIVE, ONGOING)
    assert row['event_decided_at'] == t - 3 and row['clock_usable'] == 0 and row['late_arrival'] == 0
    results['C10'] = id

    # C11: queued before a power loss and sent after the reboot (no age header; the clock sample was taken while
    # the device was still calibrated, so the wall-clock age is used): runtime_stop -> live, recovered -> review.
    for reason, mode, code in (('runtime_stop', LIVE, OUTCOME_UNKNOWN), ('recovered', MANUAL_REVIEW, LATE_RECOVERED)):
        d = Device(fixtures)
        id = edge.receive(d.message('open', 1, 'ALERT', t - 500), now=t, transport={'event_backlog': 1})['alert_id']
        row = expect(alerts, id, CLASSIFYING)
        assert row['age_source'] == 'wall_clock' and row['event_age_on_arrival_ms'] == 500_000
        edge.receive(d.message('close', 2, 'COOLDOWN', t - 400, reason), now=t + 0.1, transport={'event_backlog': 0})
        expect(alerts, id, mode, code)
        results[f'C11:{reason}'] = id

    # C12: queued before a power loss and sent after the reboot while the device's wall clock is still unset (the
    # send-time calibration says 11 days), with the device's word that its clock was synchronised when the event
    # opened: decided_at is used as is, so the detection time and the age are right.
    unset = {'offset_s': 968_000.0, 'rtt_s': 0.01}
    for reason, mode, code in (('runtime_stop', LIVE, OUTCOME_UNKNOWN), ('recovered', MANUAL_REVIEW, LATE_RECOVERED)):
        d = Device(fixtures)
        id = edge.receive(d.message('open', 1, 'ALERT', t - 500, clock=unset), now=t, transport={'event_backlog': 1, 'decided_clock_synced': True})['alert_id']
        row = expect(alerts, id, CLASSIFYING)
        assert row['age_source'] == 'decided_wall' and row['event_age_on_arrival_ms'] == 500_000 and row['event_decided_at'] == t - 500
        edge.receive(d.message('close', 2, 'COOLDOWN', t - 400, reason, clock=unset), now=t + 0.1, transport={'event_backlog': 0, 'decided_clock_synced': True})
        expect(alerts, id, mode, code)
        results[f'C12:{reason}'] = id
    # Without the device's word the same messages give an unknown age (the calibration is not trusted).
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 500, clock=unset), now=t, transport={'event_backlog': 1})['alert_id']
    assert expect(alerts, id, CLASSIFYING)['age_source'] is None
    edge.receive(d.message('close', 2, 'COOLDOWN', t - 400, 'runtime_stop', clock=unset), now=t + 0.1, transport={'event_backlog': 0})
    expect(alerts, id, LIVE, OUTCOME_UNKNOWN)

    # An old device sends no headers at all: a recent open is live at once; a late one is held and its close settles it.
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 10), now=t)['alert_id']
    expect(alerts, id, LIVE, ONGOING)
    d = Device(fixtures)
    id = edge.receive(d.message('open', 1, 'ALERT', t - 200), now=t)['alert_id']
    expect(alerts, id, CLASSIFYING)
    edge.receive(d.message('update', 2, 'POST_FALL', t - 190), now=t + 0.1)
    expect(alerts, id, CLASSIFYING), 'an update without a backlog count is not the latest state'
    edge.receive(d.message('close', 3, 'COOLDOWN', t - 180, 'track_lost'), now=t + 0.2)
    expect(alerts, id, LIVE, OUTCOME_UNKNOWN)
    results['legacy-device'] = id

    # Invariants over everything stored so far.
    db = events._db
    assert db.execute("SELECT COUNT(*) FROM alerts WHERE notification_mode='classifying'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM alerts WHERE notification_mode='live' AND suppression_reason IS NOT NULL").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM alerts WHERE notification_mode='manual_review' AND suppression_reason IS NULL").fetchone()[0] == 0
    # A legacy client (another policy version) sees settled live alerts only, including late ones.
    legacy = alerts.snapshot(None, live_only=True)['alerts']
    assert legacy and all(a['notification_mode'] == 'live' for a in legacy)
    assert any(a['late_arrival'] == 1 for a in legacy), 'late live alerts must reach old clients'
    # The schema refuses contradictions.
    for sql in (
        "UPDATE alerts SET notification_mode='manual_review',suppression_reason=NULL WHERE id=?",
        "UPDATE alerts SET notification_mode='live',suppression_reason='late_recovered' WHERE id=?",
    ):
        try:
            with db:
                db.execute(sql, (results['C2'],))
            raise AssertionError('constraint accepted ' + sql)
        except Exception as exc:
            assert not isinstance(exc, AssertionError), exc
    print('PASS C1-C12: latest-state classification, hold and timeout, reminder, resend, reboot, synced-clock and legacy-device cases')
    return results


def restart(fixtures, pending_id_holder):
    """C7: an alert classifying when the bridge stops is settled by the same rules after the restart."""
    events = EventStore(url())
    edge = EdgeEventStore(events, 120, hold_s=5)
    d = Device(fixtures)
    t = 20_000.0
    id = edge.receive(d.message('open', 1, 'ALERT', t - 300), now=t, transport={'event_age_ms': 300_000, 'event_backlog': 1})['alert_id']
    assert AlertStore(events).get(id, None)['notification_mode'] == CLASSIFYING
    events.close()
    restored = EventStore(url())
    edge = EdgeEventStore(restored, 120, hold_s=5)
    # The close arrives after the restart within the hold: classified from it.
    edge.receive(d.message('close', 2, 'COOLDOWN', t - 200, 'recovered'), now=t + 3, transport={'event_age_ms': 303_000, 'event_backlog': 0})
    assert AlertStore(restored).get(id, None)['classification_reason'] == LATE_RECOVERED
    # And one whose hold ran out during the downtime is settled by the first tick.
    d = Device(fixtures)
    id2 = edge.receive(d.message('open', 1, 'ALERT', t - 300), now=t + 4, transport={'event_age_ms': 300_000, 'event_backlog': 1})['alert_id']
    restored.close()
    again = EventStore(url())
    edge = EdgeEventStore(again, 120, hold_s=5)
    assert edge.tick(now=t + 30)['settled'] == 1
    assert AlertStore(again).get(id2, None)['classification_reason'] == HOLD_TIMEOUT
    again.close()
    print('PASS C7: classification holds survive a bridge restart')


def main():
    fixtures = json.loads((FIXTURES / 'edge_event_v1.json').read_text())
    with Schema():
        events = EventStore(url())
        CameraRegistry([{'id': '1'}], connection=events._db, lock=events._lock)
        results = scenarios(events, fixtures)
        events.close()
        restored = EventStore(url())
        alerts = AlertStore(restored)
        assert all(alerts.get(id, None)['notification_mode'] != CLASSIFYING for id in results.values())
        restored.close()
        restart(fixtures, results)


if __name__ == '__main__':
    main()
