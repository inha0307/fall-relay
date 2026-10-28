import { dateTime, quantity } from '@/utils/format';
import ui from '../ui.module.css';

const Row = ({ label, children }) => (
    <tr>
        <th scope="row" style={{ width: '32%' }}>
            {label}
        </th>
        <td>{children ?? '—'}</td>
    </tr>
);

/* What the device says about itself; fields taken from its configuration are marked as such. */
export default function Specification({ device }) {
    const s = device.spec || {};
    const flags = device.profile?.limit_flags || {};
    const listFlags = state =>
        !state
            ? 'Unavailable'
            : Object.entries(flags)
                  .filter(([k]) => state[k])
                  .map(([, l]) => l)
                  .join(', ') || 'None';
    return (
        <div className={ui.stack}>
            <div className={ui.tableWrap}>
                <table className={ui.table}>
                    <tbody>
                        <Row label="Device ID">
                            <span className={ui.mono}>{device.device_id}</span>
                        </Row>
                        <Row label="Kind">
                            {device.profile?.label} (profile “{device.profile?.name}”, design rate{' '}
                            {quantity(device.profile?.design_rate_hz, 0, 'Hz')})
                        </Row>
                        <Row label="Model">{s.model}</Row>
                        <Row label="CPU cores">{s.cpu_cores}</Row>
                        <Row label="Operating system">
                            {s.os}
                            {s.kernel ? ` · kernel ${s.kernel}` : ''}
                        </Row>
                        <Row label="Camera">{s.camera}</Row>
                        <Row label="Cooling">{s.cooling}</Row>
                        <Row label="Power supply">
                            {s.power_supply_rating
                                ? `${s.power_supply_rating} (from the device configuration, not measured)`
                                : null}
                        </Row>
                        {Object.keys(flags).length > 0 && (
                            <Row label="Hardware limits">
                                Now: {listFlags(device.throttle?.current)} · since boot:{' '}
                                {listFlags(device.throttle?.since_boot)}
                            </Row>
                        )}
                        <Row label="Runtime packages">
                            {Object.entries(s.runtime_versions || {})
                                .map(([n, v]) => `${n} ${v || '(version unavailable)'}`)
                                .join(' · ') || null}
                        </Row>
                        <Row label="Reporting">
                            Every {quantity(device.interval_s, 0, 's')}; collection takes{' '}
                            {quantity(device.collection_ms, 0, 'ms')}. Last device time {dateTime(device.measured_at)};
                            freshness uses the server&apos;s receipt time.
                        </Row>
                    </tbody>
                </table>
            </div>
            {Object.keys(s.model_sha256 || {}).length > 0 && (
                <section>
                    <h2 className={ui.sectionTitle}>Model files (SHA-256)</h2>
                    <div className={ui.tableWrap}>
                        <table className={ui.table}>
                            <tbody>
                                {Object.entries(s.model_sha256).map(([name, hash]) => (
                                    <Row key={name} label={name}>
                                        <span className={ui.mono}>{hash || 'unavailable'}</span>
                                    </Row>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </section>
            )}
            {device.errors?.length > 0 && <p className={ui.note}>Unavailable readings: {device.errors.join('; ')}</p>}
        </div>
    );
}
