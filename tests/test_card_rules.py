"""V2 role rules: direct unified travel.py lookups, slim roles, and safe Kanban fallbacks."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import setup  # noqa: E402
HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles"


def texts(repo_role, live_profile):
    found = [(f"roles/{repo_role}.md",
              setup.expand_includes((ROOT / "roles" / f"{repo_role}.md").read_text(encoding="utf-8")))]
    live = HERMES / live_profile / "SOUL.md"
    if live.is_file():
        found.append((f"live {live_profile}", live.read_text(encoding="utf-8")))
    return found


class CardRulesTest(unittest.TestCase):
    def test_overseas_uses_direct_travel_cli_without_kanban_hop(self):
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("travel.py overseas", text)
                self.assertIn("不必建 Kanban 卡片或另請覆核", text)

    def test_local_uses_direct_travel_cli(self):
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn('travel.py" local', text)
                self.assertIn("原樣貼上輸出", text)

    def test_kanban_fallback_keeps_safety_rules(self):
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("--body '<單引號內容>'", text)
                self.assertIn("show <卡號>`（不加 `--json`）", text)
                self.assertNotIn("show <卡號> --json", text)
                self.assertIn("絕不回 `NO_REPLY`", text)

    def test_trip_does_not_rerun_or_guess_on_limit(self):
        for name, text in texts("trip", "trip"):
            with self.subTest(name):
                self.assertIn("travel.py overseas", text)
                self.assertIn("就不重跑", text)
                self.assertIn("未確認", text)
                self.assertIn("不讀 `trip-bot` 程式碼除錯", text)

    def test_opportunities_is_searched_not_read_whole(self):
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("search_files", text)
                self.assertIn("pending_question", text)


if __name__ == "__main__":
    unittest.main()
