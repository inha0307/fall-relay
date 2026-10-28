import assert from 'node:assert/strict';
import {
    isLiveAlert,
    isUrgentWaiting,
    selectUrgentWaiting,
    selectManualReview,
    reviewReason,
} from '../utils/alertPolicy.mjs';
const normal = {
    id: 1,
    type: 'fall',
    state: 'pending',
    notification_mode: 'live',
    late_arrival: 0,
    suppression_reason: null,
};
const old = {
    ...normal,
    id: 2,
    notification_mode: 'manual_review',
    late_arrival: 1,
    suppression_reason: 'event_too_old',
};
const clock = { ...old, id: 3, suppression_reason: 'clock_unreliable' };
for (const state of ['pending', 'sent', 'delivered']) {
    assert(isUrgentWaiting({ ...normal, state }));
    for (const a of [old, clock]) assert(!isUrgentWaiting({ ...a, state }));
}
for (const state of ['acknowledged', 'resolved', 'expired']) assert(!isUrgentWaiting({ ...normal, state }));
assert(!isLiveAlert({ ...normal, notification_mode: undefined }));
assert(!isLiveAlert({ ...normal, late_arrival: 1 }));
assert(!isLiveAlert({ ...normal, suppression_reason: 'event_too_old' }));
assert.deepEqual(
    selectUrgentWaiting([normal, old, clock]).map(a => a.id),
    [1]
);
assert.deepEqual(
    selectManualReview([normal, old, clock, { ...old, state: 'resolved' }]).map(a => a.id),
    [2, 3]
);
assert.equal(selectManualReview([{ ...old, state: 'acknowledged' }]).length, 1);
assert.equal(reviewReason(clock), 'Device clock unreliable');
console.log('PASS common live/manual eligibility, unknown-policy fail-closed, urgent counts and manual review counts');
