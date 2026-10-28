import { NextResponse } from 'next/server';
import { CAMERA_ID, forwardAdmin, readJson } from '@/utils/adminAuth';

/* Camera list (including disabled) and creation: administrators only. */
export async function GET(request) {
    return forwardAdmin(request, '/api/admin/cameras');
}

export async function POST(request) {
    const body = await readJson(request);
    if (!body || !CAMERA_ID.test(String(body.id ?? ''))) {
        return NextResponse.json({ error: 'Camera id: 1-32 letters, digits, _ or -.' }, { status: 400 });
    }
    const { id, label = '', location = '' } = body;
    return forwardAdmin(request, '/api/admin/cameras', { method: 'POST', body: { id: String(id), label, location } });
}
