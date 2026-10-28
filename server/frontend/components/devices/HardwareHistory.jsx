'use client';
import { useState } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { useBridge } from '@/utils/bridge';
import { clock, dateTime, bytes, quantity } from '@/utils/format';
import ui from '../ui.module.css';

const RANGES = [1, 6, 24, 72];

/* Readings over time; which lines exist depends on what the device's profile reports. */
export default function HardwareHistory({ device }) {
    const [hours, setHours] = useState(1);
    const history = useBridge(`/api/devices/${encodeURIComponent(device.device_id)}/metrics`, { hours }, 15000);
    const keys = new Set((device.profile?.readings || []).map(r => r.key));
    const samples = history.data?.samples || [];
    const gap = Math.max(30, (device.interval_s || 5) * 2.5);
    const rows = samples.flatMap((s, i) => {
        const m = s.metrics || {};
        const point = {
            t: s.received_at,
            cpu: m.cpu_pct,
            temperature: m.temperature_c,
            memory: m.ram_total_bytes && m.ram_used_bytes != null ? (100 * m.ram_used_bytes) / m.ram_total_bytes : null,
        };
        // Break the line where reports stopped instead of drawing across the gap.
        return i && s.received_at - samples[i - 1].received_at > gap
            ? [{ t: samples[i - 1].received_at + 1 }, point]
            : [point];
    });
    return (
        <div className={ui.stack}>
            <div className={ui.row}>
                <div className={ui.toolbar} role="group" aria-label="Time range">
                    {RANGES.map(h => (
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
                </div>
                <a
                    className={ui.link}
                    href={`/api/bridge/devices/${encodeURIComponent(device.device_id)}/metrics?hours=${hours}&format=csv`}
                >
                    Export CSV
                </a>
            </div>
            {history.error && <p className={ui.warning}>The history could not be refreshed.</p>}
            {history.data?.truncated && (
                <p className={ui.note}>Showing the latest 10,000 samples; export a shorter range for complete data.</p>
            )}
            <section className={ui.card}>
                {rows.length ? (
                    <ResponsiveContainer width="100%" height={260}>
                        <LineChart data={rows}>
                            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                            <XAxis
                                dataKey="t"
                                type="number"
                                domain={['dataMin', 'dataMax']}
                                tickFormatter={t => clock(t, false)}
                                minTickGap={40}
                            />
                            <YAxis yAxisId="percent" domain={[0, 100]} unit=" %" width={50} />
                            {keys.has('temperature_c') && (
                                <YAxis yAxisId="temp" orientation="right" unit=" °C" width={56} />
                            )}
                            <Tooltip
                                labelFormatter={t => dateTime(t)}
                                formatter={(v, name) => [v == null ? '—' : Number(v).toFixed(1), name]}
                            />
                            <Legend />
                            {keys.has('cpu_pct') && (
                                <Line
                                    yAxisId="percent"
                                    dataKey="cpu"
                                    name="CPU %"
                                    stroke="#2563eb"
                                    dot={false}
                                    connectNulls={false}
                                    isAnimationActive={false}
                                />
                            )}
                            {keys.has('ram_used_bytes') && (
                                <Line
                                    yAxisId="percent"
                                    dataKey="memory"
                                    name="Memory %"
                                    stroke="#059669"
                                    dot={false}
                                    connectNulls={false}
                                    isAnimationActive={false}
                                />
                            )}
                            {keys.has('temperature_c') && (
                                <Line
                                    yAxisId="temp"
                                    dataKey="temperature"
                                    name="Temperature °C"
                                    stroke="#dc2626"
                                    dot={false}
                                    connectNulls={false}
                                    isAnimationActive={false}
                                />
                            )}
                        </LineChart>
                    </ResponsiveContainer>
                ) : (
                    <p className={ui.empty}>No readings in this time range.</p>
                )}
            </section>
            {device.processes?.length > 0 && (
                <section>
                    <h2 className={ui.sectionTitle}>Services on the device</h2>
                    <div className={ui.tableWrap}>
                        <table className={ui.table}>
                            <thead>
                                <tr>
                                    <th scope="col">Service</th>
                                    <th scope="col">State</th>
                                    <th scope="col" style={{ textAlign: 'right' }}>
                                        CPU
                                    </th>
                                    <th scope="col" style={{ textAlign: 'right' }}>
                                        Memory
                                    </th>
                                </tr>
                            </thead>
                            <tbody>
                                {device.processes.map(p => (
                                    <tr key={p.service}>
                                        <td className={ui.mono}>{p.service}</td>
                                        <td>{p.running ? 'Running' : 'Not running'}</td>
                                        <td style={{ textAlign: 'right' }}>{quantity(p.cpu_pct, 1, '%')}</td>
                                        <td style={{ textAlign: 'right' }}>{bytes(p.rss_bytes)}</td>
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
