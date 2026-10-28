/* Only an isolated check-app-api seeded server may be used. No real camera/label writes. */
import assert from 'node:assert/strict';
const base = process.env.CHECK_BASE_URL;
const schema = process.env.FALLRELAY_APP_SCHEMA;
assert(base && schema && schema !== 'app', 'Explicit isolated server and account schema required');
function client() {
    let cookie = '';
    return async (url, method = 'GET', body) => {
        const response = await fetch(base + url, {
            method,
            headers: { ...(cookie ? { cookie } : {}), ...(body ? { 'content-type': 'application/json' } : {}) },
            body: body ? JSON.stringify(body) : undefined,
        });
        const token = response.headers.get('set-cookie')?.match(/fall_relay_session=([^;]*)/);
        if (token) cookie = 'fall_relay_session=' + token[1];
        return { status: response.status, data: await response.json() };
    };
}
const admin = client(),
    responder = client();
assert.equal((await admin('/api/auth/login', 'POST', { userid: 'tadmin', password: 'admin-pass-123' })).status, 200);
const login = 'care-' + Date.now();
const created = await admin('/api/admin/users', 'POST', {
    login_id: login,
    name: 'Synthetic responder',
    role: 'responder',
});
assert.equal(created.status, 201);
assert.equal(
    (await responder('/api/auth/login', 'POST', { userid: login, password: created.data.temporaryPassword })).status,
    200
);
assert.equal(
    (
        await responder('/api/auth/password', 'POST', {
            current_password: created.data.temporaryPassword,
            new_password: 'synthetic-responder-pass-123',
        })
    ).status,
    200
);
const current = await responder('/api/auth/me');
assert.equal(current.data.user.role, 'responder');
assert.equal((await responder('/api/bridge/live')).status, 403);
assert.equal((await responder('/api/media-token')).status, 403);
assert.equal((await responder('/api/admin/users')).status, 403);
assert.equal((await admin(`/api/admin/users/${created.data.user.id}`, 'PATCH', { role: 'viewer' })).status, 200);
assert.equal((await responder('/api/auth/me')).status, 401);
const history = await admin(`/api/admin/users/${created.data.user.id}`);
assert(history.data.history.some(item => item.action === 'role_changed'));
console.log('PASS responder create/login/password, bridge/media/admin deny, role-change session revocation and audit');
