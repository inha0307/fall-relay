'use client';
import { useState } from 'react';
import { BarChart3 } from 'lucide-react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { useAuth } from '@/context/AuthContext';
import { useBridge, useCameraNames } from '@/utils/bridge';
import { FALL_OUTCOMES } from '@/utils/alerts';
import Link from 'next/link';
import { duration as dur, quantity } from '@/utils/format';
import Page from '@/components/ui/Page';
import ui from '@/components/ui.module.css';

/* Validated categorical slots (dataviz reference palette, light surface): fall alerts, device alerts. */
const SERIES = { fall: '#2a78d6', device: '#eb6834' };
const RANGES = [
    [1, '24 h'],
    [7, '7 days'],
    [30, '30 days'],
];
const NTU_FA_PER_H = 1.38; // NTU60 cross-subject test, deployed operating point (edge/MODEL.md)
const num = (v, d = 0, unit = '') => quantity(v, d, unit.trim());
const axis = { fontSize: 11, fill: '#64748b' };

function Tile({ label, value, note }) {
    return (
        <div className="dash-tile">
            <span>{label}</span>
            <strong>{value}</strong>
            {note && <small>{note}</small>}
        </div>
    );
}

export default function ReportsPage() {
    const { user, loading } = useAuth();
    const [days, setDays] = useState(7);
    const { data, error } = useBridge('/api/dashboard', { days }, 30000);
    const cams = useCameraNames();
    if (!loading && !user) return null;
    const a = data?.alerts,
        q = data?.quality;
    const perDay = a?.per_day || [];
    const hours = (a?.fall_hour_of_day || []).map((count, hour) => ({
        hour: `${String(hour).padStart(2, '0')}`,
        count,
    }));
    const outcomes = Object.entries(q?.outcomes || {}).sort((x, y) => y[1] - x[1]);
    const maxOutcome = Math.max(1, ...outcomes.map(([, n]) => n));
    return (
        <Page
            icon={BarChart3}
            title="Reports"
            subtitle="Statistics over a chosen period: how alerts were handled, how often they were real, and how reliably each camera was detecting. What needs attention now is on Status."
            actions={
                <div className={ui.toolbar} role="group" aria-label="Period">
                    {RANGES.map(([d, label]) => (
                        <button
                            key={d}
                            type="button"
                            aria-pressed={days === d}
                            className={`${ui.button} ${days === d ? ui.primary : ''}`}
                            onClick={() => setDays(d)}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            }
        >
            {error && !data && <p className={ui.error}>Cannot load statistics from the server.</p>}
            {data && (
                <>
                    <section className="dash-tiles">
                        <Tile
                            label="Fall alerts"
                            value={a.fall}
                            note={`${a.unresolved_falls} without an outcome · ${a.device} device alerts`}
                        />
                        <Tile
                            label="Time to acknowledge"
                            value={dur(a.ack_median_s)}
                            note={a.ack_p90_s != null ? `median · 90 % within ${dur(a.ack_p90_s)}` : 'median'}
                        />
                        <Tile
                            label="Confirmed falls"
                            value={q.field_precision == null ? '—' : `${Math.round(100 * q.field_precision)} %`}
                            note={`${q.confirmed} of ${q.confirmed + q.false_alarms} alerts with a fall / false-alarm outcome`}
                        />
                        <Tile
                            label="False alarms per hour"
                            value={q.resolved_falls ? num(q.false_alarms_per_h, 2) : '—'}
                            note={
                                q.resolved_falls
                                    ? `over ${num(q.monitored_h, 1)} monitored h · NTU test ${NTU_FA_PER_H} per h`
                                    : 'needs alerts with an on-site outcome'
                            }
                        />
                        <Tile
                            label="Detecting"
                            value={num(data.availability_pct, 1, '%')}
                            note="of observed time, all cameras"
                        />
                    </section>
                    <div className="dash-grid">
                        <section className={ui.card}>
                            <h2>Alerts per day</h2>
                            {perDay.length ? (
                                <ResponsiveContainer width="100%" height={240}>
                                    <BarChart
                                        data={perDay}
                                        margin={{ top: 8, right: 8, left: -18, bottom: 0 }}
                                        barCategoryGap="30%"
                                    >
                                        <CartesianGrid vertical={false} stroke="#eef2f6" />
                                        <XAxis
                                            dataKey="day"
                                            tick={axis}
                                            tickLine={false}
                                            axisLine={{ stroke: '#cbd5e1' }}
                                            tickFormatter={d => d.slice(5)}
                                        />
                                        <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                                        <Tooltip cursor={{ fill: '#f1f5f9' }} />
                                        <Legend
                                            content={() => (
                                                <div className="dash-legend">
                                                    {[
                                                        ['fall', 'Fall alerts'],
                                                        ['device', 'Device alerts'],
                                                    ].map(([k, t]) => (
                                                        <span key={k}>
                                                            <i style={{ background: SERIES[k] }} />
                                                            {t}
                                                        </span>
                                                    ))}
                                                </div>
                                            )}
                                        />
                                        <Bar
                                            dataKey="fall"
                                            name="Fall alerts"
                                            stackId="a"
                                            fill={SERIES.fall}
                                            stroke="#fff"
                                            strokeWidth={2}
                                        />
                                        <Bar
                                            dataKey="device"
                                            name="Device alerts"
                                            stackId="a"
                                            fill={SERIES.device}
                                            stroke="#fff"
                                            strokeWidth={2}
                                            radius={[4, 4, 0, 0]}
                                        />
                                    </BarChart>
                                </ResponsiveContainer>
                            ) : (
                                <p className={ui.empty}>No alerts in this period.</p>
                            )}
                            <details className="dash-table">
                                <summary>Show as table</summary>
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Day</th>
                                            <th>Fall</th>
                                            <th>Device</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {perDay.map(r => (
                                            <tr key={r.day}>
                                                <td>{r.day}</td>
                                                <td>{r.fall}</td>
                                                <td>{r.device}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </details>
                        </section>
                        <section className={ui.card}>
                            <h2>Fall alerts by hour of day</h2>
                            {a.fall ? (
                                <ResponsiveContainer width="100%" height={240}>
                                    <BarChart
                                        data={hours}
                                        margin={{ top: 8, right: 8, left: -18, bottom: 0 }}
                                        barCategoryGap="20%"
                                    >
                                        <CartesianGrid vertical={false} stroke="#eef2f6" />
                                        <XAxis
                                            dataKey="hour"
                                            tick={axis}
                                            tickLine={false}
                                            axisLine={{ stroke: '#cbd5e1' }}
                                            interval={2}
                                        />
                                        <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                                        <Tooltip cursor={{ fill: '#f1f5f9' }} labelFormatter={h => `${h}:00–${h}:59`} />
                                        <Bar
                                            dataKey="count"
                                            name="Fall alerts"
                                            fill={SERIES.fall}
                                            radius={[4, 4, 0, 0]}
                                        />
                                    </BarChart>
                                </ResponsiveContainer>
                            ) : (
                                <p className={ui.empty}>No fall alerts in this period.</p>
                            )}
                            <p className={ui.note}>
                                Clusters at certain hours point to routines that trigger false alarms.
                            </p>
                        </section>
                    </div>
                    <section className={ui.card} style={{ marginBottom: 20 }}>
                        <h2>On-site outcomes</h2>
                        {!outcomes.length && (
                            <p className={ui.empty}>
                                No fall alert has an outcome yet
                                {a.unresolved_falls ? ` (${a.unresolved_falls} waiting for one)` : ''}.
                            </p>
                        )}
                        {outcomes.map(([key, n]) => (
                            <div key={key} className="dash-outcome">
                                <span>{FALL_OUTCOMES[key] || key}</span>
                                <div className="dash-bar">
                                    <i style={{ width: `${(100 * n) / maxOutcome}%` }} />
                                </div>
                                <strong>{n}</strong>
                            </div>
                        ))}
                    </section>
                    <section className={ui.card} style={{ marginBottom: 20 }}>
                        <h2>Cameras</h2>
                        <div className="dash-scroll">
                            <table className="dash-data">
                                <thead>
                                    <tr>
                                        <th>Camera</th>
                                        <th>Detecting</th>
                                        <th>Monitored</th>
                                        <th>Outages</th>
                                        <th>Decision rate</th>
                                        <th>Processing time p50 / p95</th>
                                        <th>Fall alerts</th>
                                        <th>False alarms</th>
                                        <th>False alarms / h</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {data.cameras.map(c => (
                                        <tr key={c.camera_id}>
                                            <td>{cams.name(c.camera_id)}</td>
                                            <td>{num(c.availability_pct, 1, '%')}</td>
                                            <td>{num(c.monitored_h, 1, ' h')}</td>
                                            <td>
                                                {c.outages
                                                    ? `${c.outages} · ${num(c.outage_min, 0)} min (longest ${num(c.longest_outage_min, 0)})`
                                                    : 'none'}
                                            </td>
                                            <td>
                                                {c.mean_hz == null
                                                    ? '—'
                                                    : `${num(c.mean_hz, 1, 'Hz')} (${num(c.design_rate_pct, 0, '%')})`}
                                            </td>
                                            <td>
                                                {c.latency_p50_ms == null
                                                    ? '—'
                                                    : `${num(c.latency_p50_ms)} / ${num(c.latency_p95_ms)} ms`}
                                            </td>
                                            <td>{c.fall_alerts}</td>
                                            <td>{c.false_alarms}</td>
                                            <td>{q.resolved_falls ? num(c.false_alarms_per_h, 2) : '—'}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        <p className={ui.note}>
                            Detecting = share of the time the server was observing during which the camera&apos;s
                            decisions arrived. Outages count only observed time. Decision rate is against the{' '}
                            {data.design_hz} Hz design rate. Device temperatures and hardware limits are on{' '}
                            <Link className={ui.link} href="/devices">
                                Devices
                            </Link>
                            .
                        </p>
                    </section>
                </>
            )}
            <style jsx global>{`
                .dash-legend {
                    display: flex;
                    justify-content: center;
                    gap: 16px;
                    font-size: 12px;
                    color: #475569;
                    margin-top: 4px;
                }
                .dash-legend span {
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                }
                .dash-legend i {
                    width: 10px;
                    height: 10px;
                    border-radius: 2px;
                    display: inline-block;
                }
                .dash-tiles {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                    gap: 14px;
                    margin-bottom: 20px;
                }
                .dash-tile {
                    background: #fff;
                    border: 1px solid #e2e8f0;
                    border-radius: 14px;
                    padding: 14px 16px;
                    display: flex;
                    flex-direction: column;
                    gap: 2px;
                }
                .dash-tile span {
                    font-size: 12px;
                    font-weight: 700;
                    color: #64748b;
                }
                .dash-tile strong {
                    font-size: 28px;
                    font-weight: 800;
                    color: #0f172a;
                    font-variant-numeric: tabular-nums;
                }
                .dash-tile small {
                    font-size: 11px;
                    color: #64748b;
                    line-height: 1.4;
                }
                .dash-grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(min(460px, 100%), 1fr));
                    gap: 20px;
                    margin-bottom: 20px;
                }
                .dash-outcome {
                    display: grid;
                    grid-template-columns: 200px 1fr 40px;
                    align-items: center;
                    gap: 12px;
                    font-size: 13px;
                    color: #334155;
                    padding: 4px 0;
                }
                .dash-bar {
                    height: 12px;
                    background: #f1f5f9;
                    border-radius: 4px;
                    overflow: hidden;
                }
                .dash-bar i {
                    display: block;
                    height: 100%;
                    background: ${SERIES.fall};
                    border-radius: 0 4px 4px 0;
                }
                .dash-outcome strong {
                    text-align: right;
                    font-variant-numeric: tabular-nums;
                }
                .dash-scroll {
                    overflow-x: auto;
                }
                .dash-data,
                .dash-table table {
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 13px;
                }
                .dash-data th,
                .dash-table th {
                    text-align: left;
                    color: #64748b;
                    font-weight: 700;
                    font-size: 12px;
                    border-bottom: 1px solid #e2e8f0;
                    padding: 6px 8px;
                    white-space: nowrap;
                }
                .dash-data td,
                .dash-table td {
                    border-bottom: 1px solid #f1f5f9;
                    padding: 8px;
                    color: #334155;
                    white-space: nowrap;
                    font-variant-numeric: tabular-nums;
                }
                .dash-table {
                    margin-top: 8px;
                    font-size: 12px;
                    color: #475569;
                }
                .dash-table summary {
                    cursor: pointer;
                    font-weight: 600;
                }
                @media (max-width: 640px) {
                    .dash-outcome {
                        grid-template-columns: 120px 1fr 32px;
                    }
                }
            `}</style>
        </Page>
    );
}
