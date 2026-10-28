import { NextResponse } from 'next/server';
import { createHmac } from 'node:crypto';
import { requireRole } from '@/lib/session';
import { serverEnv } from '@/lib/env.mjs';

/*
 * Short-lived token that lets the browser load video and clips straight from the bridge
 * (?t=<token>), without proxying the streams through this server. The bridge verifies
 * '<expiry>.<HMAC-SHA256(FALLRELAY_WRITE_TOKEN, "media:<expiry>")>' (video_bridge.media_token_valid).
 */
const LIFETIME_S = 3600;

export async function GET(request) {
    const auth = await requireRole(request, ['admin', 'viewer']);
    if (auth.error) return auth.error;
    const secret = serverEnv('WRITE_TOKEN');
    if (!secret)
        return NextResponse.json(
            { error: 'FALLRELAY_WRITE_TOKEN is not configured on the web server' },
            { status: 503 }
        );
    const expiry = Math.floor(Date.now() / 1000) + LIFETIME_S;
    const signature = createHmac('sha256', secret).update(`media:${expiry}`).digest('hex');
    return NextResponse.json(
        { token: `${expiry}.${signature}`, expires_at: expiry },
        { headers: { 'Cache-Control': 'no-store' } }
    );
}
