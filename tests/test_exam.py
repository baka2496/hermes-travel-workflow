from contextlib import redirect_stdout
import datetime as dt
import importlib.util
import io
import json
from unittest import mock
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("exam", REPO / "tools/exam.py")
exam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exam)

PLAN = ("12/5 礁溪 2人｜3 個方案\n"
        "① 甲湯屋｜雙人湯屋\n總計 NT$1,200\n12/5｜交通：開車約 50 分鐘\n🏨 [官網](https://a.example/) 📞 03-1\n"
        "② 乙湯屋｜雙人湯屋\n總計 NT$1,500\n12/5｜交通：開車約 50 分鐘\n🏨 [官網](https://b.example/) 📞 03-2\n"
        "③ 丙湯屋｜雙人湯屋\n總計 NT$1,800\n12/5｜交通：開車約 50 分鐘\n🏨 [官網](https://c.example/) 📞 03-3")
PACKAGES = {"id": "t", "expect": "packages", "place": "礁溪", "budget": 2000}


class DatesTests(unittest.TestCase):
    def test_dates_follow_the_coming_weekend(self):
        wednesday = dt.date(2026, 11, 25)
        self.assertEqual(exam.fill_dates("{週五}–{週日} 兩天一夜，{下週六} 也行", wednesday),
                         "11/27–11/29 兩天一夜，12/5 也行")
        self.assertEqual(exam.fill_dates("{週五}", dt.date(2026, 11, 27)), "11/27")  # a Friday: today
        # From Saturday on, next week's: {週五} must never be a day already past.
        self.assertEqual(exam.fill_dates("{週五}–{週日}", dt.date(2026, 11, 28)), "12/4–12/6")
        self.assertEqual(exam.fill_dates("{週六}", dt.date(2026, 11, 29)), "12/5")

    def test_an_unknown_question_is_refused_not_scored_zero_of_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps({"questions": [{"id": "vague"}]}), encoding="utf-8")
            args = type("Args", (), {"questions": path, "only": ["vage"]})()
            printed = io.StringIO()  # CI's console is not UTF-8
            with mock.patch.object(exam, "gateway_started", return_value=None), redirect_stdout(printed):
                self.assertEqual(exam.run(args), 2)
            self.assertIn("沒有這些題目：vage", printed.getvalue())

    def test_every_question_is_well_formed(self):
        seen = set()
        for q in exam.questions():
            self.assertNotIn(q["id"], seen)
            seen.add(q["id"])
            self.assertIn(q["expect"], ("packages", "ask_dates", "one", "cheaper"), q["id"])
            self.assertIn(q["profile"], ("project-manager", "trip"), q["id"])
            self.assertTrue(q["turns"], q["id"])
            self.assertNotRegex(json.dumps(q, ensure_ascii=False), r"\d{1,2}/\d{1,2}", "use {週六} so it never goes stale")


class GradeTests(unittest.TestCase):
    def grade(self, reply, outputs=(PLAN,), question=PACKAGES, said=("12/5 礁溪 2人 預算 2000",), **kw):
        return exam.grade(question, list(said), reply, list(outputs), **kw)

    def test_three_packages_from_format_plan_pass(self):
        self.assertEqual(self.grade(PLAN), [])

    def test_packages_written_by_hand_fail(self):
        self.assertEqual(len([p for p in self.grade(PLAN, outputs=()) if "不是 format-plan.py 組的" in p]), 3)
        hand = PLAN.replace("總計 NT$1,200", "總計 NT$1,000")
        self.assertTrue(any("甲湯屋" in p for p in self.grade(hand)))

    def test_only_hot_springs_fail_when_no_kind_was_named(self):
        # 2026-11-26: 「為甚麼只有溫泉明明還有那麼多地方可以玩」.
        mixed = dict(PACKAGES, mixed=True, place=None, budget=None)
        self.assertIn("沒說玩什麼，三個方案卻全是溫泉", self.grade(PLAN, question=mixed))
        trail = PLAN.replace("② 乙湯屋｜雙人湯屋", "② 乙步道｜步道")
        self.assertEqual(self.grade(trail, outputs=(trail,), question=mixed), [])
        self.assertEqual(self.grade(PLAN), [])  # 「想去泡溫泉」: all hot springs is right

    def test_one_place_twice_fails(self):
        # 2026-11-26: three packages were three rooms of one 龍山溫泉旅館.
        twin = PLAN.replace("② 乙湯屋", "② 甲湯屋")
        self.assertIn("「甲湯屋」重複：三個方案要三個不同的地方", self.grade(twin, outputs=(twin,)))

    def test_no_phone_line_is_fine_when_the_owner_hides_unconfirmed(self):
        # 「未確認就不要傳給我」: the plugin drops 「電話：未取得」, so the package has no phone line.
        bare = PLAN.replace(" 📞 03-2", "")
        self.assertTrue(any("incomplete_package" in p for p in self.grade(bare, outputs=(bare,))))
        printed = PLAN.replace(" 📞 03-2", "\n電話：未取得")  # what format-plan.py printed
        self.assertEqual(self.grade(bare, outputs=(printed,), hide_unconfirmed=True), [])
        # The tool found a phone and the reply left it out: still a fault (Antigravity's review).
        self.assertTrue(any("incomplete_package" in p for p in self.grade(bare, outputs=(PLAN,), hide_unconfirmed=True)))
        one = dict(PACKAGES, expect="one", place=None, budget=None)
        plain = ("11/28 礁溪 2人\n總計 NT$300\n📍 [Google 地圖・查空房](https://maps.example/)\n"
                 "🏨 [官網訂房](https://a.example/)\n📞 03-1")  # 「照片：未取得」 dropped
        self.assertTrue(any("missing_photo" in p for p in self.grade(plain, outputs=(plain,), question=one)))
        self.assertEqual(self.grade(plain, outputs=(plain + "\n照片：未取得",), question=one, hide_unconfirmed=True), [])
        found = plain + "\nMEDIA:C:/photos/a.jpg"  # the tool had a photo the reply left out
        self.assertTrue(any("missing_photo" in p for p in self.grade(plain, outputs=(found,), question=one,
                                                                    hide_unconfirmed=True)))
        no_link = PLAN.replace("🏨 [官網](https://b.example/) 📞 03-2", "")
        self.assertTrue(any("incomplete_package" in p for p in self.grade(no_link, outputs=(no_link,),
                                                                         hide_unconfirmed=True)))

    def test_pick_one_wants_one_full_plan(self):
        # 2026-11-26 (exam): 「你幫我挑一個」 got two packages.
        one = dict(PACKAGES, expect="one", place=None, budget=None)
        single = "11/29 烏來 2人\n總計 NT$800\n照片：未取得\n🏨 [官網](https://a.example/) 📞 03-1"
        self.assertEqual(self.grade(single, outputs=(single,), question=one), [])
        self.assertIn("要它挑一個，卻給了 3 個方案", self.grade(PLAN, question=one))
        self.assertIn("總價不是 format-plan.py 算的", self.grade(single, outputs=(), question=one))
        self.assertIn("沒有總價", self.grade("烏來比較好", outputs=(), question=one))
        two = single + "\n\n11/28 礁溪 2人\n總計 NT$4,200\n🏨 [官網](https://b.example/) 📞 03-2"
        self.assertIn("要它挑一個，卻給了 2 個方案", self.grade(two, outputs=(two,), question=one))
        headed = "① 烏來｜湯屋\n" + two
        self.assertIn("要它挑一個，卻給了 2 個方案", self.grade(headed, outputs=(headed,), question=one))

    def test_cheaper_wants_cheaper_packages_or_an_honest_not_found(self):
        # 2026-11-26 (exam): the first reply's cheapest was NT$300 and nothing cheaper was found; saying so is right.
        cheaper = dict(PACKAGES, expect="cheaper")
        honest = "玫瑰園的雙人湯屋每間 NT$300，查了礁溪幾家官網，沒有找到更便宜的。"
        self.assertEqual(self.grade(honest, outputs=(), question=cheaper, earlier=(PLAN,)), [])
        plain = "玫瑰園原泉湯屋為整間 NT$300（50分鐘）\n目前官網核實價格中，\n沒有比玫瑰園更便宜的雙人湯屋。"
        self.assertEqual(self.grade(plain, outputs=(), question=cheaper, earlier=(PLAN,)), [])
        lazy = "玫瑰園 NT$300，沒有比較其他家，應該有更便宜的"  # across clauses: not a not-found (review)
        self.assertIn("沒有更便宜的方案，也沒說沒找到", self.grade(lazy, outputs=(), question=cheaper, earlier=(PLAN,)))
        self.assertIn("沒有更便宜的方案，也沒說沒找到",
                      self.grade("好的，我再找找", outputs=(), question=cheaper, earlier=(PLAN,)))
        self.assertIn("沒有比原本最便宜的 NT$1,200 更便宜", self.grade(PLAN, question=cheaper, earlier=(PLAN,)))
        less = PLAN.replace("NT$1,200", "NT$900").replace("NT$1,500", "NT$950").replace("NT$1,800", "NT$1,000")
        self.assertEqual(self.grade(less, outputs=(less,), question=cheaper, earlier=(PLAN,)), [])

    def test_no_packages_or_wrong_place_fail(self):
        self.assertIn("沒有給 ①②③ 方案", self.grade("請問想去哪裡？", outputs=()))
        self.assertIn("方案不在烏來", self.grade(PLAN, question=dict(PACKAGES, place="烏來")))

    def test_over_budget_must_be_said(self):
        question = dict(PACKAGES, budget=1000)
        self.assertIn("全部超出預算 NT$1,000 卻沒說", self.grade(PLAN, question=question))
        said = PLAN.replace("3 個方案", "3 個方案，都超出 NT$200")
        self.assertEqual(self.grade(said, outputs=(said,), question=question), [])

    def test_unsaid_budget_fails(self):
        reply = PLAN.replace("3 個方案", "預算 NT$5,000")
        self.assertIn("寫了主人沒說過的預算：預算 NT$5,000", self.grade(reply, outputs=(reply,), said=("去礁溪",)))

    def test_disagreeing_dates_are_asked_not_planned(self):
        question = {"id": "d", "expect": "ask_dates"}
        self.assertEqual(self.grade("12/5–12/7 其實是三天兩夜，要住兩晚還是一晚？", question=question,
                                    outputs=()), ["手機排版檢查沒過：text_question"])
        # The question itself goes on clarify buttons (telegram rules), so the text only explains: a pass.
        self.assertEqual(self.grade("12/5–12/7 其實是三天兩夜，跟兩天一夜對不上，請按下面的按鈕選住幾晚",
                                    question=question, outputs=()), [])
        problems = self.grade(PLAN, question=question)
        self.assertIn("日期對不上卻直接出方案", problems)
        self.assertIn("沒講日期和天數對不上", problems)

    def test_errors_and_slow_answers_fail(self):
        self.assertEqual(self.grade("", error="invalid_params"), ["出錯：invalid_params"])
        self.assertEqual(self.grade(""), ["沒有回覆"])
        self.assertIn("太慢：20 分鐘", self.grade(PLAN, elapsed=1200))


class FolderTests(unittest.TestCase):
    def test_the_bot_never_answers_from_inside_this_checkout(self):
        # 2026-11-26: started here, the bot read tests/test_format_plan.py to get past the checks.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(exam.answer_folder(root), root)
            (root / "profiles").mkdir()
            self.assertEqual(exam.answer_folder(root), root / "profiles")
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout="", stderr="")
            exam.ask("trip", "你好")
            self.assertNotIn(str(REPO), str(run.call_args.kwargs["cwd"]))


class StreamTests(unittest.TestCase):
    def test_reads_the_result_event(self):
        out = "\n".join(json.dumps(e, ensure_ascii=False) for e in (
            {"type": "system", "subtype": "init", "session_id": ""},
            {"type": "tool_use", "name": "terminal", "tool_call_id": "c1", "input": {"command": "py page-prices.py"}},
            {"type": "tool_result", "tool_call_id": "c1", "output": json.dumps({"output": "價格"})},
            {"type": "tool_use", "name": "terminal", "tool_call_id": "c2", "input": {"command": "py format-plan.py --package"}},
            {"type": "tool_result", "tool_call_id": "c2", "output": json.dumps({"output": "① 甲\r\n總計 NT$1"})},
            {"type": "result", "session_id": "s1", "exit_code": 0, "text": "你好", "error": None}))
        self.assertEqual(exam.parse_stream(out), ("你好", "s1", "", ["① 甲\n總計 NT$1"]))
        out = '{"type": "result", "session_id": "", "exit_code": 2, "text": "", "error": "Unsupported"}'
        self.assertEqual(exam.parse_stream(out, session_id="s0"), ("", "s0", "Unsupported", []))
        self.assertEqual(exam.parse_stream("", "Hermes 無法建立模型連線"), ("", None, "Hermes 無法建立模型連線", []))


class GatewayTests(unittest.TestCase):
    def test_a_plugin_changed_after_the_gateway_started_is_not_live(self):
        # 2026-11-26: the gateway still ran the one-package plugin, so packages ② and ③ lost their totals.
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "__init__.py"
            plugin.write_text("x", encoding="utf-8")
            changed = dt.datetime.fromtimestamp(plugin.stat().st_mtime, dt.timezone.utc)
            self.assertEqual(exam.newer_than([plugin], changed - dt.timedelta(minutes=5)), [plugin])
            self.assertEqual(exam.newer_than([plugin], changed + dt.timedelta(minutes=5)), [])
            self.assertEqual(exam.newer_than([plugin], None), [])


class RestoreTests(unittest.TestCase):
    def test_puts_back_what_the_exam_changed_and_keeps_the_changed_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "opportunities.json").write_text("before", encoding="utf-8")
            (folder / "preferences.json").write_text("same", encoding="utf-8")
            before = exam.snapshot(folder)
            (folder / "opportunities.json").write_text("during", encoding="utf-8")
            self.assertEqual(exam.restore(folder, before, "s"), ["opportunities.json"])
            self.assertEqual((folder / "opportunities.json").read_text(encoding="utf-8"), "before")
            self.assertEqual((folder / "opportunities.exam-s.json").read_text(encoding="utf-8"), "during")
            self.assertFalse((folder / "preferences.exam-s.json").exists())

    def test_a_file_the_exam_made_is_moved_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            before = exam.snapshot(folder)
            (folder / "recommended.json").write_text("[]", encoding="utf-8")
            self.assertEqual(exam.restore(folder, before, "s"), ["recommended.json"])
            self.assertFalse((folder / "recommended.json").exists())
            self.assertTrue((folder / "recommended.exam-s.json").exists())


if __name__ == "__main__":
    unittest.main()
