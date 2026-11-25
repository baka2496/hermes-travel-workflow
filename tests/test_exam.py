import datetime as dt
import importlib.util
import json
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
            exam.gateway_started = lambda: None
            self.assertEqual(exam.run(args), 2)

    def test_every_question_is_well_formed(self):
        seen = set()
        for q in exam.questions():
            self.assertNotIn(q["id"], seen)
            seen.add(q["id"])
            self.assertIn(q["expect"], ("packages", "ask_dates"), q["id"])
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


if __name__ == "__main__":
    unittest.main()
