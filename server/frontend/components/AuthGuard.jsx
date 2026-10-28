'use client';
import { useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { roleHome } from '@/utils/roles';

export default function AuthGuard({ children }) {
    const { user, loading } = useAuth();
    const router = useRouter();
    const pathname = usePathname();

    const isPublicPath = pathname === '/';
    // An account with a temporary password may only open /account until it sets its own password.
    const mustChange = !!user?.must_change_password && pathname !== '/account';
    const responderBlocked = user?.role === 'responder' && !['/alerts', '/account'].includes(pathname);

    useEffect(() => {
        if (!loading) {
            if (!user && !isPublicPath) {
                console.log(`[AuthGuard] Unauthorized access to ${pathname}, redirecting to /`);
                router.replace('/');
            } else if (user && (isPublicPath || mustChange || responderBlocked)) {
                router.replace(user.must_change_password ? '/account' : roleHome(user.role));
            }
        }
    }, [user, loading, isPublicPath, mustChange, responderBlocked, router, pathname]);

    if (loading) {
        return (
            <div
                style={{
                    height: '100vh',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    backgroundColor: '#f5f7fa',
                    color: '#64748b',
                    fontSize: '1.1rem',
                    fontWeight: '500',
                }}
            >
                Checking sign-in…
            </div>
        );
    }

    // Not signed in on a protected route: render nothing, so the page never shows before the redirect.
    if (!user && !isPublicPath) {
        return null;
    }

    if (user && (isPublicPath || mustChange || responderBlocked)) {
        return null;
    }

    return <>{children}</>;
}
