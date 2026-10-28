'use client';
import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Bell, BellOff, Volume2, VolumeX, AlertTriangle, WifiOff, Thermometer } from 'lucide-react';
import { useAlerts, LIVE, ALERT_TITLES, ESCALATE_AFTER_S } from '@/context/AlertContext';
import { ALERT_STATES as states, FALL_OUTCOMES as labels, DEVICE_OUTCOMES } from '@/utils/alerts';
import ui from '@/components/ui.module.css';
import {
    UNACKED,
    needsAcknowledgement,
    isManualReview,
    isUrgentWaiting,
    isOpen,
    reviewReason,
} from '@/utils/alertPolicy.mjs';
import { dateTime, duration } from '@/utils/format';

const clock = t => dateTime(t);
const after = (t, from) => {
    const s = Math.max(0, Math.round(t - from));
    return s < 60 ? `+${s} s` : `+${Math.floor(s / 60)} min ${s % 60} s`;
};
const ago = s => `${duration(s)} ago`;
const KIND = {
    fall: { icon: AlertTriangle, tone: 'fall' },
    camera_offline: { icon: WifiOff, tone: 'device' },
    device_warning: { icon: Thermometer, tone: 'device' },
};

/* One alert: what happened, the evidence (fall clip), who did what when, and the single next step. */
function AlertCard({ a, nowS, canHandle, busy, action, onResolve }) {
    const kind = KIND[a.type] || KIND.camera_offline;
    const Icon = kind.icon;
    const needsAck = needsAcknowledgement(a);
    const waiting = isUrgentWaiting(a);
    const manual = isManualReview(a);
    const receivedAt = a.server_received_at ?? a.created_at;
    const overdue = waiting && nowS - a.created_at >= ESCALATE_AFTER_S;
    const [clipError, setClipError] = useState('');
    const outcome = a.type === 'fall' ? labels[a.resolution] : DEVICE_OUTCOMES[a.resolution];
    const steps = [
        ...(a.type === 'fall'
            ? [
                  ['Detected on device', a.event_decided_at, a.clock_usable ? 'clock-aligned device time' : '', true],
                  ['Received by server', receivedAt, '', true],
              ]
            : [['Raised by server', a.created_at, 'device check', true]]),
        ['Delivered to browser', a.first_delivered_at, 'receipt stored'],
        ['Acknowledged', a.acknowledged_at, a.acknowledged_by && `by ${a.acknowledged_by}`],
        ['Resolved', a.resolved_at, a.resolved_by && `${outcome || a.resolution} · by ${a.resolved_by}`],
    ];
    return (
        <article
            data-alert-id={a.id}
            className={`alert-card alert-${kind.tone} ${waiting ? 'alert-waiting' : ''} ${manual ? 'alert-manual' : ''} ${overdue ? 'alert-overdue' : ''}`}
        >
            <div className="alert-head">
                <span className={`alert-kind alert-kind-${kind.tone}`}>
                    <Icon size={16} />
                    {ALERT_TITLES[a.type] || a.type}
                </span>
                <span className="alert-camera">Camera {a.camera_id}</span>
                <span
                    className={`alert-state alert-state-${manual && needsAck ? 'manual' : waiting ? 'waiting' : a.state}`}
                >
                    {manual && needsAck ? 'Awaiting manual review' : states[a.state] || a.state}
                </span>
            </div>
            <p className="alert-when">
                Received {ago(nowS - receivedAt)} · {clock(receivedAt)} · #{a.id}
            </p>
            {manual && (
                <p className="alert-manual-note">
                    <strong>Manual review — {reviewReason(a)}</strong>. Saved for review; urgent reminders are
                    suppressed.
                </p>
            )}
            {a.type === 'fall' && a.event_decided_at == null && (
                <p className={ui.note}>
                    Reliable event time is unavailable. An exact delivery delay cannot be established.
                </p>
            )}
            {overdue && (
                <p className="alert-overdue-note">
                    Nobody has acknowledged this alert for {Math.floor((nowS - a.created_at) / 60)} min.
                </p>
            )}
            {a.type === 'fall' && (
                <div className="alert-clip">
                    {clipError ? (
                        <p className={ui.note}>{clipError}</p>
                    ) : (
                        <video
                            controls
                            muted
                            playsInline
                            preload="metadata"
                            src={`/api/alerts/${a.id}/clip`}
                            onError={() =>
                                setClipError(
                                    'No clip: it may still be uploading, this camera does not send clips, or it was deleted after the 3-day retention.'
                                )
                            }
                        />
                    )}
                    {!clipError && (
                        <p className={ui.note}>
                            Seconds before and after the detection, sent by the camera only for this alert.
                        </p>
                    )}
                </div>
            )}
            <ol className="alert-steps">
                {steps.map(([name, t, who, absolute]) => (
                    <li key={name} className={t ? 'done' : ''}>
                        <strong>{name}</strong>
                        <span>
                            {t ? `${absolute ? clock(t) : after(t, receivedAt)}${who ? ` · ${who}` : ''}` : '—'}
                        </span>
                    </li>
                ))}
            </ol>
            {a.note && <p className={ui.note}>Note: {a.note}</p>}
            <div className={ui.toolbar}>
                {canHandle && needsAck && (
                    <button
                        className={`${ui.button} ${ui.primary}`}
                        disabled={busy === a.id}
                        onClick={() => action(a.id, 'ack')}
                    >
                        {manual ? 'Start review' : 'Acknowledge'}
                    </button>
                )}
                {canHandle && a.state === 'acknowledged' && (
                    <button
                        className={`${ui.button} ${ui.primary}`}
                        disabled={busy === a.id}
                        onClick={() => onResolve(a)}
                    >
                        Record outcome
                    </button>
                )}
                <Link className={ui.button} href="/skeleton">
                    Skeleton view
                </Link>
            </div>
        </article>
    );
}

export default function AlertsPage() {
    const {
        alerts,
        connection,
        error,
        filter,
        setFilter,
        busy,
        now,
        sound,
        notify,
        canHandle,
        action,
        toggleSound,
        toggleNotify,
        pending,
        unacknowledged,
        manualReviewPending,
        truncated,
    } = useAlerts();
    const [resolve, setResolve] = useState(null);
    const [label, setLabel] = useState('uncertain');
    const [note, setNote] = useState('');
    const [saved, setSaved] = useState(null); // the alert just resolved: it leaves the Open list, so say where it went
    const dialog = useRef(null);
    useEffect(() => {
        if (resolve && dialog.current && !dialog.current.open) dialog.current.showModal();
    }, [resolve]);
    const inView = (a, f) =>
        f === 'all' ||
        (f === 'open' ? isOpen(a) : f === 'manual_review' ? isManualReview(a) && isOpen(a) : a.state === 'resolved');
    const counts = Object.fromEntries(
        ['open', 'manual_review', 'resolved', 'all'].map(f => [f, alerts.filter(a => inView(a, f)).length])
    );
    const shown = alerts
        .filter(a => inView(a, filter))
        .sort(
            (x, y) =>
                isUrgentWaiting(y) - isUrgentWaiting(x) ||
                UNACKED.includes(y.state) - UNACKED.includes(x.state) ||
                (y.type === 'fall') - (x.type === 'fall') ||
                y.created_at - x.created_at
        );
    const nowS = now / 1000;
    return (
        <main className="home-page-root">
            <div className={ui.content} style={{ padding: 24 }}>
                <header className={ui.header}>
                    <div>
                        <h1 className={ui.title}>Alerts</h1>
                        <p className={ui.subtitle}>
                            Live alerts need acknowledgement and on-site follow-up. Delayed or clock-uncertain cases are
                            saved for manual review.
                        </p>
                    </div>
                    <div className={ui.toolbar}>
                        <button className={ui.button} onClick={toggleSound} aria-pressed={sound}>
                            {sound ? <Volume2 size={15} /> : <VolumeX size={15} />}
                            {sound ? 'Sound on' : 'Sound off'}
                        </button>
                        <button className={ui.button} onClick={toggleNotify} aria-pressed={notify}>
                            {notify ? <Bell size={15} /> : <BellOff size={15} />}
                            {notify ? 'Desktop notifications on' : 'Desktop notifications off'}
                        </button>
                    </div>
                </header>
                <div className="alert-summary">
                    <span className={connection === LIVE ? 'alert-live' : 'alert-offline'}>
                        {connection === LIVE ? '● Live' : connection}
                    </span>
                    <span>
                        <strong>{unacknowledged}</strong> urgent to acknowledge
                    </span>
                    <span>
                        <strong>{manualReviewPending}</strong> awaiting manual review
                    </span>
                    <span>
                        <strong>{pending}</strong> open total
                    </span>
                    <div className="alert-filter" role="group" aria-label="Show">
                        {[
                            ['open', 'Open'],
                            ['manual_review', 'Manual review'],
                            ['resolved', 'Resolved'],
                            ['all', 'All'],
                        ].map(([v, t]) => (
                            <button
                                key={v}
                                className={filter === v ? 'active' : ''}
                                onClick={() => {
                                    setFilter(v);
                                    setSaved(null);
                                }}
                            >
                                {t} <span className="alert-count">{counts[v]}</span>
                            </button>
                        ))}
                    </div>
                </div>
                {saved && filter === 'open' && (
                    <p role="status" className="alert-saved">
                        Alert #{saved} resolved. It moved to <strong>Resolved</strong>, where its record and fall clip
                        stay available (clips are kept 3 days).
                        <button
                            className={ui.button}
                            onClick={() => {
                                setFilter('resolved');
                                setSaved(null);
                            }}
                        >
                            Show resolved
                        </button>
                    </p>
                )}
                {truncated && (
                    <p role="status" className={ui.note}>
                        More than 500 alerts: open alerts are listed first. Ask an administrator for older history.
                    </p>
                )}
                {error && (
                    <p role="alert" className={ui.error}>
                        {error}
                    </p>
                )}
                <div className="alert-grid">
                    {shown.map(a => (
                        <AlertCard
                            key={a.id}
                            a={a}
                            nowS={nowS}
                            canHandle={canHandle}
                            busy={busy}
                            action={action}
                            onResolve={x => {
                                setResolve(x);
                                setLabel('uncertain');
                                setNote('');
                            }}
                        />
                    ))}
                </div>
                {!shown.length && (
                    <p className={ui.empty}>
                        {connection === LIVE
                            ? filter === 'manual_review'
                                ? 'No cases awaiting manual review.'
                                : filter === 'open'
                                  ? 'No open alerts.'
                                  : 'No alerts in this view.'
                            : 'Checking the connection…'}
                    </p>
                )}
                {resolve && (
                    <dialog
                        ref={dialog}
                        onCancel={() => setResolve(null)}
                        aria-labelledby="resolve-title"
                        className={ui.card}
                        style={{
                            position: 'fixed',
                            inset: '20% max(20px, calc((100vw - 520px) / 2)) auto',
                            zIndex: 1000,
                            width: 'min(520px, calc(100vw - 40px))',
                            margin: 'auto',
                            color: '#334155',
                        }}
                    >
                        <h2 id="resolve-title">Alert #{resolve.id} outcome</h2>
                        <p>
                            {isManualReview(resolve)
                                ? 'Record what can be established from available evidence. Use Uncertain when the event cannot be verified.'
                                : 'Record what was found on site.'}
                        </p>
                        <label>
                            Outcome{' '}
                            <select
                                aria-label="Outcome"
                                autoFocus
                                className={ui.control}
                                value={label}
                                onChange={e => setLabel(e.target.value)}
                            >
                                {Object.entries(resolve.type === 'fall' ? labels : DEVICE_OUTCOMES).map(([v, t]) => (
                                    <option key={v} value={v}>
                                        {t}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <p>
                            <label>
                                Note{' '}
                                <textarea
                                    aria-label="Note"
                                    className={ui.control}
                                    value={note}
                                    maxLength={2000}
                                    onChange={e => setNote(e.target.value)}
                                />
                            </label>
                        </p>
                        <div className={ui.toolbar}>
                            <button
                                className={`${ui.button} ${ui.primary}`}
                                disabled={busy === resolve.id}
                                onClick={async () => {
                                    if (await action(resolve.id, 'resolve', { label, note })) {
                                        setSaved(resolve.id);
                                        setResolve(null);
                                    }
                                }}
                            >
                                Save outcome
                            </button>
                            <button className={ui.button} onClick={() => setResolve(null)}>
                                Cancel
                            </button>
                        </div>
                    </dialog>
                )}
                <style jsx global>{`
                    .alert-summary {
                        display: flex;
                        align-items: center;
                        flex-wrap: wrap;
                        gap: 16px;
                        margin: 4px 0 18px;
                        font-size: 13px;
                        color: #475569;
                    }
                    .alert-live {
                        color: #059669;
                        font-weight: 700;
                    }
                    .alert-offline {
                        color: #b45309;
                        font-weight: 700;
                    }
                    .alert-filter {
                        margin-left: auto;
                        display: inline-flex;
                        border: 1px solid #e2e8f0;
                        border-radius: 10px;
                        overflow: hidden;
                        background: #fff;
                    }
                    .alert-filter button {
                        border: none;
                        background: transparent;
                        padding: 6px 14px;
                        font-size: 13px;
                        font-weight: 600;
                        color: #475569;
                        cursor: pointer;
                    }
                    .alert-filter button + button {
                        border-left: 1px solid #e2e8f0;
                    }
                    .alert-filter button.active {
                        background: #2563eb;
                        color: #fff;
                    }
                    .alert-count {
                        display: inline-block;
                        min-width: 18px;
                        padding: 0 5px;
                        margin-left: 4px;
                        border-radius: 9px;
                        font-size: 11px;
                        background: rgba(100, 116, 139, 0.15);
                    }
                    .alert-filter button.active .alert-count {
                        background: rgba(255, 255, 255, 0.25);
                    }
                    .alert-saved {
                        display: flex;
                        align-items: center;
                        flex-wrap: wrap;
                        gap: 10px;
                        margin: 0 0 16px;
                        padding: 10px 14px;
                        border-radius: 10px;
                        background: #ecfdf5;
                        border: 1px solid #a7f3d0;
                        color: #065f46;
                        font-size: 13px;
                    }
                    .alert-grid {
                        display: grid;
                        grid-template-columns: repeat(auto-fill, minmax(min(380px, 100%), 1fr));
                        gap: 16px;
                    }
                    .alert-card {
                        background: #fff;
                        border: 1px solid #e2e8f0;
                        border-left: 5px solid #cbd5e1;
                        border-radius: 14px;
                        padding: 16px 18px;
                        display: flex;
                        flex-direction: column;
                        gap: 10px;
                    }
                    .alert-card.alert-fall.alert-waiting {
                        border-left-color: #dc2626;
                    }
                    .alert-card.alert-device.alert-waiting {
                        border-left-color: #d97706;
                    }
                    .alert-card.alert-overdue {
                        box-shadow: 0 0 0 2px #fecaca;
                    }
                    .alert-head {
                        display: flex;
                        align-items: center;
                        gap: 10px;
                        flex-wrap: wrap;
                    }
                    .alert-kind {
                        display: inline-flex;
                        align-items: center;
                        gap: 6px;
                        font-weight: 800;
                        font-size: 14px;
                    }
                    .alert-kind-fall {
                        color: #dc2626;
                    }
                    .alert-kind-device {
                        color: #b45309;
                    }
                    .alert-camera {
                        font-weight: 700;
                        color: #0f172a;
                        font-size: 14px;
                    }
                    .alert-state {
                        margin-left: auto;
                        font-size: 12px;
                        font-weight: 700;
                        padding: 3px 10px;
                        border-radius: 999px;
                        background: #f1f5f9;
                        color: #475569;
                    }
                    .alert-state-waiting {
                        background: #fee2e2;
                        color: #b91c1c;
                    }
                    .alert-state-manual {
                        background: #e2e8f0;
                        color: #334155;
                    }
                    .alert-manual-note {
                        margin: 0;
                        padding: 8px 10px;
                        background: #f1f5f9;
                        border-radius: 8px;
                        font-size: 13px;
                        color: #475569;
                    }
                    .alert-state-acknowledged {
                        background: #fef3c7;
                        color: #92400e;
                    }
                    .alert-state-resolved {
                        background: #dcfce7;
                        color: #166534;
                    }
                    .alert-when {
                        margin: 0;
                        font-size: 12px;
                        color: #64748b;
                    }
                    .alert-overdue-note {
                        margin: 0;
                        font-size: 13px;
                        font-weight: 700;
                        color: #b91c1c;
                    }
                    .alert-clip video {
                        width: 100%;
                        border-radius: 10px;
                        background: #0f172a;
                        max-height: 260px;
                    }
                    .alert-clip p {
                        margin: 4px 0 0;
                    }
                    .alert-steps {
                        list-style: none;
                        margin: 0;
                        padding: 0;
                        display: grid;
                        gap: 4px;
                        font-size: 12px;
                        color: #94a3b8;
                    }
                    .alert-steps li {
                        display: grid;
                        grid-template-columns: 110px 1fr;
                        gap: 8px;
                    }
                    .alert-steps li.done {
                        color: #334155;
                    }
                    .alert-steps li strong {
                        font-weight: 700;
                    }
                `}</style>
            </div>
        </main>
    );
}
