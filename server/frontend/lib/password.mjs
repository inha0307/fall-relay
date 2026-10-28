/* Password hashing (scrypt, built-in crypto). Format: scrypt$N$r$p$salt(base64)$hash(base64) */
import { randomBytes, randomInt, scryptSync, timingSafeEqual } from 'node:crypto';

const N = 16384,
    R = 8,
    P = 1,
    KEYLEN = 64;
export const MIN_PASSWORD_LENGTH = 8;

export function hashPassword(password) {
    const salt = randomBytes(16);
    const hash = scryptSync(String(password), salt, KEYLEN, { N, r: R, p: P });
    return `scrypt$${N}$${R}$${P}$${salt.toString('base64')}$${hash.toString('base64')}`;
}

export function verifyPassword(password, stored) {
    if (!stored || typeof stored !== 'string') return false;
    const [scheme, n, r, p, saltB64, hashB64] = stored.split('$');
    if (scheme !== 'scrypt' || !saltB64 || !hashB64) return false;
    const expected = Buffer.from(hashB64, 'base64');
    const actual = scryptSync(String(password), Buffer.from(saltB64, 'base64'), expected.length, {
        N: Number(n),
        r: Number(r),
        p: Number(p),
    });
    return actual.length === expected.length && timingSafeEqual(actual, expected);
}

/* Spend the same time for unknown accounts so responses do not reveal which ids exist. */
const DUMMY_HASH = hashPassword(randomBytes(16).toString('hex'));
export function verifyAgainstDummy(password) {
    verifyPassword(password, DUMMY_HASH);
    return false;
}

export function passwordProblem(password) {
    if (typeof password !== 'string' || password.length < MIN_PASSWORD_LENGTH) {
        return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
    }
    if (password.length > 128) return 'Password is too long.';
    return null;
}

/* Readable temporary password (no ambiguous 0/O/1/l). */
export function temporaryPassword(length = 12) {
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
    return Array.from({ length }, () => alphabet[randomInt(alphabet.length)]).join('');
}
