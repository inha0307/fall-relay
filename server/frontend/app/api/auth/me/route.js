import { NextResponse } from 'next/server';
import { sessionUser, setSessionCookie, currentToken } from '@/lib/session';

/* Current user. Each call extends the session, so active users stay signed in. */
export async function GET(request) {
    const user = await sessionUser(request, { renew: true });
    if (!user) return NextResponse.json({ user: null }, { status: 401, headers: { 'Cache-Control': 'no-store' } });
    const response = NextResponse.json({ user }, { headers: { 'Cache-Control': 'no-store' } });
    return setSessionCookie(response, currentToken(request), request);
}
