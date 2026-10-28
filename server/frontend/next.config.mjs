/** @type {import('next').NextConfig} */
const nextConfig = {
    allowedDevOrigins: (process.env.DEV_ALLOWED_ORIGINS || '')
        .split(',')
        .map(value => value.trim())
        .filter(Boolean),
    devIndicators: false,
    serverExternalPackages: ['pg'],
    // Keep old bookmarked paths working.
    async redirects() {
        return [
            { source: '/home', destination: '/overview', permanent: false },
            { source: '/logs', destination: '/alerts', permanent: false },
            { source: '/monitor', destination: '/skeleton', permanent: false },
            { source: '/dashboard', destination: '/reports', permanent: false },
        ];
    },
};

export default nextConfig;
