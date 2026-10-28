'use client';
import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { roleHome } from '@/utils/roles';
import './login.css';

export default function LoginPage() {
    const router = useRouter();
    const { signIn } = useAuth();
    const [userid, setUserid] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const [splashPhase, setSplashPhase] = useState('hidden');

    useEffect(() => {
        if (!sessionStorage.getItem('fall-relay_splash_shown')) {
            setSplashPhase('assembling');
            setTimeout(() => setSplashPhase('assembled'), 1400);
            setTimeout(() => setSplashPhase('fading'), 4000);
            setTimeout(() => {
                setSplashPhase('hidden');
                sessionStorage.setItem('fall-relay_splash_shown', '1');
            }, 5000);
        }
    }, []);

    const handleLogin = async e => {
        e.preventDefault();
        setError('');
        setLoading(true);

        try {
            // The server looks up the account and writes the login log.
            const account = await signIn(userid, password);
            router.push(account.must_change_password ? '/account' : roleHome(account.role));
        } catch (err) {
            console.error('Login error:', err);
            setError(err.message || 'Sign-in failed.');
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="login-container">
            {splashPhase !== 'hidden' && (
                <div className={`splash-overlay splash-${splashPhase}`}>
                    <div className="splash-logo-wrap">
                        <img src="/fall-relay.svg" className="logo-piece logo-piece-1" alt="" />
                        <img src="/fall-relay.svg" className="logo-piece logo-piece-2" alt="" />
                        <img src="/fall-relay.svg" className="logo-piece logo-piece-3" alt="" />
                        <div className="splash-impact" />
                    </div>
                    <div className="splash-text">
                        <h1 className="splash-title">Fall-Relay</h1>
                        <p className="splash-subtitle">Fall-event delivery and alert handling</p>
                    </div>
                </div>
            )}
            <div className="bg-shape bg-shape-1"></div>
            <div className="bg-shape bg-shape-2"></div>
            <div className="bg-shape bg-shape-3"></div>

            <main className="login-card">
                <div className="login-header">
                    <h1 className="main-title">Sign in</h1>
                    {error && (
                        <p className="error-message" style={{ color: '#ff4d4d', marginTop: '10px' }}>
                            {error}
                        </p>
                    )}
                </div>

                <form className="login-form" onSubmit={handleLogin}>
                    <div className="input-group">
                        <input
                            type="text"
                            id="userid"
                            name="userid"
                            placeholder="User ID"
                            required
                            autoComplete="off"
                            value={userid}
                            onChange={e => setUserid(e.target.value)}
                        />
                    </div>

                    <div className="input-group">
                        <input
                            type="password"
                            id="password"
                            name="password"
                            placeholder="Password"
                            required
                            value={password}
                            onChange={e => setPassword(e.target.value)}
                        />
                    </div>

                    <button type="submit" className="login-btn" disabled={loading}>
                        {loading ? 'Signing in…' : 'Sign in'}
                    </button>
                </form>
            </main>
        </div>
    );
}
