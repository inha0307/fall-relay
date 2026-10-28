"""Run identities: the pose13/feature59 contract with its implementation set is accepted and stored in model_runs;
other contracts, implementation sets and undeclared decision overrides are refused."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401

from _pg import Schema, url
from event_store import EventStore
from edge_fixtures import DECISION, identity
from model_contract import CONTRACT_V2, validate_identity
from model_run_store import ModelRunStore


def refused(item):
    try:
        validate_identity(item)
    except ValueError:
        return
    raise AssertionError(f'accepted {item}')


assert validate_identity(identity())['contract_version'] == CONTRACT_V2
assert validate_identity(identity(decision=DECISION))['event_decision']['declared'] == DECISION
refused(identity(contract_version='fallscope-pose12-feature55-v1'))  # the earlier runtime's contract
refused(identity(contract_version='fallscope-pose99-feature1-v9'))
refused(identity(implementation_sha256={k: 'a' * 64 for k in ('ncnn_pose', 'pose_candidates', 'causal_tracker')}))
overridden = identity(decision=DECISION)
overridden['event_decision']['effective'] = {**DECISION, 'theta': 0.5}
refused(overridden)
refused(identity(decision={**DECISION, 'k': 6}))  # k > m
with Schema():
    store = ModelRunStore(EventStore(url()))
    store.observe('1', identity())
    row = store.db.execute('SELECT bundle_id FROM model_runs').fetchone()
    assert row['bundle_id'] == 'fall-relay-v2-test'
print('PASS v2 identity accepted and stored; other contracts, implementation sets and undeclared overrides refused')
