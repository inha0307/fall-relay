"""Storage of fall events (written by edge_event_store.py), their review status and per-camera health metrics.

Stored in PostgreSQL through db_backend.py. No inference or thresholding happens here: events are the
explicit messages the device decided (docs/EVENT_PROTOCOL.md).
"""

from __future__ import annotations

from db_backend import PostgresDatabase
from postgres.schema import check as check_schema

# Evaluation labels a reviewer can give an episode ('new' = not reviewed yet).
FALSE_ALARM_LABELS = ('fa_sitting', 'fa_lying', 'fa_bending', 'fa_other')
REVIEW_STATUSES = ('new', 'fall', *FALSE_ALARM_LABELS, 'uncertain')
# Labels used before the research labels; accepted on input and migrated in stored data.


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


class EventStore:
    def __init__(self, database_url: str, metrics_retention_days: int = 30):
        self.metrics_retention_days = int(metrics_retention_days)
        self._db = PostgresDatabase(database_url)
        self._lock = self._db.scope
        try:
            with self._lock:
                self.schema_versions = check_schema(self._db)
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def has_open_event(self, camera_id: str) -> bool:
        """An explicit fall event on this camera that the device has not closed yet."""
        with self._lock:
            return (
                self._db.execute(
                    "SELECT 1 FROM events WHERE camera_id=? AND source='edge_event' AND ended_at IS NULL", (camera_id,)
                ).fetchone()
                is not None
            )

    def list_events(
        self,
        since: float | None = None,
        until: float | None = None,
        camera: str | None = None,
        review_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        where, params = [], []
        if since is not None:
            where.append('started_at >= ?')
            params.append(since)
        if until is not None:
            where.append('started_at < ?')
            params.append(until)
        if camera:
            where.append('camera_id = ?')
            params.append(camera)
        if review_status == 'false_alarm':  # any false-alarm type
            where.append(f"review_status IN ({','.join('?' * len(FALSE_ALARM_LABELS))})")
            params.extend(FALSE_ALARM_LABELS)
        elif review_status:
            where.append('review_status = ?')
            params.append(review_status)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        limit = max(1, min(int(limit), 500))
        with self._lock:
            total = self._db.execute(f'SELECT COUNT(*) FROM events {clause}', params).fetchone()[0]
            rows = self._db.execute(
                f'SELECT * FROM events {clause} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?',
                [*params, limit, max(0, int(offset))],
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event['ongoing'] = event['ended_at'] is None
            event['duration_s'] = round((event['ended_at'] or event['started_at']) - event['started_at'], 2)
            events.append(event)
        return {'total': total, 'events': events}

    def record_metrics(
        self,
        camera_id: str,
        now: float,
        video_connected: bool,
        edge_connected: bool,
        video_fps: float,
        edge_hz: float,
        latencies_ms: list[float],
        results: int,
        ineligible: int,
    ) -> None:
        with self._lock:
            self._db.execute(
                'INSERT INTO metrics(ts, camera_id, video_connected, edge_connected, video_fps, edge_hz, '
                'latency_p50_ms, latency_p95_ms, results, ineligible) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (
                    now,
                    camera_id,
                    int(video_connected),
                    int(edge_connected),
                    video_fps,
                    edge_hz,
                    _percentile(latencies_ms, 0.5),
                    _percentile(latencies_ms, 0.95),
                    results,
                    ineligible,
                ),
            )
            self._db.execute('DELETE FROM metrics WHERE ts < ?', (now - self.metrics_retention_days * 86400,))
            self._db.commit()

    def metrics(self, since: float, camera: str | None = None, limit: int = 2000) -> list[dict]:
        params: list = [since]
        clause = 'ts >= ?'
        if camera:
            clause += ' AND camera_id = ?'
            params.append(camera)
        with self._lock:
            rows = self._db.execute(
                f'SELECT * FROM metrics WHERE {clause} ORDER BY ts DESC LIMIT ?',
                [*params, max(1, min(int(limit), 10000))],
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
