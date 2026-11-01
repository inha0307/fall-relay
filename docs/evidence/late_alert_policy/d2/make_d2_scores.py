#!/usr/bin/env python3
"""Write the two per-slot score sequences of check D2 (late arrival, policy version 2) and verify them offline.

Both sequences are 262 s at 10 Hz and open one event at 2.2 s (three slots at or above theta 0.95 among the last
five). They differ only in when the person gets up:
  lying_still.csv       lying from 2.6 s until 255.0 s, upright from 255.0 s -> the engine closes the event as
                        `recovered` at 258.0 s. With the link dead from the start until 180 s the open and ~90 updates arrive
                        together after the restore while the event is still open: the bridge must classify it live
                        (ONGOING or, if the backlog takes longer than the hold, HOLD_TIMEOUT).
  recovered_in_cut.csv  lying from 2.6 s until 30.0 s, upright from 30.0 s -> closed as `recovered` at 33.0 s, i.e.
                        inside the cut: the close arrives with the open, more than 120 s late -> manual review,
                        LATE_RECOVERED.
The sequences continue with low scores after the recovery so that the adapter keeps running (and its outbox keeps
retrying) through the cut. Scores are hand-written, not detector output; the file says so in its first line.
"""

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'server' / 'bridge' / 'video_bridge.py').exists())
sys.path.insert(0, str(ROOT / 'edge'))
from decision_engine import DecisionEngine, DecisionParams  # noqa: E402

DEPLOYED = DecisionParams(theta=0.95, k=3, n=5, m=3, cooldown_s=0.0)
END_S = 262.0


def sequence(recover_at):
    for i in range(int(END_S * 10) + 1):
        t = round(i / 10, 1)
        if t < 2.0:
            yield t, 0.02 + 0.01 * (i % 3), 1, 1, 0  # upright, low score
        elif t < 2.6:
            yield t, 0.97 + 0.005 * (i % 4), 1, 0, 0  # falling: high scores, neither upright nor lying yet
        elif t < recover_at:
            decay = max(0.15, 0.6 - 0.02 * (t - 2.6))  # lying still: score falls off, posture flag says lying
            yield t, round(decay, 3), 1, 0, 1
        else:
            yield t, 0.03, 1, 1, 0  # up again


def write(name, recover_at):
    path = HERE / name
    engine = DecisionEngine(DEPLOYED)
    events = []
    with path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['t_s', 'score', 'eligible', 'upright', 'lying'])
        for t, score, eligible, upright, lying in sequence(recover_at):
            w.writerow([f'{t:.1f}', f'{score:.3f}', eligible, upright, lying])
            events += [(ev['kind'], ev['t'], ev.get('reason')) for ev in engine.step(t, score, eligible, upright, lying)]
    events += [(ev['kind'], ev['t'], ev.get('reason')) for ev in engine.finish(END_S)]
    assert events[0][:2] == ('open', 2.2) and events[1] == ('close', round(recover_at + 3.0, 1), 'recovered'), events
    assert len(events) == 2, events
    print(name, 'rows', int(END_S * 10) + 1, 'events', events)


if __name__ == '__main__':
    write('lying_still.csv', 255.0)
    write('recovered_in_cut.csv', 30.0)
