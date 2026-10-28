"""Checksummed SQL scripts for the device outbox (migrations/edge). Shared by the edge bundle and the server."""

import hashlib
from pathlib import Path
import sqlite3


def scripts(directory):
    result = []
    for path in sorted(Path(directory).glob('[0-9][0-9][0-9][0-9]_*.sql')):
        version = int(path.name.split('_')[0])
        raw = path.read_bytes()
        result.append((version, path.stem, hashlib.sha256(raw).hexdigest(), raw.decode('utf-8')))
    if [item[0] for item in result] != list(range(1, len(result) + 1)):
        raise ValueError('migration versions must be contiguous from 1')
    return result


def statements(sql):
    pending = ''
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise ValueError('incomplete migration SQL')
