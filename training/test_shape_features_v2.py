import _paths  # noqa: F401  (edge on sys.path)
import math
import numpy as np
from shape_features_v2 import (
    DynamicFeatureState,
    dynamic_features,
    VY,
    VX,
    OMEGA,
    SHORT_VALID,
    DY_1S,
    DTILT_1S,
    LONG_VALID,
    WIDTH,
)


def body(hip=(320.0, 200.0), length=100.0, tilt=0.0):
    """12-joint ncnn_pose order. Torso and thigh are each length/2 px; tilt rotates the
    hip->shoulder vector away from image up."""
    k = np.zeros((12, 3))
    k[:, 2] = 0.9
    hip = np.asarray(hip)
    half = length / 2
    up = np.array([math.sin(tilt), -math.cos(tilt)])
    side = np.array([math.cos(tilt), math.sin(tilt)])
    k[6, :2], k[7, :2] = hip - 8 * side, hip + 8 * side
    k[0, :2], k[1, :2] = hip + up * half - 15 * side, hip + up * half + 15 * side
    k[2:4, :2] = k[0:2, :2] - up * 20
    k[4:6, :2] = k[2:4, :2] - up * 20
    k[8, :2], k[9, :2] = k[6, :2] - up * half, k[7, :2] - up * half
    k[10:12, :2] = k[8:10, :2] - up * 30
    return k


def run(frames, t=None, ids=None, available=None):
    n = len(frames)
    t = np.arange(n) / 10 if t is None else t
    return dynamic_features(
        frames, t, np.ones(n, bool) if available is None else available, np.ones(n, int) if ids is None else ids
    )


# A still person gives zeros with valid flags, upright or lying.
for tilt in (0.0, math.pi / 2):
    x = run([body(tilt=tilt)] * 15)
    assert x.shape == (15, WIDTH) and not x[0].any()
    assert np.allclose(x[1:, [VY, VX, OMEGA, DY_1S, DTILT_1S]], 0) and x[1:, SHORT_VALID].all()
    assert x[10:, LONG_VALID].all() and not x[:8, LONG_VALID].any()

# Distance/FOV invariance: the same motion at half the image size gives identical features.
fall = [body(hip=(320, 150 + 6 * i), tilt=min(i / 8, 1) * math.pi / 2) for i in range(15)]
far = [np.c_[k[:, :2] * 0.5 + [100, 60], k[:, 2:]] for k in fall]
a, b = run(fall), run(far)
assert np.allclose(a, b, atol=1e-5), np.abs(a - b).max()

# A fall-like descent and tilt are positive in both short and 1 s features.
assert (a[1:, VY] > 0).all() and (a[1:9, OMEGA] > 0).all()
assert a[10, DY_1S] > 0.5 and a[10, DTILT_1S] > 1.0

# Body-length reference keeps the upright length while lying (foreshortened torso).
s = DynamicFeatureState()
s.update(body(length=100), 0.0, True, 1)
y = s.update(body(hip=(320, 210), length=40, tilt=math.pi / 2), 0.1, True, 1)
assert math.isclose(y[VY], 10 / 0.1 / 100, rel_tol=1e-5)

# Gaps and identity changes never mix references.
x = run([body()] * 4, available=np.array([1, 0, 1, 1], bool))
assert not x[1].any() and x[2, SHORT_VALID] == 0 and x[3, SHORT_VALID] == 1
x = run([body()] * 15, ids=np.array([1] * 12 + [2] * 3))
assert not x[12].any() and x[13, SHORT_VALID] == 1 and x[14, LONG_VALID] == 0

# Long steps (missed slots) do not produce a short-term velocity.
x = run([body(), body(hip=(320, 260))], t=np.array([0.0, 0.3]))
assert x[1, SHORT_VALID] == 0 and x[1, VY] == 0

# Causality: future frames do not change past outputs; streaming equals batch.
future = fall[:10] + [body(hip=(50, 50))] * 5
assert np.array_equal(run(future)[:10], a[:10])
state = DynamicFeatureState()
assert np.array_equal(np.stack([state.update(k, i / 10, True, 1) for i, k in enumerate(fall)]), a)

# Camera roll correction: a body rolled with the camera reads as upright again.
rolled = [
    np.c_[
        (k[:, :2] - [320, 180]) @ np.array([[math.cos(0.2), -math.sin(0.2)], [math.sin(0.2), math.cos(0.2)]]).T
        + [320, 180],
        k[:, 2:],
    ]
    for k in [body()] * 3
]
assert np.allclose(
    dynamic_features(rolled, np.arange(3) / 10, np.ones(3, bool), np.ones(3, int), roll_deg=-math.degrees(0.2)),
    run([body()] * 3),
    atol=1e-5,
)
print(
    'Passed: still postures zero, distance invariance, fall direction, upright length reference, gap/identity resets, causality and roll correction.'
)
