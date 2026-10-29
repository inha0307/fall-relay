import _paths  # noqa: F401  (edge on sys.path)
import numpy as np
from causal_tracker import CausalTracker


def detection(x, score=0.8):
    k = np.zeros((12, 3), np.float32)
    k[:, :2] = [x + 20, 100]
    k[:, 2] = 0.9
    return dict(box=np.array([x, 20, x + 80, 300], np.float32), keypoints=k, score=score)


t = CausalTracker()
d, identity, reset = t.update([detection(300), detection(0, 0.7)])
assert reset
# A bystander's rising pose-model confidence must not change primary identity.
d, new, reset = t.update([detection(0, 0.99), detection(302, 0.5)])
assert identity == new and not reset and d['box'][0] == 302
for _ in range(5):
    d, new, reset = t.update([detection(0, 0.99)])
    assert d is None and new == identity and not reset
d, new, reset = t.update([detection(0, 0.99)])
assert new != identity and reset and d is not None
# A large jump cannot silently inherit the prior track identity.
t = CausalTracker()
_, old, _ = t.update([detection(0)])
d, new, reset = t.update([detection(500)])
assert d is None and new == old
# Future observations cannot affect earlier outputs: independent prefix runs.
a = CausalTracker()
b = CausalTracker()
for seq in [[detection(300)], [detection(304)], [detection(308)]]:
    da, ia, ra = a.update(seq)
    db, ib, rb = b.update(seq)
    assert ia == ib and ra == rb and np.array_equal(da['box'], db['box'])
print('Passed: persistent primary, missing slots, no bystander fallback, reacquisition reset, and causal prefix.')
