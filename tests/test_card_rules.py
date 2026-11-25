"""What the 2026-11-22 Telegram test of a Fukuoka card found, kept from coming back.

The card's budget arrived as "NT0,000" (bash ate `$4` inside double quotes); both of Trip's
`plan` runs met Google's limit 11 seconds apart; and the reviewer sent the correct 「未確認」
back asking Trip to pick hotels it had not priced, which sent Trip back to Google.
"""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import setup  # noqa: E402  (roles share their card rules through {{INCLUDE:}} files)
HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles"


def texts(repo_role, live_profile):
    found = [(f"roles/{repo_role}.md",
              setup.expand_includes((ROOT / "roles" / f"{repo_role}.md").read_text(encoding="utf-8")))]
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

    def test_the_reviewer_rejects_prices_plan_did_not_print(self):
        # The same card's second try was all estimates with a made-up NT$70,000 budget, and passed.
        for name, text in texts("travel-commander", "commander"):
            with self.subTest(name):
                self.assertIn("價格只能是 `plan` 印的", text)
                self.assertIn("預算和成果對不上，就退回", text)

    def test_a_broken_card_budget_is_reported_not_replaced(self):
        for name, text in texts("trip", "trip"):
            with self.subTest(name):
                self.assertIn("不要自己找別的預算", text)

    def test_plan_is_not_rerun_when_first_run_has_prices(self):
        # 2026-11-25 Osaka card: Trip ran `deals plan` a second time (+77s) after the first succeeded.
        for name, text in texts("trip", "trip"):
            with self.subTest(name):
                self.assertIn("第一次 `plan` 已印出機票與住宿價格時直接寫報告交付，不准重跑第二次 `plan`", text)

    def test_reviewer_does_not_read_trip_bot_code_or_run_cli(self):
        # 2026-11-25 Osaka card: reviewer wasted ~95s reading trip-bot code and running sqlite3/deals.
        for name, text in texts("travel-commander", "commander"):
            with self.subTest(name):
                self.assertIn("覆核只看卡片內文與交付檔", text)
                self.assertIn("無誤就直接結案", text)
                plain = text.replace("`", "")
                self.assertIn("不要搜或讀 trip-bot 程式碼、不要跑 sqlite3 或 deals", plain)

    def test_a_done_card_is_always_reported(self):
        # The same card: after compressing, the commander answered its "done" notice with NO_REPLY.
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("「已完成」通知就是新成果：一定要讀成果、照規則回報使用者，絕不回 `NO_REPLY`", text)
                self.assertIn("show <卡號>`，不加 `--json`", text)
                self.assertNotIn("show <卡號> --json", text)

    def test_card_report_requires_flight_times_hotel_and_links(self):
        # 2026-11-25: Fukuoka card reply omitted flight times, room type, and booking links.
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("回報機酒查價卡必附去回程班號與時間、行李說明、飯店全名與房型", text)
                self.assertIn("不能只寫總價", text)

    def test_opportunities_is_searched_not_read_whole(self):
        # The retry: the commander read all 38K characters of opportunities.json first and compressed.
        for name, text in texts("travel-commander", "project-manager"):
            with self.subTest(name):
                self.assertIn("先用 `search_files` 在 opportunities.json 找 `pending_question`", text)
                self.assertIn("offset/limit 只讀那一筆", text)
        self.assertNotIn("與同目錄的 `opportunities.json`。這兩份資料由你維護。",
                         (ROOT / "roles" / "travel-commander.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
