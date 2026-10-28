'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAlerts, LIVE } from '@/context/AlertContext';
/* Connection problems only: waiting alerts are announced by AlertBanner. */
export default function AlertStatus() {
    const alerts = useAlerts();
    const pathname = usePathname();
    if (!alerts?.enabled || pathname === '/alerts' || alerts.connection === LIVE) return null;
    return (
        <aside
            aria-label="Live alert status"
            style={{
                position: 'fixed',
                right: 16,
                bottom: 16,
                zIndex: 200,
                background: '#fffbeb',
                border: '1px solid #fcd34d',
                borderRadius: 12,
                padding: '10px 16px',
                boxShadow: '0 2px 8px #0001',
                color: '#92400e',
                fontSize: 13,
            }}
        >
            <span role="status">Alert connection: {alerts.connection}</span> · <Link href="/alerts">Alerts</Link>
        </aside>
    );
}
