'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { UserCircle, KeyRound } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { ROLE_LABELS, ROLE_HELP, roleHome } from '@/utils/roles';
import { dateTime } from '@/utils/format';
import Page from '@/components/ui/Page';
import ui from '@/components/ui.module.css';

const formatIso = iso => (iso ? dateTime(iso, { seconds: false }) : '—');

/* Own account: details and password change (forced after a temporary password). */
export default function AccountPage() {
    const { user, loading: authLoading, refresh } = useAuth();
    const router = useRouter();
    const [form, setForm] = useState({ current: '', next: '', confirm: '' });
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState(null); // { ok, text }
    if (authLoading || !user) return null;
    const submit = async e => {
        e.preventDefault();
        setMessage(null);
        if (form.next !== form.confirm) {
            setMessage({ ok: false, text: 'The new passwords do not match.' });
            return;
        }
        setBusy(true);
        try {
            const res = await fetch('/api/auth/password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: form.current, new_password: form.next }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
            setForm({ current: '', next: '', confirm: '' });
            const wasForced = user.must_change_password;
            await refresh();
            setMessage({ ok: true, text: 'Password changed. Other sessions of this account were signed out.' });
            if (wasForced) router.replace(roleHome(user.role));
        } catch (err) {
            setMessage({ ok: false, text: err.message });
        } finally {
            setBusy(false);
        }
    };
    const rows = [
        ['User code', user.user_code],
        ['User ID', user.login_id],
        ['Name', user.name || '—'],
        ['E-mail', user.email || '—'],
        ['Affiliation', user.affiliation || '—'],
        ['Role', `${ROLE_LABELS[user.role] || user.role} — ${ROLE_HELP[user.role] || ''}`],
        ['Last sign-in', formatIso(user.last_login_at)],
        ['Created', formatIso(user.created_at)],
    ];
    const field = (id, labelText, props) => (
        <label className={ui.field} htmlFor={id}>
            {labelText}
            <input
                id={id}
                type="password"
                required
                className={ui.control}
                value={form[props.key]}
                onChange={e => setForm(f => ({ ...f, [props.key]: e.target.value }))}
                minLength={props.min}
                autoComplete={props.auto}
            />
        </label>
    );
    return (
        <Page icon={UserCircle} title="My account">
            <div className={ui.stack} style={{ maxWidth: 760 }}>
                {user.must_change_password && (
                    <p className={ui.warning}>
                        You signed in with a temporary password. Set your own password to continue.
                    </p>
                )}
                <section className={ui.card}>
                    <table className={ui.table}>
                        <tbody>
                            {rows.map(([k, v]) => (
                                <tr key={k}>
                                    <th scope="row" style={{ width: 160, background: 'none' }}>
                                        {k}
                                    </th>
                                    <td>{k === 'User code' ? <strong>{v}</strong> : v}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                    <p className={ui.note}>Name, affiliation and role are managed by an administrator.</p>
                </section>
                <form onSubmit={submit} className={`${ui.card} ${ui.stack}`}>
                    <h2 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <KeyRound size={18} aria-hidden="true" />
                        Change password
                    </h2>
                    {field('current', user.must_change_password ? 'Temporary password' : 'Current password', {
                        key: 'current',
                        auto: 'current-password',
                    })}
                    {field('next', 'New password (at least 8 characters)', {
                        key: 'next',
                        min: 8,
                        auto: 'new-password',
                    })}
                    {field('confirm', 'Repeat new password', { key: 'confirm', min: 8, auto: 'new-password' })}
                    {message && (
                        <p role="status" className={message.ok ? ui.note : ui.error}>
                            {message.text}
                        </p>
                    )}
                    <div>
                        <button type="submit" disabled={busy} className={`${ui.button} ${ui.primary}`}>
                            {busy ? 'Saving…' : 'Change password'}
                        </button>
                    </div>
                </form>
            </div>
        </Page>
    );
}
