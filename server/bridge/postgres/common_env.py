"""Connection string for the PostgreSQL tools (FALLRELAY_DATABASE_URL)."""

import os


def database_url(required=True):
    value = os.environ.get('FALLRELAY_DATABASE_URL')
    if required and not value:
        raise SystemExit('set FALLRELAY_DATABASE_URL (a libpq connection string with a private passfile)')
    return value
