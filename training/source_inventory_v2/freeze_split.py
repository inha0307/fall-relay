#!/usr/bin/env python3
"""Phase 1.2: freeze the subject split derived from paired_manifest_v2.csv.

Writes split_v2.json (subjects, eligible capture groups and counts per split, and the
manifest hash it was derived from). Downstream steps must check this hash.
"""

import collections, csv, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import _paths  # noqa: E402

MANIFEST = HERE / "paired_manifest_v2.csv"
rows = [r for r in csv.DictReader(MANIFEST.open()) if r["eligible"] == "1"]
split = {}
for s in ("train", "val", "test"):
    sub = [r for r in rows if r["split"] == s]
    split[s] = {
        "subjects": sorted({int(r["person"]) for r in sub}),
        "videos": len(sub),
        "capture_groups": len({r["capture_group"] for r in sub}),
        "fall_capture_groups": sorted({r["capture_group"] for r in sub if r["action"] == "43"}),
    }
subjects = [set(v["subjects"]) for v in split.values()]
assert not (subjects[0] & subjects[1] or subjects[0] & subjects[2] or subjects[1] & subjects[2])
groups = collections.defaultdict(set)
for r in rows:
    groups[r["capture_group"]].add(r["split"])
assert all(len(v) == 1 for v in groups.values())
out = {
    "protocol": "NTU60 official cross-subject test; val = 4 official-train subjects (ntu60_protocol.json)",
    "scope": "eligible A001-A049, official missing skeletons excluded",
    "manifest_fingerprint": _paths.manifest_fingerprint(MANIFEST),
    "test_policy": "do not decode/extract/evaluate test until all choices are frozen on val; use once",
    "splits": split,
}
(HERE / "split_v2.json").write_text(json.dumps(out, indent=2) + "\n")
print({s: {k: (len(v) if isinstance(v, list) else v) for k, v in d.items()} for s, d in split.items()})
