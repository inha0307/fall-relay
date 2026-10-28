'use client';
import { useState } from 'react';
import { useBridge } from '@/utils/bridge';
import { dateTime, duration } from '@/utils/format';

const PAGE = 5,
    STEP = 10,
    FETCH = 50;
import ui from './ui.module.css';

/* Which model bundle each camera's device reports running; reported identity, not remote attestation. */
export default function ModelRuns() {
    const history = useBridge('/api/model_runs', { limit: FETCH }, 5000);
    const [shown, setShown] = useState(PAGE);
    const connection = useBridge('/api/connection', {}, 5000);
    const cameras = Object.values(connection.data?.cameras || {});
    const runs = history.data?.runs || [];
    return (
        <section className={ui.card}>
            <h2>Model runs</h2>
            {(history.error || connection.error) && (
                <p className={ui.warning}>Cannot refresh model identity; the values shown may be outdated.</p>
            )}
            {cameras.map(c => {
                const identity = c.last_edge_result?.model_identity;
                const live = c.edge_inference_connected && !connection.error;
                return (
                    <div
                        key={c.camera_id}
                        className={ui.note}
                        style={{
                            padding: '8px 0',
                            borderBottom: '1px solid var(--border-light)',
                            overflowWrap: 'anywhere',
                        }}
                    >
                        <strong>Camera {c.camera_id}</strong> · {live ? 'sending decisions' : 'no recent decisions'}
                        <br />
                        {identity ? (
                            <>
                                Bundle <strong>{identity.bundle_id}</strong> · run{' '}
                                <span className={ui.mono}>{identity.run_id}</span>
                                <br />
                                Bundle SHA-256 <span className={ui.mono}>{identity.bundle_sha256}</span>
                                <br />
                                TCN SHA-256 <span className={ui.mono}>{identity.artifacts_sha256.tcn}</span>
                                <br />
                                {identity.settings.inference_hz} Hz · {identity.settings.window_slots} slots · threshold{' '}
                                {identity.settings.fall_threshold} · confirm {identity.settings.fall_confirm_slots}{' '}
                                slots
                            </>
                        ) : (
                            'This device reports no model identity.'
                        )}
                    </div>
                );
            })}
            <h3 style={{ marginTop: 16 }}>Recent runs</h3>
            <p className={ui.note}>A device starts a new run each time its detection service starts.</p>
            {runs.length ? (
                <>
                    <div className={ui.tableWrap}>
                        <table className={ui.table}>
                            <thead>
                                <tr>
                                    {['First received', 'Ran for', 'Camera', 'Bundle', 'Run', 'Settings digest'].map(
                                        t => (
                                            <th key={t} scope="col">
                                                {t}
                                            </th>
                                        )
                                    )}
                                </tr>
                            </thead>
                            <tbody>
                                {runs.slice(0, shown).map(r => (
                                    <tr key={r.run_id}>
                                        <td>{dateTime(r.first_seen, { seconds: false })}</td>
                                        <td>{r.last_seen ? duration(r.last_seen - r.first_seen) : '—'}</td>
                                        <td>{r.camera_id}</td>
                                        <td>{r.bundle_id}</td>
                                        <td className={ui.mono} title={r.run_id}>
                                            {r.run_id.slice(0, 12)}…
                                        </td>
                                        <td className={ui.mono} title={r.config_sha256}>
                                            {r.config_sha256.slice(0, 12)}…
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <div className={ui.row} style={{ marginTop: 8 }}>
                        <span className={ui.note}>
                            Showing {Math.min(shown, runs.length)} of the latest {runs.length}
                            {runs.length === FETCH ? '+' : ''} runs
                        </span>
                        <span className={ui.toolbar}>
                            {shown < runs.length && (
                                <button type="button" className={ui.button} onClick={() => setShown(n => n + STEP)}>
                                    Show {Math.min(STEP, runs.length - shown)} more
                                </button>
                            )}
                            {shown > PAGE && (
                                <button type="button" className={ui.button} onClick={() => setShown(PAGE)}>
                                    Show fewer
                                </button>
                            )}
                        </span>
                    </div>
                </>
            ) : (
                <p className={ui.empty}>No model runs recorded yet.</p>
            )}
            <p className={ui.note}>
                Models are activated on the device with its local model manager; this page shows what devices report and
                does not upload or activate models.
            </p>
        </section>
    );
}
