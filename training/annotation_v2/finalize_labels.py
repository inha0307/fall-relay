#!/usr/bin/env python3
"""Final A043 event labels (v2): rule-generated only.

Every label comes from auto_labels.py (rule_v2: Kinect head-descent onset, first non-foot floor
contact impact), so anyone holding NTU RGB+D can regenerate the same file. Capture groups for
which the rule gives no label are excluded and listed in the summary; no visual or human
labels are added. If audit/audit_result.json exists (a local visual audit of the rule), its
summary is copied into the summary file.
"""

import csv, hashlib, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FPS = 30.0


def main():
    auto = list(csv.DictReader((HERE / "auto_labels_v2.csv").open()))
    out, excluded = [], []
    for r in auto:
        g = r["capture_group"]
        if not r["onset_frame"]:
            excluded.append({"capture_group": g, "split": r["split"], "view_status": r["view_status"]})
            continue
        on, im, flags = int(r["onset_frame"]), int(r["impact_frame"]), []
        if int(r["impact_spread_frames"]) > 6:
            flags.append("camera_impact_spread_gt6")
        if int(r["views_ok"]) < 3:
            flags.append(f"views_ok_{r['views_ok']}")
        if int(r["dev_group"]):
            flags.append("rule_dev_group")
        out.append(
            {
                "capture_group": g,
                "split": r["split"],
                "onset_frame": on,
                "impact_frame": im,
                "onset_s": round(on / FPS, 3),
                "impact_s": round(im / FPS, 3),
                "source": "rule_v2",
                "flags": ";".join(flags),
                "note": "",
            }
        )
    path = HERE / "event_labels_v2.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    summary = {
        "groups": len(out),
        "by_source": {"rule_v2": len(out)},
        "by_split": {s: sum(o["split"] == s for o in out) for s in ("train", "val", "test")},
        "excluded_no_rule_label": excluded,
        "flagged": sum(bool(o["flags"]) for o in out),
        "fps": FPS,
        "caveat": "Rule-generated, not human-annotated; groups without a rule label are excluded.",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    audit = HERE / "audit" / "audit_result.json"
    if audit.exists():
        summary["rule_audit"] = json.loads(audit.read_text())["summary"]
    (HERE / "event_labels_v2_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rule_audit"}, indent=1))


if __name__ == "__main__":
    main()
