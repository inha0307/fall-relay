"""Administrator commands for edge devices, delivered by the device itself (pull).

An administrator queues one allow-listed command for a device. The device's device agent picks it up in
the reply to its next telemetry post, runs it locally and posts the result. The server never connects to a
device. One command per device at a time; a command nobody picks up expires, and one that never reports
back fails, so a device that froze or rebooted cannot leave a command hanging.
"""

import time
import json
from decision_log_runtime import options as log_options
from db_backend import lock_key

COMMANDS = (
    'restart_inference',
    'stop_inference',
    'start_inference',
    'logs',
    'reboot',
    'decision_log_start',
    'decision_log_stop',
    'decision_log_collect',
)
PICKUP_S = 60  # queued -> expired when the device does not ask for it in time
RESULT_S = 120  # sent -> failed when no result arrives
MAX_RESULT = 65536  # characters kept from a command's output (logs)


class CommandError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class DeviceCommandStore:
    def __init__(self, events):
        self.db, self.lock = events._db, events._lock

    def _age_out(self, now, device_id):
        self.db.execute(
            "UPDATE device_commands SET state='expired',finished_at=?,result='not picked up by the device' "
            "WHERE state='queued' AND requested_at<? AND device_id=?",
            (now, now - PICKUP_S, device_id),
        )
        self.db.execute(
            "UPDATE device_commands SET state='failed',finished_at=?,result='no result from the device' "
            "WHERE state='sent' AND sent_at<? AND device_id=?",
            (now, now - RESULT_S, device_id),
        )

    def request(
        self, device_id, camera_id, command, actor, allowed, open_event=False, confirmed=False, now=None, params=None
    ):
        now = time.time() if now is None else now
        if command not in COMMANDS:
            raise CommandError(422, 'unknown command')
        if command not in allowed:
            raise CommandError(403, 'this device does not accept that command', 'not_allowed')
        params = {} if params is None else params
        try:
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            if command == 'decision_log_start':
                params = log_options(params)
            elif command == 'decision_log_stop':
                if set(params) - {'collect'} or type(params.get('collect', False)) is not bool:
                    raise ValueError('collect must be boolean')
            elif params:
                raise ValueError('this command does not accept parameters')
        except ValueError as exc:
            raise CommandError(422, str(exc)) from exc
        if open_event and command in ('restart_inference', 'stop_inference', 'reboot') and not confirmed:
            raise CommandError(409, 'a fall event is open on this camera; confirm to interrupt it', 'open_fall_event')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                lock_key(self.db, 'device-command:' + device_id)
                self._age_out(now, device_id)
                if self.db.execute(
                    "SELECT 1 FROM device_commands WHERE device_id=? AND state IN ('queued','sent')", (device_id,)
                ).fetchone():
                    raise CommandError(409, 'another command for this device is still running', 'busy')
                id = self.db.execute(
                    "INSERT INTO device_commands(device_id,camera_id,command,requested_by,requested_at,state,params_json) VALUES (?,?,?,?,?,'queued',?)",
                    (device_id, camera_id, command, actor, now, json.dumps(params)),
                ).lastrowid
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return self._row(id)

    def take(self, device_id, now=None):
        """Commands handed to the device in its telemetry reply (at most one)."""
        now = time.time() if now is None else now
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                lock_key(self.db, 'device-command:' + device_id)
                self._age_out(now, device_id)
                rows = self.db.execute(
                    "SELECT id,command,params_json,requested_at FROM device_commands WHERE device_id=? AND state='queued' ORDER BY id LIMIT 1",
                    (device_id,),
                ).fetchall()
                for row in rows:
                    self.db.execute("UPDATE device_commands SET state='sent',sent_at=? WHERE id=?", (now, row['id']))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return [
            {
                'id': row['id'],
                'command': row['command'],
                'params': json.loads(row['params_json']),
                'expires_at': row['requested_at'] + PICKUP_S,
            }
            for row in rows
        ]

    def complete(self, id, device_id, ok, output, now=None):
        now = time.time() if now is None else now
        if not isinstance(ok, bool) or not isinstance(output, str):
            raise CommandError(422, 'ok (bool) and output (text) required')
        with self.lock:
            lock_key(self.db, 'device-command:' + device_id)
            row = self.db.execute('SELECT device_id,state FROM device_commands WHERE id=?', (id,)).fetchone()
            if row is None or row['device_id'] != device_id:
                raise CommandError(404, 'command not found for this device')
            if row['state'] != 'sent':
                raise CommandError(409, f"command is {row['state']}")
            self.db.execute(
                'UPDATE device_commands SET state=?,finished_at=?,result=? WHERE id=?',
                ('done' if ok else 'failed', now, output.replace('\x00', '')[-MAX_RESULT:], id),
            )
            self.db.commit()
            return self._row(id)

    def listing(self, device_id, limit=20, now=None):
        now = time.time() if now is None else now
        with self.lock:
            lock_key(self.db, 'device-command:' + device_id)
            self._age_out(now, device_id)
            self.db.commit()
            rows = self.db.execute(
                'SELECT * FROM device_commands WHERE device_id=? ORDER BY id DESC LIMIT ?', (device_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def _row(self, id):
        return dict(self.db.execute('SELECT * FROM device_commands WHERE id=?', (id,)).fetchone())
