import { NextResponse } from 'next/server';
import { requireRole } from '@/lib/session';
import { serverEnv } from '@/lib/env.mjs';
export const dynamic = 'force-dynamic';
export async function GET(request, { params }) {
    const auth = await requireRole(request, ['admin', 'viewer']);
    if (auth.error) return auth.error;
    const { path } = await params;
    if (
        !path?.length ||
        path.some(p => !/^[A-Za-z0-9_-]+$/.test(p)) ||
        ['alerts', 'edge_event', 'admin'].includes(path[0])
    )
        return NextResponse.json({ error: 'Unknown bridge route' }, { status: 404 });
    const token = serverEnv('WRITE_TOKEN');
    if (!token) return NextResponse.json({ error: 'Bridge is not configured' }, { status: 503 });
    const base = (process.env.PIPELINE_SERVER_URL || 'http://127.0.0.1:5001').replace(/\/+$/, '');
    try {
        const upstream = await fetch(`${base}/api/${path.join('/')}${request.nextUrl.search}`, {
            headers: { 'X-Write-Token': token },
            cache: 'no-store',
            signal: request.signal,
        });
        return new Response(upstream.body, {
            status: upstream.status,
            headers: {
                'Content-Type': upstream.headers.get('content-type') || 'application/json',
                'Cache-Control': 'no-store',
                ...(upstream.headers.get('content-disposition')
                    ? { 'Content-Disposition': upstream.headers.get('content-disposition') }
                    : {}),
            },
        });
    } catch {
        return NextResponse.json({ error: 'Bridge unavailable' }, { status: 502 });
    }
}
