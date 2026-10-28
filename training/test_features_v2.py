"""Runtime simulation parity: streaming SlotFeaturizer + per-slot TFLite LPN + assemble reproduces
build_features_v2 rows, and lpn_input reproduces the LPN training pairs (build_lpn_pairs_v2)."""

import numpy as np, tensorflow as tf
import _paths  # noqa: F401  (edge on sys.path)
from features_v2 import SlotFeaturizer, assemble
from head_point import load_table

F = _paths.FEATURES / 'val.npz'
TRACK = _paths.TRACKING / 'clips'
TFL = str(_paths.LPN_RUNS / 'final_v2/full/lpn13_fp32.tflite')
d = np.load(F)
ids = d['clip_sample_id']
table = load_table()
it = tf.lite.Interpreter(model_path=TFL)
it.allocate_tensors()
i, o = it.get_input_details()[0], it.get_output_details()[0]
checked = worst = 0
for sid in [s for s in ids if s.endswith('A043')][:3] + [s for s in ids if s.endswith('A008')][:2]:
    ci = int(np.flatnonzero(ids == sid)[0])
    tr = np.load(f'{TRACK}/{sid}.npz')
    for s in np.flatnonzero(d['seq_clip'] == ci):
        p, a, b = int(d['seq_phase'][s]), int(d['seq_start'][s]), int(d['seq_stop'][s])
        fz = SlotFeaturizer(table)
        rows = []
        for f, k17, tid, rs in zip(tr[f'p{p}_frame'], tr[f'p{p}_kpts17'], tr[f'p{p}_track'], tr[f'p{p}_reset']):
            x, v, dyn = fz.step(k17, f / 30.0, int(tid), bool(rs))
            it.set_tensor(i['index'], (x if v else np.zeros(39, np.float32))[None])
            it.invoke()
            rows.append(assemble(it.get_tensor(o['index']), x[None], [v], dyn[None])[0])
        diff = np.abs(np.asarray(rows) - d['feat'][a:b]).max()
        worst = max(worst, diff)
        checked += b - a
        assert diff < 1e-4, (sid, p, diff)
    pairs = np.load(_paths.LPN_DATA / 'parts' / f'{sid}.npz')
    pz = {(int(ph), int(fr)): x for ph, fr, x in zip(pairs['phase'], pairs['frame'], pairs['x'])}
    for s in np.flatnonzero(d['seq_clip'] == ci):
        p, a, b = int(d['seq_phase'][s]), int(d['seq_start'][s]), int(d['seq_stop'][s])
        for fr, feat, v in zip(d['frame'][a:b], d['feat'][a:b], d['valid'][a:b]):
            if v and (p, int(fr)) in pz:
                assert np.allclose(feat[39:52], pz[(p, int(fr))][26:39])
print(
    f'Passed: streaming runtime simulation == batch features on {checked} slots (max |diff| {worst:.1e}); masks match LPN pairs.'
)
