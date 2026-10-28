'use client';
import { useCallback, useEffect, useState } from 'react';
import { dateTime } from '@/utils/format';
import ui from '../ui.module.css';

const STATE = {
    queued: 'Waiting for the device',
    sent: 'Running on the device',
    done: 'Done',
    failed: 'Failed',
    expired: 'Not picked up',
};
const GROUPS = [
    ['service', 'Detection service'],
    ['device', 'Device'],
];

/* Commands come from the server's catalogue; which ones a device accepts comes from its own agent. */
export function useCommands(deviceId, enabled) {
    const [items, setItems] = useState([]);
    const load = useCallback(async () => {
        if (!enabled) return;
        try {
            const res = await fetch(`/api/admin/devices/${encodeURIComponent(deviceId)}/commands`, {
                cache: 'no-store',
            });
            if (res.ok) setItems((await res.json()).commands || []);
        } catch {}
    }, [deviceId, enabled]);
    useEffect(() => {
        load();
        const timer = setInterval(load, 3000);
        return () => clearInterval(timer);
    }, [load]);
    return [items, load];
}

export default function RemoteActions({ device, catalog, admin }) {
    const [items, reload] = useCommands(device.device_id, admin);
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState('');
    const allowed = device.control?.commands || [];
    const running = items.some(c => ['queued', 'sent'].includes(c.state));
    async function send(command, confirmOpen = false) {
        const spec = catalog[command] || { label: command };
        if (!confirmOpen && spec.confirm && !window.confirm(spec.confirm)) return;
        setBusy(true);
        setMessage('');
        try {
            const res = await fetch(`/api/admin/devices/${encodeURIComponent(device.device_id)}/commands`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command, confirm_open_event: confirmOpen }),
            });
            const data = await res.json().catch(() => ({}));
            if (res.status === 409 && data.code === 'open_fall_event') {
                if (
                    window.confirm(
                        'A fall event is open on this camera. Interrupting detection closes it as a runtime stop. Continue?'
                    )
                )
                    return send(command, true);
                return;
            }
            setMessage(
                res.ok
                    ? `${spec.label}: sent. The device picks it up within a few seconds.`
                    : data.error || 'The command was refused.'
            );
            reload();
        } finally {
            setBusy(false);
        }
    }
    if (!admin)
        return <p className={ui.note}>Administrators can restart detection, fetch logs or reboot the device here.</p>;
    return (
        <div className={ui.stack}>
            {!allowed.length && (
                <p className={ui.warning}>
                    Remote control is not enabled on this device. Enable it in the device agent&apos;s
                    &quot;control&quot; settings.
                </p>
            )}
            {allowed.length > 0 && device.stale && (
                <p className={ui.warning}>The device is not reporting, so it cannot pick up commands now.</p>
            )}
            {GROUPS.map(([group, title]) => {
                const names = Object.entries(catalog).filter(
                    ([name, spec]) => spec.group === group && allowed.includes(name)
                );
                if (!names.length) return null;
                return (
                    <section key={group} className={ui.card}>
                        <h2>{title}</h2>
                        <div className={ui.toolbar}>
                            {names.map(([name, spec]) => (
                                <button
                                    key={name}
                                    type="button"
                                    className={`${ui.button} ${spec.interrupts_detection ? ui.danger : ''}`}
                                    disabled={busy || running || device.stale}
                                    onClick={() => send(name)}
                                >
                                    {spec.label}
                                </button>
                            ))}
                        </div>
                    </section>
                );
            })}
            {message && (
                <p className={ui.note} role="status">
                    {message}
                </p>
            )}
            {items.length > 0 && (
                <section>
                    <h2 className={ui.sectionTitle}>Recent commands</h2>
                    <div className={ui.tableWrap}>
                        <table className={ui.table}>
                            <thead>
                                <tr>
                                    <th scope="col">Requested</th>
                                    <th scope="col">Command</th>
                                    <th scope="col">By</th>
                                    <th scope="col">State</th>
                                </tr>
                            </thead>
                            <tbody>
                                {items.slice(0, 10).map(c => (
                                    <tr key={c.id}>
                                        <td>{dateTime(c.requested_at)}</td>
                                        <td>{catalog[c.command]?.label || c.command}</td>
                                        <td>{c.requested_by}</td>
                                        <td>
                                            {STATE[c.state] || c.state}
                                            {c.result ? (
                                                <details>
                                                    <summary>Output</summary>
                                                    <pre
                                                        className={ui.mono}
                                                        style={{
                                                            maxHeight: 260,
                                                            overflow: 'auto',
                                                            whiteSpace: 'pre-wrap',
                                                        }}
                                                    >
                                                        {c.result}
                                                    </pre>
                                                </details>
                                            ) : null}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </section>
            )}
        </div>
    );
}
