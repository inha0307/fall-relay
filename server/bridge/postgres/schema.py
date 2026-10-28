"""Bridge schema in PostgreSQL: versioned DDL scripts, the migration ledger and the startup check.

Scripts live in migrations/postgres/NNNN_<name>.sql, contiguous from 0001_baseline.sql. A fresh schema is
initialised by applying every script in one transaction; a running bridge only verifies that the ledger
matches the scripts and refuses to start when a migration is pending (apply it offline with upgrade.py).
"""

import hashlib
import re
import time
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parents[1] / 'migrations' / 'postgres'
LEDGER = (
    'CREATE TABLE IF NOT EXISTS schema_migrations ('
    'version bigint PRIMARY KEY, description text NOT NULL, applied_at double precision NOT NULL, checksum text NOT NULL)'
)
NAME = re.compile('[a-z][a-z0-9_]{0,62}')


def scripts(directory=DIRECTORY):
    result = []
    for path in sorted(Path(directory).glob('[0-9][0-9][0-9][0-9]_*.sql')):
        raw = path.read_bytes()
        result.append((int(path.name.split('_')[0]), path.stem, hashlib.sha256(raw).hexdigest(), raw.decode('utf-8')))
    if [item[0] for item in result] != list(range(1, len(result) + 1)):
        raise ValueError('migration versions must be contiguous from 1')
    return result


def applied(execute):
    """Ledger rows as {version: checksum}; `execute` runs one SQL string and returns a cursor-like object."""
    rows = execute('SELECT version,checksum FROM schema_migrations').fetchall()
    result = {int(row[0]): row[1] for row in rows}
    if sorted(result) != list(range(1, len(result) + 1)):
        raise ValueError('migration ledger has gaps')
    return result


def pending(execute, directory=DIRECTORY):
    available = scripts(directory)
    known = {version: checksum for version, _, checksum, _ in available}
    done = applied(execute)
    for version, checksum in done.items():
        if known.get(version) != checksum:
            raise ValueError(f'migration {version} checksum mismatch or newer database; refusing')
    return [item for item in available if item[0] not in done]


def ledger_exists(execute):
    return bool(
        execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='schema_migrations'"
        ).fetchone()
    )


def check(db, directory=DIRECTORY):
    """Startup check through the pool facade: the schema must be initialised and fully migrated."""
    if not ledger_exists(db.execute):
        raise ValueError('PostgreSQL schema is not initialised; run python -m postgres.bootstrap')
    if pending(db.execute, directory):
        raise ValueError('PostgreSQL schema requires an offline migration before startup')
    return sorted(applied(db.execute))


def initialize(connection, schema, directory=DIRECTORY):
    """Create `schema` (when missing and empty) in the database of a psycopg connection and apply every script."""
    from psycopg import sql

    if not NAME.fullmatch(schema):
        raise ValueError('invalid schema name')
    with connection.transaction():
        if connection.execute(
            'SELECT 1 FROM information_schema.tables WHERE table_schema=%s LIMIT 1', (schema,)
        ).fetchone():
            raise ValueError(f'schema {schema} already holds tables')
        connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
        connection.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        connection.execute(LEDGER)
        for version, name, checksum, text in scripts(directory):
            connection.execute(text)
            connection.execute(
                'INSERT INTO schema_migrations VALUES (%s,%s,%s,%s)', (version, name, time.time(), checksum)
            )
    return [item[0] for item in scripts(directory)]


def baseline_columns(directory=DIRECTORY):
    """{table: {column, ...}} declared by the baseline script, for adopting a database created before it."""
    text = scripts(directory)[0][3]
    result = {}
    for table, body in re.findall(r'CREATE TABLE (\w+) \((.*?)\n\);', text, re.S):
        columns = set()
        for line in body.splitlines():
            word = line.strip().split(' ')[0].rstrip(',')
            keywords = ('CHECK', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'CONSTRAINT', 'AND', 'OR')
            if word and word[0].isalpha() and word.upper() not in keywords:
                columns.add(word)
        result[table] = columns
    return result


def adopt_baseline(connection, schema, directory=DIRECTORY):
    """Replace a legacy ledger with version 1 when the live schema already has every baseline column.

    For databases that were initialised by the SQLite import path before the baseline script existed.
    """
    from psycopg import sql

    if not NAME.fullmatch(schema):
        raise ValueError('invalid schema name')
    expected = baseline_columns(directory)
    with connection.transaction():
        connection.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        missing = {}
        for table, columns in expected.items():
            present = {
                row[0]
                for row in connection.execute(
                    'SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s',
                    (schema, table),
                )
            }
            if not present:
                missing[table] = 'table'
            elif columns - present:
                missing[table] = sorted(columns - present)
        if missing:
            raise ValueError(f'live schema lacks baseline objects: {missing}')
        version, name, checksum, _ = scripts(directory)[0]
        connection.execute(LEDGER)
        connection.execute('DELETE FROM schema_migrations')
        connection.execute('INSERT INTO schema_migrations VALUES (%s,%s,%s,%s)', (version, name, time.time(), checksum))
    return {'schema': schema, 'tables': sorted(expected), 'version': version}
