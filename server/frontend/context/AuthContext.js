'use client';
import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

/* Sign-in state: the server session (httpOnly cookie) is checked through /api/auth/me. */
const AuthContext = createContext();
const CHECK_INTERVAL_MS = 5 * 60 * 1000; // re-check periodically; an expired session returns to the sign-in page

export const AuthProvider = ({ children }) => {
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useState(true);

    const refresh = useCallback(async () => {
        try {
            const res = await fetch('/api/auth/me', { cache: 'no-store' });
            const data = res.ok ? await res.json() : { user: null };
            setUser(data.user || null);
            return data.user || null;
        } catch {
            // a network error is not a sign-out (keep the last state)
            return null;
        } finally {
            setLoading(false);
        }
    }, []);

    const signIn = useCallback(async (userid, password) => {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ userid, password }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Sign-in failed.');
        setUser(data.user);
        return data.user;
    }, []);

    const signOut = useCallback(async () => {
        try {
            await fetch('/api/auth/logout', { method: 'POST' });
        } catch {}
        setUser(null);
    }, []);

    useEffect(() => {
        refresh();
        const timer = setInterval(refresh, CHECK_INTERVAL_MS);
        const onVisible = () => {
            if (!document.hidden) refresh();
        };
        document.addEventListener('visibilitychange', onVisible);
        return () => {
            clearInterval(timer);
            document.removeEventListener('visibilitychange', onVisible);
        };
    }, [refresh]);

    return (
        <AuthContext.Provider value={{ user, role: user?.role ?? null, loading, refresh, signIn, signOut }}>
            {children}
        </AuthContext.Provider>
    );
};

export const useAuth = () => {
    return useContext(AuthContext);
};
