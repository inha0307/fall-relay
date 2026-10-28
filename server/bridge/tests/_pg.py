"""Disposable PostgreSQL schema for one check.

FALLRELAY_DATABASE_URL names a database in which the role may create schemas. A check creates a schema of its own,
initialised from migrations/postgres, and drops it at exit; nothing outside that schema is read or changed. Without the
variable the check fails, so a missing database is never reported as a pass.
"""

from _bridge_path import BRIDGE  # noqa: F401
import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from postgres.schema import initialize

ENV = 'FALLRELAY_TEST_DATABASE_URL'


def url():
    """The scoped connection string of the check's schema (inside `with Schema()`)."""
    return os.environ[ENV]


def conninfo():
    value = os.environ.get('FALLRELAY_DATABASE_URL')
    if not value:
        raise SystemExit(
            'no database: set FALLRELAY_DATABASE_URL to a PostgreSQL database in which this role may create schemas'
        )
    if 'options' in conninfo_to_dict(value):
        raise SystemExit('FALLRELAY_DATABASE_URL must not set options (search_path)')
    return value


class Schema:
    """`with Schema() as db:` gives db.url (scoped connection string); the schema is dropped on exit."""

    def __init__(self, prefix='test'):
        self.base = conninfo()
        self.name = prefix + '_' + uuid.uuid4().hex
        self.url = make_conninfo(self.base, options='-c search_path=' + self.name)

    def __enter__(self):
        with psycopg.connect(self.base) as connection:
            initialize(connection, self.name)
        os.environ[ENV] = self.url
        return self

    def __exit__(self, *error):
        os.environ.pop(ENV, None)
        with psycopg.connect(self.base) as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(self.name)))

    def connect(self):
        """A plain psycopg connection scoped to the schema, for direct queries in a check."""
        return psycopg.connect(self.url)

    def config(self, **overrides):
        """A bridge configuration for create_app that stores in this schema."""
        base = {'schema_version': 1, 'database_url_env': ENV}
        base.update(overrides)
        return base
