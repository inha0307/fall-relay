import { NextResponse } from 'next/server';
import { requireAdmin } from '@/lib/session';
import { getDb } from '@/lib/appDb.mjs';
import { replaceCameraGrants } from '@/lib/cameraAccess.mjs';
import { serverEnv } from '@/lib/env.mjs';

export async function GET(request, { params }) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    const { id } = await params,
        db = await getDb();
    const account = await db.get('SELECT role,status FROM accounts WHERE id=?', id);
    if (!account) return NextResponse.json({ error: 'Account not found.' }, { status: 404 });
    if (account.role !== 'responder')
        return NextResponse.json({ error: 'Cameras can be assigned to responder accounts only.' }, { status: 422 });
    const rows = await db.all('SELECT camera_id FROM account_camera_grants WHERE account_id=? ORDER BY camera_id', id);
    return NextResponse.json({ cameras: rows.map(row => row.camera_id) }, { headers: { 'Cache-Control': 'no-store' } });
}

export async function PUT(request, { params }) {
    const auth = await requireAdmin(request);
    if (auth.error) return auth.error;
    if (request.headers.get('origin') !== `${request.nextUrl.protocol}//${request.headers.get('host')}`)
        return NextResponse.json({ error: 'Same-origin request required.' }, { status: 403 });
    const { id } = await params;
    const token = serverEnv('WRITE_TOKEN');
    if (!token) return NextResponse.json({ error: 'The camera server is not configured.' }, { status: 503 });
    let body;
    try {
        body = await request.json();
    } catch {
        return NextResponse.json({ error: 'Invalid JSON.' }, { status: 400 });
    }
    if (!body || !Array.isArray(body.cameras) || body.cameras.length > 100)
        return NextResponse.json({ error: 'Check the camera list.' }, { status: 422 });
    try {
        const base = (process.env.PIPELINE_SERVER_URL || 'http://127.0.0.1:5001').replace(/\/+$/, '');
        const response = await fetch(`${base}/api/admin/cameras`, {
            headers: { 'X-Write-Token': token },
            cache: 'no-store',
            signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) return NextResponse.json({ error: 'Cannot read the camera list.' }, { status: 502 });
        const data = await response.json();
        const known = new Set(data.cameras.filter(c => c.enabled !== false && c.enabled !== 0).map(c => String(c.id)));
        const cameras = await replaceCameraGrants(await getDb(), auth.user.id, id, body.cameras, known);
        return NextResponse.json({ cameras }, { headers: { 'Cache-Control': 'no-store' } });
    } catch (error) {
        return NextResponse.json(
            {
                error:
                    error.name === 'TimeoutError' ? 'The camera server timed out.' : 'Check the account and cameras.',
            },
            { status: 422 }
        );
    }
}
