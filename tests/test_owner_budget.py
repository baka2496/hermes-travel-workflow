"""2026-11-25 (Telegram): 「我一直都沒跟他說我的預算，他為甚麼會說2000；然後他只推一間給我選擇也太少」.

Asked 「我想出去玩」, the commander sent 「目前行事曆保留 12/07（一）校慶補假烏來一日遊（備案為 12/06–12/07
礁溪兩天一夜，兩人整趟預算 NT$2,000，自駕前往）。這次想先走哪一趟？」 with the buttons 烏來 / 礁溪 /
「這週末（11/28–11/29）出發」. The NT$2,000 came from preferences.json, written on 2026-11-11 by a handoff
(「使用者（本人）透過 Claude 轉交…整趟預算：新台幣 2,000 元」), not by the owner; the record it asked about
had been cancelled at 15:59 that day. These tests keep both from happening again:
- a budget is stated, passed to trip-cost.py/format-plan.py, put on a card or kept in preferences.json
  only when the owner's own words give that amount;
- a where-to-go button question offers three different places (a date is not a place).
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import hermes_sessions  # noqa: E402
import travel_money  # noqa: E402

spec = importlib.util.spec_from_file_location("reply_check_budget", REPO / "plugins/travel-reply-check/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

# The question exactly as it reached the phone on 2026-11-25 17:56.
QUESTION = ("目前行事曆保留 12/07（一）校慶補假烏來一日遊（備案為 12/06–12/07 礁溪兩天一夜，"
            "兩人整趟預算 NT$2,000，自駕前往）。\n這次想先走哪一趟？")
CHOICES = ["12/07 烏來一日遊（校慶補假）", "12/06–12/07 礁溪兩天一夜", "這週末（11/28–11/29）出發"]
RELAY = "使用者（本人）透過 Claude 轉交這次的旅行條件…整趟預算：新台幣 2,000 元，兩人合計"


def hermes_home(*telegram, session_messages=(), preferences=None, older_telegram=()):
    """A Hermes root with one profile: Telegram messages the owner typed, the current session's
    user messages ("s1", a cron/CLI session), and optionally a preferences.json."""
    root = Path(scratch.new_dir("hermes-owner-budget-"))
    path = root / "profiles" / "project-manager" / "state.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    db.execute("INSERT INTO sessions VALUES ('tg', 'telegram'), ('s1', 'cli'), ('old', 'telegram')")
    for text in older_telegram:
        db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('old', 'user', ?, ?)",
                   (text, time.time() - 2 * 86400))
    when = time.time()
    for text in telegram:
        db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('tg', 'user', ?, ?)", (text, when))
    for text in session_messages:
        db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'user', ?, ?)",
                   (text, time.time()))
    db.commit()
    db.close()
    if preferences is not None:
        prefs = root / "shared" / "travel-workflow" / "preferences.json"
        prefs.parent.mkdir(parents=True)
        prefs.write_text(json.dumps(preferences, ensure_ascii=False), encoding="utf-8")
    return root


def with_home(home, call):
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        return call()
    finally:
        if old is None:
            del os.environ["HERMES_HOME"]
        else:
            os.environ["HERMES_HOME"] = old


def clarify(home, question, choices, session="s1"):
    return with_home(home, lambda: plugin.pre_tool_call(
        "clarify", {"questions": [{"question": question, "choices": choices}]}, session_id=session))


def terminal(home, command, session="s1"):
    return with_home(home, lambda: plugin.pre_tool_call("terminal", {"command": command}, session_id=session))


RELAYED_PREFS = {"budget": {"amount": 2000, "currency": "TWD", "scope": "entire_trip"},
                 "source": {"channel": "使用者本人透過 Claude 轉交授權更新"}}


class BudgetWordsTests(unittest.TestCase):
    def test_a_budget_claim_is_found_and_what_tools_print_is_not(self):
        self.assertEqual([a for _, a in travel_money.stated_budgets(QUESTION)], [2000])
        for text, amount in (("在 NT$2,000 預算內", 2000), ("整趟預算: NT$40,000", 40000), ("預算約 3000元", 3000)):
            self.assertEqual([a for _, a in travel_money.stated_budgets(text)], [amount], text)
        # trip-cost.py's own words about a budget it was given, and 「預算未確認」, are not claims.
        for text in ("總計 NT$2,932，預算剩 NT$3,068", "超出預算 NT$32", "預算未確認", "超過預算 NT$500"):
            self.assertEqual(travel_money.stated_budgets(text), [], text)

    def test_the_ways_a_person_writes_an_amount(self):
        # 「2000以內的」 is what the owner typed on 2026-10-17 (about Tokyo rooms).
        for text, amount in (("2000以內的", 2000), ("兩千", 2000), ("預算 4萬", 40000), ("一萬五", 15000),
                             ("2.5k", 2500), ("NT$2,000 左右", 2000), ("兩千五", 2500)):
            self.assertIn(amount, travel_money.spoken_amounts(text), text)

    def test_only_the_owners_words_back_a_budget(self):
        self.assertEqual([a for _, a in travel_money.unsaid_budgets(QUESTION, ["我想出去玩"], RELAYED_PREFS)], [2000])
        self.assertEqual(travel_money.unsaid_budgets(QUESTION, ["兩個人預算兩千就好"]), [])
        quoted = {"budget": {"amount": 2000, "currency": "TWD", "scope": "entire_trip", "quote": "以後都兩千以內"}}
        self.assertEqual(travel_money.unsaid_budgets(QUESTION, [], quoted), [])
        self.assertIsNone(travel_money.owner_budget(RELAYED_PREFS))  # no quote: not the owner's budget

    def test_hermes_notes_are_not_the_owner_talking(self):
        home = hermes_home("[CONTEXT COMPACTION — REFERENCE ONLY] …兩人預算 NT$2,000…", "我想出去玩")
        self.assertEqual(hermes_sessions.owner_words(home), ["我想出去玩"])

    def test_only_this_conversation_counts(self):
        # 2026-11-25: older Telegram conversations held test requests 「12/14 想去礁溪泡湯，2人，預算 2000」;
        # the conversation that asked the question had only 「我想出去玩」.
        home = hermes_home("我想出去玩", older_telegram=["12/14 想去礁溪泡湯，2人，預算 2000"])
        self.assertEqual(hermes_sessions.owner_words(home), ["我想出去玩"])  # the latest Telegram conversation
        self.assertEqual(hermes_sessions.owner_words(home, "tg"), ["我想出去玩"])
        self.assertEqual(clarify(home, QUESTION, CHOICES, session="tg")["action"], "block")
        self.assertIsNone(hermes_sessions.owner_words(Path(scratch.new_dir("hermes-empty-"))))


class PhoneCheckBudgetTests(unittest.TestCase):
    SCRIPT = REPO / "scripts" / "phone-check.py"

    def check(self, home, text):
        env = {**os.environ, "HERMES_HOME": str(home)}
        result = subprocess.run([sys.executable, str(self.SCRIPT), "--json"], input=text, capture_output=True,
                                text=True, encoding="utf-8", env=env)
        return [p for p in json.loads(result.stdout)["problems"] if p["kind"] == "unsaid_budget"]

    def test_the_relayed_budget_is_caught(self):
        problems = self.check(hermes_home("我想出去玩", preferences=RELAYED_PREFS), QUESTION.replace("？", "。"))
        self.assertEqual(len(problems), 1)
        self.assertIn("NT$2,000", problems[0]["text"])
        self.assertIn("預算未確認", problems[0]["hint"])

    def test_a_budget_the_owner_typed_passes(self):
        self.assertEqual(self.check(hermes_home("兩人 2000 以內"), "兩人整趟預算 NT$2,000。"), [])
        self.assertEqual(self.check(hermes_home("我想出去玩"), "預算未確認，總計 NT$1,480。"), [])


class ButtonBudgetTests(unittest.TestCase):
    def test_the_question_of_2026_11_25_is_held(self):
        result = clarify(hermes_home("我想出去玩", preferences=RELAYED_PREFS), QUESTION, CHOICES, session="tg")
        self.assertEqual(result["action"], "block")
        self.assertIn("NT$2,000", result["message"])

    def test_a_relayed_handoff_in_the_same_session_does_not_count_as_the_owner(self):
        # The 11-11 handoff was a CLI session's user message; a Telegram question is checked against
        # that Telegram conversation and the owner's other Telegram messages, not other sessions.
        home = hermes_home("我想出去玩", session_messages=[RELAY])
        self.assertEqual(clarify(home, QUESTION, CHOICES, session="tg")["action"], "block")

    def test_trip_cost_gets_no_budget_the_owner_never_gave(self):
        home = hermes_home("我想出去玩", preferences=RELAYED_PREFS)
        command = '"C:/Python313/python.exe" "C:/x/shared/travel-workflow/trip-cost.py" --budget 2000 --date 2026-12-07 "湯屋=600"'
        result = terminal(home, command, session="tg")
        self.assertEqual(result["action"], "block")
        self.assertIn("拿掉 --budget", result["message"])
        self.assertIsNone(terminal(home, command.replace("--budget 2000 ", ""), session="tg"))
        self.assertIsNone(terminal(hermes_home("這次預算兩千"), command, session="tg"))

    def test_a_card_gets_no_budget_the_owner_never_gave(self):
        home = hermes_home("想去福岡")
        card = "hermes kanban --board command-center create \"福岡\" --assignee trip --body '整趟預算: NT$70,000' --json"
        self.assertEqual(terminal(home, card, session="tg")["action"], "block")
        self.assertIsNone(terminal(hermes_home("想去福岡，兩個人 7萬"), card, session="tg"))

    def test_preferences_keep_only_a_budget_the_owner_gave(self):
        home = hermes_home("我想出去玩")
        patch = {"path": "C:/x/shared/travel-workflow/preferences.json", "old_string": '"budget": null',
                 "new_string": '"budget": {"amount": 2000, "currency": "TWD", "scope": "entire_trip"}'}
        result = with_home(home, lambda: plugin.pre_tool_call("patch", patch, session_id="tg"))
        self.assertEqual(result["action"], "block")
        self.assertIn("quote", result["message"])
        said = hermes_home("以後預算都 2000")
        self.assertIsNone(with_home(said, lambda: plugin.pre_tool_call("patch", patch, session_id="tg")))

    def test_a_reply_says_unconfirmed_instead(self):
        home = hermes_home("我想出去玩")
        reply = "12/07 烏來一日（2人）\n兩人整趟預算 NT$2,000，總計 NT$1,480"
        fixed = with_home(home, lambda: plugin.transform_llm_output(reply, session_id="tg"))
        self.assertEqual(fixed, "12/07 烏來一日（2人）\n兩人整趟預算未確認，總計 NT$1,480")


class WhereToGoTests(unittest.TestCase):
    def ask(self, question, choices):
        return plugin.pre_tool_call("clarify", {"questions": [{"question": question, "choices": choices}]})

    def test_one_place_a_backup_and_a_date_are_not_enough(self):
        result = self.ask("目前保留 12/07 烏來一日遊，備案礁溪。\n這次想先走哪一趟？", CHOICES)
        self.assertEqual(result["action"], "block")
        self.assertIn("3 個不同的地點", result["message"])
        self.assertIn("這週末（11/28–11/29）出發", result["message"])
        self.assertIn("已取消", result["message"])

    def test_three_places_pass(self):
        result = self.ask("12/07 校慶補假，三個自駕可到的溫泉：\n想去哪？",
                          ["北投一日遊（瀧乃湯）", "烏來一日遊（湯布苑）", "礁溪兩天一夜"])
        self.assertIsNone(result)

    def test_fewer_places_pass_when_the_question_says_why(self):
        result = self.ask("12/07 只有烏來和礁溪排得進（其他地方當天客滿）。\n想去哪？", ["烏來一日遊", "礁溪兩天一夜"])
        self.assertIsNone(result)

    def test_other_questions_are_not_counted(self):
        self.assertIsNone(self.ask("礁溪山口溫泉飯店\n要哪天？", ["12/6", "12/13"]))
        self.assertIsNone(self.ask("要去哪間？", ["山口溫泉飯店", "龍山溫泉旅館"]))


class NoBudgetToolTests(unittest.TestCase):
    def test_trip_cost_adds_up_without_a_budget(self):
        result = subprocess.run([sys.executable, str(REPO / "scripts" / "trip-cost.py"), "油錢=300", "餐費=800"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        first = result.stdout.splitlines()[0]
        self.assertTrue(first.startswith("總計 NT$1,100"), first)
        self.assertNotIn("預算", first)

    def test_no_example_or_rule_hands_the_bot_a_budget(self):
        # The tools' examples said --budget 2000 / 5000 and the rules said `--budget <預算>`: a filled-in
        # budget the bot copied. A budget is added only when the user gave one.
        for path in ("scripts/trip-cost.py", "scripts/format-plan.py", "roles/_shared/telegram-research.md"):
            text = (REPO / path).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"--budget (\d|<預算>)", path)


if __name__ == "__main__":
    unittest.main()
