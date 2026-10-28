#!/usr/bin/env python3
"""Unit checks for edge_config: defaults, validation, retired settings, camera helpers."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import json
import sys
import tempfile
from pathlib import Path

HERE = BRIDGE
sys.path.insert(0, str(HERE))
from edge_config import load_edge_config, pose_to_frame_scale, select_sensor_mode  # noqa: E402


def write(config):
    handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, dir=HERE)
    json.dump(config, handle)
    handle.close()
    return Path(handle.name)


def expect_error(config, fragment):
    path = write(config)
    try:
        load_edge_config(path)
    except ValueError as exc:
        assert fragment in str(exc), (fragment, str(exc))
        print(f'PASS rejects: {fragment}')
    else:
        raise AssertionError(f'expected error containing {fragment!r}')
    finally:
        path.unlink()


EXAMPLE = BRIDGE.parents[1] / 'edge' / 'deploy' / 'edge_config.example.json'
base = json.loads(EXAMPLE.read_text(encoding='utf-8'))
cfg = load_edge_config(EXAMPLE)
assert cfg['camera_index'] == 0 and cfg['pose_threads'] == 4 and cfg['sensor_mode'] == 'full'
assert Path(cfg['pose_model_dir']).is_absolute() and cfg['pose_model_dir'].endswith('yolo26n-pose_ncnn_model')
print('PASS shipped example config loads; pose model folder resolved from the config folder')

second = write({**base, 'camera_id': '2', 'camera_index': 1, 'pose_threads': 2})
try:
    cam2 = load_edge_config(second)
finally:
    second.unlink()
assert cam2['camera_id'] == '2' and cam2['camera_index'] == 1 and cam2['pose_threads'] == 2
print('PASS second camera on the other CSI port')

minimal = {k: v for k, v in base.items() if k not in ('camera_index', 'sensor_mode', 'pose_threads')}
minimal['schema_version'] = 1
path = write(minimal)
try:
    old = load_edge_config(path)
finally:
    path.unlink()
assert old['camera_index'] == 0 and old['pose_threads'] == 4
assert old['extra_python_paths'] == ['/usr/lib/python3/dist-packages']
print('PASS missing camera fields take their defaults')

expect_error({**base, 'width': 1280, 'height': 960}, '16:9')
expect_error({**base, 'camera_id': 'cam 1'}, 'camera_id')
expect_error({**base, 'camera_index': -1}, 'camera_index')
expect_error({**base, 'sensor_mode': 'wide'}, 'sensor_mode')
expect_error({**base, 'schema_version': 3}, 'schema_version')
expect_error({**base, 'fall_threshold': 0.932, 'lpn_model_path': 'x.tflite'}, 'no longer supported')
missing = dict(base)
del missing['pose_model_dir']
expect_error(missing, 'pose_model_dir')

imx708 = [
    {'size': (1536, 864), 'fps': 120.13, 'crop_limits': (768, 432, 3072, 1728)},
    {'size': (2304, 1296), 'fps': 56.03, 'crop_limits': (0, 0, 4608, 2592)},
    {'size': (4608, 2592), 'fps': 14.35, 'crop_limits': (0, 0, 4608, 2592)},
]
assert select_sensor_mode(imx708, 'full')['size'] == (2304, 1296)
assert select_sensor_mode(imx708, 'fast')['size'] == (1536, 864)
other_sensor = [{'size': (1920, 1080), 'fps': 30.0, 'crop_limits': (0, 0, 1920, 1080)}]
assert select_sensor_mode(other_sensor, 'full')['size'] == (1920, 1080)
print('PASS sensor-mode selection (IMX708 matches former full-field rule; other sensors work)')

assert pose_to_frame_scale({'width': 1280, 'height': 720}) == (2.0, 2.0)
assert pose_to_frame_scale({'width': 1920, 'height': 1080}) == (3.0, 3.0)
print('PASS bbox scale follows stream resolution (former fixed x2 only for 1280x720)')
print('All edge_config checks passed.')
