#!/usr/bin/env python3
"""Safe automated maintenance for Hermes SQLite state.db files and travel-workflow caches.

Prevents unbounded disk growth by:
1. Running Hermes's own compact FTS storage migration (`sessions optimize-storage --yes`)
   and old-session pruning (`sessions prune`) when Hermes CLI is available.
2. Clearing old encrypted reasoning blobs (`messages.codex_reasoning_items`) on ended sessions
   older than `--keep-reasoning-days` (default 7 days) while keeping conversation text intact.
3. Pruning stale completed gateway cache entries in `state_meta` (`gateway.local_policy.v1:*`
   and `gateway.admission.result.v1.*`) older than `--keep-days` (default 7 days).
4. Running `PRAGMA wal_checkpoint(TRUNCATE)` and `VACUUM` to return freed pages to disk.
5. Removing expired `shared/travel-workflow/pages/*.json` snapshots older than `--page-days`.

Defaults to `--dry-run` (read-only inspection). Pass `--apply` to execute.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def default_hermes_root():
    if HERE.name == "travel-workflow" and HERE.parent.name == "shared":
        return HERE.parent.parent
    if os.environ.get("HERMES_HOME"):
        home = Path(os.environ["HERMES_HOME"])
        if home.parent.name == "profiles":
            return home.parent.parent
        return home
    return Path.home() / ".hermes"


def discover_profile_dbs(hermes_root, profiles=None):
    """Return list of (profile_name, state_db_path) tuples."""
    root = Path(hermes_root)
    pdir = root / "profiles"
    if not pdir.is_dir():
        return []
    wanted = set(profiles) if profiles else None
    found = []
    for sub in sorted(pdir.iterdir()):
        if not sub.is_dir():
            continue
        if wanted is not None and sub.name not in wanted:
            continue
        db = sub / "state.db"
        if db.is_file():
            found.append((sub.name, db))
    return found


def _has_column(con, table, col):
    try:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
        return col in cols
    except sqlite3.Error:
        return False


def _has_table(con, table):
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _parse_iso_ts(val):
    if not val or not isinstance(val, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return None


def find_stale_state_meta_keys(con, cutoff_ts):
    """Return (stale_keys, total_bytes) in state_meta safe to prune (completed & older than cutoff)."""
    if not _has_table(con, "state_meta"):
        return [], 0

    stale = []
    total_bytes = 0

    # 1. gateway.local_policy.v1:* where no active turn and updated_at < cutoff_ts
    rows = con.execute(
        "SELECT key, value, LENGTH(CAST(COALESCE(value,'') AS BLOB)) "
        "FROM state_meta WHERE key LIKE 'gateway.local_policy.v1:%'"
    ).fetchall()
    for key, raw, nbytes in rows:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            continue
        entry = data.get("entry") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            continue
        if entry.get("active_turn_started_at"):
            continue
        ts = _parse_iso_ts(entry.get("updated_at") or entry.get("created_at"))
        if ts is not None and ts < cutoff_ts:
            stale.append(key)
            total_bytes += int(nbytes or 0)

    # 2. gateway.admission.result.v1.<admission_id> for terminal admissions whose session ended before cutoff
    adm_rows = con.execute(
        "SELECT key, LENGTH(CAST(COALESCE(value,'') AS BLOB)) "
        "FROM state_meta WHERE key LIKE 'gateway.admission.result.v1.%'"
    ).fetchall()
    if adm_rows and _has_table(con, "session_admissions") and _has_table(con, "sessions"):
        old_terminal = {
            r[0] for r in con.execute(
                "SELECT a.admission_id FROM session_admissions a "
                "LEFT JOIN sessions s ON s.id = a.target_session_id "
                "WHERE a.status = 'terminal' AND ("
                "  s.id IS NULL OR "
                "  (s.ended_at IS NOT NULL AND s.ended_at < ?) OR "
                "  (s.ended_at IS NULL AND s.started_at < ?)"
                ")",
                (cutoff_ts, cutoff_ts),
            ).fetchall()
        }
        prefix = "gateway.admission.result.v1."
        for key, nbytes in adm_rows:
            adm_id = key[len(prefix):]
            if adm_id in old_terminal:
                stale.append(key)
                total_bytes += int(nbytes or 0)

    return stale, total_bytes


def inspect_db(db_path, *, keep_days=7, keep_reasoning_days=7, now=None):
    """Read-only inspection of reclaimable bloat in one state.db."""
    now = now or time.time()
    cutoff = now - keep_days * 86400
    reason_cutoff = now - keep_reasoning_days * 86400
    size_bytes = db_path.stat().st_size

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        reasoning_rows, reasoning_bytes = 0, 0
        if _has_table(con, "messages") and _has_column(con, "messages", "codex_reasoning_items") and _has_table(con, "sessions"):
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(m.codex_reasoning_items AS BLOB))), 0) "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.codex_reasoning_items IS NOT NULL AND m.codex_reasoning_items != '' "
                "AND ( (s.ended_at IS NOT NULL AND s.ended_at < ?) OR (s.ended_at IS NULL AND m.timestamp < ?) )",
                (reason_cutoff, reason_cutoff),
            ).fetchone()
            reasoning_rows, reasoning_bytes = int(row[0] or 0), int(row[1] or 0)

        stale_meta_keys, stale_meta_bytes = find_stale_state_meta_keys(con, cutoff)

        fts_version = None
        if _has_table(con, "state_meta"):
            r = con.execute("SELECT value FROM state_meta WHERE key = 'fts_storage_version'").fetchone()
            fts_version = r[0] if r else None

        fts_bytes = 0
        for tbl in ("messages_fts_data", "messages_fts_trigram_data"):
            if _has_table(con, tbl):
                r = con.execute(f'SELECT COALESCE(SUM(LENGTH(block)), 0) FROM "{tbl}"').fetchone()
                fts_bytes += int(r[0] or 0)
    finally:
        con.close()

    return {
        "path": str(db_path),
        "size_mb": round(size_bytes / 1024 / 1024, 2),
        "reasoning_rows": reasoning_rows,
        "reasoning_mb": round(reasoning_bytes / 1024 / 1024, 2),
        "stale_meta_rows": len(stale_meta_keys),
        "stale_meta_mb": round(stale_meta_bytes / 1024 / 1024, 2),
        "fts_version": fts_version,
        "fts_data_mb": round(fts_bytes / 1024 / 1024, 2),
    }


def clean_db_bloat(db_path, *, keep_days=7, keep_reasoning_days=7, now=None, backup=True, vacuum=True):
    """Safely prune reasoning blobs & stale gateway state_meta rows from one state.db, then VACUUM."""
    now = now or time.time()
    cutoff = now - keep_days * 86400
    reason_cutoff = now - keep_reasoning_days * 86400
    before_bytes = db_path.stat().st_size

    if backup:
        bak = db_path.with_name(db_path.name + ".before-maintain.bak")
        shutil.copy2(db_path, bak)

    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        cleared_reasoning = 0
        if _has_table(con, "messages") and _has_column(con, "messages", "codex_reasoning_items") and _has_table(con, "sessions"):
            cur = con.execute(
                "UPDATE messages SET codex_reasoning_items = NULL "
                "WHERE codex_reasoning_items IS NOT NULL AND codex_reasoning_items != '' "
                "AND session_id IN ("
                "  SELECT id FROM sessions WHERE (ended_at IS NOT NULL AND ended_at < ?) "
                "  OR (ended_at IS NULL AND started_at < ?)"
                ")",
                (reason_cutoff, reason_cutoff),
            )
            cleared_reasoning = cur.rowcount

        stale_keys, _ = find_stale_state_meta_keys(con, cutoff)
        deleted_meta = 0
        if stale_keys:
            # Batch deletes to stay well under SQLite variable limit
            for i in range(0, len(stale_keys), 400):
                batch = stale_keys[i:i + 400]
                placeholders = ",".join("?" for _ in batch)
                cur = con.execute(f"DELETE FROM state_meta WHERE key IN ({placeholders})", batch)
                deleted_meta += cur.rowcount

        con.commit()
        if vacuum:
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            con.execute("VACUUM")
    finally:
        con.close()

    after_bytes = db_path.stat().st_size
    return {
        "before_mb": round(before_bytes / 1024 / 1024, 2),
        "after_mb": round(after_bytes / 1024 / 1024, 2),
        "saved_mb": round((before_bytes - after_bytes) / 1024 / 1024, 2),
        "cleared_reasoning": cleared_reasoning,
        "deleted_meta": deleted_meta,
    }


def prune_expired_snapshots(hermes_root, *, page_days=7, apply=False, now=None):
    """Count or remove expired shared/travel-workflow/pages/*.json snapshots one explicit file at a time."""
    now = now or time.time()
    cutoff = now - page_days * 86400
    pdir = Path(hermes_root) / "shared" / "travel-workflow" / "pages"
    if not pdir.is_dir():
        return {"count": 0, "mb": 0.0}
    expired = []
    total_bytes = 0
    for f in sorted(pdir.glob("*.json")):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            expired.append(f)
            total_bytes += st.st_size
    if apply:
        for f in expired:
            try:
                f.unlink()
            except OSError:
                pass
    return {"count": len(expired), "mb": round(total_bytes / 1024 / 1024, 2)}


def gateway_is_running(hermes_bin):
    if not hermes_bin:
        return False
    try:
        res = subprocess.run([hermes_bin, "gateway", "status"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20)
        return "running" in (res.stdout + res.stderr).lower()
    except (OSError, subprocess.SubprocessError):
        return False


def run_hermes_storage_ops(hermes_bin, profile, keep_session_days=None):
    """Run Hermes's own safe FTS v23 compaction & optional session prune for one profile."""
    notes = []
    if not hermes_bin:
        return notes
    cmds = [
        ("optimize-storage", [hermes_bin, "-p", profile, "sessions", "optimize-storage", "--yes"]),
        ("prune-never-active", [hermes_bin, "-p", profile, "sessions", "prune", "--never-active", "--older-than", "7d", "--yes"]),
    ]
    if keep_session_days is not None and keep_session_days > 0:
        cmds.append(("prune-old", [hermes_bin, "-p", profile, "sessions", "prune", "--older-than", f"{keep_session_days}d", "--yes"]))
    for label, cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            if res.returncode == 0:
                notes.append(f"{label}: OK")
            else:
                msg = (res.stderr or res.stdout or "").strip().splitlines()
                notes.append(f"{label}: 略過（{(msg[-1] if msg else f'exit {res.returncode}')[:80]}）")
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append(f"{label}: 錯誤（{exc}）")
    return notes


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hermes-root", help="Hermes root directory (default: auto-detected)")
    parser.add_argument("--profiles", help="Comma-separated profile names (default: all profiles with state.db)")
    parser.add_argument("--keep-days", type=int, default=7, help="Keep gateway cache entries newer than N days (default 7)")
    parser.add_argument("--keep-reasoning-days", type=int, default=7, help="Keep codex_reasoning_items newer than N days (default 7)")
    parser.add_argument("--prune-sessions-days", type=int, help="Also prune ended sessions older than N days via Hermes CLI")
    parser.add_argument("--page-days", type=int, default=7, help="Remove cached page snapshots older than N days (default 7)")
    parser.add_argument("--apply", action="store_true", help="Actually apply cleanup (without this flag, read-only dry-run)")
    parser.add_argument("--restart-gateway", action="store_true", help="Stop Hermes gateway before --apply and restart it after")
    parser.add_argument("--no-backup", action="store_true", help="Skip rolling .before-maintain.bak backup before --apply")
    parser.add_argument("--skip-hermes-cli", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args(argv)

    root = Path(args.hermes_root) if args.hermes_root else default_hermes_root()
    prof_filter = [p.strip() for p in args.profiles.split(",") if p.strip()] if args.profiles else None
    dbs = discover_profile_dbs(root, prof_filter)
    hermes_bin = None if args.skip_hermes_cli else shutil.which("hermes")

    if not args.apply:
        report = {
            "mode": "dry-run",
            "hermes_root": str(root),
            "databases": [
                {"profile": name, **inspect_db(path, keep_days=args.keep_days, keep_reasoning_days=args.keep_reasoning_days)}
                for name, path in dbs
            ],
            "expired_pages": prune_expired_snapshots(root, page_days=args.page_days, apply=False),
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(f"資料庫瘦身預覽（未修改任何檔案，根目錄：{root}）：")
        for row in report["databases"]:
            print(
                f"• {row['profile']}/state.db：{row['size_mb']} MB"
                f"｜可清推理殘留 {row['reasoning_mb']} MB（{row['reasoning_rows']} 筆）"
                f"｜可清舊閘道快取 {row['stale_meta_mb']} MB（{row['stale_meta_rows']} 筆）"
                f"｜FTS 索引 {row['fts_data_mb']} MB（版本={row['fts_version']}）"
            )
        ep = report["expired_pages"]
        print(f"• pages/ 過期快照：{ep['count']} 份（{ep['mb']} MB）")
        print("要實際執行瘦身請加 --apply --restart-gateway。")
        return 0

    # Apply mode
    was_running = False
    if hermes_bin and gateway_is_running(hermes_bin):
        if not args.restart_gateway:
            print("偵測到 Hermes 閘道正在執行；為避免資料庫鎖定，請加 --restart-gateway 讓工具先安全停止再重啟。", file=sys.stderr)
            return 1
        was_running = True
        subprocess.run([hermes_bin, "gateway", "stop", "--yes"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)

    results = []
    try:
        for name, path in dbs:
            before_mb = round(path.stat().st_size / 1024 / 1024, 2)
            hermes_notes = run_hermes_storage_ops(hermes_bin, name, keep_session_days=args.prune_sessions_days)
            stats = clean_db_bloat(
                path,
                keep_days=args.keep_days,
                keep_reasoning_days=args.keep_reasoning_days,
                backup=not args.no_backup,
                vacuum=True,
            )
            stats["before_mb"] = before_mb
            stats["saved_mb"] = round(before_mb - stats["after_mb"], 2)
            results.append({"profile": name, **stats, "hermes_cli": hermes_notes})
        expired = prune_expired_snapshots(root, page_days=args.page_days, apply=True)
    finally:
        if was_running and hermes_bin:
            subprocess.run([hermes_bin, "gateway", "start"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)

    summary = {"mode": "applied", "databases": results, "expired_pages": expired, "gateway_restarted": was_running}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    total_saved = round(sum(r["saved_mb"] for r in results) + expired["mb"], 2)
    print(f"資料庫自動瘦身完成（共釋放 {total_saved} MB）：")
    for r in results:
        print(
            f"• {r['profile']}/state.db：{r['before_mb']} MB → {r['after_mb']} MB（省下 {r['saved_mb']} MB；"
            f"清推理 {r['cleared_reasoning']} 筆、舊閘道快取 {r['deleted_meta']} 筆）"
        )
        if r["hermes_cli"]:
            print("  Hermes 內建優化：" + "；".join(r["hermes_cli"]))
    print(f"• pages/ 清理過期快照：{expired['count']} 份（省下 {expired['mb']} MB）")
    if was_running:
        print("• Hermes 閘道已自動重啟完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
