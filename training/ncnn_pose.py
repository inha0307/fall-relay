"""Deployment-compatible recorded-frame NCNN pose preprocessing and selection."""

import os, sys
from pathlib import Path
import _paths  # noqa: E402

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ['OMP_WAIT_POLICY'] = 'PASSIVE'
import cv2, ncnn, numpy as np

ORDER = np.array([6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15])


def load_net(threads=1):
    net = ncnn.Net()
    net.opt.num_threads = threads
    net.opt.use_vulkan_compute = False
    net.opt.use_fp16_storage = False
    net.opt.use_fp16_packed = False
    net.opt.use_fp16_arithmetic = False
    assert net.load_param(str(_paths.POSE_MODEL / 'model.ncnn.param')) == 0
    assert net.load_model(str(_paths.POSE_MODEL / 'model.ncnn.bin')) == 0
    return net


def pose(net, image):
    h, w = image.shape[:2]
    ratio = min(320 / w, 320 / h)
    rw, rh = round(w * ratio), round(h * ratio)
    left = (320 - rw) // 2
    top = (320 - rh) // 2
    padded = cv2.copyMakeBorder(
        cv2.resize(image, (rw, rh)),
        top,
        320 - rh - top,
        left,
        320 - rw - left,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    mat = ncnn.Mat.from_pixels(padded, ncnn.Mat.PixelType.PIXEL_BGR2RGB, 320, 320)
    mat.substract_mean_normalize([0, 0, 0], [1 / 255] * 3)
    ex = net.create_extractor()
    assert ex.input('in0', mat) == 0
    status, out = ex.extract('out0')
    assert status == 0
    raw = np.array(out).copy()
    if raw.shape[0] == 56:
        raw = raw.T
    assert raw.shape[1] == 56
    candidates = raw[raw[:, 4] >= 0.25]
    if not len(candidates):
        return np.full((12, 3), np.nan, np.float32)
    boxes = candidates[:, :4].copy()
    boxes[:, :2] -= boxes[:, 2:] / 2
    keep = np.asarray(cv2.dnn.NMSBoxes(boxes.tolist(), candidates[:, 4].tolist(), 0.25, 0.45)).reshape(-1)
    if not len(keep):
        return np.full((12, 3), np.nan, np.float32)
    row = candidates[keep[np.argmax(candidates[keep, 4])]]
    k = row[5:].reshape(17, 3)[ORDER].copy()
    k[:, 0] = (k[:, 0] - left) / ratio
    k[:, 1] = (k[:, 1] - top) / ratio
    return k


def inputs(keypoints, width=640, height=360):
    k = np.asarray(keypoints)
    mask = np.isfinite(k).all(axis=1) & (k[:, 2] >= 0.2)
    mask &= (k[:, 0] >= 0) & (k[:, 0] < width) & (k[:, 1] >= 0) & (k[:, 1] < height)
    xy = np.zeros((12, 2), np.float32)
    if mask[6:8].all():
        xy[mask] = (k[mask, :2] - k[6:8, :2].mean(axis=0)) / height
    else:
        mask[:] = False
    return np.r_[xy.reshape(-1), mask.astype(np.float32)], bool(mask.sum() >= 8)


def paired_actor(k, gt_pixels, gt_present):
    """Offline supervision-only gate; never changes runtime inputs/availability."""
    good = gt_present & np.isfinite(gt_pixels).all(axis=1) & np.isfinite(k).all(axis=1) & (k[:, 2] >= 0.2)
    if good.sum() < 4:
        return False
    truth = gt_pixels[good]
    pred = k[good, :2]
    diagonal = max(float(np.linalg.norm(np.ptp(truth, axis=0))), 20.0)
    return bool(np.median(np.linalg.norm(pred - truth, axis=1)) / diagonal <= 0.35)
