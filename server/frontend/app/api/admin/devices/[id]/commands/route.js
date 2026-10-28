import { NextResponse } from 'next/server';
import { forwardAdmin, readJson } from '@/utils/adminAuth';

const DEVICE_ID = /^[A-Za-z0-9_-]{1,64}$/;
const COMMANDS = [
    'restart_inference',
    'stop_inference',
    'start_inference',
    'logs',
    'reboot',
    'decision_log_start',
    'decision_log_stop',
    'decision_log_collect',
];

/* Device commands: administrators only. The bridge records the administrator's user code as the requester. */
export async function GET(request, { params }) {
    const { id } = await params;
    if (!DEVICE_ID.test(id)) return NextResponse.json({ error: 'invalid device id' }, { status: 400 });
    return forwardAdmin(request, `/api/devices/${id}/commands`);
}

export async function POST(request, { params }) {
    const { id } = await params;
    if (!DEVICE_ID.test(id)) return NextResponse.json({ error: 'invalid device id' }, { status: 400 });
    const body = await readJson(request);
    if (!body || !COMMANDS.includes(body.command))
        return NextResponse.json({ error: 'unknown command' }, { status: 422 });
    return forwardAdmin(request, `/api/devices/${id}/commands`, {
        method: 'POST',
        withActor: true,
        body: {
            command: body.command,
            confirm_open_event: body.confirm_open_event === true,
            params: body.params || {},
        },
    });
}
