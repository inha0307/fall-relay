import { NextResponse } from 'next/server';
import { getDb, findAccountForLogin, newId, nowIso, publicAccount } from '@/lib/appDb.mjs';
import { verifyPassword, verifyAgainstDummy } from '@/lib/password.mjs';
import { clientIp, createSession, purgeExpiredSessions, setSessionCookie } from '@/lib/session';

/*
 * Sign-in. Every attempt is written to login_logs. Five failures for one user id within 10 minutes
 * (since its last success) lock that id until the window passes; the count comes from login_logs,
 * so the lock survives server restarts. X-Forwarded-For can be forged without a trusted proxy,
 * so the client address is only logged, never used for limiting.
 */
const WINDOW_MS = 10 * 60 * 1000;
const MAX_FAILURES = 5;

async function recentFailures(db, userid) {
    const since = new Date(Date.now() - WINDOW_MS).toISOString();
    const row = await db.get(
        `SELECT COUNT(*) AS n FROM login_logs
                   WHERE lower(login_user_id) = lower(?) AND status = 'failure' AND created_at >= ?
                     AND created_at > COALESCE((SELECT MAX(created_at) FROM login_logs
                                                WHERE lower(login_user_id) = lower(?) AND status = 'success'), '')`,
        userid,
        since,
        userid
    );
    return row.n;
}

async function logLogin(db, accountId, userid, ip, status) {
    await db.run(
        'INSERT INTO login_logs(id, account_id, login_user_id, ip_address, status, created_at) VALUES (?,?,?,?,?,?)',
        newId(),
        accountId,
        userid.slice(0, 100),
        ip,
        status,
        nowIso()
    );
}

export async function POST(request) {
    let body;
    try {
        body = await request.json();
    } catch {
        body = null;
    }
    const userid = typeof body?.userid === 'string' ? body.userid.trim() : '';
    const password = typeof body?.password === 'string' ? body.password : '';
    if (!userid || !password) {
        return NextResponse.json({ error: 'Enter your user id and password.' }, { status: 400 });
    }
    const db = await getDb();
    const ip = clientIp(request);
    if ((await recentFailures(db, userid)) >= MAX_FAILURES) {
        await logLogin(db, null, userid, ip, 'locked');
        return NextResponse.json(
            { error: 'Too many failed attempts. Try again later (up to 10 minutes).' },
            { status: 429 }
        );
    }

    const account = await findAccountForLogin(db, userid);
    const ok = account ? verifyPassword(password, account.password_hash) : verifyAgainstDummy(password);
    if (!ok) {
        await logLogin(db, account?.id ?? null, userid, ip, 'failure');
        return NextResponse.json({ error: 'Incorrect user id or password.' }, { status: 401 });
    }
    if (account.status !== 'active') {
        // The password was right, so saying why is safe and saves the user a support request.
        await logLogin(db, account.id, userid, ip, 'deactivated');
        return NextResponse.json({ error: 'This account is deactivated. Contact an administrator.' }, { status: 403 });
    }
    await purgeExpiredSessions();
    const now = nowIso();
    await db.run('UPDATE accounts SET last_login_at = ? WHERE id = ?', now, account.id);
    await logLogin(db, account.id, userid, ip, 'success');
    const response = NextResponse.json({ user: publicAccount({ ...account, last_login_at: now }) });
    return setSessionCookie(response, await createSession(account.id, request), request);
}
