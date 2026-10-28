/*
 * PostgreSQL connection settings for the web application, from the same libpq connection string the bridge
 * uses (FALLRELAY_DATABASE_URL: "host=... port=... dbname=... user=... passfile=..." or a postgres:// URL).
 * node-postgres reads neither keyword strings nor password files, so both are resolved here. Server code only.
 */
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const KEYS = { host: 'host', hostaddr: 'host', port: 'port', dbname: 'database', user: 'user', password: 'password' };

export function parseConnectionString(text) {
    const value = String(text || '').trim();
    if (!value) throw new Error('FALLRELAY_DATABASE_URL is not set');
    if (/^postgres(ql)?:\/\//.test(value)) {
        const url = new URL(value);
        const settings = {
            host: url.hostname || 'localhost',
            port: Number(url.port || 5432),
            database: decodeURIComponent(url.pathname.replace(/^\//, '')),
            user: decodeURIComponent(url.username),
        };
        if (url.password) settings.password = decodeURIComponent(url.password);
        for (const [key, item] of url.searchParams) if (key === 'passfile') settings.passfile = item;
        return settings;
    }
    const settings = {};
    const pattern = /(\w+)\s*=\s*(?:'((?:\\.|[^'])*)'|(\S+))/g;
    let match;
    while ((match = pattern.exec(value))) {
        const key = match[1];
        const item = match[2] !== undefined ? match[2].replace(/\\(.)/g, '$1') : match[3];
        if (key in KEYS) settings[KEYS[key]] = key === 'port' ? Number(item) : item;
        else if (key === 'passfile') settings.passfile = item;
        else if (key === 'options') throw new Error('FALLRELAY_DATABASE_URL must not set options (search_path)');
    }
    settings.host ??= 'localhost';
    settings.port ??= 5432;
    if (!settings.database || !settings.user) throw new Error('connection string needs dbname and user');
    return settings;
}

/* The first matching line of a libpq password file (host:port:database:user:password, * wildcards). */
export function passwordFromFile(file, settings) {
    let text;
    try {
        text = readFileSync(file, 'utf8');
    } catch {
        return undefined;
    }
    const matches = (field, wanted) => field === '*' || field === String(wanted);
    for (const raw of text.split('\n')) {
        const line = raw.trim();
        if (!line || line.startsWith('#')) continue;
        const fields = [];
        let current = '';
        for (let i = 0; i < line.length; i++) {
            if (line[i] === '\\' && i + 1 < line.length) current += line[++i];
            else if (line[i] === ':') {
                fields.push(current);
                current = '';
            } else current += line[i];
        }
        fields.push(current);
        if (fields.length < 5) continue;
        const [host, port, database, user] = fields;
        if (
            matches(host, settings.host) &&
            matches(port, settings.port) &&
            matches(database, settings.database) &&
            matches(user, settings.user)
        )
            return fields.slice(4).join(':');
    }
    return undefined;
}

/* Pool settings for `new pg.Pool(...)`; `schema` becomes the search_path of every connection. */
export function poolConfig(text = process.env.FALLRELAY_DATABASE_URL, schema = appSchema()) {
    const settings = parseConnectionString(text);
    if (settings.password === undefined) {
        const file = settings.passfile || process.env.PGPASSFILE || path.join(homedir(), '.pgpass');
        const password = passwordFromFile(file, settings);
        if (password !== undefined) settings.password = password;
    }
    delete settings.passfile;
    if (!/^[a-z][a-z0-9_]{0,62}$/.test(schema)) throw new Error('invalid FALLRELAY_APP_SCHEMA');
    return {
        ...settings,
        max: 5,
        idleTimeoutMillis: 60_000,
        connectionTimeoutMillis: 10_000,
        application_name: 'fall-relay-web',
        options: '-c search_path=' + schema,
        schema,
    };
}

/* The schema that holds the account tables: FALLRELAY_APP_SCHEMA, default "app". */
export function appSchema() {
    return process.env.FALLRELAY_APP_SCHEMA || 'app';
}
