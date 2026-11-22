"""What the 2026-11-22 Telegram test of a Fukuoka card found, kept from coming back.

The card's budget arrived as "NT0,000" (bash ate `$4` inside double quotes); both of Trip's
`plan` runs met Google's limit 11 seconds apart; and the reviewer sent the correct 「未確認」
back asking Trip to pick hotels it had not priced, which sent Trip back to Google.
"""
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles"


def texts(repo_role, live_profile):
    found = [(f"roles/{repo_role}.md", (ROOT / "roles" / f"{repo_role}.md").read_text(encoding="utf-8"))]
    live = HERMES / live_profile / "SOUL.md"
    if live.is_file():
        found.append((f"live {live_profile}", live.read_text(encoding="utf-8")))
    return found


class CardRulesTest(unittest.TestCase):
    def test_card_bodies_are_single_quoted(self):
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("--assignee trip --body '<內容>' --json`", text)
                self.assertNotIn('--body "<內容>"', text)
                self.assertIn("雙引號裡的 `$` 會被 bash 當成變數", text)

    def test_a_google_limit_is_handed_in_not_rerun(self):
        for name, text in texts("trip", "trip"):
            with self.subTest(name):
                self.assertIn("它說 Google 限流時不要重跑", text)
                self.assertIn("直接交「這次沒讀到，未確認」", text)

    def test_the_reviewer_passes_an_honest_unconfirmed(self):
        found = texts("travel-commander", "commander")
        for name, text in found:
            with self.subTest(name):
                self.assertIn("「未確認」（Google 限流、沒讀到）是正確交付，照實通過", text)
                self.assertIn("自己挑沒查過價格的具名飯店", text)


if __name__ == "__main__":
    unittest.main()
