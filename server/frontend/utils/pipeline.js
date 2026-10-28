// NEXT_PUBLIC values are embedded at build time; restart/rebuild after changing.
export const PIPELINE_BASE_URL = (process.env.NEXT_PUBLIC_PIPELINE_URL || 'http://localhost:5001').replace(/\/+$/, '');
