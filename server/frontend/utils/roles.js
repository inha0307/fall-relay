/* Public roles (docs/UI_GLOSSARY.md). */
export const ROLE_LABELS = { admin: 'Administrator', viewer: 'Viewer', responder: 'Responder' };
export const ROLE_HELP = {
    admin: 'Manages accounts, cameras and devices; can handle alerts.',
    viewer: 'Read-only access to status and records.',
    responder: 'Sees and handles alerts for assigned cameras.',
};
export const canAck = role => role === 'admin' || role === 'responder';
export const roleHome = role => (role === 'responder' ? '/alerts' : '/overview');
