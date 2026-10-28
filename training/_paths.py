"""Locations shared by the training scripts.

Importing this module also puts ../edge on sys.path, so training uses the exact
runtime modules the Raspberry Pi runs (features_v2, head_point, pose17, ...).

Environment variables:
  FALLRELAY_NTU_RGB        folder with nturgbd_rgb_s001.zip ... nturgbd_rgb_s017.zip
  FALLRELAY_NTU_SKELETONS  folder with the NTU RGB+D *.skeleton files
  FALLRELAY_WORK           folder for derived data and training runs (default ~/fall-relay-work)
  FALLRELAY_POSE_MODEL     folder with the YOLO26n-pose NCNN export (model.ncnn.param/.bin)
"""

import csv
import hashlib
import os
import sys
from pathlib import Path

TRAINING = Path(__file__).resolve().parent
EDGE = TRAINING.parent / "edge"
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))


def _env(name, default=None):
    value = os.environ.get(name)
    if value:
        return Path(value).expanduser()
    if default is None:
        return Path(f"<set {name}>")
    return Path(default).expanduser()


NTU_RGB = _env("FALLRELAY_NTU_RGB")
NTU_SKELETONS = _env("FALLRELAY_NTU_SKELETONS")
WORK = _env("FALLRELAY_WORK", "~/fall-relay-work")
POSE_MODEL = _env("FALLRELAY_POSE_MODEL", WORK / "yolo26n-pose_ncnn_model")

YOLO17 = WORK / "yolo17_v2"  # extract_yolo17_v2.py
TRACKING = WORK / "tracking_v2"  # build_tracking_v2.py
KINECT = WORK / "kinect_v2"  # build_kinect_cache_v2.py
LPN_DATA = WORK / "lpn_v2"  # build_lpn_pairs_v2.py
LPN_RUNS = WORK / "lpn_runs"  # train_lpn_v2.py
FEATURES = WORK / "features_v2"  # build_features_v2.py
TCN_RUNS = WORK / "tcn_runs"  # train_tcn_v2.py


def source(name):
    """Path of a module used by training, whether it lives here or in edge."""
    here = TRAINING / name
    return here if here.exists() else EDGE / name


def manifest_fingerprint(path):
    """SHA-256 over the location-independent manifest columns.

    The manifest stores absolute archive and skeleton paths, which differ between
    machines; the fingerprint covers what defines the data and the split.
    """
    keys = (
        "sample_id",
        "capture_group",
        "split",
        "rgb_member",
        "rgb_bytes",
        "rgb_crc32",
        "eligible",
        "exclusion_reason",
    )
    with open(path, newline="") as f:
        rows = sorted("\t".join(r[k] for k in keys) for r in csv.DictReader(f))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()
