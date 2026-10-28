"""Notification policy is latched when the first open message is accepted.

Later updates, retries and passage of time never promote a manual case to live,
or silence an originally live alert that still needs a responder acknowledgement.
"""

VERSION = 1


def classify(*, clock_usable, late):
    if not clock_usable:
        return 'manual_review', 'clock_unreliable'
    if late:
        return 'manual_review', 'event_too_old'
    return 'live', None


def live(alert):
    return (
        alert.get('notification_mode') == 'live'
        and not alert.get('late_arrival')
        and not alert.get('suppression_reason')
    )
