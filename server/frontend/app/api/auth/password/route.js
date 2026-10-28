import { NextResponse } from 'next/server';
import { audit, getDb, nowIso } from '@/lib/appDb.mjs';
import { hashPassword, passwordProblem, verifyPassword } from '@/lib/password.mjs';
import { currentToken, requireUser, revokeSessions } from '@/lib/session';

/* Change your own password (also clears a pending temporary-password change). Other sessions are ended. */
export async function POST(request) {
    const auth = await requireUser(request);
    if (auth.error) return auth.error;
    let body;
    try {
        body = await request.json();
    } catch {
        body = {};
    }
    const { current_password: current, new_password: next } = body || {};
    const db = await getDb();
    const account = await db.get('SELECT * FROM accounts WHERE id = ?', auth.user.id);
    if (typeof current !== 'string' || !verifyPassword(current, account.password_hash)) {
        return NextResponse.json({ error: 'Current password is incorrect.' }, { status: 400 });
    }
    const problem = passwordProblem(next);
    if (problem) return NextResponse.json({ error: problem }, { status: 400 });
    if (next === current)
        return NextResponse.json({ error: 'Choose a password different from the current one.' }, { status: 400 });
    await db.run(
        'UPDATE accounts SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?',
        hashPassword(next),
        nowIso(),
        account.id
    );
    await revokeSessions(account.id, { except: currentToken(request) });
    await audit(db, account, account, 'password_changed');
    return NextResponse.json({ ok: true });
}
