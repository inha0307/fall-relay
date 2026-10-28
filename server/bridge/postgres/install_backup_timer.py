"""Install a daily user timer; preserves preexisting unit definitions as artifacts."""

import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
UNITS = Path.home() / '.config/systemd/user'
HERE = ROOT / 'server' / 'bridge'


def main():
    folder = ROOT / 'artifacts/postgres_migration' / ('backup-units-' + time.strftime('%Y%m%dT%H%M%S'))
    folder.mkdir(mode=0o700)
    service = f'''[Unit]
Description=fall-relay PostgreSQL backup
After={os.environ.get('FALLRELAY_SERVER_UNIT', 'fall-relay-server.service')}

[Service]
Type=oneshot
WorkingDirectory={HERE}
EnvironmentFile={HERE}/deploy.env
UMask=0077
NoNewPrivileges=true
ExecStart={ROOT}/server/.venv/bin/python -m postgres.backup --output {ROOT}/artifacts/postgres_backups --retention-days 30
'''
    timer = '''[Unit]
Description=Daily fall-relay database backup

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=5m
Persistent=true
Unit=fall-relay-db-backup.service

[Install]
WantedBy=timers.target
'''
    UNITS.mkdir(parents=True, exist_ok=True)
    for name, content in [('fall-relay-db-backup.service', service), ('fall-relay-db-backup.timer', timer)]:
        path = UNITS / name
        if path.exists():
            shutil.copy2(path, folder / name)
        path.write_text(content)
        (folder / (name + '.installed')).write_text(content)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'fall-relay-db-backup.timer'], check=True)
    subprocess.run(['systemctl', '--user', 'start', 'fall-relay-db-backup.service'], check=True)
    print('Installed and ran backup timer; unit copies:', folder)


if __name__ == '__main__':
    main()
