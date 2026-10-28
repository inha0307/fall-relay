/* Helpers shared by the account admin routes. */

const LIMITS = { name: 100, email: 200, affiliation: 100, reason: 300 };

/* Trimmed text within the field's limit; undefined when absent; { error } when invalid. */
export function cleanText(value, key) {
    if (value === undefined) return undefined;
    if (value === null) return '';
    if (typeof value !== 'string' || value.length > LIMITS[key])
        return { error: `${key} must be text of at most ${LIMITS[key]} characters.` };
    const text = value.trim();
    if (key === 'email' && text && !/^[^\s@]+@[^\s@]+$/.test(text)) return { error: 'email is not a valid address.' };
    return text;
}

export async function activeAdminCount(db) {
    return (await db.get("SELECT COUNT(*) AS n FROM accounts WHERE role = 'admin' AND status = 'active'")).n;
}

/* True when the account has ever signed in (then it can only be deactivated, not deleted). */
export async function hasSignedIn(db, account) {
    return (
        account.last_login_at != null ||
        !!(await db.get("SELECT 1 FROM login_logs WHERE account_id = ? AND status = 'success' LIMIT 1", account.id))
    );
}
