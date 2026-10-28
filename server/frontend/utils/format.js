/* Display formats shared by every screen (docs/UI_GLOSSARY.md, "Writing rules"). */
const pad = n => String(n).padStart(2, '0');
const toDate = t => (t instanceof Date ? t : typeof t === 'string' ? new Date(t) : new Date(t > 1e11 ? t : t * 1000)); // ISO text, epoch seconds or milliseconds

/* Short time-zone name of this browser, e.g. "GMT+9". */
export function zoneName(date = new Date()) {
    return (
        new Intl.DateTimeFormat('en-US', { timeZoneName: 'short' })
            .formatToParts(date)
            .find(p => p.type === 'timeZoneName')?.value || ''
    );
}

/* 2026-10-22 18:30:05 GMT+9 (24-hour clock; zone optional). */
export function dateTime(t, { zone = true, seconds = true } = {}) {
    if (t == null || t === '') return '—';
    const d = toDate(t);
    if (Number.isNaN(d.getTime())) return '—';
    const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? ':' + pad(d.getSeconds()) : ''}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock}${zone ? ' ' + zoneName(d) : ''}`;
}

/* 18:30:05 */
export function clock(t, seconds = true) {
    if (t == null) return '—';
    const d = toDate(t);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? ':' + pad(d.getSeconds()) : ''}`;
}

/* 48 s, 2.3 min, 1.5 h, 2.0 d */
export function duration(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return '—';
    const s = Math.max(0, seconds);
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 5400) return `${(s / 60).toFixed(1)} min`;
    if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
    return `${(s / 86400).toFixed(1)} d`;
}

/* A number with its unit, separated by a space: 10.0 Hz, 46 ms, 64 °C, 45 % */
export function quantity(value, digits = 0, unit = '') {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return `${Number(value).toFixed(digits)}${unit ? ' ' + unit : ''}`;
}

/* 512 KiB, 1.25 GiB */
export function bytes(value) {
    if (value == null) return '—';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let v = Number(value),
        i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(i < 2 ? 0 : 2)} ${units[i]}`;
}

/* A reading from a device profile (unit "bytes" and "s" get their own formats). */
export function reading(value, unit) {
    if (unit === 'bytes') return bytes(value);
    if (unit === 's') return duration(value);
    return quantity(value, unit === '%' || unit === 'MHz' ? 0 : 1, unit);
}
