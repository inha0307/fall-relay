import { NextResponse } from 'next/server';
import { ACCOUNT_STATUSES, audit, getDb, nowIso, publicAccount, ROLES } from '@/lib/appDb.mjs';
import { requireAdmin, revokeSessions } from '@/lib/session';
import { activeAdminCount, cleanText, hasSignedIn } from '../shared';

/* One account: details with change history, edits, deactivation, deletion (administrators only). */

const load = (db, id) => db.get('SELECT * FROM accounts WHERE id = ?', id);
const notFound = () => NextResponse.json({ error: 'Account not found.' }, { status: 404 });

export async function GET(request, { params }) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    const { id } = await params;
    const db = await getDb();
    const account = await load(db, id);
    if (!account) return notFound();
    const history = (
        await db.all(
            'SELECT at, actor_code, action, details FROM account_audit WHERE target_id = ? ORDER BY id DESC LIMIT 100',
            id
        )
    ).map(h => ({ ...h, details: JSON.parse(h.details || '{}') }));
    const logins = await db.all(
        'SELECT created_at, status, ip_address FROM login_logs WHERE account_id = ? ORDER BY created_at DESC LIMIT 20',
        id
    );
    return NextResponse.json(
        {
            user: publicAccount(account),
            can_delete: !(await hasSignedIn(db, account)) && account.id !== auth.user.id,
            history,
            logins,
        },
        { headers: { 'Cache-Control': 'no-store' } }
    );
}

/* Edit name/email/affiliation/role and/or set status ('active' | 'deactivated', with an optional reason). */
export async function PATCH(request, { params }) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    const { id } = await params;
    let body;
    try {
        body = await request.json();
    } catch {
        body = null;
    }
    if (!body || typeof body !== 'object') return NextResponse.json({ error: 'invalid JSON' }, { status: 400 });

    const db = await getDb();
    const account = await load(db, id);
    if (!account) return notFound();
    const updates = {};
    for (const key of ['name', 'email', 'affiliation']) {
        const value = cleanText(body[key], key);
        if (value?.error) return NextResponse.json({ error: value.error }, { status: 400 });
        if (value !== undefined && value !== account[key]) updates[key] = value;
    }
    if ('name' in updates && !updates.name)
        return NextResponse.json({ error: 'Name cannot be empty.' }, { status: 400 });
    if (body.role !== undefined && body.role !== account.role) {
        if (!ROLES.includes(body.role))
            return NextResponse.json({ error: `role must be one of ${ROLES.join(', ')}` }, { status: 400 });
        updates.role = body.role;
    }
    if (body.status !== undefined && body.status !== account.status) {
        if (!ACCOUNT_STATUSES.includes(body.status))
            return NextResponse.json({ error: 'status must be active or deactivated' }, { status: 400 });
        updates.status = body.status;
    }
    const reason = cleanText(body.reason, 'reason');
    if (reason?.error) return NextResponse.json({ error: reason.error }, { status: 400 });
    if (Object.keys(updates).length === 0) return NextResponse.json({ user: publicAccount(account), unchanged: true });

    const self = account.id === auth.user.id;
    if (self && ('role' in updates || 'status' in updates)) {
        return NextResponse.json({ error: 'You cannot change your own role or status.' }, { status: 400 });
    }
    const losesAdmin =
        account.role === 'admin' &&
        account.status === 'active' &&
        ((updates.role && updates.role !== 'admin') || updates.status === 'deactivated');
    if (losesAdmin && (await activeAdminCount(db)) <= 1) {
        return NextResponse.json({ error: 'At least one active administrator must remain.' }, { status: 400 });
    }

    const now = nowIso();
    const result = await db.transaction(async tx => {
        const columns = { ...updates, updated_at: now };
        if (updates.status === 'deactivated')
            Object.assign(columns, {
                deactivated_at: now,
                deactivated_by: auth.user.user_code,
                deactivation_reason: reason || '',
            });
        if (updates.status === 'active')
            Object.assign(columns, { deactivated_at: null, deactivated_by: null, deactivation_reason: '' });
        await tx.run(
            `UPDATE accounts SET ${Object.keys(columns)
                .map(k => `${k} = ?`)
                .join(', ')} WHERE id = ?`,
            ...Object.values(columns),
            id
        );
        const changes = Object.fromEntries(
            Object.keys(updates)
                .filter(k => k !== 'status')
                .map(k => [k, { from: account[k], to: updates[k] }])
        );
        if (Object.keys(changes).length) await audit(tx, auth.user, account, 'updated', changes);
        if (updates.role) await audit(tx, auth.user, account, 'role_changed', { from: account.role, to: updates.role });
        if (updates.status)
            await audit(
                tx,
                auth.user,
                account,
                updates.status === 'deactivated' ? 'deactivated' : 'reactivated',
                reason ? { reason } : {}
            );
        return load(tx, id);
    });
    // A new role or a deactivation must take effect now, not at the next sign-in.
    if ('role' in updates || updates.status === 'deactivated') await revokeSessions(id);
    return NextResponse.json({ user: publicAccount(result) });
}

/* Delete only accounts that never signed in (e.g. created by mistake); others must be deactivated. */
export async function DELETE(request, { params }) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    const { id } = await params;
    const db = await getDb();
    const account = await load(db, id);
    if (!account) return notFound();
    if (account.id === auth.user.id)
        return NextResponse.json({ error: 'You cannot delete your own account.' }, { status: 400 });
    if (await hasSignedIn(db, account)) {
        return NextResponse.json(
            { error: 'This account has signed in before. Deactivate it instead of deleting it.' },
            { status: 409 }
        );
    }
    if (account.role === 'admin' && account.status === 'active' && (await activeAdminCount(db)) <= 1) {
        return NextResponse.json({ error: 'At least one active administrator must remain.' }, { status: 400 });
    }
    await db.transaction(async tx => {
        await audit(tx, auth.user, account, 'deleted', { login_id: account.login_id, name: account.name });
        await tx.run('DELETE FROM accounts WHERE id = ?', id); // sessions go via ON DELETE CASCADE
    });
    return NextResponse.json({ ok: true });
}
