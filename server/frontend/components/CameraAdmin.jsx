'use client';
import { useCallback, useEffect, useState } from 'react';
import { Camera, KeyRound, Plus, Copy, X } from 'lucide-react';
import StatusChip from '@/components/ui/StatusChip';
import ui from './ui.module.css';

/* Camera registration on /system: add, rename/relocate, disable, per-camera device tokens (administrators only). */
async function adminFetch(path, { method = 'GET', body } = {}) {
    const res = await fetch(path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
        cache: 'no-store',
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { status: res.status });
    return data;
}

function TokenState({ cam }) {
    if (!cam.enabled) return <StatusChip tone="offline">Disabled</StatusChip>;
    const rejects = cam.auth_rejects > 0 ? ` · ${cam.auth_rejects} refused` : '';
    if (cam.token_set)
        return (
            <StatusChip tone={cam.push_auth === 'token' ? 'ok' : 'degraded'}>
                {cam.push_auth === 'token' ? 'Token in use' : 'Waiting for the device'}
                {rejects}
            </StatusChip>
        );
    return <StatusChip tone="down">No token{rejects}</StatusChip>;
}

function TokenDialog({ issued, onClose }) {
    const [copied, setCopied] = useState(false);
    const line = `FALLRELAY_CAMERA_TOKEN=${issued.token}`; // variable name read by the edge runtime
    const copy = async () => {
        try {
            await navigator.clipboard.writeText(line);
            setCopied(true);
        } catch {
            setCopied(false);
        }
    };
    return (
        <div className={ui.overlay} onClick={onClose}>
            <div
                className={ui.dialog}
                role="dialog"
                aria-modal="true"
                aria-labelledby="token-title"
                onClick={e => e.stopPropagation()}
            >
                <div className={ui.row}>
                    <h2 id="token-title" style={{ margin: 0 }}>
                        Device token for camera {issued.camera_id}
                    </h2>
                    <button type="button" className={ui.button} onClick={onClose} aria-label="Close">
                        <X size={16} />
                    </button>
                </div>
                <p className={ui.warning} style={{ marginTop: 12 }}>
                    This token is shown only once. Store it on the edge device now; until then the server refuses this
                    camera&apos;s decisions.
                </p>
                <code className={ui.codeBlock}>{line}</code>
                <p>
                    <button type="button" className={ui.button} onClick={copy}>
                        <Copy size={14} />
                        {copied ? 'Copied' : 'Copy'}
                    </button>
                </p>
                <p className={ui.note}>
                    On the edge device, save this line as <code>camera_token_cam{issued.camera_id}.env</code> in the
                    runtime folder (permissions 600) and restart the detection service. See{' '}
                    <code>edge/deploy/README.md</code>.
                </p>
            </div>
        </div>
    );
}

function CameraRow({ cam, onSaved, onError, onIssued }) {
    const [label, setLabel] = useState(cam.label);
    const [location, setLocation] = useState(cam.location);
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        setLabel(cam.label);
        setLocation(cam.location);
    }, [cam.label, cam.location]);
    const dirty = label !== cam.label || location !== cam.location;
    const run = async fn => {
        setBusy(true);
        try {
            await fn();
            onSaved();
        } catch (err) {
            onError(err.message);
        } finally {
            setBusy(false);
        }
    };
    const save = () =>
        run(() => adminFetch(`/api/admin/cameras/${cam.id}`, { method: 'PATCH', body: { label, location } }));
    const toggle = () => {
        if (cam.enabled && !window.confirm(`Disable camera ${cam.id}? The server stops accepting its decisions.`))
            return;
        run(() => adminFetch(`/api/admin/cameras/${cam.id}`, { method: 'PATCH', body: { enabled: !cam.enabled } }));
    };
    const issue = () => {
        const warn = cam.token_set
            ? `Issue a new token for camera ${cam.id}? The current token stops working now, so the camera is offline until its device has the new one.`
            : `Issue a token for camera ${cam.id}? Decisions without it are refused from now on, so the camera is offline until its device has the token.`;
        if (window.confirm(warn))
            run(async () => onIssued(await adminFetch(`/api/admin/cameras/${cam.id}/token`, { method: 'POST' })));
    };
    const clear = () => {
        if (
            window.confirm(
                `Clear the token for camera ${cam.id}? The server then accepts this camera without authentication.`
            )
        )
            run(() => adminFetch(`/api/admin/cameras/${cam.id}/token`, { method: 'DELETE' }));
    };
    return (
        <tr style={{ opacity: cam.enabled ? 1 : 0.6 }}>
            <td>
                <strong>{cam.id}</strong>
            </td>
            <td>
                <input
                    className={ui.control}
                    aria-label={`Label of camera ${cam.id}`}
                    value={label}
                    maxLength={64}
                    onChange={e => setLabel(e.target.value)}
                    disabled={busy}
                />
            </td>
            <td>
                <input
                    className={ui.control}
                    aria-label={`Location of camera ${cam.id}`}
                    value={location}
                    maxLength={64}
                    placeholder="e.g. Lab room A"
                    onChange={e => setLocation(e.target.value)}
                    disabled={busy}
                />
            </td>
            <td>
                {cam.enabled
                    ? `${cam.edge_inference_connected ? 'Decisions arriving' : 'No decisions'}${cam.video_connected ? ' · debug video on' : ''}`
                    : '—'}
            </td>
            <td>
                <TokenState cam={cam} />
            </td>
            <td>
                <div className={ui.toolbar}>
                    <button type="button" className={ui.button} disabled={!dirty || busy} onClick={save}>
                        Save
                    </button>
                    {cam.enabled && (
                        <button type="button" className={ui.button} disabled={busy} onClick={issue}>
                            <KeyRound size={13} />
                            {cam.token_set ? 'Reissue token' : 'Issue token'}
                        </button>
                    )}
                    {cam.enabled && cam.token_set && (
                        <button type="button" className={ui.button} disabled={busy} onClick={clear}>
                            Clear token
                        </button>
                    )}
                    <button
                        type="button"
                        className={`${ui.button} ${cam.enabled ? ui.danger : ''}`}
                        disabled={busy}
                        onClick={toggle}
                    >
                        {cam.enabled ? 'Disable' : 'Enable'}
                    </button>
                </div>
            </td>
        </tr>
    );
}

export default function CameraAdmin() {
    const [cameras, setCameras] = useState(null);
    const [requireTokens, setRequireTokens] = useState(false);
    const [error, setError] = useState('');
    const [denied, setDenied] = useState(false);
    const [issued, setIssued] = useState(null);
    const [draft, setDraft] = useState({ id: '', label: '', location: '' });
    const [adding, setAdding] = useState(false);
    const load = useCallback(async () => {
        try {
            const data = await adminFetch('/api/admin/cameras');
            setCameras(data.cameras || []);
            setRequireTokens(!!data.require_tokens);
            setDenied(false);
        } catch (err) {
            if (err.status === 401 || err.status === 403) setDenied(true);
            else setError(err.message);
        }
    }, []);
    useEffect(() => {
        load();
        const timer = setInterval(load, 5000);
        return () => clearInterval(timer);
    }, [load]);
    const add = async e => {
        e.preventDefault();
        setAdding(true);
        setError('');
        try {
            await adminFetch('/api/admin/cameras', { method: 'POST', body: draft });
            setDraft({ id: '', label: '', location: '' });
            await load();
        } catch (err) {
            setError(`Could not add the camera: ${err.message}`);
        } finally {
            setAdding(false);
        }
    };
    if (denied)
        return (
            <p className={ui.note}>
                <Camera size={14} aria-hidden="true" /> Camera registration is available to administrators.
            </p>
        );
    const unauthenticated = (cameras || []).filter(c => c.enabled && !c.token_set);
    return (
        <section className={ui.card}>
            <div className={ui.row}>
                <h2 style={{ margin: 0 }}>Camera registration</h2>
                <span className={ui.note}>
                    Changes apply immediately, without a restart.{requireTokens ? ' Device tokens are required.' : ''}
                </span>
            </div>
            {unauthenticated.length > 0 && !requireTokens && (
                <p className={ui.warning} style={{ marginTop: 12 }}>
                    Cameras accepted without a token: {unauthenticated.map(c => c.id).join(', ')}. Anyone on the network
                    can send decisions under these IDs; issue tokens before exposing the server.
                </p>
            )}
            {error && (
                <p className={ui.error} style={{ marginTop: 12 }}>
                    {error}
                </p>
            )}
            <div className={ui.tableWrap} style={{ margin: '12px 0' }}>
                <table className={ui.table} style={{ minWidth: 860 }}>
                    <thead>
                        <tr>
                            {['ID', 'Label', 'Location', 'Decisions', 'Device token', 'Actions'].map(h => (
                                <th key={h} scope="col">
                                    {h}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {(cameras || []).map(cam => (
                            <CameraRow key={cam.id} cam={cam} onSaved={load} onError={setError} onIssued={setIssued} />
                        ))}
                        {cameras === null && (
                            <tr>
                                <td colSpan={6} className={ui.empty}>
                                    Loading…
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
            <form onSubmit={add} className={ui.inlineForm} aria-label="Add a camera">
                <input
                    className={ui.control}
                    style={{ width: 110 }}
                    placeholder="ID (e.g. 3)"
                    aria-label="New camera ID"
                    value={draft.id}
                    maxLength={32}
                    pattern="[A-Za-z0-9_\-]{1,32}"
                    required
                    onChange={e => setDraft(d => ({ ...d, id: e.target.value }))}
                />
                <input
                    className={ui.control}
                    style={{ width: 180 }}
                    placeholder="Label (e.g. CAM 3)"
                    aria-label="New camera label"
                    value={draft.label}
                    maxLength={64}
                    onChange={e => setDraft(d => ({ ...d, label: e.target.value }))}
                />
                <input
                    className={ui.control}
                    style={{ width: 200 }}
                    placeholder="Location (optional)"
                    aria-label="New camera location"
                    value={draft.location}
                    maxLength={64}
                    onChange={e => setDraft(d => ({ ...d, location: e.target.value }))}
                />
                <button type="submit" disabled={adding} className={`${ui.button} ${ui.primary}`}>
                    <Plus size={14} />
                    Add camera
                </button>
            </form>
            {issued && (
                <TokenDialog
                    issued={issued}
                    onClose={() => {
                        setIssued(null);
                        load();
                    }}
                />
            )}
        </section>
    );
}
