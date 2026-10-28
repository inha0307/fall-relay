"""Verify the local fall-relay login and save a private libpq password file.

Run interactively as the same Ubuntu user that runs the bridge service.
The password is never printed or passed as a command-line argument.
"""

import getpass
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    target = Path(__file__).resolve().parents[1] / '.postgres.pgpass'
    if target.exists() or target.is_symlink():
        raise SystemExit('Credential file already exists; refusing to overwrite it.')
    password = getpass.getpass('PostgreSQL password for fall-relay: ')
    if not password or '\n' in password or '\r' in password:
        raise SystemExit('A nonempty, single-line password is required.')
    env = {**os.environ, 'PGPASSWORD': password, 'PGCONNECT_TIMEOUT': '5'}
    result = subprocess.run(
        [
            'psql',
            '-X',
            '-w',
            '-h',
            '127.0.0.1',
            '-p',
            '5432',
            '-U',
            'fall-relay',
            '-d',
            'fall-relay',
            '-Atqc',
            "SELECT current_database(), current_user, current_setting('server_version');",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise SystemExit('Database login failed. No credential file was saved.')
    escaped = password.replace('\\', '\\\\').replace(':', '\\:')
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.postgres-', dir=target.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write('127.0.0.1:5432:fall-relay:fall-relay:' + escaped + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic create without replacing any credentials created concurrently.
        os.link(temporary, target)
    finally:
        if temporary:
            os.unlink(temporary)
    print('Login verified:', result.stdout.strip())
    print('Private PGPASSFILE saved:', target)
    print('The running application database has not been switched.')


if __name__ == '__main__':
    main()
