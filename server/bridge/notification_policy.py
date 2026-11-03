"""Notification policy, version 2: an alert is classified from the latest known state of its event.

A live alert sounds until a responder acknowledges it. A manual-review alert is listed without sound. The bridge
classifies when it accepts the first open message if the event is recent or has no later message pending on the
device; otherwise it holds the alert as `classifying` for at most a few seconds while the later messages arrive.

Event age is measured on the device's boot clock, so the wall clock and its calibration do not decide the mode. Once
classified, a live alert never becomes manual review and a manual-review alert never becomes live.
"""

VERSION = 2

# notification_mode values
CLASSIFYING, LIVE, MANUAL_REVIEW = 'classifying', 'live', 'manual_review'

# classification_reason codes, recorded with every decision
ONGOING = 'ONGOING'  # the event is still open: the person may still be down
RECOVERED_RECENT = 'RECOVERED_RECENT'  # closed with recovery, but within the late threshold
OUTCOME_UNKNOWN = 'OUTCOME_UNKNOWN'  # closed by input loss, maximum age or a runtime stop
LATE_RECOVERED = 'LATE_RECOVERED'  # closed with recovery and older than the late threshold
STALE_UNKNOWN = 'STALE_UNKNOWN'  # outcome unknown and older than the stale threshold
HOLD_TIMEOUT = 'HOLD_TIMEOUT'  # the latest state did not arrive within the hold

# suppression_reason values stored with manual-review alerts (version 1 values stay valid in old rows)
SUPPRESSION = {LATE_RECOVERED: 'late_recovered', STALE_UNKNOWN: 'stale_unknown'}


def classify(*, latest_kind, close_reason, age_s, late_threshold_s=120.0, stale_threshold_s=3600.0):
    """(mode, reason) from the latest message of the event and the event's age in seconds (None = unknown)."""
    if latest_kind != 'close':
        return LIVE, ONGOING
    if close_reason == 'recovered':
        if age_s is None or age_s > late_threshold_s:
            return MANUAL_REVIEW, LATE_RECOVERED
        return LIVE, RECOVERED_RECENT
    if age_s is not None and age_s > stale_threshold_s:
        return MANUAL_REVIEW, STALE_UNKNOWN
    return LIVE, OUTCOME_UNKNOWN


def decide_now(*, age_s, backlog, late_threshold_s=120.0):
    """Whether the open message can be classified at once: the event is recent, or nothing of it is still queued."""
    return (age_s is not None and age_s <= late_threshold_s) or backlog == 0
