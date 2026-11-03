import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
    POLICY_VERSION,
    isClassifying,
    isLiveAlert,
    isManualReview,
    isUrgentWaiting,
    selectUrgentWaiting,
    selectManualReview,
    reviewReason,
    liveReason,
} from '../utils/alertPolicy.mjs';
assert.equal(POLICY_VERSION, 2);
const normal = {
    id: 1,
    type: 'fall',
    state: 'pending',
    notification_mode: 'live',
    late_arrival: 0,
    suppression_reason: null,
    classification_reason: 'ONGOING',
};
// Version 2: a late event that is still open is live.
const lateLive = { ...normal, id: 2, late_arrival: 1, classification_reason: 'ONGOING' };
const recovered = {
    ...normal,
    id: 3,
    notification_mode: 'manual_review',
    late_arrival: 1,
    suppression_reason: 'late_recovered',
    classification_reason: 'LATE_RECOVERED',
};
const stale = { ...recovered, id: 4, suppression_reason: 'stale_unknown', classification_reason: 'STALE_UNKNOWN' };
const classifying = {
    ...normal,
    id: 5,
    notification_mode: 'classifying',
    late_arrival: 1,
    classification_reason: null,
};
// Rows written by version 1 keep their reasons.
const old = { ...recovered, id: 6, suppression_reason: 'event_too_old', classification_reason: null };
const clock = { ...recovered, id: 7, suppression_reason: 'clock_unreliable', classification_reason: null };
for (const state of ['pending', 'sent', 'delivered']) {
    assert(isUrgentWaiting({ ...normal, state }));
    assert(isUrgentWaiting({ ...lateLive, state }), 'a late but live alert is urgent');
    for (const a of [recovered, stale, classifying, old, clock]) assert(!isUrgentWaiting({ ...a, state }));
}
for (const state of ['acknowledged', 'resolved', 'expired']) assert(!isUrgentWaiting({ ...normal, state }));
assert(!isLiveAlert({ ...normal, notification_mode: undefined }), 'an unknown mode fails closed');
assert(!isLiveAlert({ ...normal, suppression_reason: 'late_recovered' }));
assert(isClassifying(classifying) && !isLiveAlert(classifying) && !isManualReview(classifying));
assert.deepEqual(
    selectUrgentWaiting([normal, lateLive, recovered, stale, classifying, old, clock]).map(a => a.id),
    [1, 2]
);
assert.deepEqual(
    selectManualReview([normal, recovered, stale, classifying, old, clock, { ...recovered, state: 'resolved' }]).map(
        a => a.id
    ),
    [3, 4, 6, 7]
);
assert.equal(selectManualReview([{ ...recovered, state: 'acknowledged' }]).length, 1);
assert.equal(reviewReason(recovered), 'Late arrival; the person had already recovered');
assert.equal(reviewReason(stale), 'Stale event; outcome unknown');
assert.equal(reviewReason(clock), 'Device clock unreliable');
assert.equal(reviewReason({ ...recovered, suppression_reason: 'something_new' }), 'Notification policy unavailable');
assert(
    liveReason(lateLive).length > 0 && liveReason({ ...lateLive, classification_reason: 'HOLD_TIMEOUT' }).length > 0
);
// The policy version must travel from the browser through the web proxy to the bridge: without it the bridge treats
// the client as an old tab and streams settled live alerts only, so classifying and manual-review cases never reach
// the Alerts page.
const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const proxy = readFileSync(join(root, 'app/api/alerts/[[...path]]/route.js'), 'utf8');
const forwarded = proxy.match(/for \(const name of \[([^\]]*)\]\)/);
assert(forwarded && /'policy'/.test(forwarded[1]), 'the alert proxy must forward the policy version to the bridge');
assert(
    readFileSync(join(root, 'context/AlertContext.js'), 'utf8').includes('&policy=${POLICY_VERSION}'),
    'the alert stream request must carry the policy version'
);
console.log(
    'PASS version-2 live/classifying/manual eligibility, late live alerts urgent, unknown-policy fail-closed, reasons and policy forwarding to the bridge'
);
