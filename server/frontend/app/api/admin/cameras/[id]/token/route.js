import { NextResponse } from 'next/server';
import { CAMERA_ID, forwardAdmin } from '@/utils/adminAuth';

/* Issue (plain value shown only in this response) or clear a camera push token: administrators only. */
export async function POST(request, { params }) {
    const { id } = await params;
    if (!CAMERA_ID.test(id)) return NextResponse.json({ error: 'invalid camera id' }, { status: 400 });
    return forwardAdmin(request, `/api/admin/cameras/${id}/token`, { method: 'POST' });
}

export async function DELETE(request, { params }) {
    const { id } = await params;
    if (!CAMERA_ID.test(id)) return NextResponse.json({ error: 'invalid camera id' }, { status: 400 });
    return forwardAdmin(request, `/api/admin/cameras/${id}/token`, { method: 'DELETE' });
}
