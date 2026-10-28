"""Initialise an empty bridge schema in PostgreSQL from migrations/postgres (no operational data)."""

import argparse
import json
from pathlib import Path

import psycopg
from postgres.common_env import database_url
from postgres.schema import initialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', default='public', help='created when missing; must hold no tables')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    with psycopg.connect(database_url()) as connection:
        versions = initialize(connection, args.schema)
    result = {'schema': args.schema, 'versions': versions}
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print('Initialized empty PostgreSQL schema:', args.schema, 'versions', versions)


if __name__ == '__main__':
    main()
