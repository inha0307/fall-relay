/* Server policy foundation. Responder grants never authorize a global media token. */
import { audit, nowIso } from './appDb.mjs';

const CAMERA_ID = /^[A-Za-z0-9_-]{1,32}$/;
export async function cameraScope(db, accountId) {
    const account = await db.get('SELECT * FROM accounts WHERE id=?', accountId);
    if (!account || account.status !== 'active' || account.must_change_password) return [];
    if (['admin', 'viewer'].includes(account.role)) return null; // administrators and viewers read every camera
    if (account.role !== 'responder') return [];
    const rows = await db.all(
        'SELECT camera_id FROM account_camera_grants WHERE account_id=? ORDER BY camera_id',
        accountId
    );
    return rows.map(row => row.camera_id);
}

export async function mayReadCamera(db, accountId, cameraId) {
    if (typeof cameraId !== 'string' || !CAMERA_ID.test(cameraId)) return false;
    const scope = await cameraScope(db, accountId);
    return scope === null || scope.includes(cameraId);
}

// An authenticated admin route must supply the live bridge camera registry as knownCameraIds.
export async function replaceCameraGrants(db, actorId, accountId, cameras, knownCameraIds) {
    if (
        !Array.isArray(cameras) ||
        cameras.length > 100 ||
        !(knownCameraIds instanceof Set) ||
        cameras.some(camera => typeof camera !== 'string' || !CAMERA_ID.test(camera) || !knownCameraIds.has(camera))
    ) {
        throw new Error('Invalid camera grants');
    }
    return db.transaction(async tx => {
        const actor = await tx.get('SELECT * FROM accounts WHERE id=?', actorId);
        const target = await tx.get('SELECT * FROM accounts WHERE id=?', accountId);
        if (!actor || actor.role !== 'admin' || actor.status !== 'active' || actor.must_change_password)
            throw new Error('Administrator required');
        if (!target || target.role !== 'responder' || target.status !== 'active')
            throw new Error('Active responder required');
        const before = await cameraScope(tx, accountId);
        await tx.run('DELETE FROM account_camera_grants WHERE account_id=?', accountId);
        const unique = [...new Set(cameras)].sort();
        for (const camera of unique)
            await tx.run(
                'INSERT INTO account_camera_grants VALUES (?,?,?,?)',
                accountId,
                camera,
                nowIso(),
                actor.user_code
            );
        await audit(tx, actor, target, 'camera_grants_changed', { from: before, to: unique });
        return unique;
    });
}
