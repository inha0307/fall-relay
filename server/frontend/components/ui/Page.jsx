import ui from '../ui.module.css';

/* Page frame: title with icon, one-sentence purpose, and actions on the right. */
export default function Page({ icon: Icon, title, subtitle, actions, back, children }) {
    return (
        <main className="home-page-root">
            <div className={`${ui.content} ${ui.page}`}>
                {back}
                <header className={ui.header}>
                    <div>
                        <h1 className={ui.title}>
                            {Icon && <Icon size={28} className={ui.titleIcon} aria-hidden="true" />}
                            {title}
                        </h1>
                        {subtitle && <p className={ui.subtitle}>{subtitle}</p>}
                    </div>
                    {actions && <div className={ui.toolbar}>{actions}</div>}
                </header>
                {children}
            </div>
        </main>
    );
}
