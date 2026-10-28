import { NextResponse } from 'next/server';
import { sessionUser } from '@/lib/session';
import { serverEnv } from '@/lib/env.mjs';

/* Server routes only: admin check and calls to the bridge admin API (never import from browser code). */

export const CAMERA_ID = /^[A-Za-z0-9_-]{1,32}$/;

/* The signed-in administrator without a pending password change, or null. */
export async function adminUser(request) {
    const user = await sessionUser(request);
    return user?.role === 'admin' && !user.must_change_password ? user : null;
}

/* After the admin check, forward to the bridge with the write token and relay its response. */
export async function forwardAdmin(request, path, { method = 'GET', body, withActor = false } = {}) {
    const admin = await adminUser(request);
    if (!admin) {
        return NextResponse.json({ error: 'Only administrators can do this.' }, { status: 401 });
    }
    const token = serverEnv('WRITE_TOKEN');
    if (!token) {
        return NextResponse.json(
            { error: 'FALLRELAY_WRITE_TOKEN is not configured on the web server' },
            { status: 503 }
        );
    }
    const base = (process.env.PIPELINE_SERVER_URL || 'http://127.0.0.1:5001').replace(/\/+$/, '');
    try {
        const res = await fetch(`${base}${path}`, {
            method,
            headers: {
                'X-Write-Token': token,
                ...(withActor ? { 'X-Actor': admin.user_code } : {}),
                ...(body ? { 'Content-Type': 'application/json' } : {}),
            },
            body: body ? JSON.stringify(body) : undefined,
            cache: 'no-store',
        });
        const data = await res.json().catch(() => ({}));
        // Responses may carry a plain camera token: never cache them.
        return NextResponse.json(data, { status: res.status, headers: { 'Cache-Control': 'no-store' } });
    } catch {
        return NextResponse.json({ error: 'Cannot reach the bridge server.' }, { status: 502 });
    }
}

export async function readJson(request) {
    try {
        const body = await request.json();
        return body && typeof body === 'object' && !Array.isArray(body) ? body : null;
    } catch {
        return null;
    }
}
