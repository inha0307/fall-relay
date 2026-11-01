// The server's persisted policy is authoritative. Unknown policies fail closed.
// Version 2: an alert is classified from the latest state of its event; while the bridge waits for that state
// (a few seconds at most) the alert is `classifying`: shown, but silent.
export const POLICY_VERSION = 2;
export const UNACKED = ['pending', 'sent', 'delivered'];
export const needsAcknowledgement = alert => UNACKED.includes(alert.state);
export const isClassifying = alert => alert.notification_mode === 'classifying';
export const isManualReview = alert => alert.notification_mode === 'manual_review';
export const isLiveAlert = alert => alert.notification_mode === 'live' && !alert.suppression_reason;
export const isUrgentWaiting = alert => isLiveAlert(alert) && needsAcknowledgement(alert);
export const isOpen = alert => !['resolved', 'expired'].includes(alert.state);
export const selectUrgentWaiting = alerts => alerts.filter(isUrgentWaiting);
export const selectManualReview = alerts => alerts.filter(alert => isManualReview(alert) && isOpen(alert));
// Why a manual-review alert makes no sound (version-2 codes, then the version-1 reasons of older rows).
const REVIEW = {
    late_recovered: 'Late arrival; the person had already recovered',
    stale_unknown: 'Stale event; outcome unknown',
    event_too_old: 'Late arrival',
    clock_unreliable: 'Device clock unreliable',
};
export const reviewReason = alert => REVIEW[alert.suppression_reason] || 'Notification policy unavailable';
// Why a late event still sounds.
const LIVE = {
    ONGOING: 'the event was still open when it arrived',
    OUTCOME_UNKNOWN: 'the event ended without a recovery',
    HOLD_TIMEOUT: 'the latest state of the event did not arrive in time',
    RECOVERED_RECENT: 'the person recovered within the live window',
};
export const liveReason = alert => LIVE[alert.classification_reason] || '';
