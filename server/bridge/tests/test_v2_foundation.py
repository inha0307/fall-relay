#!/usr/bin/env python3
"""Contract semantics, schema initialisation, ledger refusal, migration atomicity and SQL invariants."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import copy
import json
from pathlib import Path
import shutil
import tempfile
import uuid

import psycopg
from psycopg import sql
from _pg import Schema
from camera_registry import CameraRegistry
from edge_event_contract import semantic_hash, validate_edge_event
from event_store import EventStore
from postgres.schema import DIRECTORY, initialize, pending
from schema_migrations import scripts as edge_scripts

FIXTURE = FIXTURES / 'edge_event_v1.json'


def rejects(fn, error=ValueError):
    try:
        fn()
    except error:
        return
    raise AssertionError('invalid input accepted')


def contracts():
    messages = json.loads(FIXTURE.read_text())
    for message in messages:
        validate_edge_event(message)
    assert [message['message_seq'] for message in messages] == [1, 2, 3, 4]
    retry = {**messages[0], 'attempt': 2, 'sent_at': messages[0]['sent_at'] + 60}
    assert semantic_hash(messages[0]) == semantic_hash(retry)
    changed = {**messages[0], 'max_score': 0.999}
    assert semantic_hash(messages[0]) != semantic_hash(changed)
    for key, value in [
        ('message_seq', True),
        ('message_seq', 0),
        ('message_seq', 10**400),
        ('event_uid', 'b' * 32 + ':1'),
        ('max_score', float('nan')),
        ('kind', 'close'),
        ('state', 'RECOVERED'),
        ('close_reason', 'timeout'),
    ]:
        rejects(lambda k=key, v=value: validate_edge_event({**messages[0], k: v}))
    bad = copy.deepcopy(messages[0])
    bad['confirm']['m'] = 2
    rejects(lambda: validate_edge_event(bad))
    print('PASS open/multiple-update/close fixture, semantic retry hashes and malformed contracts')


def invariants(schema):
    events = EventStore(schema.url)
    cameras = CameraRegistry([{'id': '1', 'label': 'Synthetic'}], connection=events._db, lock=events._lock)
    db = events._db
    assert events.schema_versions == [1]
    with db:
        db.execute("INSERT INTO events(camera_id,track_id,started_at,ended_at,max_score) VALUES ('1',1,101,101,0.95)")
    assert db.execute('SELECT source,event_uid FROM events').fetchone()[0] == 'inferred_v1'

    def refused(statement, params=()):
        try:
            with db:
                db.execute(statement, params)
        except psycopg.errors.IntegrityError:
            return
        raise AssertionError('constraint not enforced: ' + statement)

    with db:
        db.execute("INSERT INTO alerts(event_id,type,camera_id,created_at) VALUES (1,'fall','1',101)")
    refused("INSERT INTO alerts(event_id,type,camera_id,created_at) VALUES (1,'fall','1',102)")
    with db:
        db.execute("INSERT INTO alerts(type,camera_id,created_at) VALUES ('device_warning','1',101)")
    refused("INSERT INTO alerts(type,camera_id,created_at) VALUES ('device_warning','1',102)")
    refused("UPDATE alerts SET resolution='recovered' WHERE type='fall'")
    insert = "INSERT INTO edge_event_receipts(event_uid,message_seq,kind,semantic_sha256,event_id,alert_id,received_at) VALUES ('a:1',1,'open',?,1,1,101)"
    with db:
        db.execute(insert, ('b' * 64,))
    refused(insert, ('b' * 64,))
    refused("INSERT INTO alerts(type,camera_id,created_at,notification_mode) VALUES ('fall','1',103,'manual_review')")
    cameras.close()
    events.close()
    print('PASS baseline schema: legacy row defaults, uniqueness, resolution and policy constraints')


def ledger(schema):
    """A changed script is refused; a schema with a pending script refuses to start; a broken batch rolls back."""
    with tempfile.TemporaryDirectory() as folder:
        copied = Path(folder) / 'scripts'
        shutil.copytree(DIRECTORY, copied)
        baseline = copied / '0001_baseline.sql'
        baseline.write_text('-- changed\n' + baseline.read_text())
        with schema.connect() as connection:
            rejects(lambda: pending(connection.execute, copied))
        baseline.write_text(baseline.read_text().removeprefix('-- changed\n'))
        (copied / '0002_pending.sql').write_text('CREATE TABLE later(id bigint);\n')
        with schema.connect() as connection:
            assert [item[0] for item in pending(connection.execute, copied)] == [2]
        rejects(lambda: _start(schema, copied))
        (copied / '0002_pending.sql').write_text('CREATE TABLE should_rollback(id bigint);\nTHIS IS INVALID SQL;\n')
        scratch = 'test_atomic_' + uuid.uuid4().hex
        with psycopg.connect(schema.base) as connection:
            try:
                rejects(lambda: initialize(connection, scratch, copied), psycopg.errors.SyntaxError)
                assert not connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (scratch,)).fetchone()
            finally:
                connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(scratch)))
    print('PASS checksum refusal, pending-migration refusal at startup and whole-batch rollback')


def _start(schema, directory):
    from postgres.schema import check

    with schema.connect() as connection:

        class Facade:
            def execute(self, statement, params=()):
                return connection.execute(statement.replace('?', '%s'), params)

        check(Facade(), directory)


def outbox_schema():
    import sqlite3

    edge = sqlite3.connect(':memory:')
    edge.executescript(edge_scripts(BRIDGE / 'migrations' / 'edge')[0][3])
    for seq, kind in [(1, 'open'), (2, 'update'), (3, 'update'), (4, 'close')]:
        edge.execute(
            'INSERT INTO outbox(event_uid,message_seq,kind,payload,semantic_sha256,created_at,next_attempt) VALUES (?,?,?,?,?,?,?)',
            ('run:1', seq, kind, '{}', 'a' * 64, 100, 100),
        )
    assert edge.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 4
    edge.close()
    print('PASS multiple-update outbox schema on the device')


if __name__ == '__main__':
    contracts()
    with Schema() as schema:
        invariants(schema)
        ledger(schema)
    outbox_schema()
