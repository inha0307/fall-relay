import ui from '../ui.module.css';

/* tone: ok | degraded | down | offline | fall | neutral */
export default function StatusChip({ tone = 'neutral', children, title }) {
    return (
        <span className={`${ui.chip} ${ui['chip_' + tone] || ''}`} title={title}>
            {children}
        </span>
    );
}
