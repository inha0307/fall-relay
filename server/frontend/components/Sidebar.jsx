'use client';
import { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter, usePathname } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { ROLE_LABELS, roleHome } from '@/utils/roles';
import {
    LayoutDashboard,
    PersonStanding,
    Bell,
    Server,
    Cpu,
    BarChart3,
    Cctv,
    Users,
    LogOut,
    User as UserIcon,
    Menu,
    X,
} from 'lucide-react';
import { useAlerts } from '@/context/AlertContext';

export default function Sidebar() {
    const { user, role, signOut } = useAuth();
    const router = useRouter();
    const pathname = usePathname();
    const alerts = useAlerts();
    const [mobileOpen, setMobileOpen] = useState(false);

    useEffect(() => {
        if (mobileOpen) {
            document.body.classList.add('sidebar-is-open');
        } else {
            document.body.classList.remove('sidebar-is-open');
        }
        return () => document.body.classList.remove('sidebar-is-open');
    }, [mobileOpen]);

    useEffect(() => {
        setMobileOpen(false);
    }, [pathname]);

    if (pathname === '/') return null;

    const handleLogout = async () => {
        await signOut();
        router.replace('/');
    };

    const closeMobile = () => setMobileOpen(false);

    const username = user?.name || user?.login_id || '';
    const pending = alerts?.enabled ? alerts.unacknowledged : 0; // badge: alerts nobody has acknowledged yet
    const item = (href, Icon, label, badge = 0, also = [], exact = false) => {
        const active = exact
            ? pathname === href
            : [href, ...also].some(p => pathname === p || pathname.startsWith(p + '/'));
        return (
            <Link key={href} href={href} className={`sidebar-link ${active ? 'active' : ''}`} onClick={closeMobile}>
                <Icon size={20} />
                <span>{label}</span>
                {badge > 0 && (
                    <span className="sidebar-badge" aria-label={`${badge} open`}>
                        {badge}
                    </span>
                )}
            </Link>
        );
    };

    return (
        <>
            <button className="sidebar-toggle-btn" onClick={() => setMobileOpen(v => !v)} aria-label="Toggle menu">
                {mobileOpen ? <X size={20} /> : <Menu size={20} />}
            </button>

            <div className={`sidebar-backdrop${mobileOpen ? ' sidebar-open' : ''}`} onClick={closeMobile} />

            <aside className={`global-sidebar${mobileOpen ? ' sidebar-open' : ''}`}>
                <div className="sidebar-logo-container">
                    <Link href={roleHome(role)} style={{ textDecoration: 'none' }} onClick={closeMobile}>
                        <div className="sidebar-logo">
                            <img src="/fall-relay.svg" alt="" className="logo-image" />
                            <span className="logo-text">Fall-Relay</span>
                        </div>
                    </Link>
                </div>

                {/* Operations first (the alert loop), then field evaluation, then administration. */}
                <nav className="sidebar-nav">
                    {role === 'responder' ? (
                        item('/alerts', Bell, 'Alerts', pending)
                    ) : (
                        <>
                            <div className="sidebar-section">Operations</div>
                            {item('/overview', LayoutDashboard, 'Status')}
                            {item('/alerts', Bell, 'Alerts', pending)}
                            {item('/skeleton', PersonStanding, 'Skeleton view')}
                            {item('/devices', Cpu, 'Devices')}
                            {item('/reports', BarChart3, 'Reports')}
                            <div className="sidebar-section">Administration</div>
                            {item('/system', Server, 'Server')}
                            {role === 'admin' && item('/manage/assignments', Cctv, 'Camera access')}
                            {role === 'admin' && item('/manage', Users, 'Accounts', 0, [], true)}
                        </>
                    )}
                    {item('/account', UserIcon, 'My account', 0, [], true)}
                </nav>

                <div className="sidebar-footer">
                    {user ? (
                        <div className="user-profile-card">
                            <Link
                                href="/account"
                                className="user-info"
                                onClick={closeMobile}
                                style={{ textDecoration: 'none' }}
                                title="My account"
                            >
                                <div className="user-avatar">
                                    <UserIcon size={18} color="#475569" />
                                </div>
                                <div className="user-details">
                                    <span className="user-name">{username}</span>
                                    <span className="user-role">
                                        {user.user_code} · {ROLE_LABELS[role] || role}
                                    </span>
                                </div>
                            </Link>
                            <button onClick={handleLogout} className="logout-btn" title="Sign out">
                                <LogOut size={18} />
                                <span>Sign out</span>
                            </button>
                        </div>
                    ) : (
                        <div className="loading-user"></div>
                    )}
                </div>
            </aside>
        </>
    );
}
