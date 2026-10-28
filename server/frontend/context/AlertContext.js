'use client';
import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/context/AuthContext';
import { POLICY_VERSION, isLiveAlert, selectUrgentWaiting, selectManualReview } from '@/utils/alertPolicy.mjs';
const AlertContext = createContext(null);
// Connection states shown to the user; LIVE is also what the alert screens test for.
export const LIVE = 'Live';
const DISABLED = 'Alerts are disabled on the server',
    UNCONFIGURED = 'Cannot check the alert service';
// An alert nobody has acknowledged after this long is escalated: one more desktop notification and a louder reminder.
export const ESCALATE_AFTER_S = 120;
const REMIND_EVERY_MS = 30000;
const SOUND_KEY = 'fall-relay.alert-sound',
    NOTIFY_KEY = 'fall-relay.alert-notify';
export const ALERT_TITLES = {
    fall: 'Fall detected',
    camera_offline: 'Camera offline',
    device_warning: 'Device warning',
};
const stored = key => {
    try {
        return localStorage.getItem(key) === 'on';
    } catch {
        return false;
    }
};
const store = (key, on) => {
    try {
        localStorage.setItem(key, on ? 'on' : 'off');
    } catch {}
};
export function AlertProvider({ children }) {
    const { user } = useAuth();
    const [enabled, setEnabled] = useState(null);
    const [truncated, setTruncated] = useState(false);
    const [alerts, setAlerts] = useState([]);
    const [connection, setConnection] = useState('Connecting');
    const [error, setError] = useState('');
    const [filter, setFilter] = useState('open');
    const [busy, setBusy] = useState(null);
    const [now, setNow] = useState(Date.now());
    const [sound, setSound] = useState(false); // sound is playing-capable now
    const [soundWanted, setSoundWanted] = useState(false); // remembered choice; browsers need one click before audio starts
    const [notify, setNotify] = useState(false);
    const audio = useRef(null);
    const soundRef = useRef(false);
    const notifyRef = useRef(false);
    const escalated = useRef(new Set());
    const canHandle = ['admin', 'responder'].includes(user?.role);
    const device = useRef(null);
    useEffect(() => {
        soundRef.current = sound;
    }, [sound]);
    useEffect(() => {
        notifyRef.current = notify;
    }, [notify]);
    // Restore the remembered choices; audio can only start after a user gesture on this page.
    useEffect(() => {
        if (typeof window === 'undefined') return;
        setNotify(stored(NOTIFY_KEY) && 'Notification' in window && Notification.permission === 'granted');
        if (!stored(SOUND_KEY)) return;
        setSoundWanted(true);
        const unlock = async () => {
            try {
                audio.current ||= new (window.AudioContext || window.webkitAudioContext)();
                await audio.current.resume();
                setSound(true);
            } catch {}
        };
        window.addEventListener('pointerdown', unlock, { once: true });
        window.addEventListener('keydown', unlock, { once: true });
        return () => {
            window.removeEventListener('pointerdown', unlock);
            window.removeEventListener('keydown', unlock);
        };
    }, []);
    function beep(kind) {
        if (!soundRef.current || !audio.current) return;
        const tones = kind === 'fall' ? [880, 660, 880] : [520];
        tones.forEach((f, i) => {
            const osc = audio.current.createOscillator(),
                gain = audio.current.createGain(),
                at = audio.current.currentTime + i * 0.3;
            osc.connect(gain);
            gain.connect(audio.current.destination);
            gain.gain.value = 0.08;
            osc.frequency.value = f;
            osc.start(at);
            osc.stop(at + 0.22);
        });
    }
    function desktop(alert, escalation) {
        if (!isLiveAlert(alert)) return;
        if (!notifyRef.current || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
        const title = `${escalation ? 'Still unacknowledged · ' : ''}${ALERT_TITLES[alert.type] || 'Alert'} · camera ${alert.camera_id}`;
        const n = new Notification(title, {
            body: escalation
                ? `No one has acknowledged alert #${alert.id} for ${Math.round(ESCALATE_AFTER_S / 60)} min.`
                : `Alert #${alert.id}. Open Fall-Relay to acknowledge.`,
            tag: `fall-relay-alert-${alert.id}${escalation ? '-esc' : ''}`,
            requireInteraction: alert.type === 'fall',
        });
        n.onclick = () => {
            window.focus();
            if (location.pathname !== '/alerts') location.assign('/alerts');
            n.close();
        };
    }
    useEffect(() => {
        setAlerts([]);
        setEnabled(null);
        if (!user) return;
        device.current =
            sessionStorage.getItem('alert-device') ||
            Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
        sessionStorage.setItem('alert-device', device.current);
        let stopped = false,
            source,
            retry,
            last = Date.now(),
            cursor = null;
        const receipts = new Set();
        const receive = a => {
            if (stopped || !canHandle || !a.delivery_id || receipts.has(a.delivery_id)) return;
            receipts.add(a.delivery_id);
            fetch(`/api/alerts/${a.id}/received`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ delivery_id: a.delivery_id, device_id: device.current }),
            })
                .then(r => {
                    if (!r.ok) {
                        receipts.delete(a.delivery_id);
                        if (!stopped) {
                            setError('Retrying the delivery receipt.');
                            setTimeout(() => receive(a), 2000);
                        }
                    }
                })
                .catch(() => {
                    receipts.delete(a.delivery_id);
                    if (!stopped) setTimeout(() => receive(a), 2000);
                });
        };
        function connect() {
            if (stopped) return;
            source = new EventSource(
                `/api/alerts/stream?device=${encodeURIComponent(device.current)}&policy=${POLICY_VERSION}${cursor === null ? '' : '&after=' + cursor}`
            );
            source.onopen = () => {
                last = Date.now();
                setConnection(LIVE);
            };
            source.addEventListener('heartbeat', () => {
                last = Date.now();
                setConnection(LIVE);
            });
            source.addEventListener('snapshot', event => {
                const data = JSON.parse(event.data);
                cursor = data.cursor;
                last = Date.now();
                setTruncated(data.truncated);
                setAlerts(data.alerts);
                data.alerts.forEach(receive);
                setError('');
            });
            source.addEventListener('alert', event => {
                const data = JSON.parse(event.data);
                cursor = data.cursor;
                last = Date.now();
                setAlerts(previous =>
                    [data.alert, ...previous.filter(a => a.id !== data.alert.id)]
                        .sort((a, b) => b.id - a.id)
                        .slice(0, 500)
                );
                receive(data.alert);
                if (data.alert.state === 'pending' && isLiveAlert(data.alert)) {
                    beep(data.alert.type);
                    desktop(data.alert, false);
                }
            });
            source.onerror = () => {
                source.close();
                setConnection('Reconnecting');
                retry = setTimeout(connect, 2000);
            };
        }
        function checkStatus() {
            if (stopped) return;
            fetch('/api/alerts/status', { cache: 'no-store' })
                .then(async r => {
                    if (!r.ok) throw new Error('status unavailable');
                    const data = await r.json();
                    if (stopped) return;
                    setEnabled(data.enabled);
                    setError('');
                    if (data.enabled) connect();
                    else {
                        setConnection(DISABLED);
                        retry = setTimeout(checkStatus, 5000);
                    }
                })
                .catch(() => {
                    if (!stopped) {
                        setEnabled(null);
                        setConnection(UNCONFIGURED);
                        setError('Check the server configuration or sign in again.');
                        retry = setTimeout(checkStatus, 2000);
                    }
                });
        }
        checkStatus();
        const timer = setInterval(() => {
            setNow(Date.now());
            if (Date.now() - last > 10000)
                setConnection(current =>
                    [DISABLED, UNCONFIGURED].includes(current) ? current : 'Disconnected · alerts may be out of date'
                );
        }, 1000);
        return () => {
            stopped = true;
            source?.close();
            clearTimeout(retry);
            clearInterval(timer);
        };
    }, [user?.id, canHandle]);
    // open = not yet resolved; unacknowledged = nobody has taken it yet (drives the badge, tab title and status pill).
    const pending = alerts.filter(a => !['resolved', 'expired'].includes(a.state)).length;
    const waiting = selectUrgentWaiting(alerts);
    const manualReviewPending = selectManualReview(alerts).length;
    const unacknowledged = waiting.length;
    // Reminders while anything waits: a sound every 30 s, and one escalation notification per alert.
    useEffect(() => {
        if (!waiting.length) return;
        const timer = setInterval(() => {
            beep(waiting.some(a => a.type === 'fall') ? 'fall' : 'device');
            const nowS = Date.now() / 1000;
            for (const a of waiting)
                if (nowS - a.created_at >= ESCALATE_AFTER_S && !escalated.current.has(a.id)) {
                    escalated.current.add(a.id);
                    desktop(a, true);
                }
        }, REMIND_EVERY_MS);
        return () => clearInterval(timer);
    }, [waiting.map(a => a.id).join(',')]);
    useEffect(() => {
        const old = document.title;
        if (enabled && unacknowledged) document.title = `(${unacknowledged}) Alerts · Fall-Relay`;
        return () => {
            document.title = old;
        };
    }, [unacknowledged, enabled]);
    async function action(id, name, body = {}) {
        setBusy(id);
        setError('');
        try {
            const res = await fetch(`/api/alerts/${id}/${name}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok)
                throw new Error(
                    {
                        401: 'Your session has expired. Please sign in again.',
                        403: 'You are not allowed to handle this alert.',
                        409: 'This alert was already acknowledged or changed. Check its current state.',
                        422: 'Check the outcome and note.',
                    }[res.status] || 'The request failed. Check the server connection.'
                );
            setAlerts(previous => previous.map(a => (a.id === id ? data : a)));
            return true;
        } catch (e) {
            setError(e.message);
            return false;
        } finally {
            setBusy(null);
        }
    }
    async function toggleSound() {
        if (!sound) {
            try {
                audio.current ||= new (window.AudioContext || window.webkitAudioContext)();
                await audio.current.resume();
                setSound(true);
                setSoundWanted(true);
                store(SOUND_KEY, true);
                beep('device');
            } catch {
                setError('This browser does not allow sound.');
            }
        } else {
            setSound(false);
            setSoundWanted(false);
            store(SOUND_KEY, false);
        }
    }
    async function toggleNotify() {
        if (notify) {
            setNotify(false);
            store(NOTIFY_KEY, false);
            return;
        }
        if (typeof Notification === 'undefined') {
            setError('This browser does not support desktop notifications.');
            return;
        }
        const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
        if (permission !== 'granted') {
            setError('Desktop notifications are blocked. Allow them in the browser site settings.');
            return;
        }
        setNotify(true);
        store(NOTIFY_KEY, true);
    }
    const soundPending = soundWanted && !sound; // remembered but waiting for the first click
    return (
        <AlertContext.Provider
            value={{
                alerts,
                connection,
                error,
                filter,
                setFilter,
                busy,
                now,
                sound,
                soundPending,
                notify,
                canHandle,
                pending,
                unacknowledged,
                manualReviewPending,
                waiting,
                action,
                toggleSound,
                toggleNotify,
                enabled,
                truncated,
            }}
        >
            {children}
        </AlertContext.Provider>
    );
}
export const useAlerts = () => useContext(AlertContext);
