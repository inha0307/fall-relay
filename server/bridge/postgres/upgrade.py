"""Reviewed offline PostgreSQL migrations, with a private pg_dump before apply; adopts pre-baseline databases."""

import argparse
import json
from pathlib import Path
import time

import psycopg
from psycopg import sql
from postgres.backup import backup
from postgres.common_env import database_url
from postgres.schema import NAME, adopt_baseline, pending

REVIEWED = set()  # versions above the baseline that have been reviewed for the reference deployment


def upgrade_schema(conninfo, schema='public', apply=False):
    if not NAME.fullmatch(schema):
        raise ValueError('invalid schema')
    with psycopg.connect(conninfo) as connection:
        connection.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('fall-relay-schema-upgrade'))")
        todo = pending(connection.execute)
        if any(item[0] not in REVIEWED for item in todo):
            raise ValueError('unreviewed PostgreSQL migration')
        result = {'schema': schema, 'pending': [item[0] for item in todo], 'applied': []}
        if not apply or not todo:
            return result
        connection.execute('SET LOCAL synchronous_commit = on')
        connection.execute('LOCK TABLE events,alerts IN ACCESS EXCLUSIVE MODE')
        # Stored events and alerts must come through a migration unchanged.
        before = [connection.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in ('events', 'alerts')]
        for version, name, digest, text in todo:
            connection.execute(text)
            connection.execute(
                'INSERT INTO schema_migrations VALUES (%s,%s,%s,%s)', (version, name, time.time(), digest)
            )
        after = [connection.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in ('events', 'alerts')]
        assert after == before, 'existing event or alert values changed'
        result['applied'] = result['pending']
        return result


def bridge_running(conninfo):
    with psycopg.connect(conninfo) as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND application_name='fall-relay-bridge' LIMIT 1"
            ).fetchone()
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', default='public')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument(
        '--adopt-baseline',
        action='store_true',
        help='replace the legacy ledger of a database initialised before 0001_baseline.sql (bridge stopped)',
    )
    parser.add_argument('--backup-output', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    conninfo = database_url()
    if args.adopt_baseline:
        if args.schema == 'public' and bridge_running(conninfo):
            parser.error('stop the bridge service before adopting the baseline')
        with psycopg.connect(conninfo) as connection:
            result = adopt_baseline(connection, args.schema)
    else:
        result = upgrade_schema(conninfo, args.schema)
        if args.apply and result['pending']:
            if not args.backup_output:
                parser.error('--apply requires --backup-output')
            if args.schema == 'public' and bridge_running(conninfo):
                parser.error('stop the bridge service before upgrading public')
            folder = backup(conninfo, args.backup_output, args.schema)
            result = {**upgrade_schema(conninfo, args.schema, apply=True), 'backup': str(folder)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.touch(mode=0o600, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
