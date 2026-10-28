'use client';
import { useEffect, useState } from 'react';
import ui from '../ui.module.css';

/* Tabs remembered in the URL hash (#tab-id), so a tab can be linked and survives reloads. */
export default function Tabs({ tabs, children }) {
    const ids = tabs.map(t => t.id);
    const [active, setActive] = useState(ids[0]);
    useEffect(() => {
        const read = () => {
            const id = window.location.hash.slice(1);
            if (ids.includes(id)) setActive(id);
        };
        read();
        window.addEventListener('hashchange', read);
        return () => window.removeEventListener('hashchange', read);
    }, [ids.join(',')]); // eslint-disable-line react-hooks/exhaustive-deps
    const choose = id => {
        setActive(id);
        window.history.replaceState(null, '', '#' + id);
    };
    return (
        <div>
            <div role="tablist" className={ui.tabs}>
                {tabs.map(t => (
                    <button
                        key={t.id}
                        role="tab"
                        id={`tab-${t.id}`}
                        type="button"
                        aria-selected={active === t.id}
                        aria-controls={`panel-${t.id}`}
                        className={`${ui.tab} ${active === t.id ? ui.tabActive : ''}`}
                        onClick={() => choose(t.id)}
                    >
                        {t.label}
                    </button>
                ))}
            </div>
            <div role="tabpanel" id={`panel-${active}`} aria-labelledby={`tab-${active}`} className={ui.tabPanel}>
                {children(active)}
            </div>
        </div>
    );
}
