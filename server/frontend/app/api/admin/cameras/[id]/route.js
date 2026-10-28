import { NextResponse } from 'next/server';
import { CAMERA_ID, forwardAdmin, readJson } from '@/utils/adminAuth';

/* Change a camera's label, location, enabled flag or order: administrators only. */
export async function PATCH(request, { params }) {
    const { id } = await params;
    if (!CAMERA_ID.test(id)) return NextResponse.json({ error: 'invalid camera id' }, { status: 400 });
    const body = await readJson(request);
    if (!body) return NextResponse.json({ error: 'invalid JSON' }, { status: 400 });
    const allowed = {};
    for (const key of ['label', 'location', 'enabled', 'sort_order']) if (key in body) allowed[key] = body[key];
    return forwardAdmin(request, `/api/admin/cameras/${id}`, { method: 'PATCH', body: allowed });
}
