"""Prove the backup path: copy the live schema into a disposable one, dump it, restore it and compare every table."""

import argparse
import json
from pathlib import Path
import subprocess
import uuid

import psycopg
from psycopg import sql
from postgres.backup import backup, libpq_env
from postgres.common_env import database_url
from postgres.schema import NAME, baseline_columns, initialize

TABLES = sorted(baseline_columns())


def fingerprint(connection, schema, table):
    row = connection.execute(
        sql.SQL("SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {}.{} t").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
    ).fetchone()
    return [int(row[0]), row[1]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', default='public', help='the live schema to copy')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not NAME.fullmatch(args.schema):
        parser.error('invalid schema')
    scratch = 'test_restore_' + uuid.uuid4().hex
    conninfo = database_url()
    with psycopg.connect(conninfo) as connection:
        initialize(connection, scratch)
        with connection.transaction():
            connection.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(scratch)))
            connection.execute('SET CONSTRAINTS ALL DEFERRED')
            for table in TABLES:
                connection.execute(
                    sql.SQL('INSERT INTO {}.{} SELECT * FROM {}.{}').format(
                        sql.Identifier(scratch),
                        sql.Identifier(table),
                        sql.Identifier(args.schema),
                        sql.Identifier(table),
                    )
                )
        expected = {table: fingerprint(connection, scratch, table) for table in TABLES}
    try:
        folder = backup(conninfo, args.output, scratch)
        with psycopg.connect(conninfo) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(scratch)))
        env = libpq_env(conninfo)
        subprocess.run(
            [
                'pg_restore',
                '-w',
                '--exit-on-error',
                '--no-owner',
                '--no-privileges',
                '--dbname',
                env['PGDATABASE'],
                str(folder / 'bridge.dump'),
            ],
            env=env,
            check=True,
            timeout=300,
            capture_output=True,
        )
        with psycopg.connect(conninfo) as connection:
            restored = {table: fingerprint(connection, scratch, table) for table in TABLES}
        mismatched = sorted(table for table in TABLES if restored[table] != expected[table])
        assert not mismatched, f'restored tables differ: {mismatched}'
        manifest = json.loads((folder / 'manifest.json').read_text())
        manifest.update(
            full_restore_tested=True,
            copied_from=args.schema,
            verified_tables=len(TABLES),
            verified_rows=sum(item[0] for item in expected.values()),
        )
        (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(
            json.dumps(
                {
                    'restored': True,
                    'tables': len(TABLES),
                    'rows': manifest['verified_rows'],
                    'manifest': str(folder / 'manifest.json'),
                }
            )
        )
    finally:
        with psycopg.connect(conninfo) as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(scratch)))


if __name__ == '__main__':
    main()
