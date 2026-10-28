'use client';
import { useEffect, useState } from 'react';
import { useAlerts } from '@/context/AlertContext';
import { dateTime } from '@/utils/format';
import ui from '@/components/ui.module.css';

export default function AlertOpsHealth() {
    const { enabled } = useAlerts();
    const [data, setData] = useState(null),
        [error, setError] = useState('');
    useEffect(() => {
        if (!enabled) return;
        let active = true;
        async function poll() {
            try {
                const res = await fetch('/api/alerts/metrics', { cache: 'no-store' });
                if (!res.ok) throw new Error();
                const value = await res.json();
                if (active) {
                    setData(value);
                    setError('');
                }
            } catch {
                if (active) setError('Cannot refresh the alert service status.');
            }
        }
        poll();
        const timer = setInterval(poll, 5000);
        return () => {
            active = false;
            clearInterval(timer);
        };
    }, [enabled]);
    if (!enabled) return null;
    return (
        <section className={ui.card}>
            <h2>Alert delivery and device checks</h2>
            {error && (
                <p role="alert" className={ui.error}>
                    {error}
                </p>
            )}
            {data?.watchdog_error && (
                <p role="alert" className={ui.error}>
                    The device check failed. See the server log.
                </p>
            )}
            <p className={ui.note}>
                Server time {data ? dateTime(data.server_time) : 'loading'} · device checks{' '}
                {data?.watchdog_enabled ? 'on' : 'off'}
            </p>
            <p className={ui.note}>
                Delivery jobs: {data?.dispatch.map(row => `${row.state} ${row.count}`).join(' · ') || 'none'}{' '}
                (deliveries to signed-in browsers; there is no push channel)
            </p>
            {data?.devices.map(d => (
                <div key={d.camera_id} className={ui.row}>
                    <strong>Camera {d.camera_id}</strong>
                    <span>
                        Decisions {!d.inference_seen ? 'not seen yet' : d.inference_missing ? 'stopped' : 'arriving'} ·
                        device {d.hardware_fresh ? 'reporting' : 'not reporting'}
                        {d.video_seen && !d.video_missing ? ' · debug video on' : ''}
                    </span>
                    <span>
                        Temperature {d.temperature_c != null ? `${d.temperature_c} °C` : 'unknown'} · hardware limits{' '}
                        {d.device_flags_active == null ? 'unknown' : d.device_flags_active ? 'active' : 'none'}
                    </span>
                </div>
            ))}
        </section>
    );
}
