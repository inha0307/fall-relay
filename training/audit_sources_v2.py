#!/usr/bin/env python3
"""Phase 1.1: full NTU60 RGB source inventory (S001-S017) and paired manifest.

Read-only. Originals are never extracted to disk or modified.
  manifest  zip central directories + skeleton presence + official missing list +
            the fixed subject split of ntu60_protocol.json (test = untouched official
            NTU60 cross-subject test identities). Fast.
  integrity full CRC of every AVI member, AVI header metadata (fps, frames, size) and
            skeleton frame count, written per sample. Slow (reads ~136 GB); resumable.
Outputs go to source_inventory_v2/. The S001-S005 v1 artifacts are left unchanged.
"""

import argparse, collections, csv, hashlib, json, re, zipfile, zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import _paths  # noqa: E402

RGB = _paths.NTU_RGB
SKEL = _paths.NTU_SKELETONS
OUT = ROOT / "source_inventory_v2"
SETUPS = range(1, 18)
PAT = re.compile(r"S(\d{3})C(\d{3})P(\d{3})R(\d{3})A(\d{3})")
MEMBER = re.compile(r"S\d{3}C\d{3}P\d{3}R\d{3}A\d{3}_rgb\.avi$")
SPEC = json.loads((ROOT / "ntu60_protocol.json").read_text())
TRAIN, VAL = set(SPEC["train_subjects_proposed"]), set(SPEC["validation_subjects_proposed"])
OFFICIAL_TRAIN = set(SPEC["official_train_subjects"])
assert TRAIN | VAL == OFFICIAL_TRAIN and not TRAIN & VAL
MISSING_PATH = ROOT / "ntu60_official_missing.txt"
MISSING = {m.group() for m in PAT.finditer(MISSING_PATH.read_text())}
assert len(MISSING) == 302, "official missing list version/count needs review"


def manifest():
    skeletons = {p.stem: p for p in SKEL.glob("*.skeleton")}
    rows, archives = [], []
    for setup in SETUPS:
        archive = RGB / f"nturgbd_rgb_s{setup:03}.zip"
        with zipfile.ZipFile(archive) as z:
            members = [i for i in z.infolist() if not i.is_dir()]
            unexpected = [i.filename for i in members if not MEMBER.search(i.filename)]
            if unexpected:
                raise ValueError(f"{archive.name}: unexpected members {unexpected[:3]}")
            for item in members:
                m = PAT.search(item.filename)
                sid = m.group()
                s, c, p, r, a = map(int, m.groups())
                if s != setup:
                    raise ValueError(f"{item.filename} in {archive.name}")
                split = "train" if p in TRAIN else "val" if p in VAL else "test"
                skel = skeletons.get(sid)
                reasons = []
                if a > 49:
                    reasons.append("interaction_outside_baseline")
                if sid in MISSING:
                    reasons.append("official_missing_skeleton")
                if skel is None:
                    reasons.append("skeleton_file_absent")
                elif skel.stat().st_size == 0:
                    reasons.append("skeleton_file_empty")
                rows.append(
                    dict(
                        sample_id=sid,
                        setup=s,
                        camera=c,
                        person=p,
                        repetition=r,
                        action=a,
                        capture_group=f"S{s:03}P{p:03}R{r:03}A{a:03}",
                        split=split,
                        rgb_archive=str(archive),
                        rgb_member=item.filename,
                        rgb_bytes=item.file_size,
                        rgb_crc32=f"{item.CRC:08x}",
                        skeleton_path=str(skel) if skel else "",
                        eligible=int(not reasons),
                        exclusion_reason=";".join(reasons),
                    )
                )
            archives.append(
                dict(
                    archive=archive.name,
                    bytes=archive.stat().st_size,
                    videos=len(members),
                    subjects=sorted({int(PAT.search(i.filename).group(3)) for i in members}),
                )
            )
    ids = [r["sample_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample ids across archives")
    OUT.mkdir(exist_ok=True)
    with (OUT / "paired_manifest_v2.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    eligible = [r for r in rows if r["eligible"]]
    count = lambda xs, key: dict(sorted(collections.Counter(key(x) for x in xs).items()))
    summary = {
        "rgb_root": str(RGB),
        "skeleton_root": str(SKEL),
        "setups": [f"S{s:03}" for s in SETUPS],
        "archives": archives,
        "rgb_videos": len(rows),
        "rgb_with_skeleton_file": sum(bool(r["skeleton_path"]) for r in rows),
        "official_missing_list_sha256": hashlib.sha256(MISSING_PATH.read_bytes()).hexdigest(),
        "split_definition": {
            "source": "ntu60_protocol.json",
            "train": sorted(TRAIN),
            "val": sorted(VAL),
            "test": "official NTU60 cross-subject test identities (untouched)",
        },
        "exclusion_counts": dict(collections.Counter(x for r in rows for x in r["exclusion_reason"].split(";") if x)),
        "eligible_videos": len(eligible),
        "eligible_split_counts": count(eligible, lambda r: r["split"]),
        "eligible_subjects_by_split": {
            s: sorted({r["person"] for r in eligible if r["split"] == s}) for s in ("train", "val", "test")
        },
        "fall_videos_by_split": count([r for r in eligible if r["action"] == 43], lambda r: r["split"]),
        "fall_capture_groups_by_split": count(
            {(r["split"], r["capture_group"]) for r in eligible if r["action"] == 43}, lambda x: x[0]
        ),
        "manifest_fingerprint": _paths.manifest_fingerprint(OUT / "paired_manifest_v2.csv"),
        "limits": [
            "integrity (full CRC, AVI header, skeleton frame count) is a separate step",
            "test identities are listed but no test video is decoded by this script",
        ],
    }
    (OUT / "source_summary_v2.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps({k: v for k, v in summary.items() if k not in ("archives", "eligible_subjects_by_split")}, indent=1)
    )


def _avi_header(data):
    from ntu_io import avi_metadata

    try:
        return avi_metadata(data)
    except Exception as e:
        return {"header_error": str(e)}


def _skeleton_frames(path):
    if not path:
        return None
    with open(path) as f:
        return int(f.readline())


def _check_archive(archive, rows):
    """Full CRC + header for every member of one archive (sequential read of one zip)."""
    out = []
    with zipfile.ZipFile(archive) as z:
        for r in rows:
            crc, head = 0, b""
            with z.open(r["rgb_member"]) as f:
                while chunk := f.read(1 << 22):
                    if len(head) < 65536:
                        head += chunk[: 65536 - len(head)]
                    crc = zlib.crc32(chunk, crc)
            meta = _avi_header(head)
            skel = _skeleton_frames(r["skeleton_path"])
            out.append(
                dict(
                    sample_id=r["sample_id"],
                    crc_ok=int(f"{crc:08x}" == r["rgb_crc32"]),
                    fps=meta.get("fps", ""),
                    video_frames=meta.get("frame_count", ""),
                    width=meta.get("width", ""),
                    height=meta.get("height", ""),
                    header_error=meta.get("header_error", ""),
                    skeleton_frames="" if skel is None else skel,
                    frames_match="" if skel is None or "frame_count" not in meta else int(skel == meta["frame_count"]),
                )
            )
    return Path(archive).name, out


def integrity(workers):
    with (OUT / "paired_manifest_v2.csv").open() as f:
        rows = list(csv.DictReader(f))
    done_dir = OUT / "integrity_parts"
    done_dir.mkdir(exist_ok=True)
    by_archive = collections.defaultdict(list)
    for r in rows:
        by_archive[r["rgb_archive"]].append(r)
    todo = {a: rs for a, rs in by_archive.items() if not (done_dir / f"{Path(a).stem}.csv").exists()}
    print(f"archives done {len(by_archive) - len(todo)}/{len(by_archive)}", flush=True)
    with ProcessPoolExecutor(workers) as pool:
        for fut in as_completed([pool.submit(_check_archive, a, rs) for a, rs in todo.items()]):
            name, out = fut.result()
            part = done_dir / f"{Path(name).stem}.csv"
            with part.with_suffix(".partial").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(out[0]))
                w.writeheader()
                w.writerows(out)
            part.with_suffix(".partial").replace(part)
            print(f"{name}: {len(out)} videos, crc_bad={sum(not o['crc_ok'] for o in out)}", flush=True)
    merged = []
    for a in sorted(by_archive):
        with (done_dir / f"{Path(a).stem}.csv").open() as f:
            merged += list(csv.DictReader(f))
    with (OUT / "integrity_v2.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(merged[0]))
        w.writeheader()
        w.writerows(merged)
    summary = {
        "videos": len(merged),
        "crc_bad": [m["sample_id"] for m in merged if m["crc_ok"] != "1"],
        "header_errors": [m["sample_id"] for m in merged if m["header_error"]],
        "fps_values": dict(collections.Counter(m["fps"] for m in merged)),
        "resolutions": dict(collections.Counter(f"{m['width']}x{m['height']}" for m in merged)),
        "skeleton_frame_mismatch": [m["sample_id"] for m in merged if m["frames_match"] == "0"],
        "integrity_sha256": hashlib.sha256((OUT / "integrity_v2.csv").read_bytes()).hexdigest(),
    }
    (OUT / "integrity_summary_v2.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {k: (v if not isinstance(v, list) else {"count": len(v), "first": v[:5]}) for k, v in summary.items()},
            indent=1,
        )
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["manifest", "integrity"])
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    manifest() if args.step == "manifest" else integrity(args.workers)
