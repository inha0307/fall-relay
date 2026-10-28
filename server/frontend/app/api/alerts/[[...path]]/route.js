import { NextResponse } from 'next/server';
import { requireRole } from '@/lib/session';
import { getDb } from '@/lib/appDb.mjs';
import { cameraScope } from '@/lib/cameraAccess.mjs';
import { serverEnv } from '@/lib/env.mjs';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

async function proxy(request, { params }) {
    const auth = await requireRole(request, ['admin', 'responder', 'viewer']);
    if (auth.error) return auth.error;
    const { path = [] } = await params;
    const route = path.join('/');
    if (!/^(?:|status|metrics|stream|[1-9]\d*|[1-9]\d*\/(?:received|ack|resolve|frame|clip))$/.test(route))
        return NextResponse.json({ error: 'Unknown alert route' }, { status: 404 });
    if (request.method === 'POST') {
        if (!['admin', 'responder'].includes(auth.user.role))
            return NextResponse.json({ error: 'Administrator or responder role required' }, { status: 403 });
        if (request.headers.get('origin') !== `${request.nextUrl.protocol}//${request.headers.get('host')}`)
            return NextResponse.json({ error: 'Same-origin request required' }, { status: 403 });
    }
    const token = serverEnv('WRITE_TOKEN');
    if (!token) return NextResponse.json({ error: 'Alert service is not configured' }, { status: 503 });
    const scope = await cameraScope(await getDb(), auth.user.id);
    const base = (process.env.PIPELINE_SERVER_URL || 'http://127.0.0.1:5001').replace(/\/+$/, '');
    const target = new URL(`${base}/api/alerts${route ? '/' + route : ''}`);
    for (const name of ['after', 'device'])
        if (request.nextUrl.searchParams.has(name))
            target.searchParams.set(name, request.nextUrl.searchParams.get(name));
    const headers = {
        'X-Write-Token': token,
        'X-Alert-Actor': auth.user.user_code,
        'X-Alert-Role': auth.user.role,
        'X-Alert-Cameras': scope === null ? '*' : scope.join(','),
    };
    // Scope/actor headers and destination can never be selected by the browser.
    if (route === 'stream' && request.headers.has('last-event-id'))
        headers['Last-Event-ID'] = request.headers.get('last-event-id');
    if (route.endsWith('/clip') && request.headers.has('range')) headers.Range = request.headers.get('range');
    let body;
    if (request.method === 'POST') {
        body = await request.text();
        if (Buffer.byteLength(body) > 8192) return NextResponse.json({ error: 'Body too large' }, { status: 413 });
        headers['Content-Type'] = 'application/json';
    }
    try {
        const upstream = await fetch(target, {
            method: request.method,
            headers,
            body,
            cache: 'no-store',
            signal: request.signal,
        });
        return new Response(upstream.body, {
            status: upstream.status,
            headers: {
                'Content-Type': upstream.headers.get('content-type') || 'application/json',
                'Cache-Control': 'no-store',
                'X-Accel-Buffering': 'no',
                ...Object.fromEntries(
                    ['content-range', 'accept-ranges', 'content-length']
                        .filter(name => upstream.headers.has(name))
                        .map(name => [name, upstream.headers.get(name)])
                ),
            },
        });
    } catch {
        return NextResponse.json({ error: 'Alert service is unavailable' }, { status: 502 });
    }
}
export const GET = proxy;
export const POST = proxy;
