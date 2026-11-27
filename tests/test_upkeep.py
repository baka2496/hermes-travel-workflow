"""upkeep.py: the weekly harvest + database cleanup (2026-11-28: neither was scheduled)."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upkeep", REPO / "scripts" / "upkeep.py")
upkeep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upkeep)

NOW = 1_800_000_000.0


class UpkeepTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-upkeep-"))
        (self.root / "shared" / "travel-workflow").mkdir(parents=True)
        self.calls = []
        self.locked = []

    def run_upkeep(self, last_message, codes=None, gateway=True):
        codes = codes or {}

        def run(name, *args):
            self.calls.append((name, args, bool(self.locked)))
            return codes.get(name, 0), f"{name} done"

        @contextlib.contextmanager
        def lock():
            self.locked.append(True)
            try:
                yield
            finally:
                self.locked.pop()

        return upkeep.upkeep(self.root, now=NOW, run=run, activity=lambda root: last_message, lock=lock,
                             hermes="hermes", gateway=lambda h: gateway)

    def test_harvest_first_then_cleanup_inside_the_watchdog_lock(self):
        result = self.run_upkeep(NOW - 3600)
        self.assertEqual([c[0] for c in self.calls], ["places.py", "db-maintain.py"])
        self.assertEqual(self.calls[0][1], ("harvest",))
        self.assertFalse(self.calls[0][2])  # harvest needs no gateway stop
        self.assertTrue(self.calls[1][2])  # cleanup holds the watchdog off
        self.assertIn("--apply", self.calls[1][1])
        self.assertIn("--archive-pages", self.calls[1][1])  # moved, never deleted
        self.assertFalse(any("prune" in a for a in self.calls[1][1]))  # every conversation kept
        self.assertTrue(result["harvest"]["ok"] and result["maintain"]["ok"])

    def test_someone_talking_to_a_bot_skips_the_cleanup(self):
        result = self.run_upkeep(NOW - 5 * 60)
        self.assertEqual([c[0] for c in self.calls], ["places.py"])
        self.assertIn("這週不清理", result["maintain"]["skipped"])

    def test_the_lock_not_coming_skips_the_cleanup(self):
        def busy():
            raise TimeoutError("看門狗一直在跑，這次不清理")
        result = upkeep.upkeep(self.root, now=NOW, run=lambda *a: (0, ""), activity=lambda r: 0, lock=busy,
                               hermes="hermes", gateway=lambda h: True)
        self.assertEqual(result["maintain"]["skipped"], "看門狗一直在跑，這次不清理")

    def test_a_failed_cleanup_is_reported_and_logged(self):
        result = self.run_upkeep(0, codes={"db-maintain.py": 1})
        self.assertFalse(result["maintain"]["ok"])
        upkeep.record(self.root, result)
        log = (self.root / "shared/travel-workflow/upkeep-log.txt").read_text(encoding="utf-8")
        self.assertIn("[maintain] 失敗", log)
        self.assertIn("[harvest] OK", log)
        saved = json.loads((self.root / "shared/travel-workflow/upkeep-last.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["at"], result["at"])

    def test_last_activity_reads_every_profile(self):
        for name, stamp in (("trip", 100.0), ("project-manager", 250.5)):
            folder = self.root / "profiles" / name
            folder.mkdir(parents=True)
            db = sqlite3.connect(folder / "state.db")
            db.execute("CREATE TABLE messages (timestamp REAL)")
            db.execute("INSERT INTO messages VALUES (?)", (stamp,))
            db.commit()
            db.close()
        (self.root / "profiles" / "broken").mkdir()
        (self.root / "profiles" / "broken" / "state.db").write_text("not a database", encoding="utf-8")
        self.assertEqual(upkeep.last_activity(self.root), 250.5)


if __name__ == "__main__":
    unittest.main()
