import { NextResponse } from 'next/server';
import { clearSessionCookie, endSession } from '@/lib/session';

export async function POST(request) {
    await endSession(request);
    return clearSessionCookie(NextResponse.json({ ok: true }));
}
