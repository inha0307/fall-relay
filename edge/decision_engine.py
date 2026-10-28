"""Edge fall decision state machine. Shared by the edge runtime
and offline evaluation so event-level validation runs the exact deployed logic.

States: IDLE -> SUSPECT -> ALERT -> POST_FALL -> (RECOVERED | EXPIRED) -> COOLDOWN -> IDLE
  high slot     eligible slot with score >= theta
  SUSPECT       any high slot in the last n slots
  ALERT (open)  >= k high slots among the last n slots and >= m eligible slots among them;
                ineligible slots count neither as high nor as low (no reset on a missing pose)
  latch         from ALERT until the event closes no new event is opened, however long scores stay
                high (prevents 'alarm for as long as the person lies there')
  POST_FALL     post_fall_after_s after ALERT, or earlier when `lying` is reported
  close         upright held recover_hold_s -> RECOVERED; event older than max_event_s -> EXPIRED;
                no eligible slot for track_lost_s -> close(track_lost)
  COOLDOWN      cooldown_s after close before a new event may open
`upright` / `lying` are optional geometric posture flags (None = unknown, e.g. offline NTU replay).
step() returns a list of event dicts: {'kind': 'open'|'close', 't', 'event_seq', 'reason'?, 'max_score'}.
"""

from collections import deque
from dataclasses import dataclass, field

STATES = ("IDLE", "SUSPECT", "ALERT", "POST_FALL", "COOLDOWN")


@dataclass
class DecisionParams:
    theta: float = 0.932
    k: int = 8
    n: int = 10
    m: int = 8
    post_fall_after_s: float = 5.0
    recover_hold_s: float = 3.0
    cooldown_s: float = 30.0
    max_event_s: float = 600.0
    track_lost_s: float = 10.0

    def __post_init__(self):
        if not (1 <= self.k <= self.n and 1 <= self.m <= self.n):
            raise ValueError("need 1 <= k <= n and 1 <= m <= n")
        if not 0.0 <= self.theta <= 1.0:
            raise ValueError("theta must be in [0, 1]")


@dataclass
class DecisionEngine:
    params: DecisionParams = field(default_factory=DecisionParams)

    def __post_init__(self):
        self.reset()

    def reset(self):
        self.state = "IDLE"
        self.window = deque(maxlen=self.params.n)  # (eligible, high)
        self.event_seq = 0
        self.opened_at = None
        self.cooldown_until = -1e18
        self.upright_since = None
        self.last_eligible_t = None
        self.max_score = 0.0

    def _close(self, t, reason):
        ev = {"kind": "close", "t": t, "event_seq": self.event_seq, "reason": reason, "max_score": self.max_score}
        self.state = "COOLDOWN"
        self.cooldown_until = t + self.params.cooldown_s
        self.opened_at = None
        self.upright_since = None
        self.window.clear()
        return [ev]

    def step(self, t, score, eligible, upright=None, lying=None):
        p = self.params
        high = bool(eligible and score >= p.theta)
        events = []
        self.window.append((bool(eligible), high))
        if eligible:
            self.last_eligible_t = t
        if self.state in ("ALERT", "POST_FALL"):
            self.max_score = max(self.max_score, float(score) if eligible else 0.0)
            if self.state == "ALERT" and (t - self.opened_at >= p.post_fall_after_s or lying):
                self.state = "POST_FALL"
            if upright:
                self.upright_since = t if self.upright_since is None else self.upright_since
                if t - self.upright_since >= p.recover_hold_s:
                    return self._close(t, "recovered")
            elif upright is not None:
                self.upright_since = None
            if t - self.opened_at >= p.max_event_s:
                return self._close(t, "timeout")
            if self.last_eligible_t is None or t - self.last_eligible_t >= p.track_lost_s:
                return self._close(t, "track_lost")
            return events
        if self.state == "COOLDOWN":
            if t < self.cooldown_until:
                return events
            self.state = "IDLE"
        n_high = sum(h for _, h in self.window)
        n_elig = sum(e for e, _ in self.window)
        if n_high >= p.k and n_elig >= p.m:
            self.event_seq += 1
            self.state = "ALERT"
            self.opened_at = t
            self.max_score = float(score)
            self.upright_since = None
            events.append({"kind": "open", "t": t, "event_seq": self.event_seq, "max_score": self.max_score})
        else:
            self.state = "SUSPECT" if n_high else "IDLE"
        return events

    def finish(self, t):
        """Close an open event at the end of a recording (offline evaluation)."""
        return self._close(t, "end_of_stream") if self.state in ("ALERT", "POST_FALL") else []
