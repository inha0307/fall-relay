"""Load and validate the server-side video/result bridge configuration."""

import json
import math
import re
from pathlib import Path

_CAMERA_ID = re.compile(r'^[A-Za-z0-9_-]{1,32}$')


# Settings of earlier versions (server-inferred events, research capture, per-camera event allowlist).
RETIRED_KEYS = {
    'v2_features',
    'edge_event_camera_ids',
    'episode_gap_s',
    'research_data_dir',
    'match_before_s',
    'match_after_s',
    'session_video_limit_bytes',
    'session_video_segment_s',
}


def load_server_config(path):
    path = Path(path)
    config = json.loads(path.read_text(encoding='utf-8'))
    if config.get('schema_version') != 1:
        raise ValueError('Unsupported server config schema_version')
    retired = sorted(set(config) & RETIRED_KEYS)
    if retired:
        raise ValueError(f'server config keys no longer supported: {retired} (see server/bridge/README.md)')
    features = config.get('features', {})
    names = ('alerts', 'watchdog')
    if (
        not isinstance(features, dict)
        or set(features) - set(names)
        or any(not isinstance(value, bool) for value in features.values())
    ):
        raise ValueError('features must contain only the boolean flags alerts and watchdog')
    config['features'] = {name: features.get(name, False) for name in names}
    for key in ('host', 'port', 'cameras'):
        if key not in config:
            raise ValueError(f'Missing server config key: {key}')
    config['host'] = str(config['host'])
    config['port'] = int(config['port'])
    if not 1 <= config['port'] <= 65535:
        raise ValueError('port must be in [1, 65535]')
    config['cors_origins'] = [str(origin) for origin in config.get('cors_origins', [])]
    config['edge_stale_after_s'] = float(config.get('edge_stale_after_s', 3.0))
    config['video_stale_after_s'] = float(config.get('video_stale_after_s', 5.0))
    if config['edge_stale_after_s'] <= 0 or config['video_stale_after_s'] <= 0:
        raise ValueError('stale timeouts must be positive')
    clip_dir = config.get('clip_dir')
    if clip_dir:
        clip_path = Path(clip_dir).expanduser()
        config['clip_dir'] = str(
            clip_path if clip_path.is_absolute() else (path.resolve().parent / clip_path).resolve()
        )
    else:
        config['clip_dir'] = None
    # Fall clips: seconds kept before/after the event start, output frame rate, days kept on disk.
    config['clip_pre_s'] = float(config.get('clip_pre_s', 6.0))
    config['clip_post_s'] = float(config.get('clip_post_s', 4.0))
    config['clip_fps'] = float(config.get('clip_fps', 10.0))
    config['clip_retention_days'] = int(config.get('clip_retention_days', 30))
    if not (
        0 < config['clip_pre_s'] <= 60
        and 0 < config['clip_post_s'] <= 60
        and 1 <= config['clip_fps'] <= 30
        and config['clip_retention_days'] >= 1
    ):
        raise ValueError('clip_pre_s/clip_post_s must be in (0, 60], clip_fps in [1, 30], clip_retention_days >= 1')

    if 'database_backend' in config or 'db_path' in config:
        raise ValueError('database_backend and db_path are no longer accepted: the bridge stores in PostgreSQL only')
    # Name of the environment variable holding the libpq connection string of the bridge database.
    env_name = config.get('database_url_env', 'FALLRELAY_DATABASE_URL')
    if not isinstance(env_name, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', env_name):
        raise ValueError('invalid database_url_env')
    config['database_url_env'] = env_name
    # Folder for collected decision-log archives (files; their index is in the database).
    archive_dir = Path(str(config.get('archive_dir', 'decision_logs'))).expanduser()
    config['archive_dir'] = str(
        archive_dir if archive_dir.is_absolute() else (path.resolve().parent / archive_dir).resolve()
    )
    config['metrics_interval_s'] = float(config.get('metrics_interval_s', 10.0))
    config['metrics_retention_days'] = int(config.get('metrics_retention_days', 30))
    if config['metrics_interval_s'] <= 0 or config['metrics_retention_days'] < 1:
        raise ValueError('metrics_interval_s and metrics_retention_days must be positive')
    # Name of the environment variable holding the token that authorises event edits.
    config['write_token_env'] = str(config.get('write_token_env', 'FALLRELAY_WRITE_TOKEN'))
    # Reject pushes from cameras that have no token yet (cameras WITH a token always need it).
    config['require_camera_tokens'] = bool(config.get('require_camera_tokens', False))
    # Video and clips need a short-lived token from the signed-in web application (signed with the write token).
    config['require_media_token'] = bool(config.get('require_media_token', False))
    # Per-device profile choice and overrides for the Devices page (see device_profiles.py).
    from device_profiles import validate_config as validate_device_profiles

    config['device_profiles'] = validate_device_profiles(config.get('device_profiles'))
    config['hardware_retention_days'] = int(config.get('hardware_retention_days', 30))
    config['hardware_stale_after_s'] = float(config.get('hardware_stale_after_s', 20))
    if not 1 <= config['hardware_retention_days'] <= 365 or not 5 <= config['hardware_stale_after_s'] <= 900:
        raise ValueError('hardware retention must be 1-365 days; stale timeout 5-900 seconds')
    cameras = config['cameras']
    if not isinstance(cameras, list) or not cameras:
        raise ValueError('cameras must be a non-empty list')
    seen = set()
    normalized = []
    for camera in cameras:
        camera_id = str(camera.get('id', ''))
        if not _CAMERA_ID.match(camera_id):
            raise ValueError(f'Invalid camera id: {camera_id!r} (letters, digits, _ and -; up to 32 chars)')
        if camera_id in seen:
            raise ValueError(f'Duplicate camera id: {camera_id}')
        seen.add(camera_id)
        normalized.append(
            {
                'id': camera_id,
                'label': str(camera.get('label') or f'CAM {camera_id}'),
                'location': str(camera.get('location') or ''),
            }
        )
    config['cameras'] = normalized
    age = config.get('edge_event_max_live_age_s', 120)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 1 <= age <= 3600:
        raise ValueError('invalid edge_event_max_live_age_s')
    config['edge_event_max_live_age_s'] = float(age)
    return config
