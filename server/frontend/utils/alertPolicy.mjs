// The server's persisted policy is authoritative. Unknown policies fail closed.
export const POLICY_VERSION = 1;
export const UNACKED = ['pending', 'sent', 'delivered'];
export const needsAcknowledgement = alert => UNACKED.includes(alert.state);
export const isManualReview = alert => alert.notification_mode === 'manual_review' || Boolean(alert.late_arrival);
export const isLiveAlert = alert =>
    alert.notification_mode === 'live' && !isManualReview(alert) && !alert.suppression_reason;
export const isUrgentWaiting = alert => isLiveAlert(alert) && needsAcknowledgement(alert);
export const isOpen = alert => !['resolved', 'expired'].includes(alert.state);
export const selectUrgentWaiting = alerts => alerts.filter(isUrgentWaiting);
export const selectManualReview = alerts => alerts.filter(alert => isManualReview(alert) && isOpen(alert));
export const reviewReason = alert =>
    alert.suppression_reason === 'clock_unreliable'
        ? 'Device clock unreliable'
        : alert.suppression_reason === 'event_too_old'
          ? 'Late arrival'
          : 'Notification policy unavailable';
