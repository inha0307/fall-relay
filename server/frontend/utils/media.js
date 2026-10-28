'use client';
import { useEffect, useState } from 'react';

/* Short-lived media token for video and clip URLs served by the bridge (refreshed before it expires). */
export function useMediaToken() {
    const [token, setToken] = useState(null);
    useEffect(() => {
        let stopped = false;
        let timer;
        const load = async () => {
            try {
                const res = await fetch('/api/media-token', { cache: 'no-store' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (stopped) return;
                setToken(data.token);
                const refreshIn = Math.max(60, data.expires_at - Date.now() / 1000 - 300) * 1000;
                timer = setTimeout(load, refreshIn);
            } catch {
                if (!stopped) timer = setTimeout(load, 30000);
            }
        };
        load();
        return () => {
            stopped = true;
            clearTimeout(timer);
        };
    }, []);
    return token;
}

/* Append the media token to a bridge URL (null until a token is available). */
export function withMediaToken(url, token) {
    if (!url || !token) return null;
    return `${url}${url.includes('?') ? '&' : '?'}t=${encodeURIComponent(token)}`;
}
