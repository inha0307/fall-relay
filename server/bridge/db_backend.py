"""PostgreSQL pool facade for the stores' small DB-API surface.

Store SQL uses qmark placeholders, converted here. A scope leases one connection per thread,
including nested store calls; different requests do not share transactions.
"""

import hashlib
import re
import threading
from decimal import Decimal


class Row(tuple):
    def __new__(cls, values, names):
        item = super().__new__(cls, values)
        item.names = names
        return item

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return super().__getitem__(self.names.index(key) if isinstance(key, str) else key)


class Result:
    def __init__(self, rows=(), rowcount=0, lastrowid=None):
        self.rows = iter(rows)
        self.rowcount, self.lastrowid = rowcount, lastrowid

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return self.rows


def parameters(sql):
    """Convert qmark placeholders, preserving quoted SQL literals/identifiers."""
    out, quote, i = [], None, 0
    while i < len(sql):
        char = sql[i]
        if quote:
            out.append('%%' if char == '%' else char)
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(quote)
                    i += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        else:
            out.append('%s' if char == '?' else '%%' if char == '%' else char)
        i += 1
    return ''.join(out)


class Lease:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        state = self.database.local
        if not getattr(state, 'depth', 0):
            state.context = self.database.pool.connection(timeout=10)
            state.connection = state.context.__enter__()
        state.depth = getattr(state, 'depth', 0) + 1
        return self

    def __exit__(self, *error):
        state = self.database.local
        state.depth -= 1
        if not state.depth:
            try:
                return state.context.__exit__(*error)
            finally:
                state.connection = None


class PostgresDatabase:
    dialect = 'postgresql'
    # Only tables whose generated identifiers are used by current store methods.
    generated = {
        'events',
        'alerts',
        'alert_log',
        'alert_deliveries',
        'push_subscriptions',
        'device_commands',
        'hardware_samples',
        'decision_log_audit',
    }

    def __init__(self, conninfo):
        from psycopg_pool import ConnectionPool

        self.local = threading.local()

        def configure(conn):
            conn.execute("SET application_name = 'fall-relay-bridge'")
            conn.execute("SET synchronous_commit = on")
            conn.execute("SET statement_timeout = '30s'")
            conn.execute("SET lock_timeout = '10s'")
            conn.execute("SET idle_in_transaction_session_timeout = '60s'")
            conn.execute("SET TIME ZONE 'Asia/Seoul'")
            conn.commit()

        self.pool = ConnectionPool(
            conninfo, min_size=1, max_size=8, open=False, configure=configure, name='fall-relay', timeout=10
        )
        self.scope = Lease(self)
        self.pool.open()
        try:
            self.pool.wait(timeout=10)
        except Exception:
            self.pool.close()
            raise

    def execute(self, sql, params=()):
        if not getattr(self.local, 'depth', 0):
            with self.scope:
                return self.execute(sql, params)
        conn = self.local.connection
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            from psycopg.pq import TransactionStatus

            if conn.info.transaction_status == TransactionStatus.IDLE:
                conn.execute('BEGIN')
            return Result()
        match = re.match(r'\s*INSERT\s+INTO\s+(\w+)\b', sql, re.I)
        generated = bool(match and match[1].lower() in self.generated and 'RETURNING' not in sql.upper())
        if generated:
            sql = sql.rstrip().rstrip(';') + ' RETURNING id'
        with conn.cursor() as cursor:
            cursor.execute(parameters(sql) if params else sql, params if params else None)
            names = tuple(col.name for col in cursor.description) if cursor.description else ()
            rows = (
                [
                    Row(
                        tuple(
                            int(v)
                            if isinstance(v, Decimal) and v == v.to_integral_value()
                            else float(v)
                            if isinstance(v, Decimal)
                            else v
                            for v in row
                        ),
                        names,
                    )
                    for row in cursor.fetchall()
                ]
                if names
                else []
            )
            return Result(rows, cursor.rowcount, rows[0][0] if generated and rows else None)

    def executemany(self, sql, params):
        if not getattr(self.local, 'depth', 0):
            with self.scope:
                return self.executemany(sql, params)
        with self.local.connection.cursor() as cursor:
            cursor.executemany(parameters(sql), params)
            return Result(rowcount=cursor.rowcount)

    def commit(self):
        if getattr(self.local, 'connection', None):
            self.local.connection.commit()

    def rollback(self):
        if getattr(self.local, 'connection', None):
            self.local.connection.rollback()

    def close(self):
        self.pool.close()

    def __enter__(self):
        return self.scope.__enter__()

    def __exit__(self, *error):
        if error[0]:
            self.rollback()
        else:
            self.commit()
        return self.scope.__exit__(*error)


def has_table(db, name):
    return bool(
        db.execute(
            'SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?', (name,)
        ).fetchone()
    )


def lock_key(db, key):
    """Transaction-scoped advisory lock, including identities that do not have a row yet."""
    number = int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), 'big', signed=True)
    db.execute('SELECT pg_advisory_xact_lock(?)', (number,))
