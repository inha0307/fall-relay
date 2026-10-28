'use client';
import { useState, useEffect, useRef, useCallback } from 'react';
import { X, AlertCircle, CheckCircle, ChevronLeft, ChevronRight } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';

import { PIPELINE_BASE_URL } from '@/utils/pipeline';
import { readGridStream } from '@/utils/gridStream';
import { useMediaToken, withMediaToken } from '@/utils/media';
import SkeletonView from '@/components/SkeletonView';
import { quantity } from '@/utils/format';

/* Camera list and state come from the bridge registry: one GET /api/live returns everything. */
const LIVE_POLL_MS = 500; // state refresh while the tab is visible
const SKELETON_POLL_MS = 200; // faster refresh while a skeleton-only camera is on screen (no video carries motion)
const LIVE_POLL_HIDDEN_MS = 5000; // while hidden (minimal refresh for the fall banner)
const GRID_FPS = 15; // grid tile fps (the enlarged view uses the source fps; bridge cap 15)
const MAX_TILES = 16; // max cameras per /video_grid connection
const LAYOUTS = ['auto', 1, 4, 9, 16];
const LAYOUT_KEY = 'fall-relay.skeleton.layout';

/* tile count → columns/rows (close to square) */
function gridShape(count) {
    const cols = Math.max(1, Math.ceil(Math.sqrt(count)));
    return { cols, rows: Math.max(1, Math.ceil(count / cols)) };
}

function readLayout() {
    try {
        const saved = window.localStorage.getItem(LAYOUT_KEY);
        if (saved === 'auto') return 'auto';
        return LAYOUTS.includes(Number(saved)) ? Number(saved) : 'auto';
    } catch {
        return 'auto';
    }
}

const hasFall = cam => (cam?.tracks || []).some(t => t.fall_status === 'FALL');
const skeletonOnly = cam => !!cam && !cam.video_connected && !!cam.connection?.edge_inference_connected;

/* One camera's edge decision as text (confirm slots come from the edge device). */
function edgeStatusText(edge) {
    if (!edge?.edge_inference_connected) return 'Waiting for decisions from the device';
    const r = edge.last_edge_result || {};
    const confirm = r.confirm_slots ?? '?';
    const state =
        r.status === 'FALL'
            ? 'Fall decision'
            : r.consecutive_high > 0
              ? `Confirming ${r.consecutive_high}/${confirm}`
              : r.input_valid
                ? 'Analysing'
                : 'No person detected';
    return `${state} · ${quantity(edge.edge_inference_hz, 1, 'Hz')}`;
}

/* Rendered area of an object-fit:contain image. */
function calcRegion(img) {
    if (!img || !img.naturalWidth || !img.naturalHeight) return null;
    const cW = img.clientWidth,
        cH = img.clientHeight;
    const nW = img.naturalWidth,
        nH = img.naturalHeight;
    if (!cW || !cH) return null;
    const cAR = cW / cH,
        nAR = nW / nH;
    let rW, rH, oX, oY;
    if (cAR > nAR) {
        rH = cH;
        rW = rH * nAR;
        oX = (cW - rW) / 2;
        oY = 0;
    } else {
        rW = cW;
        rH = rW / nAR;
        oX = 0;
        oY = (cH - rH) / 2;
    }
    return { nW, nH, rW, rH, oX, oY };
}

function scaleBbox(bbox, reg) {
    if (!reg || !bbox) return null;
    const { nW, nH, rW, rH, oX, oY } = reg;
    const [x1, y1, x2, y2] = bbox;
    return {
        left: oX + (x1 / nW) * rW,
        top: oY + (y1 / nH) * rH,
        width: ((x2 - x1) / nW) * rW,
        height: ((y2 - y1) / nH) * rH,
    };
}

/* Clickable bbox overlay — shown on hover, always shown for a fall. */
function PersonOverlay({ tracks, imgRef, onSelect }) {
    const [region, setRegion] = useState(null);
    const [hoveredId, setHoveredId] = useState(null);

    useEffect(() => {
        const update = () => setRegion(calcRegion(imgRef.current));
        let lastNW = 0,
            lastNH = 0;
        const onLoad = () => {
            const img = imgRef.current;
            if (!img) return;
            const { naturalWidth: nW, naturalHeight: nH } = img;
            if (nW !== lastNW || nH !== lastNH) {
                lastNW = nW;
                lastNH = nH;
                setRegion(calcRegion(img));
            }
        };
        update();
        const ro = new ResizeObserver(update);
        const img = imgRef.current;
        if (img) {
            ro.observe(img);
            img.addEventListener('load', onLoad);
        }
        window.addEventListener('resize', update);
        return () => {
            ro.disconnect();
            window.removeEventListener('resize', update);
            if (img) img.removeEventListener('load', onLoad);
        };
    }, [imgRef]);

    return (
        <>
            {tracks.map(track => {
                const pos = scaleBbox(track.bbox, region);
                if (!pos || pos.width < 8) return null;
                const isFall = track.fall_status === 'FALL';
                const hasId = !!track.person_id;
                const isHovered = hoveredId === track.track_id;

                // red for a fall, green with an identity, white otherwise
                const borderColor = isFall ? '#ef4444' : hasId ? 'rgba(34,197,94,0.8)' : 'rgba(255,255,255,0.5)';

                // always visible for a fall, otherwise only on hover
                const isVisible = isFall || isHovered;

                return (
                    <div
                        key={track.track_id}
                        onClick={e => {
                            e.stopPropagation();
                            onSelect(track);
                        }}
                        onMouseEnter={() => setHoveredId(track.track_id)}
                        onMouseLeave={() => setHoveredId(null)}
                        style={{
                            position: 'absolute',
                            left: pos.left,
                            top: pos.top,
                            width: pos.width,
                            height: pos.height,
                            border: isVisible ? `3px solid ${borderColor}` : '1px solid transparent',
                            borderRadius: 4,
                            background: isFall
                                ? 'rgba(239,68,68,0.15)'
                                : isHovered
                                  ? 'rgba(255,255,255,0.06)'
                                  : 'transparent',
                            boxShadow: isFall ? '0 0 15px rgba(239,68,68,0.6)' : 'none',
                            cursor: 'pointer',
                            zIndex: isFall ? 20 : 10,
                            pointerEvents: 'all',
                            transition: 'all 0.15s',
                            boxSizing: 'border-box',
                            animation: isFall ? 'pulse-border 1s infinite' : 'none',
                        }}
                        title={track.person_id || `Track #${track.track_id}`}
                    >
                        {isFall && (
                            <div
                                style={{
                                    position: 'absolute',
                                    top: -25,
                                    left: -3,
                                    background: '#ef4444',
                                    color: '#fff',
                                    padding: '2px 6px',
                                    fontSize: '10px',
                                    fontWeight: 800,
                                    borderRadius: '4px 4px 4px 0',
                                    whiteSpace: 'nowrap',
                                }}
                            >
                                ⚠️ FALL DETECTED
                            </div>
                        )}
                    </div>
                );
            })}
            <style jsx global>{`
                @keyframes pulse-border {
                    0% {
                        border-color: #ef4444;
                        box-shadow: 0 0 0px rgba(239, 68, 68, 0.4);
                    }
                    50% {
                        border-color: #fca5a5;
                        box-shadow: 0 0 20px rgba(239, 68, 68, 0.8);
                    }
                    100% {
                        border-color: #ef4444;
                        box-shadow: 0 0 0px rgba(239, 68, 68, 0.4);
                    }
                }
            `}</style>
        </>
    );
}

/* Diagnostics for the selected track: the edge decision and the inputs it was based on (live). */
function TrackPanel({ track, camera, onClose }) {
    const r = camera?.connection?.last_edge_result || {};
    const isFall = track.fall_status === 'FALL';
    const rows = [
        ['Camera', camera ? (camera.location ? `${camera.label} · ${camera.location}` : camera.label) : '-'],
        ['Decision', track.fall_status || '-'],
        ['Score', track.fall_score != null ? Number(track.fall_score).toFixed(3) : '-'],
        ['Threshold', r.threshold != null ? Number(r.threshold).toFixed(3) : '-'],
        ['High-score streak', `${track.consecutive_high ?? 0} / ${track.confirm_slots ?? '?'}`],
        [
            'Valid slots (last 20)',
            r.window_valid_slots != null
                ? `${r.window_valid_slots} / 20 (min ${r.window_min_valid_slots ?? '?'})`
                : '-',
        ],
        ['Visible joints', r.visible_joints ?? '-'],
        [
            'Mean keypoint confidence',
            r.mean_keypoint_confidence != null ? Number(r.mean_keypoint_confidence).toFixed(2) : '-',
        ],
        ['Detector score', r.detector_score != null ? Number(r.detector_score).toFixed(2) : '-'],
        ['People in frame', r.people ?? '-'],
        ['Processing time', quantity(r.latency_ms, 0, 'ms')],
        ['Bounding box (px)', track.bbox ? track.bbox.map(v => Math.round(v)).join(', ') : '-'],
    ];
    return (
        <div
            className="track-panel"
            style={{
                position: 'fixed',
                right: 24,
                top: 90,
                width: 300,
                zIndex: 1200,
                background: '#fff',
                border: '1px solid #e2e8f0',
                borderRadius: 12,
                boxShadow: '0 10px 25px rgba(15,23,42,0.12)',
                padding: '14px 16px',
            }}
        >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                <span style={{ fontSize: '0.8rem', fontWeight: 800, color: '#1e40af', letterSpacing: '0.5px' }}>
                    Track #{track.track_id}
                </span>
                <button
                    onClick={onClose}
                    aria-label="Close"
                    style={{
                        background: 'none',
                        border: 'none',
                        cursor: 'pointer',
                        color: '#94a3b8',
                        padding: 2,
                        display: 'flex',
                    }}
                >
                    <X size={15} />
                </button>
            </div>
            <div
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '6px 10px',
                    borderRadius: 6,
                    marginBottom: 10,
                    background: isFall ? '#fee2e2' : '#dcfce7',
                    color: isFall ? '#b91c1c' : '#047857',
                    fontSize: '0.8rem',
                    fontWeight: 700,
                }}
            >
                {isFall ? <AlertCircle size={13} /> : <CheckCircle size={13} />}
                {isFall
                    ? 'Fall decision'
                    : track.fall_status === 'NORMAL'
                      ? 'Normal activity'
                      : 'Waiting for a full window'}
            </div>
            <table style={{ width: '100%', fontSize: '0.78rem', color: '#475569', borderCollapse: 'collapse' }}>
                <tbody>
                    {rows.map(([k, v]) => (
                        <tr key={k}>
                            <td style={{ padding: '3px 0', color: '#94a3b8' }}>{k}</td>
                            <td style={{ padding: '3px 0', textAlign: 'right', fontWeight: 600, color: '#1e293b' }}>
                                {v}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

/* ── Skeleton view: the 13-joint skeletons the edge devices report; a JPEG stream appears only for a device run with --video ── */
export default function MonitorPage() {
    const { user, loading: authLoading } = useAuth();

    const [live, setLive] = useState(null); // GET /api/live response
    const [liveError, setLiveError] = useState(false);
    const [layout, setLayout] = useState('auto');
    const [page, setPage] = useState(0);
    const [narrow, setNarrow] = useState(false);
    const [selectedCam, setSelectedCam] = useState(null); // camera id of the enlarged view
    const [camLive, setCamLive] = useState({}); // whether a grid tile has received a frame
    const [streamMode, setStreamMode] = useState('grid'); // 'grid' = /video_grid, 'img' = one /video_feed per tile
    const [selectedTrack, setSelectedTrack] = useState(null);
    const [isVisible, setIsVisible] = useState(true);
    // grid img refs per camera: { "1": { current: <img> }, ... }
    const gridImgRefs = useRef({});
    const gridRef = id => (gridImgRefs.current[id] ??= { current: null });
    const singleImgRef = useRef(null);
    const fastPoll = useRef(false); // a skeleton-only camera is visible
    const mediaToken = useMediaToken(); // video URLs need a signed-in token

    useEffect(() => {
        setLayout(readLayout());
        const query = window.matchMedia('(max-width: 768px)');
        const update = () => setNarrow(query.matches);
        update();
        query.addEventListener('change', update);
        return () => query.removeEventListener('change', update);
    }, []);

    /* Page Visibility API — stop streams while hidden, poll state slowly */
    useEffect(() => {
        const handleVisibility = () => setIsVisible(!document.hidden);
        document.addEventListener('visibilitychange', handleVisibility);
        return () => document.removeEventListener('visibilitychange', handleVisibility);
    }, []);

    /* Poll tracks and connection state of all cameras (one request regardless of camera count). */
    useEffect(() => {
        let stopped = false;
        let timer;
        const poll = async () => {
            try {
                const res = await fetch(`/api/bridge/live`, { cache: 'no-store', signal: AbortSignal.timeout(4000) });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const json = await res.json();
                if (!stopped) {
                    setLive(json);
                    setLiveError(false);
                }
            } catch {
                if (!stopped) setLiveError(true); // keep the last state
            }
            if (!stopped)
                timer = setTimeout(
                    poll,
                    document.hidden ? LIVE_POLL_HIDDEN_MS : fastPoll.current ? SKELETON_POLL_MS : LIVE_POLL_MS
                );
        };
        poll();
        const wake = () => {
            if (!document.hidden) {
                clearTimeout(timer);
                poll();
            }
        };
        document.addEventListener('visibilitychange', wake);
        return () => {
            stopped = true;
            clearTimeout(timer);
            document.removeEventListener('visibilitychange', wake);
        };
    }, []);

    const cameras = live?.cameras || [];
    const pageSize = layout === 'auto' ? Math.min(Math.max(cameras.length, 1), narrow ? 4 : MAX_TILES) : layout;
    const pageCount = Math.max(1, Math.ceil(cameras.length / pageSize));
    const curPage = Math.min(page, pageCount - 1);
    const pageCams = cameras.slice(curPage * pageSize, (curPage + 1) * pageSize);
    const { cols, rows } = gridShape(pageSize);
    // stream only cameras on the current page that have video
    const streamIds = pageCams.filter(c => c.video_connected).map(c => c.id);
    const streamKey = streamIds.join(',');
    fastPoll.current = selectedCam
        ? skeletonOnly(cameras.find(c => c.id === selectedCam))
        : pageCams.some(skeletonOnly);

    /* Receive the current page over one /video_grid connection and paint each tile's img. */
    useEffect(() => {
        if (streamMode !== 'grid' || selectedCam || !isVisible || !streamKey || !mediaToken) return;
        const ctrl = new AbortController();
        const urls = {};
        let failures = 0;
        let retryTimer;

        const showFrame = (camId, bytes) => {
            const img = gridImgRefs.current[camId]?.current;
            if (!img) return;
            const url = URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' }));
            const previous = urls[camId];
            urls[camId] = url;
            img.src = url;
            if (previous) setTimeout(() => URL.revokeObjectURL(previous), 1000);
            failures = 0;
            setCamLive(prev => (prev[camId] ? prev : { ...prev, [camId]: true }));
        };

        const run = async () => {
            try {
                await readGridStream(
                    withMediaToken(
                        `${PIPELINE_BASE_URL}/video_grid?cams=${encodeURIComponent(streamKey)}&fps=${GRID_FPS}`,
                        mediaToken
                    ),
                    showFrame,
                    ctrl.signal
                );
            } catch {
                if (ctrl.signal.aborted) return;
                failures += 1;
                // after repeated failures (e.g. CORS not allowed) fall back to one <img> stream per tile
                if (failures >= 3) {
                    setStreamMode('img');
                    return;
                }
                retryTimer = setTimeout(run, 1000 * 2 ** (failures - 1));
            }
        };
        run();
        return () => {
            ctrl.abort();
            clearTimeout(retryTimer);
            Object.values(urls).forEach(u => setTimeout(() => URL.revokeObjectURL(u), 1000));
            setCamLive({});
        };
    }, [streamKey, selectedCam, isVisible, streamMode, mediaToken]);

    const handleSelect = useCallback((track, camId) => {
        setSelectedTrack({ track, camId });
    }, []);

    const chooseLayout = value => {
        setLayout(value);
        setPage(0);
        try {
            window.localStorage.setItem(LAYOUT_KEY, String(value));
        } catch {}
    };

    if (!authLoading && !user) {
        return null;
    }

    const isAnyFall = cameras.some(hasFall);
    // a fall on another page: offer a jump button
    const fallElsewhere = cameras
        .map((c, i) => ({ cam: c, page: Math.floor(i / pageSize) }))
        .find(({ cam, page: p }) => p !== curPage && hasFall(cam));
    const selCam = cameras.find(c => c.id === selectedCam);
    const selFall = hasFall(selCam);
    const empties = Math.max(0, pageSize - pageCams.length);

    return (
        <div className={`dashboard-app ${isAnyFall ? 'fall-detected-mode' : ''}`}>
            {/* fall alerts themselves are announced by the global alert banner until someone acknowledges them */}

            {selectedTrack &&
                (() => {
                    const camera = cameras.find(c => c.id === selectedTrack.camId);
                    const current =
                        camera?.tracks?.find(t => t.track_id === selectedTrack.track.track_id) || selectedTrack.track;
                    return <TrackPanel track={current} camera={camera} onClose={() => setSelectedTrack(null)} />;
                })()}

            <section className="map-container">
                <div className="action-bar">
                    <div className="action-bar-left">
                        <h2>Skeleton view</h2>
                        <span className={`status-badge ${isAnyFall ? 'status-badge-fall' : ''}`}>
                            <span className={`status-dot ${isAnyFall ? 'status-dot-fall' : ''}`} />
                            {isAnyFall ? 'Fall decision' : 'Live'}
                        </span>
                        <span
                            style={{
                                marginLeft: 10,
                                fontSize: '0.75rem',
                                fontWeight: 700,
                                color: live?.edge_connected_count ? '#2563eb' : '#94a3b8',
                            }}
                        >
                            {liveError && !live
                                ? 'Waiting for the server'
                                : `Decisions from ${live?.edge_connected_count ?? 0}/${cameras.length} cameras · skeletons only, no video stream${live?.video_connected_count ? ` · debug video from ${live.video_connected_count}` : ''}`}
                        </span>
                    </div>
                    <div className="monitor-controls">
                        {fallElsewhere && !selectedCam && (
                            <button className="monitor-fall-jump" onClick={() => setPage(fallElsewhere.page)}>
                                <AlertCircle size={14} /> Fall on{' '}
                                {fallElsewhere.cam.location || fallElsewhere.cam.label} · go to page{' '}
                                {fallElsewhere.page + 1}
                            </button>
                        )}
                        <div className="monitor-layouts" role="group" aria-label="Grid layout">
                            {LAYOUTS.filter(v => !narrow || v === 'auto' || v <= 4).map(v => (
                                <button
                                    key={v}
                                    onClick={() => chooseLayout(v)}
                                    className={layout === v ? 'active' : ''}
                                    title={v === 'auto' ? 'Fit to camera count' : `${v} tiles`}
                                >
                                    {v === 'auto' ? 'Auto' : v}
                                </button>
                            ))}
                        </div>
                        {pageCount > 1 && (
                            <div className="monitor-pager">
                                <button
                                    disabled={curPage === 0}
                                    onClick={() => setPage(curPage - 1)}
                                    aria-label="Previous page"
                                >
                                    <ChevronLeft size={16} />
                                </button>
                                <span>
                                    {curPage + 1} / {pageCount}
                                </span>
                                <button
                                    disabled={curPage + 1 >= pageCount}
                                    onClick={() => setPage(curPage + 1)}
                                    aria-label="Next page"
                                >
                                    <ChevronRight size={16} />
                                </button>
                            </div>
                        )}
                    </div>
                </div>

                <div className="floor-wrapper">
                    <div className="cctv-grid-container" style={{ display: selectedCam ? 'none' : undefined }}>
                        <div
                            className="cctv-grid"
                            style={{
                                gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
                                gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
                            }}
                        >
                            {pageCams.map((cam, idx) => {
                                const isFallOnThisCam = hasFall(cam);
                                const online = cam.video_connected;
                                const skeleton = skeletonOnly(cam);
                                return (
                                    <div
                                        key={cam.id}
                                        className={`cam-feed ${online || skeleton ? (idx % 2 === 0 ? 'cam-feed-dark' : 'cam-feed-light') : 'cam-feed-empty'} ${isFallOnThisCam ? 'cam-feed-fall' : ''}`}
                                        onClick={() => {
                                            if (online || skeleton) setSelectedCam(cam.id);
                                        }}
                                        style={{ cursor: online || skeleton ? 'pointer' : 'default' }}
                                    >
                                        <span className="cam-feed-ch-label">{cam.location || cam.label}</span>
                                        <span className="cam-feed-edge-status">{edgeStatusText(cam.connection)}</span>
                                        {online ? (
                                            <>
                                                {(streamMode === 'img' || camLive[cam.id]) && (
                                                    <div
                                                        className="cam-feed-rec"
                                                        title="This device runs with --video (debugging only)"
                                                    >
                                                        <span className="rec-dot" />
                                                        DEBUG VIDEO
                                                    </div>
                                                )}
                                                {!selectedCam && isVisible && (
                                                    <img
                                                        ref={gridRef(cam.id)}
                                                        src={
                                                            streamMode === 'img'
                                                                ? (withMediaToken(
                                                                      `${PIPELINE_BASE_URL}/video_feed/${cam.id}?fps=${GRID_FPS}`,
                                                                      mediaToken
                                                                  ) ?? undefined)
                                                                : undefined
                                                        }
                                                        alt=""
                                                        className="cam-feed-img"
                                                        style={{ pointerEvents: 'none' }}
                                                    />
                                                )}
                                                {!selectedCam && (
                                                    <PersonOverlay
                                                        tracks={cam.tracks || []}
                                                        imgRef={gridRef(cam.id)}
                                                        onSelect={t => handleSelect(t, cam.id)}
                                                    />
                                                )}
                                            </>
                                        ) : skeleton ? (
                                            !selectedCam && (
                                                <SkeletonView cam={cam} onSelect={t => handleSelect(t, cam.id)} />
                                            )
                                        ) : (
                                            <div className="cam-feed-offline">No decisions from this camera</div>
                                        )}
                                    </div>
                                );
                            })}
                            {Array.from({ length: empties }, (_, i) => (
                                <div key={`empty-${i}`} className="cam-feed cam-feed-empty" />
                            ))}
                        </div>
                        {cameras.length === 0 && (
                            <div className="cam-feed-offline" style={{ position: 'absolute', inset: 0 }}>
                                {liveError ? 'Cannot reach the server' : 'Loading cameras…'}
                            </div>
                        )}
                    </div>

                    <div
                        className={`single-video-container ${selFall ? 'cam-feed-fall' : ''}`}
                        style={{ display: selectedCam ? undefined : 'none' }}
                    >
                        {selectedCam && skeletonOnly(selCam) && (
                            <SkeletonView cam={selCam} onSelect={t => handleSelect(t, selectedCam)} />
                        )}
                        {selectedCam && isVisible && !skeletonOnly(selCam) && (
                            <img
                                ref={singleImgRef}
                                id="camera-stream"
                                src={
                                    withMediaToken(`${PIPELINE_BASE_URL}/video_feed/${selectedCam}`, mediaToken) ??
                                    undefined
                                }
                                alt={`Camera ${selectedCam} debug video`}
                                className="cam-feed-img"
                            />
                        )}
                        {selectedCam && !skeletonOnly(selCam) && (
                            <PersonOverlay
                                tracks={selCam?.tracks || []}
                                imgRef={singleImgRef}
                                onSelect={t => handleSelect(t, selectedCam)}
                            />
                        )}
                        <div className="video-caption">
                            {selCam?.location || selCam?.label || selectedCam} · {edgeStatusText(selCam?.connection)}
                        </div>
                        <button
                            onClick={() => setSelectedCam(null)}
                            className="btn-back-map"
                            style={{ left: 'auto', right: '16px', transform: 'none' }}
                        >
                            Back ✕
                        </button>
                    </div>
                </div>
            </section>

            <style jsx global>{`
                .monitor-controls {
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    flex-wrap: wrap;
                }
                .monitor-layouts,
                .monitor-pager {
                    display: inline-flex;
                    align-items: center;
                    border: 1px solid #e2e8f0;
                    border-radius: 10px;
                    overflow: hidden;
                    background: #fff;
                }
                .monitor-layouts button,
                .monitor-pager button {
                    border: none;
                    background: transparent;
                    padding: 6px 10px;
                    font-size: 0.78rem;
                    font-weight: 700;
                    color: #475569;
                    cursor: pointer;
                    display: inline-flex;
                    align-items: center;
                }
                .monitor-layouts button + button {
                    border-left: 1px solid #e2e8f0;
                }
                .monitor-layouts button.active {
                    background: #2563eb;
                    color: #fff;
                }
                .monitor-pager button:disabled {
                    color: #cbd5e1;
                    cursor: default;
                }
                .monitor-pager span {
                    font-size: 0.78rem;
                    font-weight: 700;
                    color: #334155;
                    padding: 0 4px;
                }
                .monitor-fall-jump {
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    border: none;
                    border-radius: 10px;
                    padding: 6px 12px;
                    background: #ef4444;
                    color: #fff;
                    font-size: 0.78rem;
                    font-weight: 800;
                    cursor: pointer;
                    animation: flash-overlay 1s infinite;
                }
                .cam-feed-offline {
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    width: 100%;
                    height: 100%;
                    color: #64748b;
                    font-size: 0.85rem;
                    font-weight: 600;
                }
                .cam-feed-edge-status {
                    position: absolute;
                    left: 12px;
                    bottom: 10px;
                    z-index: 3;
                    padding: 2px 8px;
                    border-radius: 6px;
                    background: rgba(15, 23, 42, 0.65);
                    color: #e2e8f0;
                    font-size: 0.7rem;
                    font-weight: 600;
                    pointer-events: none;
                }
                .cam-feed-fall {
                    border: 4px solid #ef4444 !important;
                    box-shadow: inset 0 0 30px rgba(239, 68, 68, 0.5);
                    animation: flash-border 1s infinite;
                }
                @keyframes flash-overlay {
                    0% {
                        opacity: 0.3;
                    }
                    50% {
                        opacity: 0.6;
                    }
                    100% {
                        opacity: 0.3;
                    }
                }
                @keyframes flash-border {
                    0% {
                        border-color: #ef4444;
                    }
                    50% {
                        border-color: #fca5a5;
                    }
                    100% {
                        border-color: #ef4444;
                    }
                }
                .status-badge-fall {
                    background: rgba(239, 68, 68, 0.1) !important;
                    color: #ef4444 !important;
                    border: 1px solid #ef4444;
                }
                .status-dot-fall {
                    background: #ef4444 !important;
                    animation: dot-pulse 0.5s ease-in-out infinite !important;
                }
            `}</style>
        </div>
    );
}
