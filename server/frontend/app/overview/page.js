'use client';
import Link from 'next/link';
import { ArrowRight, Activity, AlertTriangle, BellRing, WifiOff, LayoutDashboard } from 'lucide-react';
import { useAlerts, LIVE, ALERT_TITLES } from '@/context/AlertContext';
import { useBridge, useCameraNames } from '@/utils/bridge';
import { deviceHealth } from '@/utils/deviceHealth';
import { duration, quantity } from '@/utils/format';
import Page from '@/components/ui/Page';
import StatusChip from '@/components/ui/StatusChip';
import ui from '@/components/ui.module.css';

const ago = s => `${duration(s)} ago`;

/* Status answers one question: what needs attention now. History is under Alerts, statistics under Reports,
   skeletons under Skeleton view and device readings under Devices. */

/* One state per camera, most urgent first. The same colours are used on every screen. */
function cameraState(cam, waiting) {
    const mine = waiting.filter(a => a.camera_id === String(cam.id));
    const decision = cam.connection?.last_edge_result?.decision_state;
    if (mine.some(a => a.type === 'fall'))
        return {
            tone: 'fall',
            label: 'Fall alert',
            icon: AlertTriangle,
            why: 'Waiting for acknowledgement',
            href: '/alerts',
        };
    if (mine.length)
        return {
            tone: 'device',
            label: ALERT_TITLES[mine[0].type] || 'Device warning',
            icon: BellRing,
            why: 'Device alert waiting',
            href: '/alerts',
        };
    if (!cam.connection?.edge_inference_connected)
        return {
            tone: 'offline',
            label: 'Not detecting',
            icon: WifiOff,
            why: 'No decisions from the device',
            href: '/devices',
        };
    if (decision === 'ALERT' || decision === 'POST_FALL')
        return {
            tone: 'fall',
            label: 'Fall event open',
            icon: AlertTriangle,
            why: 'The device reports an open fall event',
            href: '/skeleton',
        };
    return { tone: 'ok', label: 'Detecting', icon: Activity, why: null, href: '/skeleton' };
}

export default function StatusPage() {
    const feed = useAlerts();
    const live = useBridge('/api/live', {}, 1000);
    const devices = useBridge('/api/devices', {}, 5000);
    const cams = useCameraNames();
    const cameras = live.data?.cameras || [];
    const waiting = feed?.waiting || [];
    const nowS = (feed?.now || Date.now()) / 1000;
    const states = cameras.map(c => ({ cam: c, state: cameraState(c, waiting) }));
    const detecting = states.filter(s => s.state.tone !== 'offline').length;
    const falls = waiting.filter(a => a.type === 'fall').length;
    const byId = Object.fromEntries(cameras.map(c => [String(c.id), c.connection]));
    const deviceIssues = (devices.data?.devices || [])
        .map(d => ({
            device: d,
            health: deviceHealth(
                d,
                (d.camera_ids || [d.camera_id]).map(id => ({ id, name: cams.name(id), connection: byId[String(id)] }))
            ),
        }))
        .filter(x => x.health.tone !== 'ok');
    const headline = falls
        ? { tone: 'fall', text: `${falls} fall alert${falls > 1 ? 's' : ''} waiting for acknowledgement` }
        : waiting.length
          ? { tone: 'device', text: `${waiting.length} device alert${waiting.length > 1 ? 's' : ''} waiting` }
          : cameras.length && detecting < cameras.length
            ? { tone: 'offline', text: `${cameras.length - detecting} of ${cameras.length} cameras not detecting` }
            : deviceIssues.length
              ? {
                    tone: 'device',
                    text: `All cameras detecting · ${deviceIssues.length} device${deviceIssues.length > 1 ? 's need' : ' needs'} attention`,
                }
              : cameras.length
                ? { tone: 'ok', text: `All ${cameras.length} cameras detecting · nothing needs attention` }
                : { tone: 'offline', text: 'No cameras registered' };
    return (
        <Page
            icon={LayoutDashboard}
            title="Status"
            subtitle="What needs attention now. Alert history is under Alerts and statistics over a period under Reports."
            actions={
                <span className={`board-conn ${feed?.connection === LIVE ? 'on' : 'off'}`}>
                    {feed?.connection === LIVE ? 'Alerts live' : `Alerts: ${feed?.connection || 'connecting'}`}
                </span>
            }
        >
            <p className={`board-headline board-${headline.tone}`} role="status">
                <span className="board-dot" />
                {headline.text}
            </p>
            {live.error && !live.data && <p className={ui.error}>Cannot reach the server.</p>}
            <div className={ui.stack}>
                <section className={ui.card}>
                    <div className={ui.row}>
                        <h2>Waiting for acknowledgement</h2>
                        <Link className={ui.link} href="/alerts">
                            Alerts <ArrowRight size={13} />
                        </Link>
                    </div>
                    {!waiting.length && <p className={ui.note}>Nothing waits for acknowledgement.</p>}
                    {waiting.map(a => (
                        <Link key={a.id} href="/alerts" className="board-row">
                            <StatusChip tone={a.type === 'fall' ? 'fall' : 'degraded'}>
                                {ALERT_TITLES[a.type] || a.type}
                            </StatusChip>
                            <span>{cams.name(a.camera_id)}</span>
                            <span className={ui.note}>{ago(nowS - a.created_at)}</span>
                        </Link>
                    ))}
                </section>
                <section>
                    <h2 className={ui.sectionTitle}>Cameras</h2>
                    <div className="board-grid">
                        {states.map(({ cam, state }) => {
                            const Icon = state.icon;
                            const hz = cam.connection?.edge_inference_hz;
                            return (
                                <Link key={cam.id} href={state.href} className={`board-tile board-tile-${state.tone}`}>
                                    <div className="board-tile-head">
                                        <strong>
                                            {cam.label}
                                            {cam.location ? ` · ${cam.location}` : ''}
                                        </strong>
                                        <span className={`board-chip board-chip-${state.tone}`}>
                                            <Icon size={13} aria-hidden="true" />
                                            {state.label}
                                        </span>
                                    </div>
                                    <div className="board-tile-foot">
                                        <span>
                                            {state.tone === 'offline'
                                                ? 'No decisions'
                                                : `${quantity(hz, 1, 'Hz')} decisions`}
                                        </span>
                                        {state.why && <span>{state.why}</span>}
                                    </div>
                                </Link>
                            );
                        })}
                    </div>
                </section>
                <section className={ui.card}>
                    <div className={ui.row}>
                        <h2>Devices needing attention</h2>
                        <Link className={ui.link} href="/devices">
                            Devices <ArrowRight size={13} />
                        </Link>
                    </div>
                    {devices.data && !deviceIssues.length && (
                        <p className={ui.note}>
                            Every device is detecting at its design rate with no hardware warning.
                        </p>
                    )}
                    {deviceIssues.map(({ device, health }) => (
                        <Link
                            key={device.device_id}
                            href={`/devices/${encodeURIComponent(device.device_id)}`}
                            className="board-row"
                        >
                            <StatusChip tone={health.tone}>{health.label}</StatusChip>
                            <strong>{device.device_id}</strong>
                            <span className={ui.note}>{health.why}</span>
                        </Link>
                    ))}
                </section>
            </div>
            <style jsx global>{`
                .board-headline {
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    margin: -8px 0 20px;
                    font-size: 18px;
                    font-weight: 700;
                }
                .board-dot {
                    width: 12px;
                    height: 12px;
                    border-radius: 50%;
                    background: currentColor;
                    box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 18%, transparent);
                }
                .board-ok {
                    color: var(--status-ok);
                }
                .board-fall {
                    color: var(--status-fall);
                }
                .board-device {
                    color: var(--status-device);
                }
                .board-offline {
                    color: var(--status-offline);
                }
                .board-conn {
                    font-size: 12px;
                    font-weight: 700;
                    padding: 4px 12px;
                    border-radius: 999px;
                }
                .board-conn.on {
                    background: #dcfce7;
                    color: #166534;
                }
                .board-conn.off {
                    background: var(--status-device-bg);
                    color: #92400e;
                }
                .board-grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
                    gap: 12px;
                }
                .board-tile {
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    background: var(--surface);
                    border: 1px solid var(--border);
                    border-left: 5px solid var(--status-ok);
                    border-radius: 12px;
                    padding: 12px 14px;
                    text-decoration: none;
                    color: var(--ink);
                    transition: box-shadow 0.15s;
                }
                .board-tile:hover {
                    box-shadow: 0 6px 20px rgba(15, 23, 42, 0.08);
                }
                .board-tile-fall {
                    border-left-color: var(--status-fall);
                    box-shadow: 0 0 0 2px var(--status-fall-bg);
                }
                .board-tile-device {
                    border-left-color: var(--status-device);
                }
                .board-tile-offline {
                    border-left-color: var(--status-offline);
                }
                .board-tile-head,
                .board-tile-foot {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    gap: 8px;
                    flex-wrap: wrap;
                }
                .board-tile-foot {
                    font-size: 12px;
                    color: var(--muted);
                }
                .board-chip {
                    display: inline-flex;
                    align-items: center;
                    gap: 5px;
                    font-size: 12px;
                    font-weight: 800;
                    padding: 3px 10px;
                    border-radius: 999px;
                }
                .board-chip-ok {
                    background: var(--status-ok-bg);
                    color: var(--status-ok);
                }
                .board-chip-fall {
                    background: var(--status-fall-bg);
                    color: var(--status-fall);
                }
                .board-chip-device {
                    background: var(--status-device-bg);
                    color: #92400e;
                }
                .board-chip-offline {
                    background: var(--status-offline-bg);
                    color: var(--ink-2);
                }
                .board-row {
                    display: flex;
                    align-items: center;
                    gap: 12px;
                    padding: 10px 0;
                    border-top: 1px solid var(--border-light);
                    font-size: 13px;
                    color: var(--ink-2);
                    text-decoration: none;
                    flex-wrap: wrap;
                }
            `}</style>
        </Page>
    );
}
