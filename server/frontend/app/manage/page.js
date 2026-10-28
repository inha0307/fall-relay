'use client';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Users, Plus, Search, X, Copy, KeyRound, UserX, UserCheck, Trash2, History } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { ROLE_LABELS, ROLE_HELP, roleHome } from '@/utils/roles';
import { dateTime } from '@/utils/format';
import Page from '@/components/ui/Page';
import StatusChip from '@/components/ui/StatusChip';
import ui from '@/components/ui.module.css';

/* Account administration: list, create, edit, deactivate, temporary passwords, change history. */

const ACTION_LABELS = {
    migrated: 'Imported from the earlier account table',
    created: 'Account created',
    updated: 'Details changed',
    deactivated: 'Deactivated',
    reactivated: 'Reactivated',
    password_reset: 'Temporary password issued',
    password_changed: 'Password changed by the user',
    password_set: 'Password set from the server console',
    deleted: 'Deleted',
    role_migrated: 'Role renamed by a software update',
};
const formatIso = iso => (iso ? dateTime(iso, { seconds: false }) : '—');

async function api(path, { method = 'GET', body } = {}) {
    const res = await fetch(path, {
        method,
        cache: 'no-store',
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
}

const RoleChip = ({ role }) => (
    <StatusChip tone={role === 'admin' ? 'ok' : 'neutral'}>{ROLE_LABELS[role] || role}</StatusChip>
);
const StatusOf = ({ status }) => (
    <StatusChip tone={status === 'active' ? 'ok' : 'offline'}>
        {status === 'active' ? 'Active' : 'Deactivated'}
    </StatusChip>
);

function Modal({ title, onClose, children, width = 520 }) {
    return (
        <div className={ui.overlay} onClick={onClose}>
            <div
                className={ui.dialog}
                style={{ width: `min(${width}px, 100%)` }}
                role="dialog"
                aria-modal="true"
                aria-label={title}
                onClick={e => e.stopPropagation()}
            >
                <div className={ui.row} style={{ marginBottom: 16 }}>
                    <h2 style={{ margin: 0, fontSize: 18 }}>{title}</h2>
                    <button type="button" className={ui.button} onClick={onClose} aria-label="Close">
                        <X size={16} />
                    </button>
                </div>
                {children}
            </div>
        </div>
    );
}

/* Shows a temporary password once. */
function SecretDialog({ secret, onClose }) {
    const [copied, setCopied] = useState(false);
    const copy = async () => {
        try {
            await navigator.clipboard.writeText(secret.password);
            setCopied(true);
        } catch {
            setCopied(false);
        }
    };
    return (
        <Modal title={`Temporary password for ${secret.code}`} onClose={onClose} width={460}>
            <p className={ui.warning}>
                Shown only once. Pass it to the user securely; they must replace it at their first sign-in.
            </p>
            <code className={ui.codeBlock} style={{ fontSize: 18, letterSpacing: 1, textAlign: 'center' }}>
                {secret.password}
            </code>
            <div className={ui.toolbar} style={{ justifyContent: 'flex-end', marginTop: 14 }}>
                <button type="button" className={ui.button} onClick={copy}>
                    <Copy size={14} />
                    {copied ? 'Copied' : 'Copy'}
                </button>
                <button type="button" className={`${ui.button} ${ui.primary}`} onClick={onClose}>
                    Done
                </button>
            </div>
        </Modal>
    );
}

function RoleSelect({ value, onChange, disabled, label }) {
    return (
        <label className={ui.field}>
            {label}
            <select className={ui.control} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
                {Object.entries(ROLE_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>
                        {v}
                    </option>
                ))}
            </select>
            <span className={ui.note} style={{ margin: 0, fontWeight: 400 }}>
                {ROLE_HELP[value]}
            </span>
        </label>
    );
}

function AddAccountDialog({ onClose, onCreated }) {
    const [form, setForm] = useState({ login_id: '', name: '', email: '', affiliation: '', role: 'viewer' });
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const set = k => e => setForm(f => ({ ...f, [k]: e.target.value }));
    const submit = async e => {
        e.preventDefault();
        setBusy(true);
        setError('');
        try {
            const data = await api('/api/admin/users', { method: 'POST', body: form });
            onCreated({ code: data.user.user_code, password: data.temporaryPassword });
        } catch (err) {
            setError(err.message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal title="New account" onClose={onClose}>
            <form onSubmit={submit} className={ui.stack}>
                <p className={ui.note} style={{ margin: 0 }}>
                    A user code (U-xxxx) and a temporary password are issued automatically.
                </p>
                <label className={ui.field}>
                    User ID (for sign-in)
                    <input
                        className={ui.control}
                        required
                        value={form.login_id}
                        onChange={set('login_id')}
                        maxLength={100}
                        pattern="[A-Za-z0-9._@+\-]{2,100}"
                        placeholder="e.g. jdoe"
                        autoComplete="off"
                    />
                </label>
                <label className={ui.field}>
                    Name
                    <input
                        className={ui.control}
                        required
                        value={form.name}
                        onChange={set('name')}
                        maxLength={100}
                        placeholder="e.g. Jane Doe"
                    />
                </label>
                <div className={ui.fields}>
                    <label className={ui.field}>
                        E-mail (optional)
                        <input
                            className={ui.control}
                            type="email"
                            value={form.email}
                            onChange={set('email')}
                            maxLength={200}
                        />
                    </label>
                    <label className={ui.field}>
                        Affiliation (optional)
                        <input
                            className={ui.control}
                            value={form.affiliation}
                            onChange={set('affiliation')}
                            maxLength={100}
                            placeholder="e.g. Vision Lab"
                        />
                    </label>
                </div>
                <RoleSelect label="Role" value={form.role} onChange={role => setForm(f => ({ ...f, role }))} />
                {error && <p className={ui.error}>{error}</p>}
                <div className={ui.toolbar} style={{ justifyContent: 'flex-end' }}>
                    <button type="button" className={ui.button} onClick={onClose}>
                        Cancel
                    </button>
                    <button type="submit" className={`${ui.button} ${ui.primary}`} disabled={busy}>
                        {busy ? 'Creating…' : 'Create account'}
                    </button>
                </div>
            </form>
        </Modal>
    );
}

function AccountDialog({ accountId, me, onClose, onChanged, onSecret }) {
    const [detail, setDetail] = useState(null);
    const [form, setForm] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const load = useCallback(async () => {
        try {
            const data = await api(`/api/admin/users/${accountId}`);
            setDetail(data);
            setForm({
                name: data.user.name,
                email: data.user.email,
                affiliation: data.user.affiliation,
                role: data.user.role,
            });
        } catch (err) {
            setError(err.message);
        }
    }, [accountId]);
    useEffect(() => {
        load();
    }, [load]);
    const run = async fn => {
        setBusy(true);
        setError('');
        try {
            await fn();
            await load();
            onChanged();
        } catch (err) {
            setError(err.message);
        } finally {
            setBusy(false);
        }
    };
    if (!detail || !form)
        return (
            <Modal title="Account" onClose={onClose}>
                {error ? <p className={ui.error}>{error}</p> : <p className={ui.empty}>Loading…</p>}
            </Modal>
        );
    const u = detail.user;
    const self = u.id === me.id;
    const dirty = ['name', 'email', 'affiliation', 'role'].some(k => form[k] !== u[k]);
    const set = k => e => setForm(f => ({ ...f, [k]: e.target.value }));
    const save = () => run(() => api(`/api/admin/users/${u.id}`, { method: 'PATCH', body: form }));
    const resetPassword = () => {
        if (
            !window.confirm(
                `Issue a temporary password for ${u.user_code}? The account is signed out everywhere and must set a new password at the next sign-in.`
            )
        )
            return;
        run(async () => {
            const data = await api(`/api/admin/users/${u.id}/password`, { method: 'POST' });
            onSecret({ code: u.user_code, password: data.temporaryPassword });
        });
    };
    const deactivate = () => {
        const reason = window.prompt(
            `Deactivate ${u.user_code}? It can no longer sign in; its history is kept.\nReason (optional):`,
            ''
        );
        if (reason !== null)
            run(() => api(`/api/admin/users/${u.id}`, { method: 'PATCH', body: { status: 'deactivated', reason } }));
    };
    const reactivate = () =>
        run(() => api(`/api/admin/users/${u.id}`, { method: 'PATCH', body: { status: 'active' } }));
    const remove = () => {
        if (window.confirm(`Delete ${u.user_code} permanently? Its user code will not be reused.`))
            run(async () => {
                await api(`/api/admin/users/${u.id}`, { method: 'DELETE' });
                onClose();
            });
    };
    return (
        <Modal title={`${u.user_code} · ${u.name || u.login_id}`} onClose={onClose} width={720}>
            <div className={ui.toolbar} style={{ marginBottom: 14 }}>
                <RoleChip role={u.role} />
                <StatusOf status={u.status} />
                {u.must_change_password && <StatusChip tone="degraded">Temporary password pending</StatusChip>}
                {self && <StatusChip>You</StatusChip>}
            </div>
            {u.status === 'deactivated' && (
                <p className={ui.note}>
                    Deactivated {formatIso(u.deactivated_at)} by {u.deactivated_by || '—'}
                    {u.deactivation_reason ? ` — “${u.deactivation_reason}”` : ''}
                </p>
            )}
            <div className={ui.fields}>
                <label className={ui.field}>
                    User ID
                    <input className={ui.control} value={u.login_id} disabled />
                </label>
                <label className={ui.field}>
                    Name
                    <input className={ui.control} value={form.name} onChange={set('name')} maxLength={100} />
                </label>
                <label className={ui.field}>
                    E-mail
                    <input
                        className={ui.control}
                        type="email"
                        value={form.email}
                        onChange={set('email')}
                        maxLength={200}
                    />
                </label>
                <label className={ui.field}>
                    Affiliation
                    <input
                        className={ui.control}
                        value={form.affiliation}
                        onChange={set('affiliation')}
                        maxLength={100}
                    />
                </label>
                <RoleSelect
                    label={`Role${self ? ' (you cannot change your own role)' : ''}`}
                    value={form.role}
                    disabled={self}
                    onChange={role => setForm(f => ({ ...f, role }))}
                />
                <p className={ui.note} style={{ alignSelf: 'end' }}>
                    Created {formatIso(u.created_at)}
                    <br />
                    Last sign-in {formatIso(u.last_login_at)}
                </p>
            </div>
            {error && (
                <p className={ui.error} style={{ marginTop: 12 }}>
                    {error}
                </p>
            )}
            <div className={ui.toolbar} style={{ justifyContent: 'flex-end', marginTop: 16 }}>
                {detail.can_delete && (
                    <button type="button" className={`${ui.button} ${ui.danger}`} disabled={busy} onClick={remove}>
                        <Trash2 size={14} />
                        Delete
                    </button>
                )}
                {!self && u.status === 'active' && (
                    <button type="button" className={`${ui.button} ${ui.danger}`} disabled={busy} onClick={deactivate}>
                        <UserX size={14} />
                        Deactivate
                    </button>
                )}
                {!self && u.status === 'deactivated' && (
                    <button type="button" className={ui.button} disabled={busy} onClick={reactivate}>
                        <UserCheck size={14} />
                        Reactivate
                    </button>
                )}
                <button type="button" className={ui.button} disabled={busy} onClick={resetPassword}>
                    <KeyRound size={14} />
                    Temporary password
                </button>
                <button type="button" className={`${ui.button} ${ui.primary}`} disabled={busy || !dirty} onClick={save}>
                    Save changes
                </button>
            </div>
            <h3
                className={ui.sectionTitle}
                style={{ margin: '22px 0 8px', display: 'flex', alignItems: 'center', gap: 6 }}
            >
                <History size={15} aria-hidden="true" />
                Change history
            </h3>
            <div className={ui.history}>
                {detail.history.length === 0 ? (
                    <div className={ui.note}>No changes recorded.</div>
                ) : (
                    detail.history.map((h, i) => (
                        <div key={i}>
                            <span className={ui.note}>{formatIso(h.at)}</span> ·{' '}
                            <strong>{ACTION_LABELS[h.action] || h.action}</strong>
                            {h.actor_code ? ` by ${h.actor_code}` : ''}
                            {h.action === 'updated' &&
                                Object.entries(h.details).map(([k, v]) => (
                                    <div key={k} className={ui.note}>
                                        {k}: “{v.from || '—'}” → “{v.to || '—'}”
                                    </div>
                                ))}
                            {h.details?.reason && <div className={ui.note}>reason: {h.details.reason}</div>}
                        </div>
                    ))
                )}
            </div>
            <h3 className={ui.sectionTitle} style={{ margin: '18px 0 8px' }}>
                Recent sign-ins
            </h3>
            {detail.logins.length === 0 ? (
                <p className={ui.note}>None.</p>
            ) : (
                detail.logins.map((l, i) => (
                    <p key={i} className={ui.note} style={{ margin: 0 }}>
                        {formatIso(l.created_at)} · {l.status}
                        {l.ip_address ? ` · ${l.ip_address}` : ''}
                    </p>
                ))
            )}
        </Modal>
    );
}

export default function ManageAccounts() {
    const { user: me, role, loading: authLoading } = useAuth();
    const router = useRouter();
    const [accounts, setAccounts] = useState(null);
    const [error, setError] = useState('');
    const [query, setQuery] = useState('');
    const [statusFilter, setStatusFilter] = useState('active');
    const [adding, setAdding] = useState(false);
    const [openId, setOpenId] = useState(null);
    const [secret, setSecret] = useState(null);
    useEffect(() => {
        if (!authLoading && me && role !== 'admin') router.replace(roleHome(role));
    }, [authLoading, me, role, router]);
    const load = useCallback(async () => {
        try {
            setAccounts(await api('/api/admin/users'));
            setError('');
        } catch (err) {
            setError(err.message);
        }
    }, []);
    useEffect(() => {
        if (role === 'admin') load();
    }, [role, load]);
    const visible = useMemo(() => {
        const q = query.trim().toLowerCase();
        return (accounts || []).filter(
            a =>
                (statusFilter === 'all' || a.status === statusFilter) &&
                (!q ||
                    [a.user_code, a.login_id, a.name, a.email, a.affiliation].some(v =>
                        (v || '').toLowerCase().includes(q)
                    ))
        );
    }, [accounts, query, statusFilter]);
    if (authLoading || !me || role !== 'admin') return null;
    return (
        <Page
            icon={Users}
            title="Accounts"
            subtitle="Administrator, viewer and responder accounts. User codes are permanent and never reused; accounts that have signed in are deactivated rather than deleted."
            actions={
                <button type="button" className={`${ui.button} ${ui.primary}`} onClick={() => setAdding(true)}>
                    <Plus size={16} />
                    Add account
                </button>
            }
        >
            <div className={ui.toolbar} style={{ marginBottom: 14 }}>
                <label className={ui.toolbar} style={{ flex: '1 1 260px' }}>
                    <Search size={16} aria-hidden="true" />
                    <input
                        className={ui.control}
                        style={{ flex: 1 }}
                        type="search"
                        aria-label="Search accounts"
                        placeholder="Code, user ID, name, e-mail or affiliation"
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                    />
                </label>
                <select
                    className={ui.control}
                    aria-label="Account status"
                    value={statusFilter}
                    onChange={e => setStatusFilter(e.target.value)}
                >
                    <option value="active">Active</option>
                    <option value="deactivated">Deactivated</option>
                    <option value="all">All</option>
                </select>
            </div>
            {error && <p className={ui.error}>{error}</p>}
            <div className={ui.tableWrap}>
                <table className={ui.table} style={{ minWidth: 900 }}>
                    <thead>
                        <tr>
                            {['Code', 'Name', 'User ID', 'Affiliation', 'Role', 'Status', 'Last sign-in'].map(h => (
                                <th key={h} scope="col">
                                    {h}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {visible.map(a => (
                            <tr key={a.id} style={{ opacity: a.status === 'active' ? 1 : 0.65 }}>
                                <td>
                                    <button type="button" className={ui.rowButton} onClick={() => setOpenId(a.id)}>
                                        {a.user_code}
                                    </button>
                                </td>
                                <td>
                                    {a.name || '—'}
                                    {a.id === me.id && <span className={ui.note}> (you)</span>}
                                </td>
                                <td>{a.login_id}</td>
                                <td>{a.affiliation || '—'}</td>
                                <td>
                                    <RoleChip role={a.role} />
                                </td>
                                <td>
                                    <StatusOf status={a.status} />
                                    {a.must_change_password && (
                                        <>
                                            {' '}
                                            <StatusChip tone="degraded">Temporary password</StatusChip>
                                        </>
                                    )}
                                </td>
                                <td>{formatIso(a.last_login_at)}</td>
                            </tr>
                        ))}
                        {accounts && visible.length === 0 && (
                            <tr>
                                <td colSpan={7} className={ui.empty}>
                                    No accounts match.
                                </td>
                            </tr>
                        )}
                        {!accounts && !error && (
                            <tr>
                                <td colSpan={7} className={ui.empty}>
                                    Loading…
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
            {adding && (
                <AddAccountDialog
                    onClose={() => setAdding(false)}
                    onCreated={s => {
                        setAdding(false);
                        setSecret(s);
                        load();
                    }}
                />
            )}
            {openId && (
                <AccountDialog
                    accountId={openId}
                    me={me}
                    onClose={() => {
                        setOpenId(null);
                        load();
                    }}
                    onChanged={load}
                    onSecret={setSecret}
                />
            )}
            {secret && <SecretDialog secret={secret} onClose={() => setSecret(null)} />}
        </Page>
    );
}
