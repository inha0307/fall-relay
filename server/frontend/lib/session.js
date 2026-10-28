/* Login sessions (httpOnly cookie + sessions table). Server routes only. */
import { NextResponse } from 'next/server';
import { createHash, randomBytes } from 'node:crypto';
import { getDb, publicAccount } from '@/lib/appDb.mjs';
import { mayReadCamera } from '@/lib/cameraAccess.mjs';

export const SESSION_COOKIE = 'fall_relay_session';
const SESSION_HOURS = 12; // valid for 12 h after the last check (extended while in use)
const RENEW_WHEN_LEFT_HOURS = 6;

const tokenHash = token => createHash('sha256').update(token).digest('hex');
const nowSec = () => Math.floor(Date.now() / 1000);

export function clientIp(request) {
    return (
        request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || request.headers.get('x-real-ip') || 'unknown'
    );
}

export async function createSession(accountId, request) {
    const token = randomBytes(32).toString('base64url');
    const now = nowSec();
    const db = await getDb();
    await db.run(
        'INSERT INTO sessions(token_hash, account_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)',
        tokenHash(token),
        accountId,
        now,
        now + SESSION_HOURS * 3600,
        clientIp(request),
        (request.headers.get('user-agent') || '').slice(0, 200)
    );
    return token;
}

export function setSessionCookie(response, token, request) {
    response.cookies.set(SESSION_COOKIE, token, {
        httpOnly: true,
        sameSite: 'lax',
        path: '/',
        maxAge: SESSION_HOURS * 3600,
        secure: request.nextUrl.protocol === 'https:',
    });
    return response;
}

export function clearSessionCookie(response) {
    response.cookies.set(SESSION_COOKIE, '', { httpOnly: true, sameSite: 'lax', path: '/', maxAge: 0 });
    return response;
}

/* Current user from the session cookie; renew=true extends a session that is close to expiry. */
export async function sessionUser(request, { renew = false } = {}) {
    const token = currentToken(request);
    if (!token) return null;
    const db = await getDb();
    const now = nowSec();
    const session = await db.get('SELECT * FROM sessions WHERE token_hash = ?', tokenHash(token));
    if (!session) return null;
    if (session.expires_at <= now) {
        await db.run('DELETE FROM sessions WHERE token_hash = ?', session.token_hash);
        return null;
    }
    const account = await db.get('SELECT * FROM accounts WHERE id = ?', session.account_id);
    if (!account || account.status !== 'active') {
        await db.run('DELETE FROM sessions WHERE token_hash = ?', session.token_hash);
        return null;
    }
    if (renew && session.expires_at - now < RENEW_WHEN_LEFT_HOURS * 3600) {
        await db.run(
            'UPDATE sessions SET expires_at = ? WHERE token_hash = ?',
            now + SESSION_HOURS * 3600,
            session.token_hash
        );
    }
    return publicAccount(account);
}

export async function endSession(request) {
    const token = currentToken(request);
    if (token) await (await getDb()).run('DELETE FROM sessions WHERE token_hash = ?', tokenHash(token));
}

export async function revokeSessions(accountId, { except } = {}) {
    const db = await getDb();
    if (except) {
        await db.run('DELETE FROM sessions WHERE account_id = ? AND token_hash <> ?', accountId, tokenHash(except));
    } else {
        await db.run('DELETE FROM sessions WHERE account_id = ?', accountId);
    }
}

export function currentToken(request) {
    return request.cookies.get(SESSION_COOKIE)?.value;
}

export async function purgeExpiredSessions() {
    await (await getDb()).run('DELETE FROM sessions WHERE expires_at <= ?', nowSec());
}

/* In a route: const auth = await requireUser(request); if (auth.error) return auth.error; */
export async function requireUser(request) {
    const user = await sessionUser(request);
    if (!user) return { error: NextResponse.json({ error: 'Sign-in required.' }, { status: 401 }) };
    return { user };
}

/* Signed-in user whose role is one of `roles`. A pending password change blocks everything else. */
export async function requireRole(request, roles) {
    const auth = await requireUser(request);
    if (auth.error) return auth;
    if (auth.user.must_change_password) {
        return { error: NextResponse.json({ error: 'Change your temporary password first.' }, { status: 403 }) };
    }
    if (!roles.includes(auth.user.role)) {
        return { error: NextResponse.json({ error: 'Your role does not allow this action.' }, { status: 403 }) };
    }
    return auth;
}

export const requireAdmin = request => requireRole(request, ['admin']);

export async function requireCamera(request, cameraId, roles = ['admin', 'viewer', 'responder']) {
    const auth = await requireRole(request, roles);
    if (auth.error) return auth;
    if (!(await mayReadCamera(await getDb(), auth.user.id, cameraId))) {
        return { error: NextResponse.json({ error: 'This camera is not assigned to your account.' }, { status: 403 }) };
    }
    return auth;
}
