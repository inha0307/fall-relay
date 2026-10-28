import { NextResponse } from 'next/server';
import { adminUser } from '@/utils/adminAuth';
import { serverEnv } from '@/lib/env.mjs';
export const dynamic = 'force-dynamic';

export async function GET(request, { params }) {
    const admin = await adminUser(request);
    if (!admin) return NextResponse.json({ error: 'Administrators only.' }, { status: 403 });
    const { path } = await params;
    if (
        !path ||
        !/^[A-Za-z0-9_-]{1,64}$/.test(path[0]) ||
        !(
            path.length === 1 ||
            (path.length === 3 && /^[a-f0-9]{32}$/.test(path[1]) && ['download', 'manifest'].includes(path[2]))
        )
    ) {
        return NextResponse.json({ error: 'Unknown log route.' }, { status: 404 });
    }
    const token = serverEnv('WRITE_TOKEN');
    if (!token) return NextResponse.json({ error: 'Bridge is not configured.' }, { status: 503 });
    const base = (process.env.PIPELINE_SERVER_URL || 'http://127.0.0.1:5001').replace(/\/+$/, '');
    try {
        const res = await fetch(`${base}/api/decision_logs/${path.join('/')}`, {
            headers: { 'X-Write-Token': token, 'X-Actor': admin.user_code },
            cache: 'no-store',
            signal: request.signal,
        });
        const headers = {
            'Content-Type': res.headers.get('content-type') || 'application/json',
            'Cache-Control': 'no-store',
        };
        for (const name of ['content-disposition', 'x-content-sha256'])
            if (res.headers.has(name)) headers[name] = res.headers.get(name);
        if (path[2] === 'manifest' && res.ok)
            headers['Content-Disposition'] = `attachment; filename="decision-log-${path[1]}-manifest.json"`;
        return new Response(res.body, { status: res.status, headers });
    } catch {
        return NextResponse.json({ error: 'Bridge unavailable.' }, { status: 502 });
    }
}
