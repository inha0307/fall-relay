"""Private pg_dump custom archive of the bridge schema and the web application's account schema."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from psycopg.conninfo import conninfo_to_dict
from postgres.common_env import database_url


def libpq_env(conninfo):
    names = {
        'host': 'PGHOST',
        'hostaddr': 'PGHOSTADDR',
        'port': 'PGPORT',
        'dbname': 'PGDATABASE',
        'user': 'PGUSER',
        'password': 'PGPASSWORD',
        'passfile': 'PGPASSFILE',
        'options': 'PGOPTIONS',
        'application_name': 'PGAPPNAME',
        'connect_timeout': 'PGCONNECT_TIMEOUT',
        'sslmode': 'PGSSLMODE',
        'sslrootcert': 'PGSSLROOTCERT',
        'sslcert': 'PGSSLCERT',
        'sslkey': 'PGSSLKEY',
        'target_session_attrs': 'PGTARGETSESSIONATTRS',
        'channel_binding': 'PGCHANNELBINDING',
        'gssencmode': 'PGGSSENCMODE',
        'service': 'PGSERVICE',
    }
    settings = conninfo_to_dict(conninfo)
    if set(settings) - set(names):
        raise ValueError('unsupported backup connection options: ' + ','.join(sorted(set(settings) - set(names))))
    env = {k: v for k, v in os.environ.items() if k not in names.values()}
    env['PGCONNECT_TIMEOUT'] = '5'
    env.update({names[key]: value for key, value in settings.items()})
    return env


def schema_exists(conninfo, name):
    import psycopg

    with psycopg.connect(conninfo) as connection:
        return bool(connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (name,)).fetchone())


def backup(conninfo, root, schema='public', extra_schemas=()):
    """Dump `schema` and every existing schema in `extra_schemas` (missing ones are skipped) into one archive."""
    schemas = [schema, *extra_schemas]
    if any(not re.fullmatch('[a-z][a-z0-9_]{0,62}', name) for name in schemas):
        raise ValueError('invalid schema')
    schemas = [name for name in schemas if name == schema or schema_exists(conninfo, name)]
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = root / ('pg-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    archive = folder / 'bridge.dump.part'
    archive.touch(mode=0o600)
    env = libpq_env(conninfo)
    subprocess.run(
        [
            'pg_dump',
            '-w',
            '-Fc',
            '--no-owner',
            '--no-privileges',
            *[f'--schema={name}' for name in schemas],
            '--file',
            str(archive),
        ],
        env=env,
        check=True,
        timeout=300,
        capture_output=True,
    )
    subprocess.run(['pg_restore', '--list', str(archive)], check=True, timeout=30, capture_output=True)
    with archive.open('rb') as handle:
        os.fsync(handle.fileno())
        digest = hashlib.file_digest(handle, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else None
    if digest is None:
        digest = hashlib.sha256()
        with archive.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        digest = digest.hexdigest()
    final = folder / 'bridge.dump'
    archive.rename(final)
    manifest = {
        'format': 1,
        'backend': 'postgresql',
        'created_at': time.time(),
        'schema': schema,
        'schemas': schemas,
        'archive': 'bridge.dump',
        'bytes': final.stat().st_size,
        'sha256': digest,
        'cross_database_atomic': False,
        'archive_directory_checked': True,
        'full_restore_tested': False,
    }
    path = folder / 'manifest.json'
    path.touch(mode=0o600)
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    return folder


def prune(root, days):
    """Remove only this tool's old backup directories with recognized contents."""
    cutoff = time.time() - days * 86400
    known = {'bridge.dump', 'bridge.dump.part', 'manifest.json'}
    for folder in Path(root).iterdir():
        if not re.fullmatch(r'pg-\d{8}T\d{6}Z-[a-f0-9]{8}', folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        if folder.stat().st_mtime >= cutoff or folder.stat().st_uid != os.getuid():
            continue
        entries = list(folder.iterdir())
        if any(p.name not in known or p.is_symlink() or not p.is_file() for p in entries):
            continue
        for path in entries:
            path.unlink()
        folder.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--schema', default='public')
    parser.add_argument(
        '--app-schema', default='app', help='account schema of the web application; skipped when absent'
    )
    parser.add_argument('--retention-days', type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.retention_days <= 3650:
        parser.error('retention must be 1-3650 days')
    print(backup(database_url(), args.output, args.schema, (args.app_schema,) if args.app_schema else ()))
    prune(args.output, args.retention_days)


if __name__ == '__main__':
    main()
