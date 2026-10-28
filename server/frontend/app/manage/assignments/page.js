'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useAuth } from '@/context/AuthContext';
import { Cctv } from 'lucide-react';
import Page from '@/components/ui/Page';
import ui from '@/components/ui.module.css';
async function api(path, options = {}) {
    const res = await fetch(path, { cache: 'no-store', ...options });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'The request failed.');
    return data;
}
export default function CameraAssignments() {
    const { role } = useAuth();
    const [users, setUsers] = useState([]),
        [cameras, setCameras] = useState([]),
        [selected, setSelected] = useState('');
    const [grants, setGrants] = useState([]),
        [error, setError] = useState(''),
        [message, setMessage] = useState(''),
        [busy, setBusy] = useState(false),
        [ready, setReady] = useState(false);
    useEffect(() => {
        if (role !== 'admin') return;
        let active = true;
        Promise.all([api('/api/admin/users'), api('/api/admin/cameras')])
            .then(([u, c]) => {
                if (active) {
                    setUsers(u.filter(v => v.role === 'responder' && v.status === 'active'));
                    setCameras(c.cameras.filter(v => v.enabled !== false && v.enabled !== 0));
                }
            })
            .catch(e => {
                if (active) setError(e.message);
            });
        return () => {
            active = false;
        };
    }, [role]);
    useEffect(() => {
        setReady(false);
        setGrants([]);
        setMessage('');
        setError('');
        if (!selected) return;
        let active = true;
        api(`/api/admin/users/${selected}/cameras`)
            .then(data => {
                if (active) {
                    setGrants(data.cameras);
                    setReady(true);
                }
            })
            .catch(e => {
                if (active) setError(e.message);
            });
        return () => {
            active = false;
        };
    }, [selected]);
    async function save() {
        setBusy(true);
        setError('');
        setMessage('');
        try {
            const data = await api(`/api/admin/users/${selected}/cameras`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cameras: grants }),
            });
            setGrants(data.cameras);
            setMessage('Saved. Open alert connections pick this up when they reconnect (within about 20 s).');
        } catch (e) {
            setError(e.message);
        } finally {
            setBusy(false);
        }
    }
    if (role !== 'admin')
        return (
            <Page icon={Cctv} title="Camera access">
                <p className={ui.note}>Available to administrators.</p>
            </Page>
        );
    return (
        <Page
            icon={Cctv}
            title="Camera access"
            subtitle="Assign cameras to responder accounts. A responder sees and handles alerts only for the cameras assigned here."
            back={
                <Link className={`${ui.link} ${ui.back}`} href="/manage">
                    Accounts
                </Link>
            }
        >
            {error && (
                <p role="alert" className={ui.error}>
                    {error}
                </p>
            )}
            {message && (
                <p role="status" className={ui.note}>
                    {message}
                </p>
            )}
            <section className={`${ui.card} ${ui.stack}`}>
                <label className={ui.field}>
                    Responder
                    <select
                        aria-label="Responder"
                        className={ui.control}
                        value={selected}
                        disabled={busy}
                        onChange={e => setSelected(e.target.value)}
                    >
                        <option value="">Select an account</option>
                        {users.map(u => (
                            <option key={u.id} value={u.id}>
                                {u.name} · {u.user_code}
                            </option>
                        ))}
                    </select>
                </label>
                {!users.length && (
                    <p className={ui.note}>
                        No active responder accounts. Create one under Accounts with the Responder role.
                    </p>
                )}
                <fieldset disabled={!ready || busy} style={{ border: 0, margin: 0, padding: 0 }}>
                    <legend className={ui.sectionTitle}>Cameras this responder can see</legend>
                    {cameras.map(c => (
                        <label key={c.id} style={{ display: 'block', padding: '8px 0' }}>
                            <input
                                type="checkbox"
                                checked={grants.includes(String(c.id))}
                                onChange={e =>
                                    setGrants(previous =>
                                        e.target.checked
                                            ? [...previous, String(c.id)]
                                            : previous.filter(id => id !== String(c.id))
                                    )
                                }
                            />{' '}
                            {c.label || c.id}
                            {c.location ? ` · ${c.location}` : ''} ({c.id})
                        </label>
                    ))}
                </fieldset>
                <div>
                    <button
                        type="button"
                        className={`${ui.button} ${ui.primary}`}
                        disabled={!ready || busy}
                        onClick={save}
                    >
                        {busy ? 'Saving…' : 'Save access'}
                    </button>
                </div>
            </section>
        </Page>
    );
}
