"""Lightweight causal geometric tracks. No RGB identity, class label or NTU GT."""

import numpy as np

RULE = {
    'version': 'causal_geometry_v1',
    'max_missing_slots': 5,
    'min_iou': 0.05,
    'max_normalized_joint_distance': 0.45,
    'ambiguity_margin': 0.08,
    'primary_initialization': 'largest detector box area * confidence',
    'primary_persistence': 'keep primary through missing5slots; no bystander fallback; reacquire only a track with >=2hits',
    'identity_limit': 'geometric association can still swap identity; no person identification',
}


def iou(a, b):
    size = np.maximum(0, np.minimum(a[2:], b[2:]) - np.maximum(a[:2], b[:2]))
    intersection = float(np.prod(size))
    return intersection / max(
        float(np.prod(np.maximum(0, a[2:] - a[:2])) + np.prod(np.maximum(0, b[2:] - b[:2]))) - intersection, 1e-6
    )


def similarity(track, d):
    old = track['det']
    overlap = iou(old['box'], d['box'])
    a, b = old['keypoints'], d['keypoints']
    good = np.isfinite(a).all(axis=1) & np.isfinite(b).all(axis=1) & (a[:, 2] >= 0.2) & (b[:, 2] >= 0.2)
    diagonal = max(float(np.linalg.norm(old['box'][2:] - old['box'][:2])), 20.0)
    distance = (
        float(np.median(np.linalg.norm(a[good, :2] - b[good, :2], axis=1))) / diagonal if good.sum() >= 4 else np.inf
    )
    if overlap < RULE['min_iou'] and distance > RULE['max_normalized_joint_distance']:
        return -1.0
    return 0.6 * overlap + 0.4 * max(0, 1 - distance) if np.isfinite(distance) else 0.6 * overlap


class CausalTracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 0
        self.primary = None
        self.previous_primary = None
        self.ever_selected = False

    def update(self, detections):
        # Ambiguous associations are rejected rather than asserted as same identity.
        pairs = []
        scores = {}
        for identity, tr in self.tracks.items():
            for j, d in enumerate(detections):
                s = similarity(tr, d)
                if s >= 0:
                    scores[identity, j] = s
        for (identity, j), s in scores.items():
            rivals = [
                v for (ti, dj), v in scores.items() if (ti == identity and dj != j) or (dj == j and ti != identity)
            ]
            if rivals and s - max(rivals) < RULE['ambiguity_margin']:
                continue
            pairs.append((s, identity, j))
        assigned_tracks = set()
        assigned_detections = set()
        observed = {}
        for _, identity, j in sorted(pairs, reverse=True):
            if identity in assigned_tracks or j in assigned_detections:
                continue
            assigned_tracks.add(identity)
            assigned_detections.add(j)
            observed[identity] = detections[j]
        for identity, tr in list(self.tracks.items()):
            if identity in observed:
                tr.update(det=observed[identity], missed=0, hits=tr['hits'] + 1)
            else:
                tr['missed'] += 1
                if tr['missed'] > RULE['max_missing_slots']:
                    del self.tracks[identity]
        for j, d in enumerate(detections):
            if j in assigned_detections:
                continue
            identity = self.next_id
            self.next_id += 1
            self.tracks[identity] = {'det': d, 'missed': 0, 'hits': 1}
            observed[identity] = d
        if self.primary not in self.tracks:
            self.primary = None
        if self.primary is None:
            candidates = [i for i in observed if not self.ever_selected or self.tracks[i]['hits'] >= 2]
            if candidates:
                self.primary = max(
                    candidates,
                    key=lambda i: (
                        float(np.prod(np.maximum(0, observed[i]['box'][2:] - observed[i]['box'][:2])))
                        * observed[i]['score']
                    ),
                )
                self.ever_selected = True
        reset = self.primary is not None and self.primary != self.previous_primary
        if self.primary is not None:
            self.previous_primary = self.primary
        return observed.get(self.primary), self.primary if self.primary is not None else -1, reset
