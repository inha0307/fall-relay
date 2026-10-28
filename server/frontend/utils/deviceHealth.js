/*
 * Device health from the device's profile (thresholds come from the server, see
 * server/bridge/device_profiles.py). States: Detecting · Degraded · Not detecting · Offline.
 */
const FALLBACK = { design_rate_hz: 10, degraded_rate_fraction: 0.9, temperature_warn_c: 75, limit_flags: {} };

export function activeLimits(device) {
    const flags = device.profile?.limit_flags || {};
    const current = device.throttle?.current;
    if (!current) return [];
    return Object.entries(flags)
        .filter(([key]) => current[key])
        .map(([, label]) => label);
}

/* cameras: [{ id, name, connection }] for the device's camera_ids */
export function deviceHealth(device, cameras) {
    const p = { ...FALLBACK, ...(device.profile || {}) };
    const fresh = !device.stale;
    const detecting = cameras.filter(c => c.connection?.edge_inference_connected);
    if (!detecting.length && !fresh)
        return {
            tone: 'offline',
            label: 'Offline',
            why: 'No decisions and no device reports. The device may be off, frozen or disconnected.',
        };
    if (!detecting.length)
        return {
            tone: 'down',
            label: 'Not detecting',
            why: 'The device reports, but its detection service sends no decisions. Start or restart detection.',
        };
    const issues = [];
    const minimum = p.design_rate_hz * p.degraded_rate_fraction;
    for (const c of cameras) {
        if (!c.connection?.edge_inference_connected) issues.push(`${c.name}: not detecting`);
        else if ((c.connection.edge_inference_hz ?? 0) < minimum)
            issues.push(
                `${c.name}: decision rate ${(c.connection.edge_inference_hz ?? 0).toFixed(1)} Hz is below the ${p.design_rate_hz} Hz design rate`
            );
    }
    const temp = device.metrics?.temperature_c;
    if (temp != null && temp >= p.temperature_warn_c) issues.push(`temperature ${temp.toFixed(0)} °C`);
    const limits = activeLimits(device);
    if (limits.length) issues.push(`hardware limit: ${limits.join(', ').toLowerCase()}`);
    if (!fresh) issues.push('device reports stopped');
    return issues.length
        ? { tone: 'degraded', label: 'Degraded', why: issues.join(' · ') }
        : { tone: 'ok', label: 'Detecting', why: 'Decisions arrive at the design rate; no hardware warning.' };
}

export const HEALTH_ORDER = { down: 0, offline: 1, degraded: 2, ok: 3 };
