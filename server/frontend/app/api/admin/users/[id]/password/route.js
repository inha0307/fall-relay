import { NextResponse } from 'next/server';
import { audit, getDb, nowIso } from '@/lib/appDb.mjs';
import { hashPassword, temporaryPassword } from '@/lib/password.mjs';
import { requireAdmin, revokeSessions } from '@/lib/session';

/* Issue a temporary password (shown only in this response); the account must change it at the next sign-in. */
export async function POST(request, { params }) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    const { id } = await params;
    const db = await getDb();
    const account = await db.get('SELECT * FROM accounts WHERE id = ?', id);
    if (!account) return NextResponse.json({ error: 'Account not found.' }, { status: 404 });
    const password = temporaryPassword();
    await db.run(
        'UPDATE accounts SET password_hash = ?, must_change_password = 1, updated_at = ? WHERE id = ?',
        hashPassword(password),
        nowIso(),
        id
    );
    await revokeSessions(id);
    await audit(db, auth.user, account, 'password_reset');
    return NextResponse.json(
        { temporaryPassword: password, user_code: account.user_code },
        { headers: { 'Cache-Control': 'no-store' } }
    );
}
