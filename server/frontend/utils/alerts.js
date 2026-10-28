/* Display names for alert states and on-site outcomes, shared by Alerts and Alert review. */
export const ALERT_STATES = {
    pending: 'Pending',
    sent: 'Pending',
    delivered: 'Delivered',
    acknowledged: 'Acknowledged',
    resolved: 'Resolved',
    expired: 'Expired',
};
export const FALL_OUTCOMES = {
    fall: 'Fall confirmed',
    fa_sitting: 'False alarm · sitting',
    fa_lying: 'False alarm · lying down',
    fa_bending: 'False alarm · bending',
    fa_other: 'False alarm · other',
    uncertain: 'Uncertain',
};
export const DEVICE_OUTCOMES = { recovered: 'Recovered', device_other: 'Other device problem', uncertain: 'Uncertain' };
