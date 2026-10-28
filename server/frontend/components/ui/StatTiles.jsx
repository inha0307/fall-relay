import ui from '../ui.module.css';

/* items: [{ label, value, note }] */
export default function StatTiles({ items, dim = false }) {
    return (
        <div className={`${ui.tiles} ${dim ? ui.dim : ''}`}>
            {items.map(({ label, value, note }) => (
                <div key={label} className={ui.tile}>
                    <span className={ui.tileLabel}>{label}</span>
                    <strong className={ui.tileValue}>{value}</strong>
                    {note && <small className={ui.tileNote}>{note}</small>}
                </div>
            ))}
        </div>
    );
}
