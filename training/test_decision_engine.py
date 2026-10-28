import _paths  # noqa: F401  (edge on sys.path)
from decision_engine import DecisionEngine, DecisionParams


def run(scores, eligible=None, params=None, upright=None, lying=None, dt=0.1):
    e = DecisionEngine(params or DecisionParams(theta=0.5, k=3, n=5, m=3, cooldown_s=2.0))
    out = []
    for i, s in enumerate(scores):
        el = True if eligible is None else eligible[i]
        up = None if upright is None else upright[i]
        ly = None if lying is None else lying[i]
        out += [dict(ev, i=i) for ev in e.step(round(i * dt, 3), s, el, up, ly)]
    return out, e


# k-of-n opens one event; a long high run afterwards stays latched (no second event).
ev, e = run([0, 0.9, 0.9, 0.9] + [0.9] * 200)
assert [x["kind"] for x in ev] == ["open"] and ev[0]["i"] == 3 and e.state == "POST_FALL"

# Ineligible slots neither reset nor count: high, missing, high, high still reaches k=3 within n=5.
ev, _ = run([0.9, 0.9, 0, 0.9, 0, 0], eligible=[1, 1, 0, 1, 1, 1])
assert ev and ev[0]["i"] == 3
# ...but m (eligible slots in the window) must also be met.
ev, _ = run([0.9, 0, 0, 0.9, 0.9], eligible=[1, 0, 0, 1, 1], params=DecisionParams(theta=0.5, k=3, n=5, m=4))
assert not ev

# Recovery closes the event after the upright hold; cooldown blocks immediate re-alert; later alert allowed.
p = DecisionParams(theta=0.5, k=3, n=5, m=3, recover_hold_s=1.0, cooldown_s=2.0)
scores = [0.9] * 5 + [0] * 5 + [0.9] * 10 + [0] * 30 + [0.9] * 5
upright = [False] * 5 + [True] * 15 + [True] * 30 + [False] * 5
ev, _ = run(scores, upright=upright, params=p)
kinds = [(x["kind"], x.get("reason"), x["i"]) for x in ev]
assert kinds[0][:1] == ("open",) and kinds[1][:2] == ("close", "recovered")
assert sum(k == "open" for k, _, _ in kinds) == 2 and kinds[2][2] >= kinds[1][2] + 20  # second open only after cooldown

# Track lost closes the event; finish() closes an open event at the end of a recording.
ev, e = run(
    [0.9] * 5 + [0] * 150,
    eligible=[1] * 5 + [0] * 150,
    params=DecisionParams(theta=0.5, k=3, n=5, m=3, track_lost_s=10),
)
assert ev[-1]["kind"] == "close" and ev[-1]["reason"] == "track_lost"
ev, e = run([0.9] * 6)
assert e.finish(1.0)[0]["reason"] == "end_of_stream"

# Lying flag moves ALERT to POST_FALL immediately.
ev, e = run([0.9] * 3)  # opens at the third high slot
assert ev[0]["i"] == 2 and e.state == "ALERT"
e.step(0.3, 0.9, True, None, True)
assert e.state == "POST_FALL"
print("Passed: k-of-n open, latch, ineligible handling, m rule, recovery+cooldown, track loss, end of stream, lying.")
