/*
 * Reader for the bridge /video_grid stream.
 * JPEGs of several cameras share one connection (browsers open only ~6 connections per host).
 * Each part: --frame\r\nContent-Type: image/jpeg\r\nX-Camera: <id>\r\nContent-Length: <n>\r\n\r\n<jpeg>\r\n
 */
const HEADER_END = [13, 10, 13, 10];
const MAX_HEADER_BYTES = 1024;

function findHeaderEnd(buf) {
    const limit = Math.min(buf.length, MAX_HEADER_BYTES) - 3;
    for (let i = 0; i < limit; i++) {
        if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) return i;
    }
    if (buf.length >= MAX_HEADER_BYTES) throw new Error('grid stream: header too long');
    return -1;
}

function concat(a, b) {
    if (a.length === 0) return b;
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
}

/* Throws when the stream ends or fails; aborting through `signal` raises AbortError. */
export async function readGridStream(url, onFrame, signal) {
    const res = await fetch(url, { signal, cache: 'no-store' });
    if (!res.ok || !res.body) throw new Error(`grid stream: HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = new Uint8Array(0);
    for (;;) {
        const { value, done } = await reader.read();
        if (done) throw new Error('grid stream: ended');
        buf = concat(buf, value);
        for (;;) {
            const end = findHeaderEnd(buf);
            if (end < 0) break;
            const headers = {};
            for (const line of decoder.decode(buf.subarray(0, end)).split('\r\n')) {
                const at = line.indexOf(': ');
                if (at > 0) headers[line.slice(0, at).toLowerCase()] = line.slice(at + 2);
            }
            const length = Number(headers['content-length']);
            if (!Number.isFinite(length) || !headers['x-camera']) throw new Error('grid stream: bad part header');
            const start = end + HEADER_END.length;
            if (buf.length < start + length + 2) break; // body plus trailing \r\n not complete yet
            onFrame(headers['x-camera'], buf.slice(start, start + length));
            buf = buf.slice(start + length + 2);
        }
    }
}
