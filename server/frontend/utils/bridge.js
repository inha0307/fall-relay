'use client';
import { useEffect, useRef, useState } from 'react';

/* Read-only call to the bridge (video_bridge.py) API; throws on failure. */
export async function bridgeGet(path, params = {}) {
    const query = new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString();
    const res = await fetch(`/api/bridge/${path.replace(/^\/api\//, '')}${query ? `?${query}` : ''}`, {
        cache: 'no-store',
        signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
    return res.json();
}

/* Hook that polls the bridge; refetches immediately when the parameters change. */
export function useBridge(path, params, intervalMs = 10000) {
    const [state, setState] = useState({ data: null, error: null, loading: true });
    const key = JSON.stringify([path, params]);
    const paramsRef = useRef(params);
    paramsRef.current = params;

    useEffect(() => {
        let alive = true;
        const load = () =>
            bridgeGet(path, paramsRef.current)
                .then(data => alive && setState({ data, error: null, loading: false }))
                .catch(error => alive && setState(prev => ({ data: prev.data, error, loading: false })));
        load();
        const timer = setInterval(load, intervalMs);
        return () => {
            alive = false;
            clearInterval(timer);
        };
    }, [key, intervalMs]); // eslint-disable-line react-hooks/exhaustive-deps

    return state;
}

/* Camera id → display name, including disabled cameras so older events keep their names. */
export function useCameraNames() {
    const { data } = useBridge('/api/cameras', { include_disabled: 'true' }, 30000);
    const cameras = data?.cameras || [];
    const byId = Object.fromEntries(cameras.map(c => [String(c.id), c]));
    const name = id => byId[String(id)]?.label || `CAM ${id}`;
    const full = id => {
        const c = byId[String(id)];
        if (!c) return `CAM ${id}`;
        return c.location ? `${c.label} · ${c.location}` : c.label;
    };
    return { cameras, name, full };
}

/* Evaluation labels for fall episodes (the bridge stores the keys). */
export const REVIEW_LABELS = {
    new: 'Unreviewed',
    fall: 'Real fall',
    fa_sitting: 'False alarm · sitting',
    fa_lying: 'False alarm · lying down',
    fa_bending: 'False alarm · bending',
    fa_other: 'False alarm · other',
    uncertain: 'Uncertain',
};
const FALSE_ALARM_COLOR = { bg: '#f1f5f9', fg: '#64748b' };
export const REVIEW_COLORS = {
    new: { bg: '#fee2e2', fg: '#dc2626' },
    fall: { bg: '#dbeafe', fg: '#2563eb' },
    fa_sitting: FALSE_ALARM_COLOR,
    fa_lying: FALSE_ALARM_COLOR,
    fa_bending: FALSE_ALARM_COLOR,
    fa_other: FALSE_ALARM_COLOR,
    uncertain: { bg: '#fef3c7', fg: '#b45309' },
    false_alarm: FALSE_ALARM_COLOR,
};
export const SUMMARY_LABELS = {
    new: 'Unreviewed',
    fall: 'Real fall',
    false_alarm: 'False alarm',
    uncertain: 'Uncertain',
};

const pad = n => String(n).padStart(2, '0');

/* Local time as YYYY-MM-DD HH:mm:ss (unambiguous in exported logs). */
export function formatTime(epochSeconds) {
    const d = new Date(epochSeconds * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${formatClock(epochSeconds)}`;
}

/* Local time as HH:mm:ss, or HH:mm with seconds=false. */
export function formatClock(epochSeconds, seconds = true) {
    const d = new Date(epochSeconds * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? `:${pad(d.getSeconds())}` : ''}`;
}
