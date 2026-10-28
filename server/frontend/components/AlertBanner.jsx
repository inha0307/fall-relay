'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { AlertTriangle, BellRing } from 'lucide-react';
import { useAlerts, ALERT_TITLES, ESCALATE_AFTER_S } from '@/context/AlertContext';
import { duration } from '@/utils/format';

const ago = s => `${duration(s)} ago`;

/* Shown on every screen except Alerts while any alert waits for acknowledgement. It stays until someone
   acknowledges: the live decision may already be over, the alert is not. Falls are red, device problems amber. */
export default function AlertBanner() {
    const feed = useAlerts();
    const pathname = usePathname();
    if (!feed?.enabled || !feed.waiting?.length || pathname === '/alerts' || pathname === '/login') return null;
    const falls = feed.waiting.filter(a => a.type === 'fall');
    const list = falls.length ? falls : feed.waiting;
    const newest = list.reduce((a, b) => (b.created_at > a.created_at ? b : a));
    const oldest = feed.waiting.reduce((a, b) => (b.created_at < a.created_at ? b : a));
    const nowS = feed.now / 1000;
    const escalated = nowS - oldest.created_at >= ESCALATE_AFTER_S;
    const fall = falls.length > 0;
    return (
        <div role="alert" className={`alert-banner ${fall ? 'alert-banner-fall' : 'alert-banner-device'}`}>
            {fall ? <AlertTriangle size={22} /> : <BellRing size={22} />}
            <div className="alert-banner-text">
                <strong>
                    {ALERT_TITLES[newest.type] || 'Alert'} · camera {newest.camera_id}
                </strong>
                <span>
                    {ago(nowS - newest.created_at)}
                    {feed.waiting.length > 1 ? ` · ${feed.waiting.length} alerts waiting` : ''}
                    {escalated ? ` · unacknowledged for ${Math.floor((nowS - oldest.created_at) / 60)} min` : ''}
                </span>
            </div>
            {feed.soundPending && (
                <span className="alert-banner-hint">Click anywhere to turn the alert sound back on</span>
            )}
            <Link href="/alerts" className="alert-banner-button">
                {feed.canHandle ? 'Open alerts to acknowledge' : 'Open alerts'}
            </Link>
            <style jsx global>{`
                .alert-banner {
                    position: fixed;
                    top: 12px;
                    left: 50%;
                    transform: translateX(-50%);
                    z-index: 2100;
                    display: flex;
                    align-items: center;
                    gap: 14px;
                    padding: 12px 18px;
                    border-radius: 14px;
                    color: #fff;
                    box-shadow: 0 10px 25px rgba(15, 23, 42, 0.25);
                    max-width: calc(100vw - 24px);
                    flex-wrap: wrap;
                }
                .alert-banner-fall {
                    background: #dc2626;
                    animation: alert-banner-pulse 1.6s infinite;
                }
                .alert-banner-device {
                    background: #d97706;
                }
                .alert-banner-text {
                    display: flex;
                    flex-direction: column;
                    gap: 2px;
                    font-size: 13px;
                }
                .alert-banner-text strong {
                    font-size: 15px;
                }
                .alert-banner-hint {
                    font-size: 12px;
                    opacity: 0.9;
                }
                .alert-banner-button {
                    background: #fff;
                    color: #0f172a;
                    border-radius: 10px;
                    padding: 8px 12px;
                    font-weight: 700;
                    font-size: 13px;
                    text-decoration: none;
                    white-space: nowrap;
                }
                @keyframes alert-banner-pulse {
                    0%,
                    100% {
                        box-shadow: 0 10px 25px rgba(220, 38, 38, 0.35);
                    }
                    50% {
                        box-shadow: 0 10px 35px rgba(220, 38, 38, 0.75);
                    }
                }
            `}</style>
        </div>
    );
}
