'use client';
/* The 13-joint skeleton an edge device reports, drawn without any camera image. Used by the live view
   (full size, clickable person boxes) and the home status board (small, display only). */
/* The edge model's 13 joints: 0/1 R/L shoulder, 2/3 elbow, 4/5 wrist, 6/7 hip, 8/9 knee, 10/11 ankle, 12 head point.
   Joints the model masks out arrive as null. The neck runs from the head to the shoulder midpoint. */
const LIMBS = [
    [0, 1],
    [0, 2],
    [2, 4],
    [1, 3],
    [3, 5],
    [0, 6],
    [1, 7],
    [6, 7],
    [6, 8],
    [8, 10],
    [7, 9],
    [9, 11],
];
const RIGHT_SIDE = new Set([0, 2, 4, 6, 8, 10]);

/* Camera images stay on the edge device; without a video stream the tile draws the reported skeletons. */
export default function SkeletonView({ cam, onSelect, compact = false }) {
    const size = cam?.connection?.last_edge_result?.frame_size || [1280, 720];
    const [W, H] = size;
    const stroke = Math.max(2, W / 320);
    return (
        <svg className="cam-feed-skeleton" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
            <rect x="0" y="0" width={W} height={H} className="skeleton-frame" />
            {(cam?.tracks || []).map(track => {
                const k = track.skeleton;
                if (!k) return null;
                const isFall = track.fall_status === 'FALL';
                // right side and left side in two tints so the facing direction stays readable; red for a fall
                const tint = j => (isFall ? '#ef4444' : RIGHT_SIDE.has(j) ? '#38bdf8' : '#a78bfa');
                const ok = j => !!k[j];
                const [x1, y1, x2, y2] = track.bbox || [0, 0, 0, 0];
                const neck = ok(0) && ok(1) ? [(k[0][0] + k[1][0]) / 2, (k[0][1] + k[1][1]) / 2] : null;
                return (
                    <g key={track.track_id}>
                        {track.bbox && (
                            <rect
                                x={x1}
                                y={y1}
                                width={x2 - x1}
                                height={y2 - y1}
                                fill={isFall ? 'rgba(239,68,68,0.12)' : 'transparent'}
                                stroke={isFall ? '#ef4444' : 'rgba(148,163,184,0.5)'}
                                strokeWidth={stroke}
                                rx={stroke * 2}
                                style={onSelect ? { cursor: 'pointer', pointerEvents: 'all' } : undefined}
                                onClick={
                                    onSelect
                                        ? e => {
                                              e.stopPropagation();
                                              onSelect(track);
                                          }
                                        : undefined
                                }
                            />
                        )}
                        {neck && ok(12) && (
                            <line
                                x1={k[12][0]}
                                y1={k[12][1]}
                                x2={neck[0]}
                                y2={neck[1]}
                                stroke={isFall ? '#ef4444' : '#e2e8f0'}
                                strokeWidth={stroke * 1.6}
                                strokeLinecap="round"
                            />
                        )}
                        {LIMBS.filter(([a, b]) => ok(a) && ok(b)).map(([a, b]) => (
                            <line
                                key={`${a}-${b}`}
                                x1={k[a][0]}
                                y1={k[a][1]}
                                x2={k[b][0]}
                                y2={k[b][1]}
                                stroke={
                                    RIGHT_SIDE.has(a) && RIGHT_SIDE.has(b)
                                        ? tint(a)
                                        : !RIGHT_SIDE.has(a) && !RIGHT_SIDE.has(b)
                                          ? tint(a)
                                          : isFall
                                            ? '#ef4444'
                                            : '#e2e8f0'
                                }
                                strokeWidth={stroke * 1.6}
                                strokeLinecap="round"
                            />
                        ))}
                        {k
                            .slice(0, 12)
                            .map(
                                (p, j) =>
                                    ok(j) && <circle key={j} cx={p[0]} cy={p[1]} r={stroke * 1.8} fill={tint(j)} />
                            )}
                        {ok(12) && (
                            <circle
                                cx={k[12][0]}
                                cy={k[12][1]}
                                fill="none"
                                r={
                                    neck
                                        ? Math.max(
                                              stroke * 2,
                                              0.35 * Math.hypot(k[12][0] - neck[0], k[12][1] - neck[1])
                                          )
                                        : stroke * 4
                                }
                                stroke={isFall ? '#ef4444' : '#e2e8f0'}
                                strokeWidth={stroke * 1.4}
                            />
                        )}
                    </g>
                );
            })}
            {!(cam?.tracks || []).some(t => t.skeleton) && (
                <text
                    x={W / 2}
                    y={H / 2}
                    textAnchor="middle"
                    className="skeleton-empty"
                    fontSize={compact ? W / 18 : W / 40}
                >
                    No person detected
                </text>
            )}
        </svg>
    );
}
