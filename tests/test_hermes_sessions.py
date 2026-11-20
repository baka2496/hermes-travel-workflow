"""scripts/hermes_sessions.py: the one reader of Hermes's conversation databases (2026-11-20 refactor).

Before it, the plugin and page_evidence.py each opened state.db and parsed tool calls themselves,
and both treated any database error as "nothing found": a Hermes upgrade that renamed a column
would have switched the price and link checks off without a word.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import hermes_sessions as sessions  # noqa: E402

import scratch

HERMES_TABLE = ("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
                "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def root_with(table=HERMES_TABLE, profile="travel-commander"):
    root = Path(scratch.new_dir("hermes-sessions-test-"))
    path = root / "profiles" / profile / "state.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    if table:
        db.execute(table)
    db.commit()
    db.close()
    return root


class LayoutTests(unittest.TestCase):
    def test_test_fixtures_use_the_layout_it_names(self):
        """Every test that fakes a Hermes database builds this same table."""
        columns = re.findall(r"(\w+) (?:INTEGER|TEXT|REAL)", HERMES_TABLE)
        self.assertEqual(tuple(columns), sessions.MESSAGE_COLUMNS)
        checked = []
        for path in (REPO / "tests").glob("*.py"):
            for table in re.findall(r'"CREATE TABLE messages \(([^"]*)"\s*"([^"]*)\)"', path.read_text(encoding="utf-8")):
                found = re.findall(r"(\w+) (?:INTEGER|TEXT|REAL)", "".join(table))
                self.assertEqual(tuple(found), sessions.MESSAGE_COLUMNS, path.name)
                checked.append(path.name)
        self.assertGreaterEqual(len(set(checked) - {Path(__file__).name}), 2, checked)  # reply_check and evidence fixtures

    def test_matching_database_has_no_problem(self):
        self.assertEqual(sessions.schema_problems(root_with()), [])

    def test_renamed_column_is_reported(self):
        root = root_with(HERMES_TABLE.replace("tool_calls TEXT", "calls TEXT"))
        [(path, problem)] = sessions.schema_problems(root)
        self.assertEqual(path.name, "state.db")
        self.assertIn("tool_calls", problem)

    def test_missing_table_is_reported(self):
        [(_, problem)] = sessions.schema_problems(root_with(table=None))
        self.assertIn("no messages table", problem)

    def test_page_evidence_reads_profiles_only(self):
        root = root_with()
        sqlite3.connect(root / "state.db").close()
        self.assertEqual([p.parent.name for p in sessions.profile_databases(root)], ["travel-commander"])
        self.assertEqual(len(sessions.databases(root)), 2)

    def test_read_only_never_creates_a_file(self):
        missing = Path(scratch.new_dir("hermes-sessions-test-")) / "state.db"
        with self.assertRaises(sqlite3.Error):
            with sessions.read_only(missing):
                pass
        self.assertFalse(missing.exists())


class SetupCheckTests(unittest.TestCase):
    def test_setup_check_reports_a_changed_layout(self):
        setup = load(REPO / "setup.py", "setup_sessions_check")
        root = root_with(HERMES_TABLE.replace("tool_name TEXT", "name TEXT"))
        (root / setup.RECEIPT).write_text(json.dumps({"version": 1, "profiles": {}}), encoding="utf-8")
        setup.jobs = lambda root, profile: []
        setup.gateway_serving = lambda root, profile: False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = setup.check(root)
        self.assertEqual(status, 1)
        self.assertIn("lacks tool_name", out.getvalue())
        self.assertIn("reply checks cannot read", out.getvalue())


class ParsingTests(unittest.TestCase):
    def test_calls_reads_both_shapes(self):
        nested = json.dumps([{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"a\": 1}"}}])
        flat = json.dumps([{"call_id": "c2", "name": "browser", "arguments": "go"}])
        self.assertEqual(sessions.calls(nested), [("c1", "terminal", "{\"a\": 1}")])
        self.assertEqual(sessions.calls(flat), [("c2", "browser", "go")])

    def test_calls_ignores_what_is_not_a_call_list(self):
        for value in (None, "", "not json", "{}", "[1, \"x\"]", "[{\"function\": 3}]"):
            self.assertEqual(sessions.calls(value), [], value)

    def test_result_text(self):
        self.assertEqual(sessions.result_text(json.dumps({"output": "總計 NT$1\r\n• a"})), "總計 NT$1\n• a")
        self.assertEqual(sessions.result_text("plain text "), "plain text")
        self.assertEqual(sessions.result_text(None), "")
        self.assertEqual(sessions.result_text("[1, 2]"), "[1, 2]")


class RecentTests(unittest.TestCase):
    def test_keeps_only_the_latest(self):
        notes = sessions.Recent(limit=3)
        for n in range(5):
            notes[f"s{n}"] = n
        self.assertEqual(list(notes), ["s2", "s3", "s4"])

    def test_setting_again_counts_as_recent(self):
        notes = sessions.Recent(limit=2)
        notes["a"], notes["b"] = 1, 2
        notes["a"] = 3
        notes["c"] = 4
        self.assertEqual(dict(notes), {"a": 3, "c": 4})

    def test_setdefault_and_pop(self):
        notes = sessions.Recent(limit=2)
        notes.setdefault("a", set()).add("x")
        self.assertEqual(notes["a"], {"x"})
        self.assertEqual(notes.pop("a"), {"x"})
        self.assertIsNone(notes.pop("a", None))

    def test_plugin_notes_are_bounded(self):
        plugin = load(REPO / "plugins/travel-reply-check/__init__.py", "reply_check_recent")
        for name in ("ANSWERED", "TIMED_OUT", "VIEWED"):
            notes = getattr(plugin, name)
            self.assertEqual(type(notes).__name__, "Recent", name)  # the plugin loads its own copy
            for n in range(notes.limit + 50):
                notes[f"s{n}"] = n
            self.assertEqual(len(notes), notes.limit, name)
            notes.clear()


if __name__ == "__main__":
    unittest.main()
