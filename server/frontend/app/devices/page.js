'use client';
import { useMemo, useState } from 'react';
import { Cpu, Search } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { useBridge, useCameraNames } from '@/utils/bridge';
import { deviceHealth, HEALTH_ORDER } from '@/utils/deviceHealth';
import { dateTime, duration, quantity } from '@/utils/format';
import Page from '@/components/ui/Page';
import DataTable from '@/components/ui/DataTable';
import StatusChip from '@/components/ui/StatusChip';
import ui from '@/components/ui.module.css';

const FILTERS = [
    ['all', 'All'],
    ['ok', 'Detecting'],
    ['degraded', 'Degraded'],
    ['down', 'Not detecting'],
    ['offline', 'Offline'],
];

export default function DevicesPage() {
    const { user, loading } = useAuth();
    const devices = useBridge('/api/devices', {}, 5000);
    const connection = useBridge('/api/connection', {}, 3000);
    const cams = useCameraNames();
    const [filter, setFilter] = useState('all');
    const [query, setQuery] = useState('');
    const cameras = connection.data?.cameras || {};
    const rows = useMemo(
        () =>
            (devices.data?.devices || []).map(d => {
                const list = (d.camera_ids || [d.camera_id]).map(id => ({
                    id,
                    name: cams.name(id),
                    connection: cameras[id],
                }));
                const rates = list.map(c => c.connection?.edge_inference_hz).filter(v => v != null);
                const times = list.map(c => c.connection?.last_edge_result?.latency_ms).filter(v => v != null);
                return {
                    device: d,
                    cameras: list,
                    health: deviceHealth(d, list),
                    rate: rates.length ? Math.min(...rates) : null,
                    processing: times.length ? Math.max(...times) : null,
                };
            }),
        [devices.data, cameras, cams]
    );
    if (!loading && !user) return null;
    const counts = Object.fromEntries(
        FILTERS.map(([key]) => [key, key === 'all' ? rows.length : rows.filter(r => r.health.tone === key).length])
    );
    const q = query.trim().toLowerCase();
    const shown = rows.filter(
        r =>
            (filter === 'all' || r.health.tone === filter) &&
            (!q ||
                [r.device.device_id, r.device.profile?.label, ...r.cameras.map(c => c.name)]
                    .join(' ')
                    .toLowerCase()
                    .includes(q))
    );
    const reporting = new Set(rows.flatMap(r => r.cameras.map(c => String(c.id))));
    const silent = Object.values(cameras).filter(c => !reporting.has(String(c.camera_id)));
    const columns = [
        { key: 'device', label: 'Device', sort: r => r.device.device_id, render: r => r.device.device_id },
        {
            key: 'health',
            label: 'Health',
            sort: r => HEALTH_ORDER[r.health.tone],
            render: r => (
                <StatusChip tone={r.health.tone} title={r.health.why}>
                    {r.health.label}
                </StatusChip>
            ),
        },
        {
            key: 'cameras',
            label: 'Cameras',
            sort: r => r.cameras.map(c => c.name).join(),
            render: r => r.cameras.map(c => c.name).join(', '),
        },
        { key: 'kind', label: 'Kind', sort: r => r.device.profile?.label, render: r => r.device.profile?.label || '—' },
        {
            key: 'rate',
            label: 'Decision rate',
            align: 'right',
            sort: r => r.rate,
            render: r => quantity(r.rate, 1, 'Hz'),
        },
        {
            key: 'processing',
            label: 'Processing time',
            align: 'right',
            sort: r => r.processing,
            render: r => quantity(r.processing, 0, 'ms'),
        },
        {
            key: 'temperature',
            label: 'Temperature',
            align: 'right',
            sort: r => r.device.metrics?.temperature_c,
            render: r => quantity(r.device.metrics?.temperature_c, 0, '°C'),
        },
        {
            key: 'report',
            label: 'Last report',
            sort: r => r.device.age_s,
            render: r => <span title={dateTime(r.device.received_at)}>{duration(r.device.age_s)} ago</span>,
        },
    ];
    return (
        <Page
            icon={Cpu}
            title="Devices"
            subtitle="Each edge device runs fall detection for its cameras. Open a device to see its readings, history and remote actions."
        >
            {devices.error && !devices.data && <p className={ui.error}>Cannot reach the server.</p>}
            <div className={ui.row} style={{ marginBottom: 12 }}>
                <div className={ui.toolbar} role="group" aria-label="Filter by health">
                    {FILTERS.map(([key, label]) => (
                        <button
                            key={key}
                            type="button"
                            aria-pressed={filter === key}
                            className={`${ui.button} ${filter === key ? ui.primary : ''}`}
                            onClick={() => setFilter(key)}
                        >
                            {label} ({counts[key]})
                        </button>
                    ))}
                </div>
                <label className={ui.toolbar}>
                    <Search size={16} aria-hidden="true" />
                    <input
                        className={ui.control}
                        type="search"
                        placeholder="Device or camera"
                        aria-label="Search devices"
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                    />
                </label>
            </div>
            <DataTable
                caption="Edge devices"
                columns={columns}
                rows={shown}
                rowKey={r => r.device.device_id}
                href={r => `/devices/${encodeURIComponent(r.device.device_id)}`}
                initialSort={{ key: 'health', dir: 1 }}
                empty={
                    devices.data
                        ? rows.length
                            ? 'No device matches this filter.'
                            : 'No device reports yet.'
                        : 'Loading…'
                }
            />
            {silent.map(c => (
                <p key={c.camera_id} className={ui.note}>
                    {cams.name(c.camera_id)}: no device reports. Run the device agent on its device to see health and
                    readings here.
                </p>
            ))}
        </Page>
    );
}
