#!/usr/bin/env python3
"""Freeze an outcome-independent, capture-group-disjoint NTU test replay list."""

import argparse, csv, hashlib, json, os, time, zipfile, collections, subprocess
from pathlib import Path
import cv2


def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def rank(seed, value):
    return hashlib.sha256(f'{seed}:{value}'.encode()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    p.add_argument(
        '--training',
        type=Path,
        required=True,
        help='training workspace with the NTU-derived files (see training/README.md)',
    )
    p.add_argument('--seed', type=int, default=20261021)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    (a.out / 'videos').mkdir()
    root = Path(__file__).resolve().parents[2]
    inv = a.training / 'source_inventory_v2/paired_manifest_v2.csv'
    rows = list(csv.DictReader(inv.open()))
    test = [r for r in rows if r['split'] == 'test' and r['eligible'] == '1']
    archives = {}
    unavailable = []
    for name in sorted({r['rgb_archive'] for r in test}):
        if not Path(name).is_file():
            unavailable.append(name)
            continue
        with zipfile.ZipFile(name) as z:
            archives[name] = set(z.namelist())
    available = [r for r in test if r['rgb_member'] in archives.get(r['rgb_archive'], set())]
    groups = collections.defaultdict(list)
    for r in available:
        groups[r['capture_group']].append(r)
    representatives = [min(rs, key=lambda r: rank(a.seed, r['sample_id'])) for rs in groups.values()]
    actions = sorted(
        {int(r['action']) for r in representatives} - {6, 42, 43}, key=lambda v: rank(a.seed, 'action:' + str(v))
    )[:20]
    quota = {43: 12, 6: 8, 42: 8, **{x: 1 for x in actions}}
    chosen = []
    for action, n in quota.items():
        candidates = sorted(
            [r for r in representatives if int(r['action']) == action], key=lambda r: rank(a.seed, r['capture_group'])
        )
        assert len(candidates) >= n
        chosen += candidates[:n]
    chosen.sort(key=lambda r: rank(a.seed, 'order:' + r['sample_id']))
    frozen = {
        'seed': a.seed,
        'selected_before_inference': True,
        'rule': 'one hash-ranked camera per capture group, stratified quotas, SHA256 rank order',
        'inventory_sha256': sha(inv),
        'frozen_operating_point_sha256': sha(a.training / 'tcn_v2_meta/frozen_operating_point_v2.json'),
        'eligible_test_videos': len(test),
        'eligible_test_groups': len({r['capture_group'] for r in test}),
        'available_test_videos': len(available),
        'available_test_groups': len(groups),
        'unavailable_archives': unavailable,
        'quota': quota,
        'selected': chosen,
        'frozen_at': time.time(),
    }
    (a.out / 'selection_frozen.json').write_text(json.dumps(frozen, indent=2) + '\n')
    print('FROZEN', len(chosen), 'videos/groups', sum(int(r['rgb_bytes']) for r in chosen), 'bytes', flush=True)
    result = []
    for r in chosen:
        dest = a.out / 'videos' / (r['sample_id'] + '.avi')
        with zipfile.ZipFile(r['rgb_archive']) as z:
            dest.write_bytes(z.read(r['rgb_member']))
        cap = cv2.VideoCapture(str(dest))
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        ok, _ = cap.read()
        cap.release()
        assert ok and fps > 0 and frames > 0, (dest, fps, frames)
        result.append(
            {
                **r,
                'file': dest.name,
                'fps': fps,
                'frames': frames,
                'duration_s': frames / fps,
                'width': w,
                'height': h,
                'sha256': sha(dest),
            }
        )
    (a.out / 'playlist.json').write_text(json.dumps(result, indent=2) + '\n')
    keys = list(result[0])
    with (a.out / 'playlist.csv').open('w', newline='') as f:
        wr = csv.DictWriter(f, fieldnames=keys)
        wr.writeheader()
        wr.writerows(result)
    import psycopg
    from psycopg.rows import dict_row

    # Read-only audit of the reference deployment's database (FALLRELAY_DATABASE_URL), recorded with the playlist.
    c = psycopg.connect(os.environ['FALLRELAY_DATABASE_URL'], row_factory=dict_row)
    c.read_only = True
    audit = {
        'at': time.time(),
        'tables': {},
        'event_version_groups': [],
        'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
    }
    for table, col in [
        ('events', 'received_at'),
        ('hardware_samples', 'received_at'),
        ('ingress_traffic', 'ts'),
        ('metrics', 'ts'),
    ]:
        audit['tables'][table] = dict(
            c.execute(f'SELECT COUNT(*) count,MIN({col}) first,MAX({col}) last FROM {table}').fetchone()
        )
    for r in c.execute(
        'SELECT e.camera_id,e.source,e.run_id,COUNT(*) count,MIN(e.received_at) first,MAX(e.received_at) last,m.identity_json FROM events e LEFT JOIN model_runs m ON COALESCE(e.run_id,e.model_run_id)=m.run_id GROUP BY 1,2,3'
    ):
        item = dict(r)
        ident = json.loads(item.pop('identity_json') or '{}')
        item['runtime_sha256'] = ident.get('runtime_sha256')
        item['settings'] = ident.get('settings')
        audit['event_version_groups'].append(item)
    audit['recent_runs'] = [
        dict(r) | {'identity_json': json.loads(r['identity_json'])}
        for r in c.execute('SELECT * FROM model_runs ORDER BY first_seen DESC LIMIT 6')
    ]
    c.close()
    (a.out / 'production_audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    (a.out / 'initial_git_status.txt').write_text(
        subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True)
    )
    (a.out / 'initial_changes.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=root))
    print(
        'PREPARED',
        len(result),
        'unique videos/groups;',
        round(sum(r['duration_s'] for r in result), 2),
        'source seconds',
        flush=True,
    )


if __name__ == '__main__':
    main()
