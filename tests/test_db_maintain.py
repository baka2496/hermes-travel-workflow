"""Tests for automated Hermes state.db & snapshot maintenance (scripts/db-maintain.py)."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest

import scratch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "db-maintain.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("db_maintain", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fake_hermes_db(db_path, now):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, ended_at REAL)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, codex_reasoning_items TEXT, timestamp REAL)"
    )
    con.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, admission_id TEXT, "
        "target_session_id TEXT, status TEXT)"
    )
    old_ts = now - 20 * 86400
    recent_ts = now - 1 * 86400
    con.execute("INSERT INTO sessions VALUES ('old_s', ?, ?)", (old_ts, old_ts + 60))
    con.execute("INSERT INTO sessions VALUES ('new_s', ?, NULL)", (recent_ts,))
    # Big encrypted reasoning blob on old session vs recent session
    con.execute("INSERT INTO messages VALUES (1, 'old_s', 'assistant', 'old reply', ?, ?)", ("R" * 200_000, old_ts))
    con.execute("INSERT INTO messages VALUES (2, 'new_s', 'assistant', 'new reply', ?, ?)", ("K" * 50_000, recent_ts))
    # Stale vs active gateway.local_policy.v1 entries
    con.execute(
        "INSERT INTO state_meta VALUES ('gateway.local_policy.v1:old_s', ?)",
        (json.dumps({"entry": {"updated_at": "2020-01-01T00:00:00", "active_turn_started_at": None}, "pad": "X" * 100_000}),),
    )
    con.execute(
        "INSERT INTO state_meta VALUES ('gateway.local_policy.v1:new_s', ?)",
        (json.dumps({"entry": {"updated_at": "2099-01-01T00:00:00", "active_turn_started_at": None}}),),
    )
    # Stale terminal admission vs active admission
    con.execute("INSERT INTO session_admissions VALUES (1, 'adm_old', 'old_s', 'terminal')")
    con.execute("INSERT INTO session_admissions VALUES (2, 'adm_new', 'new_s', 'running')")
    con.execute(
        "INSERT INTO state_meta VALUES ('gateway.admission.result.v1.adm_old', ?)",
        (json.dumps({"result": "Y" * 100_000}),),
    )
    con.execute(
        "INSERT INTO state_meta VALUES ('gateway.admission.result.v1.adm_new', ?)",
        (json.dumps({"result": "keep"}),),
    )
    con.execute("INSERT INTO state_meta VALUES ('store_instance_id', 'keep-me')")
    con.commit()
    con.close()


class DbMaintainTests(unittest.TestCase):
    def test_dry_run_and_apply_reclaims_bloat_while_preserving_recent_and_conversation_text(self):
        mod = load_mod()
        root = Path(scratch.new_dir("db-maintain"))
        pdir = root / "profiles" / "trip"
        pdir.mkdir(parents=True)
        db_path = pdir / "state.db"
        now = time.time()
        make_fake_hermes_db(db_path, now)

        pages = root / "shared" / "travel-workflow" / "pages"
        pages.mkdir(parents=True)
        old_snap = pages / "old.json"
        new_snap = pages / "new.json"
        old_snap.write_text("{" + "a" * 50_000 + "}", encoding="utf-8")
        new_snap.write_text("{}", encoding="utf-8")
        old_mtime = now - 15 * 86400
        os.utime(old_snap, (old_mtime, old_mtime))

        # 1. Dry-run inspects without changing DB or files
        info = mod.inspect_db(db_path, keep_days=7, keep_reasoning_days=7, now=now)
        self.assertEqual(info["reasoning_rows"], 1)
        self.assertEqual(info["stale_meta_rows"], 2)
        self.assertTrue(old_snap.is_file())

        # 2. Apply mode cleans old reasoning + stale state_meta + old snapshot + VACUUMs + writes rolling backup
        rc = mod.main([
            "--hermes-root", str(root),
            "--apply", "--skip-hermes-cli", "--json",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue((pdir / "state.db.before-maintain.bak").is_file())
        self.assertFalse(old_snap.exists())
        self.assertTrue(new_snap.is_file())

        # Verify old conversation text & recent data are 100% preserved
        con = sqlite3.connect(db_path)
        old_msg = con.execute("SELECT content, codex_reasoning_items FROM messages WHERE id = 1").fetchone()
        new_msg = con.execute("SELECT content, codex_reasoning_items FROM messages WHERE id = 2").fetchone()
        meta_keys = {r[0] for r in con.execute("SELECT key FROM state_meta").fetchall()}
        con.close()
        self.assertEqual(old_msg[0], "old reply")
        self.assertIsNone(old_msg[1])
        self.assertEqual(new_msg[0], "new reply")
        self.assertEqual(len(new_msg[1]), 50_000)
        self.assertIn("store_instance_id", meta_keys)
        self.assertIn("gateway.local_policy.v1:new_s", meta_keys)
        self.assertIn("gateway.admission.result.v1.adm_new", meta_keys)
        self.assertNotIn("gateway.local_policy.v1:old_s", meta_keys)
        self.assertNotIn("gateway.admission.result.v1.adm_old", meta_keys)


if __name__ == "__main__":
    unittest.main()
