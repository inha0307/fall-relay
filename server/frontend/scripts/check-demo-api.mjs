/* Checks the real web routes against demo.py --serve (synthetic detection run), never production. */
import assert from 'node:assert/strict';
const base = process.env.CHECK_BASE_URL;
const bridge = process.env.PIPELINE_SERVER_URL;
assert(base && bridge && process.env.FALLRELAY_APP_SCHEMA, 'Use verify_web_demo.py with isolated data');
let cookie = '';
const cors = await fetch(`${bridge}/api/live`, { headers: { Origin: base }, signal: AbortSignal.timeout(10000) });
assert.equal(cors.headers.get('access-control-allow-origin'), base);
assert.equal(cors.status, 403, 'bridge reads need the server write token');
async function request(path, body) {
    const response = await fetch(base + path, {
        method: body ? 'POST' : 'GET',
        signal: AbortSignal.timeout(10000),
        headers: { ...(cookie ? { cookie } : {}), ...(body ? { 'content-type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
    });
    const set = response.headers.get('set-cookie');
    if (set) cookie = set.split(';')[0];
    return response;
}
assert.equal((await request('/api/alerts')).status, 401);
assert.equal((await request('/api/auth/login', { userid: 'tadmin', password: 'admin-pass-123' })).status, 200);
let response = await request('/api/alerts');
assert.equal(response.status, 200);
const { alerts } = await response.json();
assert.equal(alerts.length, 1);
const [alert] = alerts;
assert(alert.type === 'fall' && alert.camera_id === '1' && alert.state === 'resolved' && alert.resolution === 'fall');
response = await request(`/api/alerts/${alert.id}/clip`);
assert.equal(response.status, 200);
assert(response.headers.get('content-type').includes('video/webm'));
assert((await response.arrayBuffer()).byteLength > 1000);
response = await request('/api/bridge/live');
assert.equal(response.status, 200);
const { cameras } = await response.json();
assert.equal(cameras.length, 2);
for (const cam of cameras) {
    assert.equal(cam.video_connected, false, 'camera images stay on the device');
    assert.equal(cam.connection.last_edge_result.skeleton.length, 13);
}
console.log(
    JSON.stringify(
        {
            synthetic_only: true,
            checks: [
                'bridge reads need the server token',
                'alerts need sign-in',
                'resolved synthetic fall alert listed',
                'fall clip served through the web route',
                'two cameras: no video, 13-joint skeleton',
            ],
        },
        null,
        2
    )
);
