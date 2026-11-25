import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import unittest
from urllib.parse import unquote

import scratch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reply_check", REPO / "plugins/travel-reply-check/__init__.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

COST = ("總計 NT$2,932，預算剩 NT$3,068（全部未確認）\r\n"
        "• 客運來回 NT$392（NT$98×4），未確認（來源網頁上沒看到這個價格）\r\n"
        "• 山口溫泉飯店週六雙人房 NT$1,600，未確認（來源網頁上沒看到這個價格）\r\n"
        "• 兩天餐飲小吃估算 NT$800，未確認")


def home_with(*outputs, session="s1", tool="trip-cost.py", home=None):
    """A Hermes home whose session ran `tool` once per output, in order."""
    path = Path(home or scratch.new_dir("hermes-reply-check-")) / "profiles" / "travel-commander" / "state.db"
    home = path.parents[2]
    if not path.exists():
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
                   "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        db.execute("INSERT INTO messages (session_id, role, content) VALUES ('s1', 'user', '排方案')")
        db.commit()
        db.close()
    db = sqlite3.connect(path)
    for output in outputs:
        n = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        call = {"id": f"c{n}", "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": f'"python" "…/{tool}" x=1'})}}
        db.execute("INSERT INTO messages (session_id, role, tool_calls) VALUES (?, 'assistant', ?)",
                   (session, json.dumps([call])))
        db.execute("INSERT INTO messages (session_id, role, content, tool_call_id, tool_name) VALUES (?, 'tool', ?, ?, "
                   "'terminal')", (session, json.dumps({"output": output, "exit_code": 0}), f"c{n}"))
    db.commit()
    db.close()
    return home


def ask(home, question, session="s1"):
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        return check.pre_tool_call("clarify", {"questions": [{"question": question, "choices": ["好"]}]},
                                   session_id=session)
    finally:
        if old is None:
            del os.environ["HERMES_HOME"]
        else:
            os.environ["HERMES_HOME"] = old


GOOD = ("11/21–11/22 礁溪兩天一夜（2人）\n總計 NT$2,932，預算剩 NT$3,068（全部未確認）\n"
        "首選：山口溫泉飯店（週六雙人房 NT$1,600，未確認）\n下一步想怎麼做？")


class ReplyCheckTests(unittest.TestCase):
    def test_total_must_be_the_tools_headline(self):
        # 2026-11-14 (Telegram test 10): the question said "（NT$800 未確認）" while the tool printed
        # "（全部未確認）" — clarify had gone out in the same batch as trip-cost.py.
        home = home_with(COST)
        result = ask(home, GOOD.replace("（全部未確認）", "（NT$800 未確認）"))
        self.assertEqual(result["action"], "block")
        self.assertIn("「總計 NT$2,932，預算剩 NT$3,068（全部未確認）」", result["message"])
        self.assertIsNone(ask(home, GOOD))

    def test_held_until_the_tool_has_run(self):
        result = ask(home_with(), GOOD)
        self.assertEqual(result["action"], "block")
        self.assertIn("先單獨執行 trip-cost.py", result["message"])

    def test_the_latest_run_counts(self):
        older = "總計 NT$2,432，預算剩 NT$3,568（NT$800 未確認）\n• 兩天餐飲 NT$800，未確認"
        home = home_with(older, COST)
        self.assertIsNone(ask(home, GOOD))
        self.assertEqual(ask(home, GOOD.replace("NT$2,932，預算剩 NT$3,068（全部未確認）",
                                                "NT$2,432，預算剩 NT$3,568（NT$800 未確認）"))["action"], "block")

    def test_an_unconfirmed_price_must_say_so(self):
        result = ask(home_with(COST), GOOD.replace("NT$1,600，未確認", "NT$1,600"))
        self.assertEqual(result["action"], "block")
        self.assertIn("山口溫泉飯店（週六雙人房 NT$1,600）", result["message"])
        # A larger figure that merely starts with the same digits is not that price.
        self.assertIsNone(ask(home_with(COST), GOOD + "\n備案：龍山 NT$1,6000 套房（未確認來源）"))

    def test_a_photo_line_cannot_go_into_a_button_question(self):
        # 2026-11-14 (Telegram test 13b): the phone showed "MEDIA:C:/…/photos/132e773d353ebaab.jpg"
        # as text inside the button question instead of a photo.
        question = GOOD.replace("首選：", "MEDIA:C:/hermes/shared/travel-workflow/photos/x.jpg\n首選：")
        result = ask(home_with(COST), question)
        self.assertEqual(result["action"], "block")
        self.assertIn("不能放 MEDIA", result["message"])
        self.assertEqual(ask(home_with(), "要哪一間？\nMEDIA:C:/x.jpg")["action"], "block")

    def test_other_tools_and_plain_questions_pass(self):
        home = home_with()
        self.assertIsNone(ask(home, "要排 12/6 還是 12/13？"))
        self.assertIsNone(check.pre_tool_call("terminal", {"command": "總計 NT$1"}, session_id="s1"))

    def test_another_conversations_run_does_not_count(self):
        home = home_with(COST, session="other")
        self.assertEqual(ask(home, GOOD)["action"], "block")

    def test_a_broken_check_lets_the_answer_through(self):
        home = home_with(COST)
        (home / "profiles" / "travel-commander" / "state.db").write_bytes(b"not a database")
        self.assertIsNone(ask(home, GOOD.replace("全部", "NT$1 ")))

    def test_installed_for_every_travel_profile(self):
        import importlib
        setup_spec = importlib.util.spec_from_file_location("workflow_setup_rc", REPO / "setup.py")
        setup = importlib.util.module_from_spec(setup_spec)
        setup_spec.loader.exec_module(setup)
        source = (REPO / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"plugins", "enable", REPLY_CHECK', source)
        self.assertEqual(setup.REPLY_CHECK, "travel-reply-check")
        for name in setup.REPLY_CHECK_FILES:
            self.assertTrue((REPO / "plugins" / setup.REPLY_CHECK / name).is_file())


MAPS = "https://www.google.com/maps/search/?api=1&query=%E7%83%8F%E4%BE%86%E6%B9%AF%E5%B8%83%E8%8B%91%E6%BA%AB%E6%B3%89%E9%A4%A8"
PLAN = ("12/7 烏來溫泉一日遊，兩人總價 NT$1,600，預算剩 NT$400\n"
        "總計 NT$1,600，預算剩 NT$400（NT$760 未確認）\n首選：烏來湯布苑溫泉館（雙人溪景湯屋）\n"
        f"照片：未取得\n📍 [Google 地圖・查空房]({MAPS})\n🏨 [官網訂房](https://www.yufuinspring.com.tw/)\n"
        "📞 02-2661-8001\n備案：12/6–12/7 礁溪山口兩天一夜\n下一步想如何安排？")
CHOICES = ["維持 12/7 烏來一日遊", "改看 12/6–12/7 礁溪兩天一夜", "改看其他溫泉地點"]


def timed_out(question=PLAN, choices=CHOICES):
    return json.dumps({"responses": [{"question": question, "choices_offered": choices, "status": "unanswered",
                                      "user_response": None}],
                       "outcome": "timed_out", "notice": "[user did not respond within 10m]"}, ensure_ascii=False)


class ButtonQuestionTests(unittest.TestCase):
    def test_links_in_a_button_question_are_written_out_short(self):
        # 2026-11-15: the phone showed "[Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=
        # %E7%83%8F…)" — a clarify question is plain text, so markdown links show as typed.
        result = check.pre_tool_call("clarify", {"questions": [{"question": PLAN, "choices": CHOICES}]})
        self.assertEqual(result["action"], "modify")
        text = result["args"]["questions"][0]["question"]
        self.assertIn("📍 Google 地圖・查空房：https://maps.google.com/?q=烏來湯布苑溫泉館\n", text)
        self.assertIn("🏨 官網訂房：https://www.yufuinspring.com.tw/\n", text)
        self.assertNotIn("](", text)
        self.assertEqual(result["args"]["questions"][0]["choices"], CHOICES)
        self.assertIsNone(check.pre_tool_call("clarify", {"questions": [{"question": "要哪天？", "choices": ["a"]}]}))

    def test_a_timed_out_question_is_not_sent_again(self):
        # 2026-11-15 21:47 → 21:57: nobody pressed a button for 10 minutes, then the reply repeated the
        # whole plan as a second message under buttons that had stopped working.
        noted = json.loads(check.transform_tool_result("clarify", {}, timed_out(), session_id="s9", turn_id="t1"))
        self.assertIn("不要重貼方案", noted["note"])
        reply = PLAN.rsplit("\n", 1)[0] + "\nMEDIA:C:/hermes/photos/a.jpg"
        out = check.transform_llm_output(response_text=reply, session_id="s9", turn_id="t1")
        self.assertEqual(out, "MEDIA:C:/hermes/photos/a.jpg\n上面的按鈕問題 10 分鐘沒選，按鈕已失效。"
                              "想繼續就直接回覆編號或打字：\n1. 維持 12/7 烏來一日遊\n"
                              "2. 改看 12/6–12/7 礁溪兩天一夜\n3. 改看其他溫泉地點")
        # Only that turn's reply; the next turn is left alone.
        self.assertIsNone(check.transform_llm_output(response_text=reply, session_id="s9", turn_id="t2"))

    def test_a_short_reply_or_an_answer_is_left_alone(self):
        check.transform_tool_result("clarify", {}, timed_out(), session_id="s9", turn_id="t1")
        self.assertIsNone(check.transform_llm_output(response_text="按鈕已失效，回覆 1–3 即可。",
                                                     session_id="s9", turn_id="t1"))
        answered = json.dumps({"responses": [], "outcome": "submitted"})
        self.assertIsNone(check.transform_tool_result("clarify", {}, answered, session_id="s9", turn_id="t3"))
        self.assertIsNone(check.transform_llm_output(response_text=PLAN, session_id="s9", turn_id="t3"))

    def test_another_conversation_is_left_alone(self):
        check.transform_tool_result("clarify", {}, timed_out(), session_id="s9", turn_id="t1")
        self.assertIsNone(check.transform_llm_output(response_text=PLAN, session_id="other", turn_id="t1"))
        check.TIMED_OUT.clear()

    def test_booking_lines_must_be_what_the_tool_printed(self):
        # 2026-11-16 (Telegram): booking-links.py printed the map search for 「山口溫泉飯店」, and the
        # question showed 「山口溫泉館」, a link the commander had typed itself.
        printed = ("📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=%E5%B1%B1%E5%8F%A3"
                   "%E6%BA%AB%E6%B3%89%E9%A3%AF%E5%BA%97)\n"
                   "🛏 [Booking](https://www.booking.com/hotel/tw/shan-kou-wen-quan-fan-dian.zh-tw.html)")
        home = home_with(printed, tool="booking-links.py")
        typed = ("12/6–12/7 礁溪兩天一夜\n📍 [Google 地圖](https://www.google.com/maps/search/?api=1&query="
                 "%E5%B1%B1%E5%8F%A3%E6%BA%AB%E6%B3%89%E9%A4%A8)\n"
                 "🏨 [Booking 訂房](https://www.booking.com/hotel/tw/shan-kou-wen-quan-fan-dian.zh-tw.html)\n下一步？")
        result = ask(home, typed)
        self.assertEqual(result["action"], "block")
        self.assertIn("山口溫泉館", unquote(result["message"]).split("它印過的是")[0])
        self.assertNotIn("Booking 訂房", result["message"].split("它印過的是")[0])
        self.assertIn(printed, result["message"])
        # Pasted as printed (or already written short), it goes out, with its links written out.
        self.assertEqual(ask(home, "方案\n" + printed + "\n下一步？")["action"], "modify")
        short = "方案\n📍 Google 地圖：https://maps.google.com/?q=山口溫泉飯店\n下一步？"
        self.assertIsNone(ask(home, short))

    def test_a_phone_must_be_one_the_tool_printed(self):
        home = home_with("📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=x)\n📞 03-9888699",
                         tool="booking-links.py")
        self.assertIsNone(ask(home, "方案\n📞 03-9888699\n下一步？"))
        result = ask(home, "方案\n📞 03-9888698\n下一步？")
        self.assertEqual(result["action"], "block")
        self.assertIn("📞 03-9888698", result["message"])

    def test_links_without_a_tool_run_are_held_back(self):
        result = ask(home_with(), "方案\n🏨 [官網](https://www.example-hotel.com.tw/)\n下一步？")
        self.assertEqual(result["action"], "block")
        self.assertIn("還沒有執行 booking-links.py", result["message"])

    def test_other_lines_and_addresses_pass(self):
        home = home_with()
        self.assertIsNone(ask(home, "📍 集合：台北車站東三門\n來源：https://www.metro.taipei/\n要哪天？"))

    def test_information_is_not_a_choice(self):
        # 2026-11-19: 「看費用明細」「看烏來一日來回交通動線」 were buttons, so seeing the costs took a press.
        for choice in ("看費用明細", "看烏來一日來回交通動線", "查看完整行程"):
            result = check.pre_tool_call("clarify", {"questions": [{"question": "要哪天？",
                                                                    "choices": ["12/6", choice]}]}, session_id="s5")
            self.assertEqual(result["action"], "block")
            self.assertIn(f"「{choice}」", result["message"])
        self.assertIsNone(check.pre_tool_call("clarify", {"questions": [{"question": "要哪天？", "choices": [
            "改看礁溪兩天一夜", "維持 12/7 烏來"]}]}, session_id="s5"))

    def test_one_button_question_per_message(self):
        # 2026-11-19: after 「維持烏來一日首選」 was pressed, the same turn asked again (「等到 11/30 提醒
        # 再確認」…), and after that answer asked a third time.
        question = {"questions": [{"question": "已鎖定 12/07 烏來\n下一步？", "choices": ["等 11/30 提醒", "改礁溪"]}]}
        self.assertIsNone(check.pre_tool_call("clarify", question, session_id="s6", turn_id="t1"))
        answered = json.dumps({"responses": [{"question": "x", "status": "answered", "user_response": "維持"}],
                               "outcome": "submitted"})
        self.assertIsNone(check.transform_tool_result("clarify", {}, answered, session_id="s6", turn_id="t1"))
        result = check.pre_tool_call("clarify", question, session_id="s6", turn_id="t1")
        self.assertEqual(result["action"], "block")
        self.assertIn("不要再用按鈕問下一步", result["message"])
        # The user's next message may get a button question again.
        self.assertIsNone(check.pre_tool_call("clarify", question, session_id="s6", turn_id="t2"))

    def test_the_question_mark_is_not_doubled(self):
        # 2026-11-19: the phone showed "❓ ❓ 12/07（一）烏來…" — Telegram adds its own ❓.
        result = check.pre_tool_call("clarify", {"questions": [{"question": "❓ 12/07 烏來\n要哪個？",
                                                                "choices": ["a", "b"]}]})
        self.assertEqual(result["args"]["questions"][0]["question"], "12/07 烏來\n要哪個？")

    def test_an_ordinary_reply_gets_the_tools_total_and_unconfirmed_marks(self):
        # 2026-11-19 (Telegram, after the button change): the plan went out as an ordinary reply that
        # the clarify checks never saw, with its own total wording and no 未確認 on 公車 or 餐飲.
        output = ("總計 NT$1,600，預算剩 NT$400（NT$820 未確認）\n• 捷運輔大至新店來回 NT$180（NT$90×2）\n"
                  "• 新店客運849來回 NT$60（NT$30×2），未確認\n• 烏來湯布苑雙人溪景湯屋平日 NT$600\n"
                  "• 老街餐飲估算 NT$760（NT$380×2），未確認")
        home = home_with(output)
        reply = ("2026-12-07 烏來溫泉一日遊（2人）\n總計 NT$1,600（預算剩 NT$400；含未確認 NT$820）\n"
                 "交通：捷運來回 NT$180、公車來回 NT$60（NT$30×2）\n泡湯：烏來湯布苑溪景湯屋平日 NT$600\n"
                 "餐飲：老街小吃估算 NT$760\n待確認：現場候位狀況")
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            fixed = check.transform_llm_output(response_text=reply, session_id="s1", turn_id="t1")
            self.assertEqual(fixed, "2026-12-07 烏來溫泉一日遊（2人）\n" + output + "\n待確認：現場候位狀況")
            # 2026-11-19, second run: 「捷運及公車 NT$240」 hid the unconfirmed NT$60 inside a merged line.
            merged = ("12/7（一）烏來一日遊（兩人）\n總計 NT$1,600，預算剩 NT$400（NT$820 未確認）\n"
                      "• 交通：捷運及公車 NT$240\n• 泡湯：雙人溪景湯屋 NT$600\n• 餐飲：老街餐食估算 NT$760（未確認）\n"
                      "照片：未取得\n📞 02-2661-8001")
            self.assertEqual(check.transform_llm_output(response_text=merged, session_id="s1", turn_id="t1"),
                             "12/7（一）烏來一日遊（兩人）\n" + output + "\n照片：未取得\n📞 02-2661-8001")
            # Amounts mentioned elsewhere in the reply still get their mark.
            later = "總計 NT$1,600，預算剩 NT$400\n照片：未取得\n提醒：餐飲 NT$760 現場付"
            self.assertTrue(check.transform_llm_output(response_text=later, session_id="s1", turn_id="t1")
                            .endswith("提醒：餐飲 NT$760（未確認） 現場付"))
            # Already right: left alone.
            self.assertIsNone(check.transform_llm_output(response_text=fixed, session_id="s1", turn_id="t1"))
            # A total no run printed (say, a backup plan priced earlier) is not touched.
            other = "備案：礁溪\n總計 NT$2,332，超出預算 NT$332\n餐飲 NT$760"
            self.assertIsNone(check.transform_llm_output(response_text=other, session_id="s1", turn_id="t1"))
        finally:
            if old is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old

    def test_items_listed_away_from_the_total_are_not_shown_twice(self):
        # 2026-11-19 (Telegram): the reply put its item lines under the photo, not under 總計. The tool's
        # lines went in under 總計 and the reply's copy stayed, so every item showed twice, and the
        # confirmed 溪景湯屋 NT$600 got 「（未確認）」 because the meal was NT$600 too.
        output = ("總計 NT$1,440，預算剩 NT$560（NT$840 未確認）\r\n• 油錢來回估算 NT$120，未確認\r\n"
                  "• 立體停車場停車費 NT$120（NT$30×4），未確認\r\n• 湯布苑雙人溪景湯屋平日 NT$600（每1小時）\r\n"
                  "• 老街午餐及點心兩人 NT$600，未確認")
        reply = ("12/7 烏來 兩人\n總計 NT$1,440，預算剩 NT$560（NT$840 未確認）\nMEDIA:x.jpg\n📷 溪景湯屋（www.spa.example）\n"
                 "騎車或開車約 50–60 分鐘\n• 湯布苑雙人溪景湯屋平日 NT$600（每1小時）\n• 老街午餐及點心兩人 NT$600，未確認\n"
                 "• 油錢來回估算 NT$120，未確認\n• 立體停車場停車費 NT$120（NT$30×4），未確認\n📞 02-2661-8001")
        fixed = check.corrected_costs(reply, [output])
        self.assertEqual(fixed, "12/7 烏來 兩人\n" + output.replace("\r", "") +
                         "\nMEDIA:x.jpg\n📷 溪景湯屋（www.spa.example）\n騎車或開車約 50–60 分鐘\n📞 02-2661-8001")
        self.assertNotIn("（每1小時）（未確認）", fixed)
        self.assertEqual(fixed.count("溪景湯屋平日"), 1)

    def test_nothing_unconfirmed_goes_out_when_the_user_wants_only_confirmed(self):
        # 2026-11-19: 「未確認就不要傳給我，傳給我確認過的就好」.
        output = "總計 NT$600，預算剩 NT$1,400\n• 溪景湯屋 1小時 NT$600（平日價）"
        home = home_with(output)
        shared = home / "shared" / "travel-workflow"
        shared.mkdir(parents=True)
        (shared / "preferences.json").write_text(json.dumps({"show_unconfirmed": False}), encoding="utf-8")
        reply = ("12/07（一）烏來一日遊（2人）\n總計 NT$600，預算剩 NT$1,400\n• 溪景湯屋 1小時 NT$600（平日價）\n"
                 "• 油錢（估算） NT$120，未確認\n老街餐食大約 NT$760\n📷 溪景湯屋（www.spa.example）\n"
                 "📞 02-2661-8001\n待確認：自備毛巾省備品費、湯屋現場候位狀況")
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            self.assertEqual(check.transform_llm_output(response_text=reply, session_id="s1", turn_id="t1"),
                             "12/07（一）烏來一日遊（2人）\n總計 NT$600，預算剩 NT$1,400\n• 溪景湯屋 1小時 NT$600（平日價）\n"
                             "📷 溪景湯屋（www.spa.example）\n📞 02-2661-8001")
            # Without the preference nothing is dropped.
            (shared / "preferences.json").write_text(json.dumps({}), encoding="utf-8")
            self.assertIn("待確認", check.transform_llm_output(response_text=reply, session_id="s1", turn_id="t1")
                          or reply)
        finally:
            if old is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old

    def test_hooks_are_declared(self):
        manifest = (REPO / "plugins/travel-reply-check/plugin.yaml").read_text(encoding="utf-8")
        for hook in ("pre_tool_call", "transform_tool_result", "transform_llm_output"):
            self.assertIn(f"  - {hook}", manifest)



class CheckersAreOffLimitsTests(unittest.TestCase):
    # 2026-11-19 (Telegram): a cheaper-hot-spring request took 42 minutes; the commander queried state.db,
    # read the checkers' code and patched page_evidence.py to make a price pass.
    TOOLS = "C:/Users/shiro/AppData/Local/hermes/shared/travel-workflow"

    def blocked(self, tool, **args):
        result = check.pre_tool_call(tool, args, session_id="s1")
        return bool(result) and result.get("action") == "block" and "檢查結果就是結論" in result["message"]

    def test_editing_or_reading_the_checkers_is_blocked(self):
        self.assertTrue(self.blocked("patch", path=f"{self.TOOLS}/page_evidence.py", old_string="a", new_string="b"))
        self.assertTrue(self.blocked("write_file", path=r"C:\hermes\shared\travel-workflow\trip-cost.py", content="x"))
        self.assertTrue(self.blocked("read_file", path=f"{self.TOOLS}/trip-cost.py"))
        self.assertTrue(self.blocked("patch", path="C:/hermes/profiles/project-manager/plugins/travel-reply-check/__init__.py",
                                     old_string="a", new_string="b"))
        for command in (
                "python -c \"import sqlite3; sqlite3.connect('C:/hermes/profiles/project-manager/state.db')\"",
                f"python -c \"import sys; sys.path.append('{self.TOOLS}'); import page_evidence\"",
                f"type {self.TOOLS}/page_evidence.py",
                f"python -c \"import importlib.util; importlib.util.spec_from_file_location('x', 'y')\""):
            with self.subTest(command=command):
                self.assertTrue(self.blocked("terminal", command=command))

    def test_running_the_tools_is_fine(self):
        for command in (
                f"\"C:/Python313/python.exe\" \"{self.TOOLS}/trip-cost.py\" --budget 2000 \"湯屋=699@https://a.example/\"",
                f"python {self.TOOLS}/place-photo.py 春和 --page https://a.example/ --list",
                f"python {self.TOOLS}/trip-cost.py --help"):
            with self.subTest(command=command):
                self.assertFalse(self.blocked("terminal", command=command))
        self.assertFalse(self.blocked("read_file", path=f"{self.TOOLS}/opportunities.json"))
        self.assertFalse(self.blocked("patch", path=f"{self.TOOLS}/opportunities.json", old_string="a", new_string="a b"))

if __name__ == "__main__":
    unittest.main()


BEFORE = '''      "candidate_windows": [
        "2026-11-21..2026-11-22",
        "2026-12-05..2026-12-07"
      ],
      "source": "官方旅宿網站（捷絲旅宜蘭礁溪館、品文旅礁溪、礁溪老爺酒店）與宜蘭勁好玩官方觀光網",
      "queried_at": "2026-11-11T01:36:00+08:00",
      "status": "待補資料",
'''


class RecordEditTests(unittest.TestCase):
    PATH = "C:/hermes/shared/travel-workflow/opportunities.json"

    def patch(self, old, new, path=None):
        return check.pre_tool_call("patch", {"path": path or self.PATH, "old_string": old, "new_string": new},
                                   session_id="s1")

    def test_a_records_source_cannot_be_rewritten(self):
        # 2026-11-14 (Telegram test 13c): the commander replaced this record's source with 龍山's page and its
        # queried_at with that day, so 捷絲旅's prices looked sourced to a page that never showed them.
        after = BEFORE.replace("官方旅宿網站（捷絲旅宜蘭礁溪館、品文旅礁溪、礁溪老爺酒店）與宜蘭勁好玩官方觀光網",
                               "礁溪龍山溫泉旅館官網（https://www.ls-hotel.com.tw/rooms01.php）與葛瑪蘭客運票價網")
        after = after.replace("2026-11-11T01:36:00+08:00", "2026-11-14T22:58:00+08:00")
        result = self.patch(BEFORE, after)
        self.assertEqual(result["action"], "block")
        self.assertIn("source", result["message"])
        self.assertIn("2026-11-11T01:36:00+08:00", result["message"])

    def test_adding_a_question_or_a_new_finding_is_fine(self):
        question = BEFORE + '      "pending_question": {"question": "要排哪天？", "choices": ["11/21", "12/6"]},\n'
        self.assertIsNone(self.patch(BEFORE, question))
        finding = BEFORE + '      "longshan_weekend": {"price_twd": 1800, "source": "https://www.ls-hotel.com.tw/rooms01.php",' \
                           ' "queried_at": "2026-11-14T22:58:00+08:00"},\n'
        self.assertIsNone(self.patch(BEFORE, finding))

    def test_other_files_are_not_checked(self):
        self.assertIsNone(self.patch(BEFORE, "", path="C:/hermes/profiles/trip/notes.json"))

    def test_rewriting_the_whole_file_keeps_provenance_too(self):
        folder = Path(scratch.new_dir("hermes-records-"))
        path = folder / "opportunities.json"
        path.write_text("{\n" + BEFORE + "}\n", encoding="utf-8")
        result = check.pre_tool_call("write_file", {"path": str(path), "content": "{}\n"}, session_id="s1")
        self.assertEqual(result["action"], "block")
        self.assertIsNone(check.pre_tool_call("write_file", {"path": str(path),
                                                              "content": "{\n" + BEFORE + '  "x": 1\n}\n'},
                                              session_id="s1"))


PLAN_REPLY = ("福岡 2027/03/16–03/20 雙人機酒 NT$35,094，在預算 4 萬內。\n\n華航直飛來回 NT$26,474\n"
              "首選法華俱樂部 4 晚 NT$8,620\n\n來源為 Google Flights 與 Hotels，訂票與付款由你自行確認。")


def home_with_card(how="review", status="done"):
    """A confirmed-only home whose session read card t_6e12091c, with that card on the board."""
    home = Path(scratch.new_dir("hermes-reply-card-"))
    (home / "shared" / "travel-workflow").mkdir(parents=True)
    (home / "shared" / "travel-workflow" / "preferences.json").write_text('{"show_unconfirmed": false}', encoding="utf-8")
    session = home / "profiles" / "travel-commander" / "state.db"
    session.parent.mkdir(parents=True)
    db = sqlite3.connect(session)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    db.execute("INSERT INTO messages (session_id, role, content) VALUES ('s8', 'user', '看板任務 t_6e12091c 已完成。')")
    db.commit()
    db.close()
    board = home / "kanban" / "boards" / "command-center"
    (board / "attachments" / "t_6e12091c").mkdir(parents=True)
    plan = board / "attachments" / "t_6e12091c" / "plan.md"
    plan.write_text("機票每人 NT$13,237，兩人 NT$26,474\n法華俱樂部 4晚 NT$8,620\n總計 NT$35,094", encoding="utf-8")
    db = sqlite3.connect(board / "kanban.db")
    db.execute("CREATE TABLE tasks (id TEXT, status TEXT, body TEXT)")
    db.execute("CREATE TABLE task_runs (id INTEGER, task_id TEXT, outcome TEXT, summary TEXT)")
    db.execute("CREATE TABLE task_events (task_id TEXT, run_id INTEGER, kind TEXT)")
    db.execute("CREATE TABLE task_attachments (task_id TEXT, stored_path TEXT)")
    db.execute("INSERT INTO tasks VALUES ('t_6e12091c', ?, '城市: 福岡 人數: 2 整趟預算: NT$40,000')", (status,))
    db.execute("INSERT INTO task_runs VALUES (1, 't_6e12091c', 'completed', '')")
    for kind in (["claimed", "spawned", "completed"] if how == "review" else ["completed"]):
        db.execute("INSERT INTO task_events VALUES ('t_6e12091c', 1, ?)", (kind,))
    db.execute("INSERT INTO task_attachments VALUES ('t_6e12091c', ?)", (str(plan),))
    db.commit()
    db.close()
    return home


def send(home, reply):
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        out = check.transform_llm_output(response_text=reply, session_id="s8", turn_id="t1")
        return reply if out is None else out
    finally:
        if old is None:
            del os.environ["HERMES_HOME"]
        else:
            os.environ["HERMES_HOME"] = old


class ReviewedCardAmountsTests(unittest.TestCase):
    # 2026-11-21: with show_unconfirmed off, a Kanban card's reviewed prices were all stripped, and twice
    # the user got only 「來源為 Google Flights 與 Hotels，訂票與付款由你自行確認。」.
    def test_prices_from_a_reviewed_card_are_kept(self):
        self.assertEqual(send(home_with_card(), PLAN_REPLY), PLAN_REPLY)

    def test_a_price_the_card_does_not_have_is_still_removed(self):
        out = send(home_with_card(), PLAN_REPLY.replace("NT$8,620", "NT$6,999"))
        self.assertNotIn("NT$6,999", out)
        self.assertIn("NT$26,474", out)

    def test_a_card_closed_by_hand_or_not_done_confirms_nothing(self):
        for home in (home_with_card(how="hand"), home_with_card(status="review")):
            self.assertNotIn("NT$", send(home, PLAN_REPLY))


class FormatPlanEvidenceTests(unittest.TestCase):
    def test_format_plan_output_counts_for_costs_and_booking_links(self):
        home = Path(scratch.new_dir("hermes-format-evidence-"))
        db_path = home / "profiles" / "travel-commander" / "state.db"
        db_path.parent.mkdir(parents=True)
        plan_out = (
            "12/14 礁溪泡湯 2人\n"
            "總計 NT$300，預算剩 NT$1,700\n"
            "• 溫泉湯屋 50分鐘 NT$300\n"
            "📍 [Google 地圖・查空房](https://www.google.com/maps/search/?api=1&query=%E7%8E%AB%E7%91%B0%E5%9C%92)\n"
            "🏨 [官網訂房](https://spa.example/rooms)\n"
            "📞 039883364"
        )
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT)")
        db.execute("INSERT INTO messages VALUES (1, 'sf1', 'assistant', '', ?, NULL)",
                   (json.dumps([{"id": "c1", "function": {"name": "terminal", "arguments": '{"command": "python format-plan.py --header ..."}'}}]),))
        db.execute("INSERT INTO messages VALUES (2, 'sf1', 'tool', ?, NULL, 'c1')",
                   (json.dumps({"output": plan_out}, ensure_ascii=False),))
        db.commit()
        db.close()

        self.assertEqual(check.latest_cost_output("sf1", home), "總計 NT$300，預算剩 NT$1,700\n• 溫泉湯屋 50分鐘 NT$300")
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            verdict = check.pre_tool_call("clarify", {"question": plan_out + "\n要選這間嗎？", "choices": ["去這間", "換別間"]},
                                          session_id="sf1", turn_id="t1")
        finally:
            if old is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old
        # Only link formatting modification, never blocked for missing trip-cost.py / booking-links.py.
        self.assertNotEqual((verdict or {}).get("action"), "block")
