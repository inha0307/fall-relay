/* Account store checks in a disposable PostgreSQL schema: initialisation, ledger, counters, grants, deactivation. */
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import pg from 'pg';
import { openDb, nextUserCode, ROLES, SCHEMA_VERSION } from '../lib/appDb.mjs';
import { poolConfig } from '../lib/pgConn.mjs';
import { cameraScope, mayReadCamera, replaceCameraGrants } from '../lib/cameraAccess.mjs';

const url = process.env.FALLRELAY_DATABASE_URL;
assert(url, 'set FALLRELAY_DATABASE_URL to a database in which this role may create schemas');
const schema = 'test_app_' + randomBytes(12).toString('hex');
const { schema: _schema, options: _options, max: _max, ...plain } = poolConfig(url, schema);
const admin = new pg.Client(plain);
await admin.connect();
try {
    let db = await openDb(url, schema);
    assert.deepEqual(ROLES, ['admin', 'viewer', 'responder']);
    const ledger = await db.all('SELECT version FROM app_schema_migrations');
    assert.deepEqual(
        ledger.map(r => r.version),
        [SCHEMA_VERSION]
    );
    for (const [id, role] of [
        ['A', 'admin'],
        ['B', 'viewer'],
        ['D', 'responder'],
    ]) {
        await db.transaction(async tx =>
            tx.run(
                'INSERT INTO accounts(id,user_code,login_id,name,role,created_at,updated_at,password_hash) VALUES (?,?,?,?,?,?,?,?)',
                id,
                await nextUserCode(tx),
                id + 'login',
                'Synthetic ' + id,
                role,
                '2026-01-01',
                '2026-01-01',
                'preserved-test-hash'
            )
        );
    }
    assert.deepEqual(
        (await db.all('SELECT user_code FROM accounts ORDER BY user_code')).map(r => r.user_code),
        ['U-0001', 'U-0002', 'U-0003']
    );
    await assert.rejects(
        db.run(
            "INSERT INTO accounts(id,user_code,login_id,created_at,updated_at) VALUES ('X','U-0009','ALOGIN','n','n')"
        ),
        /duplicate key|unique/i
    );
    await assert.rejects(db.run("UPDATE accounts SET role='annotator' WHERE id='B'"), /check/i);
    assert.deepEqual(await cameraScope(db, 'D'), []);
    assert.equal(await cameraScope(db, 'B'), null);
    assert.equal(await mayReadCamera(db, 'D', '1'), false);
    await replaceCameraGrants(db, 'A', 'D', ['1'], new Set(['1', '2']));
    assert.equal(await mayReadCamera(db, 'D', '1'), true);
    assert.equal(await mayReadCamera(db, 'D', '2'), false);
    await assert.rejects(replaceCameraGrants(db, 'B', 'D', ['2'], new Set(['1', '2'])));
    await assert.rejects(replaceCameraGrants(db, 'A', 'D', ['missing'], new Set(['1', '2'])));
    assert.equal((await db.get("SELECT COUNT(*) n FROM account_audit WHERE action='camera_grants_changed'")).n, 1);
    await db.run("UPDATE accounts SET status='deactivated' WHERE id='D'");
    assert.equal(await mayReadCamera(db, 'D', '1'), false);
    await db.run("INSERT INTO sessions VALUES ('hash','B',1,9999999999,'test','test')");
    await db.run("DELETE FROM accounts WHERE id='B'");
    assert.equal((await db.get('SELECT COUNT(*) n FROM sessions')).n, 0, 'sessions follow the account');
    await db.close();
    db = await openDb(url, schema); // reopening an initialised schema changes nothing
    assert.equal((await db.get('SELECT COUNT(*) n FROM accounts')).n, 2);
    await db.run("UPDATE app_schema_migrations SET checksum='changed'");
    await db.close();
    await assert.rejects(openDb(url, schema), /checksum/);
    console.log(
        'PASS account schema initialisation and ledger, user codes, case-insensitive ids, role constraint, responder default-deny, per-camera grants, immediate deactivation, cascade and checksum refusal'
    );
} finally {
    await admin.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
    await admin.end();
}
