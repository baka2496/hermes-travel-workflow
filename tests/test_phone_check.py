import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/phone-check.py"
sys.path.insert(0, str(REPO / "scripts"))  # phone-check imports travel_money from beside it
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("phone_check", SCRIPT)
phone_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phone_check)


def check(text):
    result = subprocess.run([sys.executable, str(SCRIPT), "--json"], input=text, capture_output=True,
                            text=True, encoding="utf-8")
    return result.returncode, json.loads(result.stdout)


TMP = tempfile.TemporaryDirectory()
PHOTO = Path(TMP.name) / "room.jpg"
PHOTO.write_bytes(b"real room photo")
Path(str(PHOTO) + ".source.json").write_text(json.dumps({"sha256": hashlib.sha256(b"real room photo").hexdigest()}))
MEDIA = f"MEDIA:{PHOTO.as_posix()}"


class PhoneCheckTests(unittest.TestCase):
    def test_short_bulleted_message_passes(self):
        status, report = check("**12/7 烏來一日，兩人約 NT$1,600。**\n\n• 交通：捷運加 849，NT$240\n• 湯屋：湯布苑平日 NT$600\n\n來源：官網，11/11 查詢")
        self.assertEqual(status, 0, report)
        self.assertEqual(report["problems"], [])

    def test_tables_code_blocks_and_long_bullets_fail(self):
        long_bullet = "• 營業時間：官網寫 08:30–24:00，但官方粉專 2024 年貼文寫到 02:00（最晚入場 23:00），兩邊不一致。"
        status, report = check(f"結論\n\n| 項目 | 費用 |\n```\nx\n```\n{long_bullet}")
        self.assertEqual(status, 1)
        kinds = {p["kind"] for p in report["problems"]}
        self.assertEqual(kinds, {"table", "code_block", "long_line"})

    def test_counts_wrapped_phone_lines_and_screens(self):
        status, report = check("\n".join(["• 一二三四五六七八九十" for _ in range(40)]))
        self.assertEqual(report["phone_lines"], 40)
        self.assertGreater(report["screens"], 1)
        self.assertIn("too_long", {p["kind"] for p in report["problems"]})

    def test_media_lines_are_photos_not_text(self):
        status, report = check(f"結論\n\n{MEDIA}\n• 評分：4.2 星")
        self.assertEqual(status, 0, report)
        self.assertEqual(report["phone_lines"], 3)

    def test_narration_and_internal_ids_are_flagged(self):
        status, report = check("結論\n\n我先寫好需求單，再建立看板卡。\n• 卡號 t_c07de335，狀態待處理")
        kinds = {p["kind"] for p in report["problems"]}
        self.assertEqual(kinds, {"narration", "internal_id"})

    def test_replies_over_fourteen_phone_lines_are_too_long(self):
        status, report = check("\n".join(f"• 第{i}項" for i in range(15)))
        self.assertIn("too_long", {p["kind"] for p in report["problems"]})
        status, report = check("\n".join(f"• 第{i}項" for i in range(14)))
        self.assertEqual(report["problems"], [])

    def test_text_argument_needs_no_file(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--json", "--text", "結論\\n\\n• 交通：NT$240"],
                                capture_output=True, text=True, encoding="utf-8")
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, report)
        self.assertEqual(report["phone_lines"], 3)

    def test_link_urls_do_not_count_toward_width(self):
        url = "https://www.google.com/maps/search/?api=1&query=" + "%E4%B8%89" * 20
        status, report = check(f"結論\n\n{MEDIA}\n📍 [Google 地圖・查空房]({url})\n📞 05-2679777")
        self.assertEqual(status, 0, report)
        self.assertEqual(report["phone_lines"], 4)

    def test_trailing_text_question_is_flagged(self):
        status, report = check("結論\n\n• 湯屋要不要先預約？")
        self.assertIn("text_question", {p["kind"] for p in report["problems"]})

    def test_bare_links_local_files_and_missing_photo_are_flagged(self):
        # The 2026-11-12 Alishan reply: raw URLs, a tel: link, a computer file path and no photo.
        reply = ("12/19–12/20 阿里山雙人 NT$4,980，剩 NT$20\n萬國別館\n訂房｜https://wankouhotel.com.tw/\n"
                 "電話｜tel:05-2679777\n詳細行程：C:/Users/x/hermes/shared/travel-workflow/alishan_plan.md\n"
                 "📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=x)")
        status, report = check(reply)
        self.assertEqual(status, 1)
        kinds = [p["kind"] for p in report["problems"]]
        self.assertEqual(kinds.count("bare_link"), 2)
        self.assertIn("local_path", kinds)
        self.assertIn("missing_photo", kinds)

    def test_a_recommended_place_needs_its_phone(self):
        # 2026-11-20: 「電話沒附」.
        place = f"結論\n\n{MEDIA}\n📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=x)"
        self.assertIn("missing_phone", {p["kind"] for p in check(place)[1]["problems"]})
        self.assertEqual(check(place + "\n電話：未取得")[0], 0)

    def test_booking_links_with_photo_pass(self):
        status, report = check(f"結論\n\n{MEDIA}\n• 評分 4.5\n"
                               "📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=x)\n📞 05-2679777")
        self.assertEqual(status, 0, report)

    def test_photos_need_a_matching_source_record(self):
        # 2026-11-12: a Pikachu picture was saved as the hotel photo to pass the photo check.
        fake = Path(TMP.name) / "alishan_wankou.jpg"
        fake.write_bytes(b"pikachu")
        changed = Path(TMP.name) / "changed.jpg"
        changed.write_bytes(b"swapped")
        Path(str(changed) + ".source.json").write_text(json.dumps({"sha256": "0" * 64}))
        for path in (fake, changed, Path(TMP.name) / "missing.jpg"):
            with self.subTest(path=path.name):
                status, report = check(f"結論\n\nMEDIA:{path.as_posix()}")
                self.assertIn("unverified_photo", {p["kind"] for p in report["problems"]})

    def test_no_photo_found_is_allowed_when_said(self):
        status, report = check("結論\n\n照片：未取得\n📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=x)"
                               "\n📞 03-9881892")
        self.assertEqual(status, 0, report)

    def test_wide_and_narrow_characters(self):
        status, report = check("一二三四五六七八九十一二三四五六七八九十")  # 20 full-width chars = one line
        self.assertEqual(report["phone_lines"], 1)
        status, report = check("一二三四五六七八九十一二三四五六七八九十一")
        self.assertEqual(report["phone_lines"], 2)


def board(cards, watched=None, done=None):
    """A command-center board holding *cards* (id, seconds ago, assignee[, body]); *watched* have notify subs (default all).

    *done* maps a card to how it was closed: "review" (a spawned reviewer worker) or "hand" (completed by a CLI call).
    """
    import sqlite3
    import time
    done = done or {}
    path = Path(scratch.new_dir()) / "kanban.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, created_at INTEGER, status TEXT, body TEXT)")
    db.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                   [(i, who, int(time.time()) - ago, "done" if i in done else "running", (rest or ["整趟預算: NT$40,000"])[0])
                    for i, ago, who, *rest in cards])
    db.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT)")
    subs = [c[0] for c in cards] if watched is None else watched
    db.executemany("INSERT INTO kanban_notify_subs VALUES (?, 'telegram')", [(i,) for i in subs])
    db.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, outcome TEXT)")
    db.execute("CREATE TABLE task_events (task_id TEXT, run_id INTEGER, kind TEXT)")
    for run, (card, how) in enumerate(done.items(), 1):
        db.execute("INSERT INTO task_runs VALUES (?, ?, 'commander', 'completed')", (run, card))
        kinds = ["claimed", "spawned", "completed"] if how == "review" else ["completed"]
        db.executemany("INSERT INTO task_events VALUES (?, ?, ?)", [(card, run, k) for k in kinds])
    db.commit()
    db.close()
    return path


def check_handoff(text, db, *args):
    import os
    result = subprocess.run([sys.executable, str(SCRIPT), "--json", *args], input=text, capture_output=True,
                            text=True, encoding="utf-8", env={**os.environ, "HERMES_KANBAN_DB": str(db)})
    return result.returncode, json.loads(result.stdout)


class HandoffTests(unittest.TestCase):
    # 2026-11-21: 「下一步｜派工補查機票住宿」 was sent with no card made, and nobody looked.
    REPLY = "已接單規劃大阪兩人行。\n・日期｜1/19–1/24 6天5夜\n・下一步｜派工補查機票住宿"

    def kinds(self, report):
        return [p["kind"] for p in report["problems"]]

    def test_saying_it_was_handed_on_needs_a_card(self):
        db = board([("t_3139ddb2", 60, "trip")])
        status, report = check_handoff(self.REPLY, db)
        self.assertEqual(status, 1)
        self.assertEqual(self.kinds(report), ["handoff_without_card"])
        self.assertIn("no --card", report["problems"][0]["text"])

    def test_a_card_made_just_now_backs_it(self):
        db = board([("t_3139ddb2", 60, "trip")])
        status, report = check_handoff(self.REPLY, db, "--card", "t_3139ddb2")
        self.assertEqual(status, 0, report)

    def test_a_made_up_or_old_card_does_not(self):
        db = board([("t_8b0f1a6e", 14 * 86400, "trip")])
        for card, reason in (("t_deadbeef", "is not on the board"), ("t_8b0f1a6e", "is an old card"), ("trip", "not a card ID")):
            status, report = check_handoff(self.REPLY, db, "--card", card)
            self.assertEqual(status, 1, card)
            self.assertIn(reason, report["problems"][0]["text"], card)

    def test_a_card_nobody_is_told_about_does_not(self):
        # 2026-11-21: a card made with `hermes kanban create` finished and the user was never told.
        db = board([("t_3ded90ad", 60, "trip")], watched=[])
        status, report = check_handoff(self.REPLY, db, "--card", "t_3ded90ad")
        self.assertEqual(status, 1)
        self.assertIn("no notify-subscribe", report["problems"][0]["text"])

    def test_a_card_whose_amounts_lost_their_dollar_sign_does_not(self):
        # 2026-11-22: `--body "...NT$40,000..."` in bash made the card say "整趟預算: NT0,000".
        db = board([("t_4f8382c6", 60, "trip", "城市: 福岡\n人數: 2\n整趟預算: NT0,000")])
        status, report = check_handoff(self.REPLY, db, "--card", "t_4f8382c6")
        self.assertEqual(status, 1)
        self.assertIn("says NT0,000", report["problems"][0]["text"])
        self.assertIn("single quotes", report["problems"][0]["text"])
        self.assertIn("--body '<body>'", report["problems"][0]["hint"])
        ok = board([("t_4f8382c7", 60, "trip", "整趟預算: NT$40,000（約 TWD 40000）")])
        self.assertEqual(check_handoff(self.REPLY, ok, "--card", "t_4f8382c7")[0], 0)

    def test_other_wordings_count_and_plain_replies_need_no_card(self):
        db = board([])
        for text in ("已交給 Trip Agent 查", "請 Trip Agent 重查價格", "派給專員補查", "已為你送出福岡機票與住宿查詢，查好後回報。"):
            self.assertEqual(self.kinds(check_handoff(text, db)[1]), ["handoff_without_card"], text)
        self.assertEqual(check_handoff("大阪 1/19–1/24 兩人\n機票每人 NT$13,310", db)[0], 0)


class ResultTests(unittest.TestCase):
    # 2026-11-21: the Fukuoka card's workers crashed; the commander ran `hermes -p commander kanban complete`
    # itself, so the card said done with no review, and the user got a plan nobody had checked.
    RESULT = "福岡 2/2–2/6 兩人機酒\n首選｜Plus One Nishikoen，機酒 41,310 元"

    def test_results_from_a_reviewed_card_pass(self):
        db = board([("t_3ded90ad", 600, "trip")], done={"t_3ded90ad": "review"})
        status, report = check_handoff(self.RESULT, db, "--card", "t_3ded90ad")
        self.assertEqual(status, 0, report)

    def test_a_card_closed_by_hand_is_not_reviewed(self):
        db = board([("t_b4fe3cd7", 600, "trip")], done={"t_b4fe3cd7": "hand"})
        status, report = check_handoff(self.RESULT, db, "--card", "t_b4fe3cd7")
        self.assertEqual(status, 1)
        self.assertEqual(report["problems"][0]["kind"], "unreviewed_result")
        self.assertIn("closed by hand", report["problems"][0]["text"])

    def test_a_card_still_running_has_no_results(self):
        db = board([("t_b4fe3cd7", 600, "trip")])
        status, report = check_handoff(self.RESULT, db, "--card", "t_b4fe3cd7")
        self.assertEqual(status, 1)
        self.assertIn("not done", report["problems"][0]["text"])

    def test_an_empty_lookup_is_not_called_not_on_sale(self):
        # 2026-11-21 message 11045: 「機票與房價尚未開賣」 for March 2027; Google had Tigerair at NT$12,281.
        for text in ("福岡 2 人旅遊規劃已完成，機票與房價尚未開賣。", "機票｜遠期尚未開賣", "待航空公司開賣後重新查價",
                     "房價還沒開放預訂"):
            status, report = check_handoff(text, board([]))
            self.assertIn("guessed_not_on_sale", [p["kind"] for p in report["problems"]], text)
        status, report = check_handoff("福岡 3/2–3/6 兩人\n機票這次沒讀到價格，未確認", board([]))
        self.assertEqual(status, 0, report)

    def test_flight_hotel_card_reply_requires_times_and_links(self):
        # 2026-11-25: t_0569fddd's fukuoka_plan.md had flight times and booking links,
        # but the Telegram reply sent only bare totals.
        db = board([("t_0569fddd", 600, "trip")], done={"t_0569fddd": "review"})
        attach = db.parent / "attachments" / "t_0569fddd"
        attach.mkdir(parents=True)
        (attach / "fukuoka_plan.md").write_text(
            "## ✈️ 來回航班\n- 去程：華航 CI116 16:40→20:00\n- 回程：華航 CI111 11:00→12:30\n"
            "[機票訂票連結](https://www.google.com/travel/flights/booking?tfs=abc)\n"
            "## 🏨 住宿\nFukuoka UBL Hotel 雙人房 4晚 NT$7,960\n"
            "[住宿頁面連結](https://www.google.com/travel/search?q=Fukuoka+UBL+Hotel)\n",
            encoding="utf-8",
        )
        bare = ("06/05–06/09 福岡五天四夜（2人）\n"
                "• 機票｜華航來回 NT$27,508\n"
                "• 住宿｜UBL四晚 NT$7,960\n"
                "• 總計｜NT$35,468（預算剩 NT$9,532）")
        status, report = check_handoff(bare, db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertEqual([p["kind"] for p in report["problems"]], ["card_missing_details"])
        self.assertIn("去回程航班時間／班號", report["problems"][0]["text"])
        self.assertIn("可點擊的機票與住宿連結", report["problems"][0]["text"])

        complete = ("06/05–06/09 福岡五天四夜（2人）\n"
                    "總計 NT$35,468（預算剩 NT$9,532）\n"
                    "✈️ 華航 2人 NT$27,508（含托運行李）\n"
                    "• 去 06/05 CI116 16:40→20:00\n"
                    "• 回 06/09 CI111 11:00→12:30\n"
                    "• [機票訂票連結](https://www.google.com/travel/flights/booking?tfs=abc)\n"
                    "🏨 Fukuoka UBL Hotel 雙人房 4晚 NT$7,960\n"
                    "• 中央區｜⭐4.3｜[住宿頁面連結](https://www.google.com/travel/search?q=Fukuoka+UBL+Hotel)")
        status, report = check_handoff(complete, db, "--card", "t_0569fddd")
        self.assertEqual(status, 0, report)

        empty = "06/05–06/09 福岡五天四夜（2人）\n這次沒讀到價格，未確認。"
        status, report = check_handoff(empty, db, "--card", "t_0569fddd")
        self.assertEqual(status, 0, report)

    def fukuoka_card(self):
        db = board([("t_0569fddd", 600, "trip")], done={"t_0569fddd": "review"})
        attach = db.parent / "attachments" / "t_0569fddd"
        attach.mkdir(parents=True)
        (attach / "fukuoka_plan.md").write_text(
            "- **去程**：2027-06-05 華航 CI116 16:40（TPE）→ 20:00（FUK）\n"
            "  - 飛行時間：2h20，預計約 21:15 抵達飯店\n"
            "- **回程**：2027-06-09 華航 CI111 11:00（FUK）→ 12:30（TPE）\n"
            "[機票訂票連結](https://www.google.com/travel/flights/booking?tfs=abc)\n"
            "Fukuoka UBL Hotel 雙人房 4晚 NT$7,960 入住 15:00\n"
            "[住宿頁面連結](https://www.google.com/travel/search?q=Fukuoka+UBL+Hotel)\n", encoding="utf-8")
        return db

    def test_an_unconfirmed_side_item_does_not_skip_the_detail_check(self):
        # Antigravity's review (2026-11-25): any 「未確認」 skipped the check, so bare totals plus
        # 「早餐未確認」 went out without times or links.
        bare = "06/05–06/09 福岡（2人）\n• 機票 NT$27,508\n• 住宿 NT$7,960（早餐未確認）\n總計 NT$35,468"
        status, report = check_handoff(bare, self.fukuoka_card(), "--card", "t_0569fddd")
        self.assertEqual([p["kind"] for p in report["problems"]], ["card_missing_details"])

    def test_times_written_right_after_chinese_are_seen(self):
        # Antigravity's review (2026-11-25): \b does not split Chinese from digits, so 「去程16:40」 was missed.
        reply = ("06/05–06/09 福岡（2人）總計 NT$35,468\n"
                 "去程16:40起飛、20:00抵達；回程11:00起飛\n"
                 "[機票訂票連結](https://www.google.com/travel/flights/booking?tfs=abc)｜"
                 "[住宿頁面連結](https://www.google.com/travel/search?q=Fukuoka+UBL+Hotel)")
        status, report = check_handoff(reply, self.fukuoka_card(), "--card", "t_0569fddd")
        self.assertEqual(status, 0, report)

    def test_half_the_details_is_not_enough(self):
        # Antigravity's review (2026-11-25): one time of any kind (here the 15:00 check-in) and one link
        # passed, though the return flight and the hotel link were missing.
        reply = ("06/05–06/09 福岡（2人）總計 NT$35,468\n• 去 16:40→20:00\n• 15:00 入住\n"
                 "[機票訂票連結](https://www.google.com/travel/flights/booking?tfs=abc)")
        status, report = check_handoff(reply, self.fukuoka_card(), "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("回程", report["problems"][0]["text"])
        self.assertIn("住宿", report["problems"][0]["text"])

    def card_with(self, attachment):
        db = board([("t_0569fddd", 600, "trip")], done={"t_0569fddd": "review"})
        attach = db.parent / "attachments" / "t_0569fddd"
        attach.mkdir(parents=True)
        (attach / "plan.md").write_text(attachment, encoding="utf-8")
        return db

    def test_official_airline_and_hotel_sites_count_by_their_link_text(self):
        # Antigravity's review of 2f15bdc: china-airlines.com / toyoko-inn.com have no "flight"/"hotel" in them.
        db = self.card_with("- 去程：CI116 16:40→20:00\n- 回程：CI111 11:00→12:30\n"
                            "[華航官網訂票](https://www.china-airlines.com/tw/zh/booking?x=1)\n"
                            "[東橫INN 博多口](https://www.toyoko-inn.com/search/detail/00059)\n")
        ok = ("福岡 2人 總計 NT$35,468\n去 16:40→20:00｜回 11:00→12:30\n"
              "[華航官網訂票](https://www.china-airlines.com/tw/zh/booking?x=1)｜[東橫INN 博多口](https://www.toyoko-inn.com/search/detail/00059)")
        self.assertEqual(check_handoff(ok, db, "--card", "t_0569fddd")[0], 0)
        no_flight = ("福岡 2人 總計 NT$35,468\n去 16:40→20:00｜回 11:00→12:30\n"
                     "[東橫INN 博多口](https://www.toyoko-inn.com/search/detail/00059)")
        status, report = check_handoff(no_flight, db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("機票", report["problems"][0]["text"])

    def test_link_kind_by_text_alone_and_by_site_alone(self):
        # Antigravity's review of 3ed6613: the test above passes on either path; check each on its own,
        # and that short words only match whole words / whole host parts.
        kind = phone_check.link_kind
        self.assertEqual(kind("https://example.com/a", "華航訂票"), "機票")          # text only
        self.assertEqual(kind("https://example.com/b", "東橫INN 博多口"), "住宿")
        self.assertEqual(kind("https://example.com/c", "Holiday Inn Osaka"), "住宿")
        self.assertEqual(kind("https://www.china-airlines.com/x", "官網"), "機票")  # site only
        self.assertEqual(kind("https://www.toyoko-inn.com/x", "官網"), "住宿")
        self.assertEqual(kind("https://www.ana.co.jp/x"), "機票")
        self.assertEqual(kind("https://www.finnair.com/x"), "機票")                  # not 住宿 via "inn"
        self.assertEqual(kind("https://www.hotel-hana.co.jp/"), "住宿")              # not 機票 via "ana.co"
        self.assertIsNone(kind("https://example.com/dinner", "晚餐 dinner"))
        # Real attachment links (2026-11-25) that were left unsorted before:
        # Neutral link text, so the address itself is what is tested (Antigravity's review of e363c3c).
        self.assertEqual(kind("https://www.google.com/travel/lodging/clk?x=1", "官網"), "住宿")
        self.assertEqual(kind("https://deals.vio.com/", ""), "住宿")
        self.assertEqual(kind("https://www.priceline.com", "預訂"), "住宿")
        self.assertEqual(kind("https://www.jtb.co.jp/kokunai-hotel/htl/6492106/plan/", "官網方案"), "住宿")

    def test_sites_selling_both_are_sorted_by_the_address_first(self):
        # Antigravity's review of e363c3c: the brand name won over the address.
        kind = phone_check.link_kind
        self.assertEqual(kind("https://flights.google.com/x", "Google"), "機票")
        self.assertEqual(kind("https://flights.booking.com/x", "Booking.com"), "機票")
        self.assertEqual(kind("https://www.expedia.com/flights?x=1", "Expedia"), "機票")
        self.assertEqual(kind("https://www.expedia.com/hotels?x=1", "Expedia"), "住宿")
        self.assertEqual(kind("https://www.expedia.com.tw/", "Expedia"), "住宿")  # real attachment links
        self.assertEqual(kind("https://www.agoda.com/partners/partnersearch.aspx?hid=2264692", "Agoda"), "住宿")
        self.assertEqual(kind("https://www.agoda.com/flights/x", "Agoda"), "機票")
        self.assertEqual(kind("https://www.skyscanner.com.tw/hotels/x", "Skyscanner"), "住宿")
        self.assertEqual(kind("https://www.skyscanner.com.tw/transport/flights/tpe/fuk/", "Skyscanner"), "機票")
        self.assertEqual(kind("https://www.britishairways.com/x", ""), None)  # one word, not "airways"
        self.assertEqual(kind("https://www.klook.com/activity/123", "Klook 門票"), None)
        self.assertEqual(kind("https://www.klook.com/hotels/123", "Klook"), "住宿")
        self.assertEqual(kind("https://example.com/x", "booking"), None)  # plain "booking" is not a hotel

    def test_a_blank_line_under_a_leg_heading_keeps_the_leg(self):
        # Antigravity's review of 3ed6613: 「### 回程」, a blank line, then the times was not read.
        db = self.card_with("### 去程\n\n- CI116 16:40 → 20:00\n\n### 回程\n\n- CI111 11:00 → 12:30\n")
        status, report = check_handoff("福岡 NT$27,508\n去 16:40→20:00\n15:00 入住", db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("回程", report["problems"][0]["text"])

    def test_a_sentence_naming_a_leg_does_not_take_the_next_line(self):
        # Antigravity's review of 3ed6613: 「- 回程含 20kg 行李」 then 「- 15:00 入住」 made 15:00 a return time.
        db = self.card_with("- 去程：CI116 16:40→20:00\n- 回程：CI111 11:00→12:30\n- 回程含 20kg 行李\n- 15:00 入住\n")
        status, report = check_handoff("福岡 NT$27,508\n去 16:40→20:00\n15:00 入住", db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("回程", report["problems"][0]["text"])

    def test_headings_with_more_than_the_leg_name_count(self):
        # Antigravity's review of e363c3c: 「### 去程 06/05」「### ✈️ 回程航班」 were not read as headings.
        db = self.card_with("### 去程 06/05\n- CI116 16:40 → 20:00\n\n### ✈️ 回程航班\n- CI111 11:00 → 12:30\n")
        status, report = check_handoff("福岡 NT$27,508\n去 16:40→20:00\n15:00 入住", db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("回程", report["problems"][0]["text"])
        ok = "福岡 NT$27,508\n去 16:40→20:00｜回 11:00→12:30"
        self.assertNotIn("card_missing_details",
                         [p["kind"] for p in check_handoff(ok, db, "--card", "t_0569fddd")[1]["problems"]])

    def test_a_note_on_the_same_leg_keeps_the_section(self):
        # Antigravity's review of e363c3c: 「- 回程含 20kg 行李」 under 「### 回程」 dropped the time below it.
        db = self.card_with("### 去程\n- CI116 16:40 → 20:00\n\n### 回程\n- 回程含 20kg 行李\n- CI111 11:00 → 12:30\n")
        status, report = check_handoff("福岡 NT$27,508\n去 16:40→20:00\n15:00 入住", db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("回程", report["problems"][0]["text"])

    def test_times_match_with_or_without_a_leading_zero(self):
        # Antigravity's review of 2f15bdc: 09:05 in the attachment and 9:05 in the reply did not match.
        db = self.card_with("- 去程：IT201 09:05→12:30\n- 回程：IT202 13:30→16:05\n")
        status, report = check_handoff("大阪 NT$12,000\n去 9:05 起飛｜回 13:30 起飛", db, "--card", "t_0569fddd")
        self.assertEqual(status, 0, report)

    def test_leg_times_on_the_lines_under_a_heading_are_used(self):
        # Antigravity's review of 2f15bdc: 「### 去程」 with the times on the next line was not read.
        db = self.card_with("### 去程\nCI116 16:40 → 20:00\n\n### 回程\nCI111 11:00 → 12:30\n")
        status, report = check_handoff("福岡 NT$27,508\n去 16:40→20:00\n15:00 入住", db, "--card", "t_0569fddd")
        self.assertEqual(status, 1)
        self.assertIn("回程", report["problems"][0]["text"])

    def test_every_real_attachment_passes_when_its_details_are_copied(self):
        # The stricter rule must not block a reply that does carry the attachment's details.
        import os
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/kanban/boards/command-center/attachments"
        if not root.is_dir():
            self.skipTest("no live attachments on this machine")
        for card_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            texts = [f.read_text(encoding="utf-8", errors="ignore") for f in sorted(card_dir.iterdir())
                     if f.suffix.lower() in (".md", ".txt")]
            if not texts:
                continue
            db = board([(card_dir.name, 600, "trip")], done={card_dir.name: "review"})
            copy = db.parent / "attachments" / card_dir.name
            copy.mkdir(parents=True)
            (copy / "plan.md").write_text("\n".join(texts), encoding="utf-8")
            status, report = check_handoff("\n".join(texts), db, "--card", card_dir.name)
            self.assertNotIn("card_missing_details", [p["kind"] for p in report["problems"]], card_dir.name)

    def test_the_live_fukuoka_card_is_caught(self):
        import os
        live = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/kanban/boards/command-center/kanban.db"
        if not live.is_file():
            self.skipTest("no live board on this machine")
        status, report = check_handoff(self.RESULT, live, "--card", "t_b4fe3cd7")
        self.assertIn("closed by hand", report["problems"][0]["text"])


if __name__ == "__main__":
    unittest.main()
