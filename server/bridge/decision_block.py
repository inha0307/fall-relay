"""The decision block: the decision-rule parameters a run declares in its identity and that every event must match.

The rule itself runs on the device (edge/decision_engine.py). The bridge only checks the block's form here; the
values are whatever the device's bundle declares (edge/bundle_manifest.json, "decision").
"""

import math

DECISION_FIELDS = (
    'theta',
    'k',
    'n',
    'm',
    'post_fall_after_s',
    'recover_hold_s',
    'cooldown_s',
    'max_event_s',
    'track_lost_s',
    'update_interval_s',
)


def validate_decision(value):
    if not isinstance(value, dict) or set(value) != set(DECISION_FIELDS):
        raise ValueError('decision block fields do not match the supported contract')
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(f'invalid decision {key}')
    if (
        any(not isinstance(value[key], int) for key in ('k', 'n', 'm'))
        or not 1 <= value['k'] <= value['m'] <= value['n'] <= 300
    ):
        raise ValueError('decision requires integer 1 <= k <= m <= n <= 300')
    if not 0 <= value['theta'] <= 1:
        raise ValueError('invalid decision theta')
    for key in set(DECISION_FIELDS) - {'theta', 'k', 'n', 'm'}:
        # cooldown 0 is a valid policy: the latch alone suppresses duplicates while an event is open.
        if not (0 if key == 'cooldown_s' else 0.05) <= value[key] <= 86400:
            raise ValueError(f'invalid decision {key}')
    return dict(value)
