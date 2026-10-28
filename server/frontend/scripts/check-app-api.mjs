/*
 * Fall-Relay web application API check: sign-in, roles, accounts, temporary passwords, deactivation,
 * change history, the persistent sign-in lock, and the event-review permission.
 *
 * Run it against a server started on a THROWAWAY account schema (the script seeds it and refuses the default):
 *
 *   export FALLRELAY_DATABASE_URL=...  FALLRELAY_APP_SCHEMA=test_app_x   # never the production schema
 *   node scripts/check-app-api.mjs --seed                    # creates tadmin (admin) and tviewer (viewer)
 *   npx next start --port 3100 &   →   node scripts/check-app-api.mjs
 *
 * The event-review check only touches a non-existent event id, so the bridge data is never modified.
 */
const BASE = process.env.CHECK_BASE_URL || 'http://127.0.0.1:3100';
const schema = process.env.FALLRELAY_APP_SCHEMA;
if (!schema || schema === 'app') {
    console.error('Set FALLRELAY_APP_SCHEMA to a throwaway schema (never the production one).');
    process.exit(2);
}

if (process.argv.includes('--seed')) {
    const { audit, newId, nextUserCode, nowIso, openDb } = await import('../lib/appDb.mjs');
    const { hashPassword } = await import('../lib/password.mjs');
    const db = await openDb();
    for (const [login, role, password] of [
        ['tadmin', 'admin', 'admin-pass-123'],
        ['tviewer', 'viewer', 'viewer-pass-123'],
    ]) {
        if (await db.get('SELECT 1 FROM accounts WHERE login_id = ?', login)) continue;
        await db.transaction(async tx => {
            const id = newId(),
                now = nowIso();
            await tx.run(
                `INSERT INTO accounts(id, user_code, login_id, name, role, status, must_change_password, password_hash, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)`,
                id,
                await nextUserCode(tx),
                login,
                `Test ${role}`,
                role,
                hashPassword(password),
                now,
                now
            );
            await audit(tx, null, await tx.get('SELECT * FROM accounts WHERE id = ?', id), 'created', {
                via: 'check-app-api seed',
            });
        });
    }
    await db.close();
    console.log(`Seeded schema ${schema}`);
    process.exit(0);
}

let pass = 0;
const check = (name, cond, detail) => {
    if (!cond) {
        console.log('FAIL', name, detail === undefined ? '' : JSON.stringify(detail));
        process.exit(1);
    }
    pass++;
    console.log('PASS', name);
};

function client() {
    let cookie = '';
    return async (urlPath, { method = 'GET', body } = {}) => {
        const res = await fetch(BASE + urlPath, {
            method,
            redirect: 'manual',
            headers: { ...(cookie ? { cookie } : {}), ...(body ? { 'content-type': 'application/json' } : {}) },
            body: body ? JSON.stringify(body) : undefined,
        });
        const set = res.headers.get('set-cookie');
        if (set) {
            const m = set.match(/fall_relay_session=([^;]*)/);
            if (m) cookie = m[1] ? `fall_relay_session=${m[1]}` : '';
        }
        let data = null;
        try {
            data = await res.json();
        } catch {}
        return { status: res.status, data, setCookie: set };
    };
}
const login = (c, userid, password) => c('/api/auth/login', { method: 'POST', body: { userid, password } });
const anon = client(),
    admin = client(),
    viewer = client();
const GATED = '/api/media-token'; // any role-checked route; blocked until a temporary password is changed

check('me without a session is 401', (await anon('/api/auth/me')).status === 401);
check('account admin needs sign-in', (await anon('/api/admin/users')).status === 401);
check('camera admin needs sign-in', (await anon('/api/admin/cameras')).status === 401);
check('alerts need sign-in', (await anon('/api/alerts')).status === 401);
for (const gone of [
    '/api/patients',
    '/api/staff?type=doctor',
    '/api/detections',
    '/api/push/subscribe',
    '/api/enroll-face',
]) {
    check(`removed endpoint ${gone} is gone`, (await anon(gone)).status === 404);
}

let r = await login(anon, 'tadmin', 'wrong-password');
check(
    'wrong password refused with a generic message',
    r.status === 401 && r.data.error.includes('Incorrect user id or password')
);
r = await login(anon, 'nobody', 'whatever-123');
check('unknown user gets the same message', r.status === 401 && r.data.error.includes('Incorrect user id or password'));
r = await login(admin, 'tadmin', 'admin-pass-123');
check('admin signs in', r.status === 200 && r.data.user.role === 'admin' && /^U-\d{4}$/.test(r.data.user.user_code));
check('session cookie is httpOnly + SameSite=Lax', /HttpOnly/i.test(r.setCookie) && /SameSite=lax/i.test(r.setCookie));
check('sign-in response never contains the password hash', !JSON.stringify(r.data).includes('scrypt'));
const adminCode = r.data.user.user_code;
r = await login(viewer, 'tviewer', 'viewer-pass-123');
check('viewer signs in', r.status === 200 && r.data.user.role === 'viewer');
check('viewer cannot list accounts', (await viewer('/api/admin/users')).status === 403);
check('viewer cannot manage cameras', (await viewer('/api/admin/cameras')).status === 401);

check(
    'invalid user id refused',
    (await admin('/api/admin/users', { method: 'POST', body: { login_id: 'a b', name: 'X' } })).status === 400
);
check(
    'removed annotator role refused',
    (
        await admin('/api/admin/users', {
            method: 'POST',
            body: { login_id: 'removed-role', name: 'X', role: 'annotator' },
        })
    ).status === 400
);
check(
    'invalid role refused',
    (await admin('/api/admin/users', { method: 'POST', body: { login_id: 'x1', name: 'X', role: 'nurse' } })).status ===
        400
);
r = await admin('/api/admin/users', {
    method: 'POST',
    body: { login_id: 'rsch1', name: 'Researcher One', email: 'r1@lab.org', affiliation: 'Vision Lab', role: 'viewer' },
});
check(
    'viewer created with a user code and a temporary password',
    r.status === 201 &&
        /^U-\d{4}$/.test(r.data.user.user_code) &&
        r.data.user.must_change_password &&
        r.data.temporaryPassword?.length === 12,
    r.data
);
const rsch = r.data.user;
let temp = r.data.temporaryPassword;
check(
    'duplicate user id refused',
    (await admin('/api/admin/users', { method: 'POST', body: { login_id: 'RSCH1', name: 'Dup' } })).status === 409
);
r = await admin('/api/admin/users');
check(
    'list shows codes and no hashes',
    r.status === 200 && r.data.some(a => a.user_code === rsch.user_code) && !JSON.stringify(r.data).includes('scrypt')
);

const researcher = client();
r = await login(researcher, 'rsch1', temp);
check('temporary-password sign-in is flagged', r.status === 200 && r.data.user.must_change_password === true);
r = await researcher(GATED);
check('pending password change blocks other actions', r.status === 403 && r.data.error.includes('temporary password'));
check(
    'wrong current password refused',
    (
        await researcher('/api/auth/password', {
            method: 'POST',
            body: { current_password: 'nope', new_password: 'research-pass-1' },
        })
    ).status === 400
);
check(
    'weak new password refused',
    (
        await researcher('/api/auth/password', {
            method: 'POST',
            body: { current_password: temp, new_password: 'short' },
        })
    ).status === 400
);
r = await researcher('/api/auth/password', {
    method: 'POST',
    body: { current_password: temp, new_password: 'research-pass-1' },
});
check('password changed', r.status === 200);
check('flag cleared after the change', (await researcher('/api/auth/me')).data.user.must_change_password === false);
r = await researcher(GATED);
check('after the change the account works normally', r.status === 200, r);

r = await admin(`/api/admin/users/${rsch.id}`, {
    method: 'PATCH',
    body: { name: 'Researcher 1', affiliation: 'Gait Lab' },
});
check(
    'admin edits name and affiliation',
    r.status === 200 && r.data.user.name === 'Researcher 1' && r.data.user.affiliation === 'Gait Lab'
);
check('details edit keeps the session', (await researcher('/api/auth/me')).status === 200);
r = await admin(`/api/admin/users/${rsch.id}`, { method: 'PATCH', body: { role: 'responder' } });
check('role change applied', r.status === 200 && r.data.user.role === 'responder');
check('role change ends the sessions', (await researcher('/api/auth/me')).status === 401);
const me = (await admin('/api/admin/users')).data.find(a => a.user_code === adminCode);
check(
    'cannot change own role',
    (await admin(`/api/admin/users/${me.id}`, { method: 'PATCH', body: { role: 'viewer' } })).status === 400
);
check(
    'cannot deactivate self',
    (await admin(`/api/admin/users/${me.id}`, { method: 'PATCH', body: { status: 'deactivated' } })).status === 400
);
check(
    'invalid e-mail refused',
    (await admin(`/api/admin/users/${rsch.id}`, { method: 'PATCH', body: { email: 'not-an-email' } })).status === 400
);

r = await admin(`/api/admin/users/${rsch.id}`, {
    method: 'PATCH',
    body: { status: 'deactivated', reason: 'left the project' },
});
check(
    'deactivated with a reason',
    r.status === 200 &&
        r.data.user.status === 'deactivated' &&
        r.data.user.deactivated_by === adminCode &&
        r.data.user.deactivation_reason === 'left the project'
);
r = await login(researcher, 'rsch1', 'research-pass-1');
check('deactivated account cannot sign in', r.status === 403 && r.data.error.includes('deactivated'));
check(
    'signed-in account cannot be deleted',
    (await admin(`/api/admin/users/${rsch.id}`, { method: 'DELETE' })).status === 409
);
r = await admin(`/api/admin/users/${rsch.id}`, { method: 'PATCH', body: { status: 'active' } });
check('reactivated', r.status === 200 && r.data.user.status === 'active' && r.data.user.deactivated_at === null);
check('reactivated account signs in', (await login(researcher, 'rsch1', 'research-pass-1')).status === 200);

r = await admin(`/api/admin/users/${rsch.id}/password`, { method: 'POST' });
check('temporary password issued', r.status === 200 && r.data.temporaryPassword?.length === 12);
temp = r.data.temporaryPassword;
check('reset ends the sessions', (await researcher('/api/auth/me')).status === 401);
r = await login(researcher, 'rsch1', temp);
check('new temporary password must be changed', r.status === 200 && r.data.user.must_change_password === true);

r = await admin(`/api/admin/users/${rsch.id}`);
const actions = r.data.history.map(h => h.action);
check(
    'change history records every step',
    ['created', 'updated', 'deactivated', 'reactivated', 'password_changed', 'password_reset'].every(a =>
        actions.includes(a)
    ),
    actions
);
check(
    'history keeps old and new values',
    r.data.history.some(
        h =>
            h.action === 'updated' &&
            h.details.affiliation?.from === 'Vision Lab' &&
            h.details.affiliation?.to === 'Gait Lab'
    )
);
check('recent sign-ins listed', r.data.logins.length >= 3 && !r.data.can_delete);

r = await admin('/api/admin/users', { method: 'POST', body: { login_id: 'mistake', name: 'Created by mistake' } });
const mistake = r.data.user;
check(
    'never-signed-in account can be deleted',
    (await admin(`/api/admin/users/${mistake.id}`)).data.can_delete === true &&
        (await admin(`/api/admin/users/${mistake.id}`, { method: 'DELETE' })).status === 200
);
r = await admin('/api/admin/users', { method: 'POST', body: { login_id: 'next1', name: 'Next' } });
const codeNum = c => Number(c.slice(2));
check('user codes are never reused', codeNum(r.data.user.user_code) === codeNum(mistake.user_code) + 1, [
    mistake.user_code,
    r.data.user.user_code,
]);

check(
    'admin reaches camera admin through the session',
    Array.isArray((await admin('/api/admin/cameras')).data?.cameras)
);
check('sign-out', (await viewer('/api/auth/logout', { method: 'POST' })).status === 200);
check('session invalid after sign-out', (await viewer('/api/auth/me')).status === 401);
const attacker = client();
for (let i = 0; i < 5; i++) await login(attacker, 'tviewer', `bad-${i}-xxxx`);
check(
    '5 failures lock the user id (even with the right password)',
    (await login(attacker, 'tviewer', 'viewer-pass-123')).status === 429
);
// -- media tokens (needs a TEST bridge: CHECK_BRIDGE_URL, same write token) ----------------------------
const BRIDGE = process.env.CHECK_BRIDGE_URL;
if (BRIDGE) {
    const signedIn = client();
    await login(signedIn, 'tadmin', 'admin-pass-123');
    check('media token needs sign-in', (await anon('/api/media-token')).status === 401);
    r = await signedIn('/api/media-token');
    const mediaToken = r.data?.token;
    check('media token issued to a signed-in user', r.status === 200 && /^\d+\.[0-9a-f]{64}$/.test(mediaToken), r.data);
    let res = await fetch(`${BRIDGE}/clips/none.webm`);
    check('bridge refuses clips without a token', res.status === 401);
    res = await fetch(`${BRIDGE}/clips/none.webm?t=${encodeURIComponent(mediaToken)}`);
    check('bridge accepts the web-issued token (missing file → 404, not 401)', res.status === 404, res.status);
} else {
    console.log('(skipping media-token checks: set CHECK_BRIDGE_URL to a test bridge that uses the same write token)');
}
console.log(`All ${pass} web application checks passed.`);
