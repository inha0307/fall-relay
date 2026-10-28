'use client';
import React, { useState } from 'react';
import { Server } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts';
import { useAuth } from '@/context/AuthContext';
import { useBridge, useCameraNames } from '@/utils/bridge';
import { bytes, clock, dateTime, duration, quantity } from '@/utils/format';
import Page from '@/components/ui/Page';
import StatTiles from '@/components/ui/StatTiles';
import StatusChip from '@/components/ui/StatusChip';
import DataTable from '@/components/ui/DataTable';
import CameraAdmin from '@/components/CameraAdmin';
import AlertOpsHealth from '@/components/AlertOpsHealth';
import ModelRuns from '@/components/ModelRuns';
import ui from '@/components/ui.module.css';

const RANGES = [1, 6, 24];
const COLORS = ['#2563eb', '#059669', '#d97706', '#dc2626', '#7c3aed', '#0d9488'];

/* Merge rows by time so every camera's series can share one chart; break lines where data stops. */
function pivot(rows, field) {
    const byTime = new Map();
    rows.forEach(r => {
        const t = Math.round(r.ts / 10) * 10;
        byTime.set(t, { ...(byTime.get(t) || { t }), [`cam_${r.camera_id}`]: r[field] });
    });
    const ordered = [...byTime.values()].sort((a, b) => a.t - b.t),
        filled = [];
    ordered.forEach((row, i) => {
        if (i && row.t - ordered[i - 1].t > 20) filled.push({ t: ordered[i - 1].t + 10 });
        filled.push(row);
    });
    return filled;
}

function MetricChart({ title, rows, field, cameras, unit }) {
    const data = pivot(rows, field);
    return (
        <section className={ui.card}>
            <h2>{title}</h2>
            {data.length === 0 ? (
                <p className={ui.empty}>No measurements in this time range.</p>
            ) : (
                <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={data} margin={{ top: 4, right: 8, left: -10, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                        <XAxis
                            dataKey="t"
                            type="number"
                            domain={['dataMin', 'dataMax']}
                            tickFormatter={t => clock(t, false)}
                            tick={{ fontSize: 11 }}
                            minTickGap={30}
                        />
                        <YAxis tick={{ fontSize: 12 }} unit={unit ? ` ${unit}` : undefined} />
                        <Tooltip labelFormatter={t => dateTime(t)} />
                        <Legend />
                        {cameras.map((c, i) => (
                            <Line
                                key={c.id}
                                type="monotone"
                                dataKey={`cam_${c.id}`}
                                name={c.name}
                                stroke={COLORS[i % COLORS.length]}
                                dot={false}
                                connectNulls={false}
                                strokeWidth={2}
                                isAnimationActive={false}
                            />
                        ))}
                    </LineChart>
                </ResponsiveContainer>
            )}
        </section>
    );
}

export default function SystemPage() {
    const { user, loading: authLoading } = useAuth();
    const [hours, setHours] = useState(1);
    const cameras = useBridge('/api/connection', {}, 3000);
    const metrics = useBridge('/api/metrics', { hours }, 10000);
    const system = useBridge('/api/system', {}, 5000);
    const cams = useCameraNames();
    if (!authLoading && !user) return null;

    const list = Object.values(cameras.data?.cameras || {});
    const rows = metrics.data?.metrics || [];
    const camIds = [...new Set([...list.map(c => String(c.camera_id)), ...rows.map(r => String(r.camera_id))])];
    const camList = camIds.map(id => ({ id, name: cams.name(id) }));
    const sys = system.data;
    const cpuHigh = sys?.cpu_pct != null && sys.cpu_pct >= 70;
    const tiles = [
        // the load test showed the server process's CPU (one core) is the capacity limit
        {
            label: 'Server CPU (one core)',
            value: quantity(sys?.cpu_pct, 0, '%'),
            note: cpuHigh ? '70 % or more: decisions and alerts may lag' : 'keep below 70 %',
        },
        { label: 'Server memory', value: bytes(sys?.rss_bytes), note: sys?.threads ? `${sys.threads} threads` : '' },
        {
            label: 'Fall clips',
            value: sys?.clip_count != null ? `${sys.clip_count} · ${bytes(sys.clip_bytes)}` : '—',
            note: sys?.clip_retention_days ? `deleted after ${sys.clip_retention_days} days` : 'clips off',
        },
        { label: 'Event database', value: bytes(sys?.db_bytes), note: `server up ${duration(sys?.uptime_s)}` },
    ];
    const columns = [
        {
            key: 'camera',
            label: 'Camera',
            sort: c => c.label,
            render: c => `${c.label}${c.location ? ` · ${c.location}` : ''}`,
        },
        {
            key: 'decisions',
            label: 'Decisions',
            sort: c => (c.edge_inference_connected ? 0 : 1),
            render: c => (
                <StatusChip tone={c.edge_inference_connected ? 'ok' : 'offline'}>
                    {c.edge_inference_connected ? 'Receiving' : 'None'}
                </StatusChip>
            ),
        },
        {
            key: 'rate',
            label: 'Decision rate',
            align: 'right',
            sort: c => c.edge_inference_hz,
            render: c => quantity(c.edge_inference_hz, 1, 'Hz'),
        },
        {
            key: 'age',
            label: 'Last decision',
            align: 'right',
            sort: c => c.edge_result_age_s,
            render: c => (c.edge_result_age_s == null ? '—' : `${duration(c.edge_result_age_s)} ago`),
        },
        { key: 'threshold', label: 'Threshold θ', align: 'right', render: c => c.fall_threshold ?? '—' },
        {
            key: 'auth',
            label: 'Device token',
            render: c => (
                <>
                    {c.push_auth ? (
                        <StatusChip tone={c.push_auth === 'token' ? 'ok' : 'down'}>
                            {c.push_auth === 'token' ? 'Required' : 'Not set'}
                        </StatusChip>
                    ) : (
                        '—'
                    )}
                    {c.auth_rejects > 0 && <span className={ui.note}> · {c.auth_rejects} refused</span>}
                </>
            ),
        },
        // Camera images stay on the device; a JPEG stream arrives only when a device runs with --video (debugging).
        {
            key: 'video',
            label: 'Debug video',
            render: c =>
                c.video_connected
                    ? `On · ${c.video_fps} fps · ${quantity(c.video_in_kbps / 1000, 1, 'Mbit/s')}`
                    : 'Off',
        },
    ];
    return (
        <Page
            icon={Server}
            title="Server"
            subtitle="Server health, alert delivery, model runs and camera registration. Device health and remote actions are on the Devices page."
            actions={RANGES.map(h => (
                <button
                    key={h}
                    type="button"
                    aria-pressed={hours === h}
                    className={`${ui.button} ${hours === h ? ui.primary : ''}`}
                    onClick={() => setHours(h)}
                >
                    {h} h
                </button>
            ))}
        >
            {cameras.error && !cameras.data && <p className={ui.error}>Cannot reach the server.</p>}
            <div className={ui.stack}>
                <StatTiles items={tiles} />
                <AlertOpsHealth />
                <section>
                    <h2 className={ui.sectionTitle}>Cameras</h2>
                    <DataTable
                        caption="Cameras"
                        columns={columns}
                        rows={list}
                        rowKey={c => c.camera_id}
                        empty="No cameras registered."
                    />
                </section>
                <div className={ui.grid} style={{ margin: 0 }}>
                    <MetricChart title="Decision rate (Hz)" rows={rows} field="edge_hz" cameras={camList} />
                    <MetricChart
                        title="Processing time, 95th percentile"
                        rows={rows}
                        field="latency_p95_ms"
                        cameras={camList}
                        unit="ms"
                    />
                    <MetricChart
                        title="Windows without enough valid slots, per 10 s"
                        rows={rows}
                        field="ineligible"
                        cameras={camList}
                    />
                </div>
                <ModelRuns />
                <CameraAdmin />
            </div>
        </Page>
    );
}
