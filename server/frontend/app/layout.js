import './globals.css';
import Sidebar from '@/components/Sidebar';
import { AuthProvider } from '@/context/AuthContext';
import { AlertProvider } from '@/context/AlertContext';
import AlertStatus from '@/components/AlertStatus';
import AlertBanner from '@/components/AlertBanner';
import AuthGuard from '@/components/AuthGuard';

export const metadata = {
    title: 'Fall-Relay',
    description: 'Fall-event delivery and alert handling for edge fall detection',
    icons: { icon: '/fall-relay.svg' },
};

export default function RootLayout({ children }) {
    return (
        <html lang="en">
            <head>
                <link rel="manifest" href="/fall-relay.webmanifest" />
            </head>
            <body>
                <AuthProvider>
                    <AuthGuard>
                        <AlertProvider>
                            <Sidebar />
                            <AlertBanner />
                            <AlertStatus />
                            {children}
                        </AlertProvider>
                    </AuthGuard>
                </AuthProvider>
            </body>
        </html>
    );
}
