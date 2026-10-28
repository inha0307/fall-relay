/* Server-side environment variables (FALLRELAY_<NAME>), read at request time and never bundled. */
export function serverEnv(name) {
    return process.env[`FALLRELAY_${name}`] || '';
}
