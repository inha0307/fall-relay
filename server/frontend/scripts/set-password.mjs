#!/usr/bin/env node
/*
 * Set an account password (typed in the terminal, never echoed).
 *
 *   node scripts/set-password.mjs <user-id>                 set the password of an existing account
 *   node scripts/set-password.mjs <user-id> --create-admin  create it as an administrator if it does not exist
 *
 * Database: FALLRELAY_DATABASE_URL, schema FALLRELAY_APP_SCHEMA (default app). Existing sessions of the account end.
 */
import readline from 'node:readline';

const { audit, findAccountForLogin, LOGIN_ID_PATTERN, newId, nextUserCode, nowIso, openDb } =
    await import('../lib/appDb.mjs');
const { hashPassword, passwordProblem } = await import('../lib/password.mjs');

const [loginArg, ...flags] = process.argv.slice(2);
if (!loginArg || loginArg.startsWith('-')) {
    console.error('Usage: node scripts/set-password.mjs <user-id> [--create-admin]');
    process.exit(2);
}
if (!process.stdin.isTTY) {
    console.error('Run this in an interactive terminal (the password is read without echo).');
    process.exit(2);
}

function askHidden(question) {
    return new Promise(resolve => {
        const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
        rl._writeToOutput = text => {
            if (text.includes(question)) process.stdout.write(text);
        };
        rl.question(question, answer => {
            rl.close();
            process.stdout.write('\n');
            resolve(answer);
        });
    });
}

const db = await openDb();
const loginId = loginArg.trim();
let account = await findAccountForLogin(db, loginId);
if (!account && !flags.includes('--create-admin')) {
    console.error(`No account '${loginId}'. Add --create-admin to create it as an administrator.`);
    process.exit(1);
}
if (!account && !LOGIN_ID_PATTERN.test(loginId)) {
    console.error('User id: 2-100 characters of letters, digits and ._@+-');
    process.exit(1);
}

const password = await askHidden(`New password for '${account?.login_id ?? loginId}': `);
const problem = passwordProblem(password);
if (problem) {
    console.error(problem);
    process.exit(1);
}
if ((await askHidden('Repeat: ')) !== password) {
    console.error('The entries differ. Nothing changed.');
    process.exit(1);
}

const now = nowIso();
if (!account) {
    account = await db.transaction(async tx => {
        const id = newId();
        await tx.run(
            `INSERT INTO accounts(id, user_code, login_id, name, role, status, must_change_password, password_hash, created_at, updated_at)
                    VALUES (?, ?, ?, 'Administrator', 'admin', 'active', 0, ?, ?, ?)`,
            id,
            await nextUserCode(tx),
            loginId,
            hashPassword(password),
            now,
            now
        );
        const created = await tx.get('SELECT * FROM accounts WHERE id = ?', id);
        await audit(tx, null, created, 'created', { login_id: loginId, role: 'admin', via: 'set-password.mjs' });
        return created;
    });
    console.log(`Created administrator ${account.user_code} ('${loginId}').`);
} else {
    await db.run(
        'UPDATE accounts SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?',
        hashPassword(password),
        now,
        account.id
    );
    const ended = (await db.run('DELETE FROM sessions WHERE account_id = ?', account.id)).changes;
    await audit(db, null, account, 'password_set', { via: 'set-password.mjs' });
    console.log(
        `Password set for ${account.user_code} '${account.login_id}' (${account.name || 'no name'}, ${account.role}, ${account.status}).` +
            (ended ? ` Ended ${ended} existing session(s).` : '')
    );
}
await db.close();
