'use client';
import { useCallback, useEffect, useState } from 'react';
import ui from './ui.module.css';
import { bytes as size, dateTime as date } from '@/utils/format';

export default function DecisionLogs({ device }) {
    const [minutes, setMinutes] = useState(30);
    const [auto, setAuto] = useState(true);
    const [sessions, setSessions] = useState([]);
    const [commands, setCommands] = useState([]);
    const [message, setMessage] = useState('');
    const [sending, setSending] = useState(false);
    const [error, setError] = useState('');
    const key = encodeURIComponent(device.device_id);
    const load = useCallback(async () => {
        try {
            const [a, b] = await Promise.all([
                fetch(`/api/admin/decision-logs/${key}`, { cache: 'no-store' }),
                fetch(`/api/admin/devices/${key}/commands`, { cache: 'no-store' }),
            ]);
            if (!a.ok || !b.ok) throw new Error('Cannot refresh log status.');
            setSessions((await a.json()).sessions || []);
            setCommands((await b.json()).commands || []);
            setError('');
        } catch (e) {
            setError(e.message);
        }
    }, [key]);
    useEffect(() => {
        load();
        const timer = setInterval(load, 5000);
        return () => clearInterval(timer);
    }, [load]);
    const status = device.control?.decision_log;
    const enabled = device.control?.commands?.includes('decision_log_start');
    const unavailable = !status || status.stale || device.stale;
    const pending = commands.some(c => ['queued', 'sent'].includes(c.state));
    const recent = commands.find(c => c.command.startsWith('decision_log_'));
    async function send(command, params = {}) {
        setSending(true);
        setMessage('');
        try {
            const res = await fetch(`/api/admin/devices/${key}/commands`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command, params }),
            });
            const value = await res.json();
            if (!res.ok) throw new Error(value.error || 'Command refused.');
            setMessage('Request queued. Device status below confirms when it is applied.');
            await load();
        } catch (e) {
            setMessage(e.message);
        } finally {
            setSending(false);
        }
    }
    return (
        <div className={ui.stack}>
            <section className={ui.card}>
                <h2>Decision diagnostics</h2>
                <p className={ui.note}>
                    Records every decision slot for a set time, for diagnosing detection on this device. Recording stops
                    after the selected duration or at the storage limit.
                </p>
                <p role="status">
                    <strong>
                        {unavailable ? 'Device recording status unavailable' : status.recording ? 'Recording' : 'Off'}
                    </strong>
                    {status?.recording && !unavailable && (
                        <>
                            {' '}
                            · ends {date(status.session?.ends_at)} · {size(status.session?.raw_bytes)}
                        </>
                    )}
                    {status?.error && <> · {status.error}</>}
                </p>
                {!enabled ? (
                    <p className={ui.note}>
                        Update this device’s device agent and detection runtime to enable log control.
                    </p>
                ) : (
                    <>
                        <div className={ui.toolbar}>
                            <label>
                                Duration{' '}
                                <select
                                    className={ui.control}
                                    value={minutes}
                                    onChange={e => setMinutes(Number(e.target.value))}
                                >
                                    {[5, 15, 30, 60, 120].map(m => (
                                        <option key={m} value={m}>
                                            {m} min
                                        </option>
                                    ))}
                                </select>
                            </label>
                            <label>
                                <input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} />{' '}
                                Upload when finished
                            </label>
                            <button
                                className={ui.button}
                                disabled={sending || pending || unavailable || status?.recording}
                                onClick={() =>
                                    send('decision_log_start', { duration_s: minutes * 60, auto_upload: auto })
                                }
                            >
                                Start recording
                            </button>
                            <button
                                className={ui.button}
                                disabled={sending || pending || unavailable || !status?.recording}
                                onClick={() => send('decision_log_stop', { collect: auto })}
                            >
                                Stop recording
                            </button>
                            <button
                                className={ui.button}
                                disabled={sending || pending || unavailable}
                                onClick={() => send('decision_log_collect')}
                            >
                                Collect logs
                            </button>
                        </div>
                        <p className={ui.note}>
                            Collecting during recording uploads the finished segment and continues in a new segment.
                            Edge: 256 MiB total, 128 MiB per segment; uploaded logs retained up to 7 days. Server: 30
                            days, 5 GiB total.
                        </p>
                    </>
                )}
                {message && (
                    <p className={ui.note} role="status">
                        {message}
                    </p>
                )}
                {error && <p className={ui.error}>{error}</p>}
                {recent && (
                    <div className={ui.note}>
                        Last log command: {recent.state}
                        {recent.result && (
                            <details>
                                <summary>Device response</summary>
                                <pre
                                    className={ui.mono}
                                    style={{ maxHeight: 120, overflow: 'auto', whiteSpace: 'pre-wrap' }}
                                >
                                    {recent.result}
                                </pre>
                            </details>
                        )}
                    </div>
                )}
            </section>
            {status?.sessions?.length > 0 && (
                <details>
                    <summary>On-device sessions ({status.sessions.length})</summary>
                    {status.sessions.map(s => (
                        <p key={s.session_id} className={ui.note}>
                            {date(s.started_at)} · {s.state} · {s.records ?? 0} slots ·{' '}
                            {s.upload?.state || 'not collected'}
                            {s.upload?.error && ` · ${s.upload.error}`}
                        </p>
                    ))}
                </details>
            )}
            {sessions.length > 0 && (
                <div className={ui.tableWrap}>
                    <table className={ui.table}>
                        <thead>
                            <tr>
                                <th scope="col">Recorded</th>
                                <th scope="col">Slots / size</th>
                                <th scope="col">Server copy</th>
                            </tr>
                        </thead>
                        <tbody>
                            {sessions.map(s => (
                                <tr key={s.session_id}>
                                    <td>
                                        {date(s.started_at)}
                                        <br />
                                        to {date(s.ended_at)}
                                    </td>
                                    <td>
                                        {s.records} / {size(s.size)}
                                    </td>
                                    <td>
                                        {s.state === 'ready' ? (
                                            <>
                                                <a
                                                    className={ui.link}
                                                    href={`/api/admin/decision-logs/${key}/${s.session_id}/download`}
                                                >
                                                    Download gzip
                                                </a>{' '}
                                                ·{' '}
                                                <a
                                                    className={ui.link}
                                                    href={`/api/admin/decision-logs/${key}/${s.session_id}/manifest`}
                                                >
                                                    Version / settings
                                                </a>
                                            </>
                                        ) : (
                                            'Expired'
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}
