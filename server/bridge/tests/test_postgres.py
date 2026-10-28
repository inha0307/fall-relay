"""Real PostgreSQL integration checks in a disposable, uniquely named schema."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import uuid

from _pg import Schema

from alert_store import AlertStore, AlertError
from camera_registry import CameraRegistry
from device_commands import DeviceCommandStore, CommandError
from edge_event_store import EdgeEventStore, EventConflict
from event_store import EventStore
from hardware_store import HardwareStore
import traffic


def parallel(function, count=24):
    with ThreadPoolExecutor(max_workers=12) as workers:
        return list(workers.map(function, range(count)))


def main():
    with Schema(prefix='test_pg') as schema:
        try:
            pg = EventStore(schema.url)
            registry = CameraRegistry([{'id': '1'}, {'id': '2'}], connection=pg._db, lock=pg._lock)
            token = registry.issue_token('1')
            assert registry.verify('1', token) == 'token'
            edge = EdgeEventStore(pg, delivery_enabled=True)
            fixture = json.loads((FIXTURES / 'edge_event_v1.json').read_text())
            run = uuid.uuid4().hex
            for index, item in enumerate(fixture):
                item.update(
                    run_id=run, event_uid=run + ':1', camera_id='1', decided_at=100 + index, sent_at=100 + index
                )
                item['clock'] = {'offset_s': 0.0, 'rtt_s': 0.01}
            opened = fixture[0]
            replies = parallel(lambda _: edge.receive(opened, now=101))
            assert sum(not r['duplicate'] for r in replies) == 1
            assert len({(r['event_id'], r['alert_id']) for r in replies}) == 1
            changed = copy.deepcopy(opened)
            changed['max_score'] = 0.99999
            try:
                edge.receive(changed, now=101)
            except EventConflict:
                pass
            else:
                raise AssertionError('changed duplicate accepted')
            for message in fixture[1:]:
                edge.receive(message, now=105)
            print('PASS concurrent duplicate open, ordered update/close, changed duplicate refusal')

            def camera_event(index):
                item = copy.deepcopy(opened)
                rid = uuid.uuid4().hex
                item.update(run_id=rid, event_uid=rid + ':1', camera_id=str(1 + index % 2))
                return edge.receive(item, now=101)

            independent = parallel(camera_event)
            assert len({r['event_id'] for r in independent}) == len(independent)
            assert len({r['alert_id'] for r in independent}) == len(independent)
            print('PASS concurrent events from two cameras have unique events, alerts and receipts')

            # A failure after event insertion must roll the entire transaction back.
            item = copy.deepcopy(opened)
            rid = uuid.uuid4().hex
            item.update(run_id=rid, event_uid=rid + ':1')
            original_execute = pg._db.execute

            def fail_dispatch(statement, params=()):
                if statement.startswith('INSERT INTO alert_dispatch_jobs'):
                    raise OSError('injected failure')
                return original_execute(statement, params)

            pg._db.execute = fail_dispatch
            try:
                try:
                    edge.receive(item, now=101)
                except OSError:
                    pass
                else:
                    raise AssertionError('injected transaction failure absent')
            finally:
                pg._db.execute = original_execute
            assert (
                pg._db.execute('SELECT COUNT(*) FROM events WHERE event_uid=?', (item['event_uid'],)).fetchone()[0] == 0
            )
            edge.receive(item, now=101)
            alerts = AlertStore(pg)
            alert = replies[0]['alert_id']

            def ack(index):
                try:
                    alerts.transition(alert, None, 'tester' + str(index), 'ack', {})
                    return True
                except AlertError as error:
                    assert error.status == 409
                    return False

            assert sum(parallel(ack)) == 1
            resolved = alerts.transition(alert, None, 'tester', 'resolve', {'label': 'fall'})
            assert resolved['state'] == 'resolved'
            delivery = alerts.delivery(independent[0]['alert_id'], None, 'tester', 'browser')
            alerts.transition(
                independent[0]['alert_id'],
                None,
                'tester',
                'received',
                {'delivery_id': delivery, 'device_id': 'browser'},
            )
            snap = alerts.snapshot(None)
            assert snap['cursor'] > 0 and not alerts.changes(None, snap['cursor'])
            print('PASS atomic event/alert/receipt rollback and retry, one winning ACK, resolution and browser receipt')

            commands = DeviceCommandStore(pg)

            def command(_):
                try:
                    return commands.request('synthetic-pi', '1', 'logs', 'tester', ['logs'])
                except CommandError as error:
                    assert error.status == 409
                    return None

            requested = [r for r in parallel(command) if r]
            assert len(requested) == 1
            taken = [r for group in parallel(lambda _: commands.take('synthetic-pi')) for r in group]
            assert len(taken) == 1
            commands.complete(taken[0]['id'], 'synthetic-pi', True, 'test')
            hardware = HardwareStore(pg._db)
            sample = {
                'schema_version': 1,
                'device_id': 'synthetic-pi',
                'camera_id': '1',
                'measured_at': 100,
                'interval_s': 5,
                'metrics': {},
                'throttle': {'raw': 0},
            }
            assert sum(parallel(lambda _: hardware.record(sample, now=100))) == 1
            assert hardware.devices(now=101)[0]['camera_id'] == '1'
            traffic.record(pg._db, pg._lock, [('1', 'result', 2, 300), ('1', 'diagnostics', 1, 200)], 100, 10, 30)
            pg.record_metrics('1', 100, False, True, 0, 10, [12, 20], 2, 0)
            assert traffic.summary(pg._db, pg._lock, 90, 110, ['1'], 10)[0]['bytes'] == 500
            assert pg.list_events(since=0)['total'] == len(independent) + 2
            print('PASS one command request/pickup, hardware deduplication, diagnostics traffic, event listing')
            pg.close()
            pg = EventStore(schema.url)
            assert edge.receipt(opened, replies[0]['event_id'], alert, True) == EdgeEventStore(pg).receive(
                opened, now=101
            )
            print('PASS connection-pool restart preserves exact event receipt and pending state')
        finally:
            pg.close()


if __name__ == '__main__':
    main()
