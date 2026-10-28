'use client';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { ArrowLeft, Cpu } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { useBridge, useCameraNames } from '@/utils/bridge';
import { deviceHealth } from '@/utils/deviceHealth';
import { bytes, dateTime, duration, quantity, reading } from '@/utils/format';
import Page from '@/components/ui/Page';
import StatusChip from '@/components/ui/StatusChip';
import StatTiles from '@/components/ui/StatTiles';
import Tabs from '@/components/ui/Tabs';
import RemoteActions from '@/components/devices/RemoteActions';
import HardwareHistory from '@/components/devices/HardwareHistory';
import Specification from '@/components/devices/Specification';
import DecisionLogs from '@/components/DecisionLogs';
import ui from '@/components/ui.module.css';

function Overview({ device, cameras, health }) {
    const m = device.metrics || {};
    const shown = new Set((device.profile?.readings || []).map(r => r.key));
    const tiles = [
        ...cameras.map(c => ({
            label: cameras.length > 1 ? `Decision rate · ${c.name}` : 'Decision rate',
            value: quantity(c.connection?.edge_inference_hz, 1, 'Hz'),
            note: `design rate ${quantity(device.profile?.design_rate_hz, 0, 'Hz')}`,
        })),
        ...cameras.map(c => ({
            label: cameras.length > 1 ? `Processing time · ${c.name}` : 'Processing time',
            value: quantity(c.connection?.last_edge_result?.latency_ms, 0, 'ms'),
            note: 'pose to decision, last slot',
        })),
        ...(device.profile?.readings || [])
            .filter(r => !['ram_total_bytes', 'ram_used_bytes', 'disk_free_bytes', 'clock_mhz'].includes(r.key))
            .map(r => ({
                label: r.label,
                value: reading(m[r.key], r.unit),
                note: r.key === 'cpu_pct' ? 'whole device' : r.key === 'uptime_s' ? 'since last boot' : '',
            })),
        ...(shown.has('ram_used_bytes')
            ? [{ label: 'Memory', value: bytes(m.ram_used_bytes), note: `of ${bytes(m.ram_total_bytes)}` }]
            : []),
        {
            label: 'Last report',
            value: `${duration(device.age_s)} ago`,
            note: device.stale ? 'stale' : dateTime(device.received_at, { zone: false }),
        },
    ];
    return (
        <div className={ui.stack}>
            <section className={ui.card}>
                <div className={ui.row}>
                    <StatusChip tone={health.tone}>{health.label}</StatusChip>
                    <span>{health.why}</span>
                </div>
            </section>
            <StatTiles items={tiles} dim={device.stale} />
            <section className={ui.card}>
                <h2>Cameras</h2>
                {cameras.map(c => (
                    <p key={c.id} className={ui.row}>
                        <span>{c.name}</span>
                        <span>
                            {c.connection?.edge_inference_connected ? 'Sending decisions' : 'No decisions'} ·{' '}
                            <Link className={ui.link} href="/skeleton">
                                Skeleton view
                            </Link>
                        </span>
                    </p>
                ))}
            </section>
        </div>
    );
}

export default function DevicePage() {
    const { id } = useParams();
    const deviceId = decodeURIComponent(id);
    const { user, loading } = useAuth();
    const detail = useBridge(`/api/devices/${encodeURIComponent(deviceId)}`, {}, 5000);
    const connection = useBridge('/api/connection', {}, 3000);
    const cams = useCameraNames();
    if (!loading && !user) return null;
    const admin = user?.role === 'admin';
    const back = (
        <Link href="/devices" className={`${ui.link} ${ui.back}`}>
            <ArrowLeft size={14} aria-hidden="true" />
            All devices
        </Link>
    );
    const device = detail.data?.device;
    if (!device)
        return (
            <Page icon={Cpu} title={deviceId} back={back}>
                {detail.error ? (
                    <p className={ui.error}>
                        {String(detail.error).includes('404')
                            ? 'This device has not reported to the server.'
                            : 'Cannot reach the server.'}
                    </p>
                ) : (
                    <p className={ui.empty}>Loading…</p>
                )}
            </Page>
        );
    const all = connection.data?.cameras || {};
    const cameras = (device.camera_ids || [device.camera_id]).map(cid => ({
        id: cid,
        name: cams.name(cid),
        connection: all[cid],
    }));
    const health = deviceHealth(device, cameras);
    const tabs = [
        { id: 'overview', label: 'Overview' },
        { id: 'actions', label: 'Remote actions' },
        ...(admin && device.control?.commands?.includes('decision_log_start')
            ? [{ id: 'logs', label: 'Decision logs' }]
            : []),
        { id: 'history', label: 'Hardware history' },
        { id: 'specification', label: 'Specification' },
    ];
    return (
        <Page
            icon={Cpu}
            back={back}
            title={deviceId}
            subtitle={`${device.profile?.label || 'Edge device'}${device.spec?.model ? ` · ${device.spec.model}` : ''} · cameras ${cameras.map(c => c.name).join(', ')}`}
            actions={<StatusChip tone={health.tone}>{health.label}</StatusChip>}
        >
            <Tabs tabs={tabs}>
                {active =>
                    active === 'overview' ? (
                        <Overview device={device} cameras={cameras} health={health} />
                    ) : active === 'actions' ? (
                        <RemoteActions device={device} catalog={detail.data.command_catalog || {}} admin={admin} />
                    ) : active === 'logs' ? (
                        <DecisionLogs device={device} />
                    ) : active === 'history' ? (
                        <HardwareHistory device={device} />
                    ) : (
                        <Specification device={device} />
                    )
                }
            </Tabs>
        </Page>
    );
}
