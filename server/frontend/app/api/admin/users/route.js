import { NextResponse } from 'next/server';
import { audit, getDb, LOGIN_ID_PATTERN, newId, nextUserCode, nowIso, publicAccount, ROLES } from '@/lib/appDb.mjs';
import { hashPassword, temporaryPassword } from '@/lib/password.mjs';
import { requireAdmin } from '@/lib/session';
import { cleanText } from './shared';

/* Account list and creation (administrators only). */

export async function GET(request) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    const rows = await (await getDb()).all('SELECT * FROM accounts ORDER BY user_code');
    return NextResponse.json(rows.map(publicAccount), { headers: { 'Cache-Control': 'no-store' } });
}

/* Create an account with a temporary password (returned only in this response; must be changed at first sign-in). */
export async function POST(request) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    let body;
    try {
        body = await request.json();
    } catch {
        body = {};
    }
    const loginId = typeof body?.login_id === 'string' ? body.login_id.trim() : '';
    if (!LOGIN_ID_PATTERN.test(loginId)) {
        return NextResponse.json({ error: 'User id: 2-100 characters of letters, digits and ._@+-' }, { status: 400 });
    }
    const role = body.role ?? 'viewer';
    if (!ROLES.includes(role))
        return NextResponse.json({ error: `role must be one of ${ROLES.join(', ')}` }, { status: 400 });
    const fields = {};
    for (const key of ['name', 'email', 'affiliation']) {
        const value = cleanText(body[key], key);
        if (value?.error) return NextResponse.json({ error: value.error }, { status: 400 });
        fields[key] = value ?? '';
    }
    if (!fields.name) return NextResponse.json({ error: 'Enter a name.' }, { status: 400 });

    const db = await getDb();
    if (await db.get('SELECT 1 FROM accounts WHERE lower(login_id) = lower(?)', loginId)) {
        return NextResponse.json({ error: 'This user id is already taken.' }, { status: 409 });
    }
    const password = temporaryPassword();
    const now = nowIso();
    const account = await db.transaction(async tx => {
        const id = newId();
        const code = await nextUserCode(tx);
        await tx.run(
            `INSERT INTO accounts(id, user_code, login_id, name, email, affiliation, role, status,
                    must_change_password, password_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)`,
            id,
            code,
            loginId,
            fields.name,
            fields.email,
            fields.affiliation,
            role,
            hashPassword(password),
            now,
            now
        );
        const created = await tx.get('SELECT * FROM accounts WHERE id = ?', id);
        await audit(tx, auth.user, created, 'created', {
            login_id: loginId,
            name: fields.name,
            role,
            affiliation: fields.affiliation,
        });
        return created;
    });
    return NextResponse.json(
        { user: publicAccount(account), temporaryPassword: password },
        { status: 201, headers: { 'Cache-Control': 'no-store' } }
    );
}
