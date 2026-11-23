"""Archive expired or past travel opportunities so opportunities.json stays small and fast to search.

Moves records that have NO open `pending_question` and are either:
  - marked expired/cancelled (「已失效」「已取消」「expired」「cancelled」), or
  - whose latest trip date (YYYY-MM-DD) is older than `--days` days ago (default: 30)
into `opportunities-archive.json` next to `opportunities.json`. No data is deleted.

Usage:
  archive-opportunities.py [--file PATH] [--days 30] [--dry-run]
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
DEFAULT_FILE = HERE / "opportunities.json"
ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
CLOSED_STATUSES = {"已失效", "已取消", "expired", "cancelled", "canceled"}
DATE_KEYS = ("return_date", "depart_date", "end_date", "start_date", "date", "dates", "travel_window", "candidate_dates")


def latest_trip_date(record):
    """Return the latest YYYY-MM-DD found in a record's trip-date fields, or None."""
    found = []

    def walk(node):
        if isinstance(node, str):
            for m in ISO_DATE.findall(node):
                try:
                    found.append(datetime.date.fromisoformat(m))
                except ValueError:
                    pass
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    if isinstance(record, dict):
        for key in DATE_KEYS:
            if key in record:
                walk(record[key])
    return max(found) if found else None


def should_archive(record, cutoff):
    """True when `record` is safe to archive (no pending_question, and closed or past cutoff)."""
    if not isinstance(record, dict):
        return False
    if record.get("pending_question"):
        return False
    status = str(record.get("status") or "").strip().lower()
    if status in CLOSED_STATUSES:
        return True
    last = latest_trip_date(record)
    return last is not None and last < cutoff


def atomic_write_json(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def archive_file(path, *, days=30, today=None, dry_run=False):
    """Archive old records from `path` into sibling `opportunities-archive.json`.

    Returns (kept_count, archived_count, archive_path).
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("opportunities"), list):
        records = data["opportunities"]
        wrapped = True
    elif isinstance(data, list):
        records = data
        wrapped = False
    else:
        raise ValueError(f"Unrecognized opportunities.json layout: {path}")

    cutoff = (today or datetime.date.today()) - datetime.timedelta(days=days)
    kept, to_archive = [], []
    for rec in records:
        if should_archive(rec, cutoff):
            to_archive.append(rec)
        else:
            kept.append(rec)

    archive_path = path.with_name("opportunities-archive.json")
    if to_archive and not dry_run:
        if archive_path.is_file():
            existing = json.loads(archive_path.read_text(encoding="utf-8-sig"))
            prior = existing.get("opportunities", []) if isinstance(existing, dict) else existing
            if not isinstance(prior, list):
                prior = []
        else:
            prior = []
        archive_payload = {"schema_version": 1, "opportunities": prior + to_archive}
        atomic_write_json(archive_path, archive_payload)
        new_main = {**data, "opportunities": kept} if wrapped else kept
        atomic_write_json(path, new_main)
    return len(kept), len(to_archive), archive_path


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="path to opportunities.json")
    parser.add_argument("--days", type=int, default=30, help="archive trips whose latest date is older than N days")
    parser.add_argument("--dry-run", action="store_true", help="show counts without modifying files")
    args = parser.parse_args()
    if not args.file.is_file():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1
    kept, archived, archive_path = archive_file(args.file, days=args.days, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}kept {kept}, archived {archived} -> {archive_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
