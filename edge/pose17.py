"""Deployment-identical frame preprocessing and NCNN detection keeping all 17 COCO joints.

The earlier 12-joint pose extraction reordered the keypoints to ORDER and dropped the face points. detect17 runs
the same letterbox, NCNN extraction, thresholds and NMS, but keeps the raw COCO-17 keypoints so the head
point can be derived later.
"""

import cv2
import ncnn
import numpy as np

POSE_W, POSE_H, LETTERBOX = 640, 360, 320
DET_CONF, NMS_IOU = 0.25, 0.45


def prepare_frame(bgr):
    """Camera/video BGR frame -> 640x360 BGR after the runtime's JPEG95 round trip."""
    small = cv2.resize(bgr, (POSE_W, POSE_H), interpolation=cv2.INTER_LINEAR)
    ok, jpeg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError('JPEG encode failed')
    return cv2.imdecode(jpeg, cv2.IMREAD_COLOR)


def detect17(net, image):
    """All NMS-kept detections, highest score first: box (x0,y0,x1,y1), score, keypoints[17,3]."""
    h, w = image.shape[:2]
    ratio = min(LETTERBOX / w, LETTERBOX / h)
    rw, rh = round(w * ratio), round(h * ratio)
    left = (LETTERBOX - rw) // 2
    top = (LETTERBOX - rh) // 2
    padded = cv2.copyMakeBorder(
        cv2.resize(image, (rw, rh)),
        top,
        LETTERBOX - rh - top,
        left,
        LETTERBOX - rw - left,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    mat = ncnn.Mat.from_pixels(padded, ncnn.Mat.PixelType.PIXEL_BGR2RGB, LETTERBOX, LETTERBOX)
    mat.substract_mean_normalize([0, 0, 0], [1 / 255] * 3)
    ex = net.create_extractor()
    assert ex.input('in0', mat) == 0
    status, out = ex.extract('out0')
    assert status == 0
    raw = np.array(out).copy()
    raw = raw.T if raw.shape[0] == 56 else raw
    assert raw.shape[1] == 56
    rows = raw[raw[:, 4] >= DET_CONF]
    if not len(rows):
        return []
    xywh = rows[:, :4].copy()
    xywh[:, :2] -= xywh[:, 2:] / 2
    keep = np.asarray(cv2.dnn.NMSBoxes(xywh.tolist(), rows[:, 4].tolist(), DET_CONF, NMS_IOU)).reshape(-1)
    found = []
    for i in keep:
        k = rows[i, 5:].reshape(17, 3).copy()
        k[:, 0] = (k[:, 0] - left) / ratio
        k[:, 1] = (k[:, 1] - top) / ratio
        b = np.r_[xywh[i, :2], xywh[i, :2] + xywh[i, 2:]].astype(np.float32)
        b[[0, 2]] = (b[[0, 2]] - left) / ratio
        b[[1, 3]] = (b[[1, 3]] - top) / ratio
        found.append({'box': b, 'keypoints': k, 'score': float(rows[i, 4])})
    return sorted(found, key=lambda d: -d['score'])
