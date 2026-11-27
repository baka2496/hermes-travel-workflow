"""Tests for automated Hermes state.db & snapshot maintenance (scripts/db-maintain.py)."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

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
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)"
    )
    con.execute("ALTER TABLE messages ADD COLUMN codex_reasoning_items TEXT")
    con.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, admission_id TEXT, "
        "target_session_id TEXT, status TEXT)"
    )
    old_ts = now - 20 * 86400
    recent_ts = now - 1 * 86400
    con.execute("INSERT INTO sessions VALUES ('old_s', ?, ?)", (old_ts, old_ts + 60))
    # Long-lived open Telegram session: started 20 days ago, still open, has both old and fresh messages
    con.execute("INSERT INTO sessions VALUES ('long_open', ?, NULL)", (old_ts,))
    con.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, codex_reasoning_items) VALUES (?, ?, ?, ?, ?, ?)",
        (1, "old_s", "assistant", "old reply", old_ts, "R" * 200_000),
    )
    con.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, codex_reasoning_items) VALUES (?, ?, ?, ?, ?, ?)",
        (2, "long_open", "assistant", "old msg in open session", old_ts + 10, "O" * 100_000),
    )
    con.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, codex_reasoning_items) VALUES (?, ?, ?, ?, ?, ?)",
        (3, "long_open", "assistant", "fresh msg in open session", recent_ts, "K" * 50_000),
    )
    # Stale vs active gateway.local_policy.v1 entries
    con.execute(
        "INSERT INTO state_meta VALUES ('gateway.local_policy.v1:old_s', ?)",
        (json.dumps({"entry": {"updated_at": "2020-01-01T00:00:00", "active_turn_started_at": None}, "pad": "X" * 100_000}),),
    )
    con.execute(
        "INSERT INTO state_meta VALUES ('gateway.local_policy.v1:long_open', ?)",
        (json.dumps({"entry": {"updated_at": "2099-01-01T00:00:00", "active_turn_started_at": None}}),),
    )
    # Stale terminal admission vs active admission
    con.execute("INSERT INTO session_admissions VALUES (1, 'adm_old', 'old_s', 'terminal')")
    con.execute("INSERT INTO session_admissions VALUES (2, 'adm_new', 'long_open', 'running')")
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
    def test_dry_run_and_apply_reclaims_bloat_preserves_long_open_fresh_messages_and_archives_pages(self):
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

        # 1. Dry-run matches exact message-timestamp count (2 old msgs, NOT the fresh msg in long_open)
        info = mod.inspect_db(db_path, keep_days=7, keep_reasoning_days=7, now=now)
        self.assertEqual(info["reasoning_rows"], 2)
        self.assertEqual(info["stale_meta_rows"], 2)
        self.assertTrue(old_snap.is_file())

        # 2. Apply mode cleans old reasoning + stale state_meta + archives old snapshot (never bulk-deletes)
        rc = mod.main([
            "--hermes-root", str(root),
            "--apply", "--archive-pages", "--skip-hermes-cli", "--json",
        ])
        self.assertEqual(rc, 0)
        bak_path = pdir / "state.db.before-maintain.bak"
        self.assertTrue(bak_path.is_file())
        # Verify WAL-safe SQLite backup is a valid readable DB with original data
        bak_con = sqlite3.connect(bak_path)
        self.assertEqual(bak_con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
        bak_con.close()

        # Expired snapshot was safely moved to .expired/, NOT permanently deleted
        self.assertFalse(old_snap.exists())
        self.assertTrue((pages / ".expired" / "old.json").is_file())
        self.assertTrue(new_snap.is_file())

        # Verify old conversation text & fresh message reasoning in long-lived open session are preserved
        con = sqlite3.connect(db_path)
        m1 = con.execute("SELECT content, codex_reasoning_items FROM messages WHERE id = 1").fetchone()
        m2 = con.execute("SELECT content, codex_reasoning_items FROM messages WHERE id = 2").fetchone()
        m3 = con.execute("SELECT content, codex_reasoning_items FROM messages WHERE id = 3").fetchone()
        meta_keys = {r[0] for r in con.execute("SELECT key FROM state_meta").fetchall()}
        con.close()
        self.assertEqual(m1, ("old reply", None))
        self.assertEqual(m2, ("old msg in open session", None))
        self.assertEqual(m3[0], "fresh msg in open session")
        self.assertEqual(len(m3[1]), 50_000)
        self.assertIn("store_instance_id", meta_keys)
        self.assertIn("gateway.local_policy.v1:long_open", meta_keys)
        self.assertIn("gateway.admission.result.v1.adm_new", meta_keys)
        self.assertNotIn("gateway.local_policy.v1:old_s", meta_keys)
        self.assertNotIn("gateway.admission.result.v1.adm_old", meta_keys)

    def test_gateway_status_parsing_and_safety_guards(self):
        mod = load_mod()
        self.assertFalse(mod.parse_gateway_running("Gateway is not running"))
        self.assertFalse(mod.parse_gateway_running("✗ Gateway stopped"))
        self.assertTrue(mod.parse_gateway_running("✓ Gateway process running (PID: 19512)"))

        root = Path(scratch.new_dir("db-guard"))
        # Missing hermes CLI must refuse --apply
        with mock.patch.object(mod.shutil, "which", return_value=None):
            self.assertEqual(mod.main(["--hermes-root", str(root), "--apply"]), 1)

        # Running gateway without --restart-gateway must refuse --apply
        with mock.patch.object(mod.shutil, "which", return_value="hermes"), \
             mock.patch.object(mod, "gateway_is_running", return_value=True):
            self.assertEqual(mod.main(["--hermes-root", str(root), "--apply"]), 1)

        # Failed gateway stop must abort --apply before touching DBs
        with mock.patch.object(mod.shutil, "which", return_value="hermes"), \
             mock.patch.object(mod, "gateway_is_running", return_value=True), \
             mock.patch.object(mod.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="stop failed")):
            self.assertEqual(mod.main(["--hermes-root", str(root), "--apply", "--restart-gateway"]), 1)


if __name__ == "__main__":
    unittest.main()
