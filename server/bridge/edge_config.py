"""Load and validate the edge runtime's device configuration (edge_config.json, one file per camera).

The model files and the decision rule come from the bundle (bundle_manifest.json), not from this file. Relative
paths are resolved from the file's folder. Camera fields that are missing take their defaults: CSI port 0, the
widest sensor mode, 4 pose threads and the system dist-packages for Picamera2.
"""

import json
import re
from pathlib import Path

# Pose-model input contract: the models were trained on 640x360 pose coordinates.
POSE_INPUT_WIDTH = 640
POSE_INPUT_HEIGHT = 360

_CAMERA_ID = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
DEFAULTS = {
    'camera_index': 0,
    'sensor_mode': 'full',
    'pose_threads': 4,
    'extra_python_paths': ['/usr/lib/python3/dist-packages'],
}
REQUIRED = (
    'server_url',
    'camera_id',
    'capture_fps',
    'width',
    'height',
    'stream_jpeg_quality',
    'inference_hz',
    'pose_model_dir',
)
# Settings of the earlier runtime. The model files and the decision rule now come from the bundle.
RETIRED = {
    'edge_code_root',
    'precision_code_root',
    'lpn_model_path',
    'tcn_model_path',
    'model_manifest_path',
    'window_slots',
    'min_valid_pose_slots',
    'fall_threshold',
    'fall_confirm_slots',
    'duration_seconds',
    'edge_event_enabled',
    'decision_overrides',
    'outbox_path',
    'camera_roll_deg',
}


def load_edge_config(path):
    path = Path(path)
    config = json.loads(path.read_text(encoding='utf-8'))
    if config.get('schema_version') not in (1, 2):
        raise ValueError('Unsupported edge config schema_version')
    retired = sorted(set(config) & RETIRED)
    if retired:
        raise ValueError(f'edge config keys no longer supported: {retired} (see edge/deploy/README.md)')
    for key, value in DEFAULTS.items():
        config.setdefault(key, value)
    missing = [key for key in REQUIRED if key not in config]
    if missing:
        raise ValueError(f'Missing edge config keys: {", ".join(missing)}')

    config['server_url'] = str(config['server_url']).rstrip('/')
    config['camera_id'] = str(config['camera_id'])
    pose_dir = Path(config['pose_model_dir']).expanduser()
    config['pose_model_dir'] = str((pose_dir if pose_dir.is_absolute() else path.resolve().parent / pose_dir).resolve())
    for key in ('capture_fps', 'inference_hz'):
        config[key] = float(config[key])
    for key in ('width', 'height', 'stream_jpeg_quality', 'camera_index', 'pose_threads'):
        config[key] = int(config[key])
    config['sensor_mode'] = str(config['sensor_mode'])
    config['extra_python_paths'] = [str(Path(p).expanduser()) for p in config['extra_python_paths']]

    if not config['server_url']:
        raise ValueError('server_url must not be empty')
    if not _CAMERA_ID.match(config['camera_id']):
        raise ValueError('camera_id must be 1-32 letters, digits, _ or - and match the server camera list')
    if config['camera_index'] < 0:
        raise ValueError('camera_index must be non-negative (0 or 1 for the Pi 5 CSI ports)')
    if config['sensor_mode'] not in ('full', 'fast'):
        raise ValueError("sensor_mode must be 'full' or 'fast'")
    if config['pose_threads'] < 1:
        raise ValueError('pose_threads must be positive')
    if config['capture_fps'] <= 0 or config['inference_hz'] <= 0:
        raise ValueError('frame rates must be positive')
    if config['width'] <= 0 or config['height'] <= 0:
        raise ValueError('camera dimensions must be positive')
    if config['width'] * POSE_INPUT_HEIGHT != config['height'] * POSE_INPUT_WIDTH:
        raise ValueError('width:height must be 16:9 so frames resize to the 640x360 pose contract without distortion')
    if not 1 <= config['stream_jpeg_quality'] <= 100:
        raise ValueError('stream_jpeg_quality must be in [1, 100]')
    if 'camera_tuning_file' in config and not isinstance(config['camera_tuning_file'], str):
        raise ValueError('camera_tuning_file must be a path')
    if 'event_clip' in config and not isinstance(config['event_clip'], dict):
        raise ValueError('event_clip must be an object')
    return config


def select_sensor_mode(modes, sensor_mode):
    """Pick a Picamera2 sensor mode without assuming a specific sensor.

    'full' keeps the widest field of view (largest crop area), 'fast' the highest frame rate.
    Ties are broken by frame rate, then output size.
    """
    if not modes:
        raise RuntimeError('Camera reports no sensor modes')

    def crop_area(mode):
        limits = mode.get('crop_limits') or (0, 0, *mode['size'])
        return int(limits[2]) * int(limits[3])

    if sensor_mode == 'full':
        widest = max(crop_area(m) for m in modes)
        candidates = [m for m in modes if crop_area(m) == widest]
    elif sensor_mode == 'fast':
        candidates = list(modes)
    else:
        raise ValueError(f'Unknown sensor_mode: {sensor_mode}')
    return max(candidates, key=lambda m: (float(m['fps']), int(m['size'][0]) * int(m['size'][1])))


def pose_to_frame_scale(config):
    """Scale factors that map 640x360 pose coordinates back to the streamed frame."""
    return config['width'] / POSE_INPUT_WIDTH, config['height'] / POSE_INPUT_HEIGHT
