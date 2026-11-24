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

    def test_the_live_fukuoka_card_is_caught(self):
        import os
        live = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/kanban/boards/command-center/kanban.db"
        if not live.is_file():
            self.skipTest("no live board on this machine")
        status, report = check_handoff(self.RESULT, live, "--card", "t_b4fe3cd7")
        self.assertIn("closed by hand", report["problems"][0]["text"])


if __name__ == "__main__":
    unittest.main()
