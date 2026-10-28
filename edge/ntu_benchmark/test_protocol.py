"""State boundaries preserve model handles and never pool tail/warmup into source slots."""

import ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'edge'))
from causal_tracker import CausalTracker
from features_v2 import SlotFeaturizer
from decision_engine import DecisionEngine, DecisionParams

source = ast.parse((Path(__file__).parent / 'benchmark_device.py').read_text())
node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'reset_sequence')
ns = {'CausalTracker': CausalTracker, 'SlotFeaturizer': SlotFeaturizer}
exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual reset_sequence>', 'exec'), ns)


class Core:
    pass


c = Core()
c.net = object()
c.lpn = object()
c.tcn = object()
handles = (c.net, c.lpn, c.tcn)
c.engine = DecisionEngine(DecisionParams(theta=0.95, k=3, n=5, m=3, cooldown_s=0))
for i in range(5):
    c.engine.step(i * 0.1, 0.99, True, False, False)
assert c.engine.state == 'ALERT'
c.tracker = object()
c.featurizer = object()
c.origin = 10
c.last_grid = 42
c.history = [1] * 20
c.history_valid = [True] * 20
ns['reset_sequence'](c)
assert (c.net, c.lpn, c.tcn) == handles
assert c.origin is None and c.last_grid == -1 and not c.history and not c.history_valid and c.engine.state == 'IDLE'
assert c.engine.step(100, 0.01, True, True, False) == [] and c.engine.state == 'IDLE'
print('PASS actual reset function clears prior alert, tracker/feature/history/grid state and preserves model handles')
# Deadline frame selection skips overdue source frames instead of extending playback.
fps = 30
indices = [int(t * fps) for t in (0, 0.04, 0.20, 0.24)]
assert indices == [0, 1, 6, 7]
rows = [
    {'phase': p, 'segment': s}
    for p, s in [('warmup', 'source'), ('measure', 'source'), ('measure', 'forced_tail'), ('measure', 'boundary')]
]
assert sum(r['phase'] == 'measure' and r['segment'] == 'source' for r in rows) == 1
print('PASS overdue-frame rule and source-only measurement inclusion')
