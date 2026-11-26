"""Hold back a button question whose cost figures do not match trip-cost.py's output.

2026-11-14 (Telegram test 10): the commander sent its clarify in the same batch as trip-cost.py, so the
question said "總計 NT$2,932 ... (NT$800 未確認)" while the tool printed "(全部未確認)": the
tool had found that none of the prices was on its source page. The role already says to call
clarify alone, after the tools; this hook enforces it for the figures:

- A "總計 NT$…" line must be exactly the first line of this conversation's latest trip-cost.py
  output. With no such output yet (for example, trip-cost.py is in the same batch), the question
  is held back until the tool has run.
- An amount trip-cost.py marked 未確認 must say 未確認 on the line that shows it.

- No MEDIA line: Telegram shows a photo only in an ordinary reply. Test 13b put
  "MEDIA:C:/…/photos/….jpg" in a clarify question, and the phone showed that file path as text.

clarify is the one tool that sends a plan to the user, so it is the one checked for figures.

Edits to opportunities.json (patch or write_file) must keep every existing "source" and
"queried_at" value. Test 13c rewrote a record's source to a different hotel's page, so the
earlier hotels' prices in that record looked sourced to a page that never showed them; it also
dropped a candidate date window. New findings go in their own fields with their own source.

A clarify question's markdown links are rewritten as "名稱：網址" (the question is plain text on
Telegram). When a question times out, the reply that follows must not repeat it: see TIMED_OUT.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote


# Modules shared with the workflow's scripts; setup.py installs a copy of each beside this file.
SHARED = ("travel_money", "hermes_sessions")


def _load_shared(name):
    """scripts/<name>.py, installed beside this file, or in the checkout's scripts/ folder. A
    missing copy fails loudly here, at load, rather than leaving every later check to fail inside
    a hook that swallows its errors."""
    here = Path(__file__).resolve().parent
    for path in (here / f"{name}.py", here.parents[1] / "scripts" / f"{name}.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"{name}.py not found beside {here}")


money = _load_shared("travel_money")  # the price format trip-cost.py prints
sessions = _load_shared("hermes_sessions")  # how Hermes stores conversations
AMOUNT = money.AMOUNT_PATTERN
TOTAL = re.compile(rf"總計 {AMOUNT}[^\n]*")
MEDIA = re.compile(r"^\s*MEDIA:", re.M)
MEDIA_PROBLEM = ("clarify 的問題裡不能放 MEDIA 照片行：手機上只會看到一串檔案路徑。"
                 "把 MEDIA 那行從 question 拿掉（可寫「照片：選完後附上」），照片放在 clarify 之後的最後回覆裡送出。")
TOOL = "trip-cost.py"
RECORDS = "opportunities.json"
KEPT = re.compile(r'"(?:source|queried_at)"\s*:\s*"[^"\n]*"')
RECORD_PROBLEM = ("機會紀錄裡已有的 source、queried_at 不能改寫或刪掉（那是舊資料的出處）："
                  "{lost}。新查到的資料加成新的欄位或新的一筆，附上自己的 source 和 queried_at。")


def lost_provenance(before, after):
    """source/queried_at entries present in `before` but fewer times in `after`."""
    lost = []
    for entry in sorted(set(KEPT.findall(before))):
        if KEPT.findall(after).count(entry) < KEPT.findall(before).count(entry):
            lost.append(entry)
    return lost


def record_edit_problem(tool_name, args):
    path = str(args.get("path") or args.get("file_path") or "")
    if not path.replace("\\", "/").endswith(RECORDS):
        return None
    if tool_name == "patch":
        before, after = str(args.get("old_string", "")), str(args.get("new_string", ""))
    elif tool_name == "write_file":
        try:
            before = Path(path).read_text(encoding="utf-8-sig")
        except OSError:
            return None
        after = str(args.get("content", ""))
    else:
        return None
    lost = lost_provenance(before, after)
    return RECORD_PROBLEM.format(lost="、".join(lost)) if lost else None


def question_text(args):
    questions = args.get("questions")
    if isinstance(questions, list):
        return "\n".join(str(q.get("question", "")) if isinstance(q, dict) else str(q) for q in questions)
    return str(args.get("question", ""))


UNKNOWN = object()  # the conversation is in no readable database: nothing to check against


COST_TOOLS = (TOOL, "format-plan.py")


def cost_blocks(output):
    """Each '總計…' headline with its following '• …' item lines, from trip-cost.py or format-plan.py output.
    format-plan.py --package prints one headline per package (2026-11-26: three packages in one reply)."""
    lines, blocks = output.splitlines(), []
    for i, line in enumerate(lines):
        if line.strip().startswith("總計"):
            block = [line.strip()]
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("•"):
                    block.append(nxt.strip())
                else:
                    break
            blocks.append("\n".join(block))
    return blocks


def cost_block(output):
    """The first cost block of an output, or None."""
    return (cost_blocks(output) or [None])[0]


def latest_cost_output(session_id, home):
    """Cost blocks of the last trip-cost.py or format-plan.py run in this conversation (one per package),
    None if none yet, or UNKNOWN if the conversation cannot be found."""
    outputs = tool_outputs(session_id, home, COST_TOOLS)
    if outputs is UNKNOWN:
        return UNKNOWN
    runs = [blocks for o in outputs if (blocks := cost_blocks(o))]
    return "\n".join(runs[-1]) if runs else None


def tool_outputs(session_id, home, tool):
    """Outputs of every run of `tool` (str or tuple of str) in this conversation, oldest first
    (empty if none), or UNKNOWN if the conversation cannot be found."""
    names = (tool,) if isinstance(tool, str) else tuple(tool)
    where = " OR ".join("tool_calls LIKE ?" for _ in names)
    params = (session_id, *(f"%{name}%" for name in names))
    for path in sessions.databases(home):
        try:
            with sessions.read_only(path) as db:
                if not db.execute("SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)).fetchone():
                    continue
                ids = [call_id for (value,) in db.execute(
                           f"SELECT tool_calls FROM messages WHERE session_id = ? AND ({where}) ORDER BY id",
                           params)
                       for call_id, _, arguments in sessions.calls(value)
                       if any(name in arguments for name in names)]
                outputs = []
                for call_id in ids:
                    row = db.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'tool' "
                                     "AND tool_call_id = ? ORDER BY id DESC LIMIT 1", (session_id, call_id)).fetchone()
                    output = sessions.result_text(row[0]) if row else ""
                    if output:
                        outputs.append(output)
                return outputs
        except sqlite3.Error:  # busy, or a layout `setup.py check` reports: no evidence either way
            continue
    return UNKNOWN


def unconfirmed_amounts(output):
    """NT$ figures (item total and unit price) of each item trip-cost.py marked 未確認."""
    amounts = set()
    for line in output.splitlines()[1:]:
        if "未確認" in line and not line.strip().startswith("總計"):
            amounts.update(money.amounts(line.split("，未確認")[0]))
    return amounts


def problem(text, output):
    """Why this question must not go out yet, or None."""
    totals = TOTAL.findall(text)
    if not totals:
        return None
    if output is None:
        return ("這則有「總計」，但這次對話還沒有 format-plan.py 的結果。先單獨執行 format-plan.py，等結果回來，"
                "再用 clarify 問，總計照它印的寫。")
    headlines = [line.strip() for line in output.splitlines() if line.strip().startswith("總計")]
    for total in totals:
        if total.strip() not in headlines:
            return f"總計要照 format-plan.py 最後印的寫：「{'」「'.join(headlines)}」，不是「{total.strip()}」。改好再問。"
    missing = []
    for line in text.splitlines():
        if TOTAL.search(line) or "未確認" in line:
            continue
        for amount in unconfirmed_amounts(output):
            if re.search(re.escape(amount) + r"(?![\d,])", line):
                missing.append(line.strip())
                break
    if missing:
        return (f"{TOOL} 把這些價格判為未確認（來源網頁上沒看到，或沒有來源），這幾行要寫「未確認」："
                + "；".join(missing))
    return None


LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def plain_links(text):
    """A clarify question is sent as plain text, so "[官網訂房](https://…)" shows as typed.
    Write "官網訂房：https://…" instead, with the address decoded so a Google Maps search reads
    as the place's name rather than a run of %E7%83%8F…; the phone still opens it as a link."""
    return LINK.sub(lambda m: f"{m.group(1)}：{short_url(m.group(2))}", text)


MAPS_SEARCH = re.compile(r"https://www\.google\.com/maps/search/\?api=1&query=([^&#\s]+)$")


def short_url(url):
    """The shortest address that opens the same page: a Google Maps search becomes
    https://maps.google.com/?q=<place name>, anything else is only decoded."""
    found = MAPS_SEARCH.match(url)
    if found:
        return "https://maps.google.com/?q=" + unquote(found.group(1)).replace(" ", "+")
    return unquote(url)


def tidy(question):
    """Markdown links written out; a leading ❓ dropped (Telegram adds its own, which showed as "❓ ❓")."""
    return re.sub(r"^\s*(?:❓\s*)+", "", plain_links(question))


def readable_questions(args):
    """args with every question tidied, or None when nothing changed."""
    questions = args.get("questions")
    if isinstance(questions, list):
        changed = [dict(q, question=tidy(str(q.get("question", "")))) if isinstance(q, dict) else q
                   for q in questions]
        return {"questions": changed} if changed != questions else None
    if isinstance(args.get("question"), str) and tidy(args["question"]) != args["question"]:
        return {"question": tidy(args["question"])}
    return None


# 2026-11-19: 「我要去泡溫泉」 took three button questions in a row — keep the plan that was
# already set, then "看費用明細", then another "下一步". A choice is a decision, never a way to
# see information, and one message from the user gets at most one button question.
INFO_CHOICE = re.compile(r"^\s*(?:\d+[.、]\s*)?(?:看|查看|顯示|列出|展開|瀏覽)")
INFO_PROBLEM = ("按鈕選項只能放不同的做法，不能放「{choice}」這種看資訊的選項：費用每項一行、交通或行程重點直接寫進方案。"
                "拿掉這個選項；剩下的如果不是真的要使用者選，就不用按鈕，直接把結果講完。")
ANSWERED = sessions.Recent()  # session_id -> turn_id whose button question the user already answered
SECOND_PROBLEM = ("這則訊息已經問過一次按鈕，使用者也選了。照選的做完，用一般回覆講結果就結束，"
                  "不要再用按鈕問下一步；之後要做什麼讓使用者自己說。")


def choices_of(args):
    questions = args.get("questions")
    if isinstance(questions, list):
        return [str(c) for q in questions if isinstance(q, dict) for c in q.get("choices") or []]
    return [str(c) for c in args.get("choices") or []]


COUNT_PROBLEM = "按鈕最多 3 個選項（推薦的放第一個），現在有 {n} 個：留最符合的三個。"


def steps_problem(args, session_id, turn_id):
    if session_id and turn_id and ANSWERED.get(session_id) == turn_id:
        return SECOND_PROBLEM
    choices = choices_of(args)
    info = [c for c in choices if INFO_CHOICE.match(c)]
    if info:
        return INFO_PROBLEM.format(choice=info[0].strip())
    return COUNT_PROBLEM.format(n=len(choices)) if len(choices) > 3 else None


# 2026-11-16: booking-links.py printed the map link for 「山口溫泉飯店」, and the question showed
# one for 「山口溫泉館」 that the commander had typed itself. Booking lines are pasted as printed.
LINKS_TOOL = "booking-links.py"
LINK_TOOLS = (LINKS_TOOL, "format-plan.py")
BOOKING_LINE = re.compile(r"^[\s•\-*]*(📍|🏨|🛏|📞)")
ADDRESS = re.compile(r"https?://[^\s)\]）]+")
MAPS = re.compile(r"https?://(?:www\.google\.[^/\s]+/maps|maps\.google\.[^/\s]+|maps\.app\.goo\.gl)")


def printed_links(outputs):
    """Addresses (as printed, and as plain_links writes them) and phone numbers booking-links.py printed."""
    addresses, phones = set(), set()
    for output in outputs:
        for line in output.splitlines():
            for url in ADDRESS.findall(line):
                addresses.update({url, short_url(url)})
            if line.strip().startswith("📞"):
                phones.add(line.strip()[1:].strip())
    return addresses, phones


def links_problem(text, outputs):
    """Why the booking lines in this question are not booking-links.py's, or None."""
    lines = [line.strip() for line in text.splitlines()
             if BOOKING_LINE.match(line) or MAPS.search(line)]
    if not lines:
        return None
    addresses, phones = printed_links(outputs)
    wrong = []
    for line in lines:
        if line.lstrip("•-* ").startswith("📞"):
            phone = line.lstrip("•-* ")[1:].strip()
            if phone and phone not in phones:
                wrong.append(line)
        elif any(unquote(url) not in {unquote(a) for a in addresses} for url in ADDRESS.findall(line)):
            # A 📍 line without an address (e.g. 「📍 集合：台北車站」) is not a booking link.
            wrong.append(line)
    if not wrong:
        return None
    printed = "\n".join(outputs) if outputs else f"（這次對話還沒有執行 {LINKS_TOOL}）"
    return (f"地圖、官網、訂房、電話這幾行要原樣貼上 {LINKS_TOOL} 印出的結果，不能自己改店名、網址或電話。"
            f"這幾行和它印的不一樣：{'；'.join(wrong)}。它印過的是：\n{printed}\n"
            f"要換地方就用正確名稱重跑 {LINKS_TOOL}，再把輸出原樣貼上。")


# 2026-11-19 (Telegram): asked for a cheaper hot spring, the commander took 42 minutes. trip-cost.py
# rightly left out 「雙人池 40分鐘 NT$250」 (the official page shows no price; its price page was
# 404), and the commander spent half an hour querying state.db, reading the checkers' code and patching
# page_evidence.py to make the price pass; the failed patch's warning reached the user's phone.
# The checkers' verdict is final: the workflow's own scripts, this plugin and the session
# databases are off limits to the bot.
# travel-workflow/pages holds page-prices.py's snapshots of what pages showed: the evidence itself.
GUARDED = re.compile(r"(?:shared[\\/]+travel-workflow[\\/]+(?:[\w.-]+\.py|pages\b)|travel-reply-check[\\/]|travel[-_]rules"
                     r"|\.db\b|sqlite3|profiles[\\/]+[\w-]+[\\/]+cache\b|page_cache|sys\.path\.(?:append|insert)|spec_from_file_location"
                     r"|\bimport\s+(?:page_evidence|travel_money|hermes_sessions|trip_cost|format_plan|place_photo|booking_links|phone_check))", re.I)
WRITES = ("patch", "write_file")
# `python …/trip-cost.py …`: the script right after a Python interpreter is being run, not read.
RUN_TOOL = re.compile(r"""((?:^|[\s;&|(])["']?[^\s"';&|]*python[\w.]*["']?\s+)["']?[^\s"']*"""
                      r"""shared[\\/]+travel-workflow[\\/]+[\w.-]+\.py["']?(?=\s|$)""", re.I)
TAMPER_PROBLEM = ("不能查或改檢查程式（shared/travel-workflow 的 .py、travel-reply-check 外掛、state.db）。"
                  "檢查結果就是結論：價格沒通過，就表示開過的官網上沒有這個價格。"
                  "要知道網頁上實際寫什麼價格，就跑 page-prices.py <官網網址>；"
                  "直接照工具輸出寫回覆：改推薦官網上有標價的地方，或不寫這個價格。"
                  "工具用法照說明書寫的跑。")


# 2026-11-26: the place pool and the recommendation log are written by their scripts only, so they stay
# valid and every price keeps its page and date (opportunities.json lost its sources to hand edits).
TOOL_WRITTEN = re.compile(r"shared[\\/]+travel-workflow[\\/]+(?:places|recommended)\.json$", re.I)
# In a terminal command any mention counts (Antigravity's review: `echo … > places.json`, a Python one-liner):
# the scripts take no path, so a command naming these files is one going around them.
TOOL_WRITTEN_IN_COMMAND = re.compile(r"\b(?:places|recommended)(?:\.backup)?\.json", re.I)
TOOL_WRITTEN_PROBLEM = ("地點庫（places.json）和推薦紀錄（recommended.json）不能直接改："
                        "收新地方用 `places.py add`，挑候選用 `places.py pick`，看全部用 `places.py list`，"
                        "推薦紀錄由 format-plan.py 自己記。")


def tamper_problem(tool_name, args):
    if tool_name in WRITES:
        path = str(args.get("path") or args.get("file_path") or "")
        if TOOL_WRITTEN.search(path.strip()):
            return TOOL_WRITTEN_PROBLEM
        return TAMPER_PROBLEM if GUARDED.search(path) else None
    if tool_name == "terminal":
        command = str(args.get("command") or "")
        if TOOL_WRITTEN_IN_COMMAND.search(command):
            return TOOL_WRITTEN_PROBLEM
        # Running the tools (python …/trip-cost.py …) is fine; reading their insides is not.
        inside = RUN_TOOL.sub(r"\1 ", command)
        return TAMPER_PROBLEM if GUARDED.search(inside) else None
    if tool_name in ("read_file", "search_files"):
        path = str(args.get("path") or args.get("file_path") or "")
        return TAMPER_PROBLEM if GUARDED.search(path) else None
    return None


# Kanban commands the commander's role used to spell out in prose (2026-11-26: rules that can be checked
# are checked here, and the role only names the command to run).
KANBAN = re.compile(r"\bkanban\b")
# 2026-11-22: `kanban list --json` printed the whole board (57,736 characters) and compressed a hand-off.
BOARD_JSON = re.compile(r"\bkanban\s+(?:--board[\s=]+\S+\s+)?(?:list|show)\b[^;&|\n]*\s--json\b")
BOARD_JSON_PROBLEM = ("不要加 --json：整板 JSON 超過 10 萬字，會讓對話被壓縮。"
                      "看卡片用 `hermes kanban --board command-center list --sort created-desc` 或 `show <卡號>`。")
# 2026-11-22: bash expanded `$4` inside a double-quoted --body: NT$40,000 reached the card as NT0,000.
DOUBLE_QUOTED_BODY = re.compile(r"""--body[\s=]+"([^"]*)\"""")
BODY_PROBLEM = ("卡片內容有 `$`，放在雙引號裡會被 bash 當成變數吃掉（11/22 NT$40,000 變成 NT0,000）。"
                "--body 改用單引號，內容本身有單引號就改用 --body-file。")
# 2026-11-21: a card number was made up because the card and its subscription ran in one batch.
SAME_BATCH_PROBLEM = ("建卡和訂閱不能同一個指令跑：先單獨跑 create，等它回 \"id\": \"t_...\"，"
                      "再用那個卡號跑 notify-subscribe。卡號絕不能自己編。")
# 2026-11-21: the workers crashed, and the commander closed the card itself as another profile.
AS_OTHER = re.compile(r"(?:^|\s)(?:-p|--profile)[\s=]+\S+\s+kanban\s+(?:--board[\s=]+\S+\s+)?(?:complete|request-review)\b")
AS_OTHER_PROBLEM = ("不能用其他身分結案或送審（-p …）：專員或覆核當掉時最多 unblock 重試一次，"
                    "還失敗就跟使用者說這次沒查成、沒有價格，用按鈕問要不要重試。")


def kanban_problem(tool_name, args):
    """Why this terminal command misuses the Kanban board, or None."""
    if tool_name != "terminal":
        return None
    command = str(args.get("command") or "")
    if not KANBAN.search(command):
        return None
    if BOARD_JSON.search(command):
        return BOARD_JSON_PROBLEM
    if re.search(r"\screate\b", command) and any("$" in body for body in DOUBLE_QUOTED_BODY.findall(command)):
        return BODY_PROBLEM
    if re.search(r"\screate\b", command) and "notify-subscribe" in command:
        return SAME_BATCH_PROBLEM
    if AS_OTHER.search(command):
        return AS_OTHER_PROBLEM
    return None


# 2026-11-25: 「…兩人整趟預算 NT$2,000…這次想先走哪一趟？」 The user never gave a budget: a handoff wrote
# it into preferences.json on 2026-11-11. A budget may be written, passed to trip-cost.py/format-plan.py
# (--budget) or put on a card only when the user's own words give that amount (this conversation, a
# Telegram message from the last week, or a preferences.json budget whose `quote` has it).
BUDGET_ARG = re.compile(r"--budget[\s=]+[\"']?(\d+)")
PREFERENCES = "preferences.json"
BUDGET_ARG_PROBLEM = ("使用者沒說過預算 {amount}：拿掉 --budget（沒有預算時工具只算總計），"
                      "回覆寫「預算未確認」或不提。")
BUDGET_RECORD_PROBLEM = ("preferences.json 的 budget 只能記使用者這次親口說過的金額，並在 quote 照抄他含這個金額的原話；"
                         "{amount} 不符合（沒說過，或沒有 quote）。沒說過就保持 null。")


def owner_preferences(home):
    for root in (Path(home), Path(home).parent.parent):
        try:
            return json.loads((root / "shared" / "travel-workflow" / PREFERENCES).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
    return None


def written_preferences(tool_name, args, path):
    """preferences.json as it will be after this write, or None when that cannot be worked out."""
    if tool_name == "write_file":
        text = str(args.get("content", ""))
    else:
        try:
            current = Path(path).read_text(encoding="utf-8-sig")
        except OSError:
            return None
        old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
        if not old or old not in current:
            return None
        text = current.replace(old, new, -1 if args.get("replace_all") else 1)
    try:
        return json.loads(text)
    except ValueError:
        return None


def record_budget_problem(tool_name, args, path, said, home):
    """A preferences.json budget must carry the owner's words (`quote`) with its amount, and the owner must
    have said that amount in this conversation, or it is the quoted budget already kept (Antigravity's
    review of 0783149: a made-up quote on an old relayed amount passed, a quote-less budget passed, and
    rewriting the file over a kept quoted budget was blocked)."""
    after = written_preferences(tool_name, args, path)
    if not isinstance(after, dict):
        return None
    budget = after.get("budget")
    if not isinstance(budget, dict) or not isinstance(budget.get("amount"), (int, float)):
        return None
    amount = int(budget["amount"])
    if money.owner_budget(after) != amount:
        return BUDGET_RECORD_PROBLEM.format(amount=money.money(amount))
    heard = set().union(*(money.spoken_amounts(t) for t in said)) if said else set()
    try:
        before = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        before = None
    kept = before.get("budget") if isinstance(before, dict) and money.owner_budget(before) is not None else None
    if amount in heard or (kept and kept.get("amount") == amount and kept.get("quote") == budget.get("quote")):
        return None
    return BUDGET_RECORD_PROBLEM.format(amount=money.money(amount))


def budget_problem(tool_name, args, session_id, home):
    """Why this call states a budget the user never gave, or None."""
    if tool_name == "clarify":
        text = question_text(args)
    elif tool_name == "terminal":
        text = str(args.get("command") or "")
    elif tool_name in WRITES:
        text = ""
    else:
        return None
    path = str(args.get("path") or args.get("file_path") or "").replace("\\", "/")
    record = tool_name in WRITES and path.endswith(PREFERENCES)
    arg = BUDGET_ARG.findall(text) if tool_name == "terminal" and re.search(r"trip-cost|format-plan", text) else []
    card = tool_name == "terminal" and "kanban" in text and " create" in text
    if not (record or arg or (tool_name == "clarify" or card) and money.stated_budgets(text)):
        return None
    said = heard(home, session_id)
    if said is None:
        return None
    if record:  # a long-lasting budget only from the owner's own words, never a card (review of db4a2e0)
        return record_budget_problem(tool_name, args, path, sessions.owner_words(home, session_id) or [], home)
    prefs = owner_preferences(home)
    for amount in map(int, arg):
        if money.unsaid_budgets(f"預算 {money.money(amount)}", said, prefs):
            return BUDGET_ARG_PROBLEM.format(amount=money.money(amount))
    unsaid = money.unsaid_budgets(text, said, prefs) if not arg else []
    return f"「{unsaid[0][0]}」：{money.BUDGET_HINT}" if unsaid else None


# 2026-11-25: asked 「我想出去玩」, the commander offered one place (烏來), its backup (礁溪) and a date with no
# place (「這週末出發」); the user: 「只推一間給我選擇也太少」. A where-to-go question offers three different
# places, each from a page or record this conversation read, unless it says only fewer fit the dates.
WHERE_QUESTION = re.compile(r"去哪(?![間家])|哪一趟|走哪|哪個地方|哪裡玩|目的地")
FEW_FIT = re.compile(r"只有.{0,12}(符合|可行|排得進|有空|來得及)")
NOT_A_PLACE = re.compile(r"[（(][^）)]*[）)]|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}(?:\s*[–—\-~～至到]\s*\d{1,2}(?:/\d{1,2})?)?|"
                         r"\d{1,2}月\d{1,2}[日號]?|\d+天\d+夜|[一二兩三四五]天[一二兩三四]夜|一日遊?|半日遊?|"
                         r"這週末?|下週末?|週末|本週|下週|[週周][一二三四五六日]|出發|改期|改天|別天|其他時間|"
                         r"校慶補假|補假|連假|平日|假日|自駕|開車|騎車|行程|方案|推薦|Recommended|"
                         r"泡湯|溫泉|之旅|\d+[年月日號]?|[\s–—\-~～至到、，,.。]")
MIN_PLACES = 3
PLACES_PROBLEM = ("問去哪裡時要給 {need} 個不同的地點讓使用者選，現在只有 {have} 個（{places}）；"
                  "日期不算地點（「{dateonly}」）。再找兩三個符合日期與交通方式、這次查過價格或紀錄裡有來源的地方；"
                  "真的只有一兩個符合，就在問題裡寫「只有…符合」並說原因。已取消、已失效的紀錄不要拿來問。")


def place_of(choice):
    return NOT_A_PLACE.sub("", choice)


def places_problem(args):
    text = question_text(args)
    if not WHERE_QUESTION.search(text) or FEW_FIT.search(text):
        return None
    choices = choices_of(args)
    places = list(dict.fromkeys(p for p in map(place_of, choices) if p))
    if len(places) >= MIN_PLACES:
        return None
    dateonly = "、".join(c for c in choices if not place_of(c)) or "無"
    return PLACES_PROBLEM.format(need=MIN_PLACES, have=len(places), places="、".join(places) or "無", dateonly=dateonly)


# 2026-11-26 (exam): 「11/27–11/29 兩天一夜」 is two nights, not one; the bot quietly booked 11/28–29. The role
# said to ask when dates disagree; now a plan waits until the owner says which.
CHINESE_NUMBER = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
DATE_RANGE = re.compile(r"(\d{1,2})/(\d{1,2})\s*(?:[–—~～\-]|到|至)\s*(?:(\d{1,2})/)?(\d{1,2})(?![\d/])")
DAYS_NIGHTS = re.compile(r"([一二兩三四五六七\d])\s*天\s*([一二兩三四五六\d])\s*夜")
# Only words that mean a same-day trip (Antigravity's review: 「第一日」「一日券」 are not).
DAY_TRIP = re.compile(r"一日遊|當天來回|當日來回|一日行程")
DATES_PROBLEM = ("使用者寫的 {span} 是 {nights} 晚，可是又說「{said}」，兩個對不上：先用 clarify 問要哪一個"
                 "（「{first}」還是「{second}」），不要自己挑日期，也先不要查價格，等使用者選了再查、再組方案。"
                 "clarify 沒人回答或逾時，就把這個問題當最後回覆送出，結束這一輪。")
# While the dates are open, research waits too (2026-11-26 exam: 19 minutes of searching around a refused plan).
RESEARCH = re.compile(r"format-plan|trip-cost|page-prices|places\.py|booking-links|place-photo")
RESEARCH_TOOLS = ("web_search", "web_extract", "browser_exec")


def number(text):
    return int(text) if text.isdigit() else CHINESE_NUMBER.get(text, 0)


def trip_words(start, nights):
    """「11/28 一日遊」 or 「12/31–1/2 三天兩夜」, built from the real dates (Antigravity's review)."""
    import datetime as dt
    if nights == 0:
        return f"{start.month}/{start.day} 一日遊"
    end = start + dt.timedelta(days=nights)
    say = {1: "一", 2: "兩", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八"}
    length = f"{say.get(nights + 1, nights + 1)}天{say.get(nights, nights)}夜"
    return f"{start.month}/{start.day}–{end.month}/{end.day} {length}"


def dates_problem(text):
    """Why the dates and the trip length in `text` disagree, or None."""
    import datetime as dt
    span = DATE_RANGE.search(text)
    if not span:
        return None
    month, day, end_month, end_day = (int(g) if g else None for g in span.groups())
    end_month = end_month or month
    try:
        year = dt.date.today().year
        start = dt.date(year, month, day)
        end = dt.date(year + (end_month < month), end_month, end_day)
    except ValueError:
        return None
    nights = (end - start).days
    stated = DAYS_NIGHTS.search(text)
    if stated:
        want, said = number(stated.group(2)), stated.group(0)
    elif DAY_TRIP.search(text) and nights:
        want, said = 0, DAY_TRIP.search(text).group(0)
    else:
        return None
    if not 0 <= nights <= 14 or nights == want:
        return None
    return DATES_PROBLEM.format(span=span.group(0), nights=nights, said=said,
                                first=trip_words(start, nights), second=trip_words(start, want))


# 2026-11-26 (exam): 「礁溪過夜和烏來一日，你幫我挑一個」 got two packages and a long sentence. The owner asked
# for one choice: one full plan, the reason on its own short line.
PICK_ONE = re.compile(r"(?:幫我|替我|你)\s*(?:挑|選)\s*一\s*[個間家處]|(?:挑|選)\s*一\s*[個間家處]\s*給我")
PICK_ONE_PROBLEM = ("使用者要你幫忙挑一個：不要加 --package。挑最合適的一個，用 format-plan.py 不加 --package 出它的"
                    "完整明細（同樣的 --item、--place、--official、--phone），為什麼選它寫成一句短的 --note。")


def answered_since_owner(home, session_id):
    """True when a clarify after the owner's latest message came back with their choice. On Telegram a
    button answer is the clarify tool's result, not a new message: without this the plan stayed refused."""
    for path in sessions.databases(home):
        try:
            with sessions.read_only(path) as db:
                last = db.execute("SELECT MAX(id) FROM messages WHERE session_id = ? AND role = 'user'",
                                  (session_id,)).fetchone()[0]
                if last is None:
                    continue
                for (content,) in db.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'tool' "
                                             "AND tool_name = 'clarify' AND id > ?", (session_id, last)):
                    try:
                        data = json.loads(content)
                    except (TypeError, ValueError):
                        continue
                    responses = data.get("responses") if isinstance(data, dict) else None  # review: only clarify's
                    if isinstance(responses, list) and any(isinstance(r, dict) and r.get("user_response")
                                                           for r in responses):
                        return True
        except sqlite3.Error:
            continue
    return False


def request_problem(tool_name, args, session_id, home):
    """Why this call does not answer what the owner just asked, or None."""
    command = str(args.get("command") or "") if tool_name == "terminal" else ""
    research = tool_name in RESEARCH_TOOLS or bool(RESEARCH.search(command))
    if not research or not session_id:
        return None
    said = sessions.owner_words(home, session_id)
    if not said:
        return None
    latest = said[-1]
    why = dates_problem(latest)
    if why and not answered_since_owner(home, session_id):
        return why
    if "format-plan" in command and PICK_ONE.search(latest) and re.search(r"(?:^|\s)--package(?:\s|$)", command):
        return PICK_ONE_PROBLEM
    return None


# 2026-11-26 (exam): told 「太貴，有沒有更便宜的」, the bot sent the same NT$300 places again as 「平價」. The role's
# rule: only places confirmed cheaper; none found is said plainly.
CHEAPER = re.compile(r"太貴|便宜一點|更便宜|省一點")  # not 「最便宜的」: that is a first request
NOT_CHEAPER = ("查過了，沒找到比上次最便宜的 {floor} 更便宜的{kind}，上次的方案照樣可以訂。"
               "想換地區或換玩法（例如不泡湯改走步道、看海）跟我說，我再找。")


def earlier_plan_floor(session_id, home):
    """The cheapest package total of the last format-plan.py run (with totals) before the owner's latest
    message, or None. Only the last: an older, cheaper trip in the same chat is not what 「太貴」 is about (review)."""
    floor = None
    for path in sessions.databases(home):
        try:
            with sessions.read_only(path) as db:
                last = db.execute("SELECT MAX(id) FROM messages WHERE session_id = ? AND role = 'user'",
                                  (session_id,)).fetchone()[0]
                if last is None:
                    continue
                ids = [call_id for (value,) in db.execute(
                           "SELECT tool_calls FROM messages WHERE session_id = ? AND id < ? AND tool_calls LIKE ?",
                           (session_id, last, "%format-plan%"))
                       for call_id, _, arguments in sessions.calls(value) if "format-plan" in arguments]
                for call_id in ids:
                    row = db.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'tool' AND "
                                     "tool_call_id = ? AND id < ?", (session_id, call_id, last)).fetchone()
                    totals = [int(re.sub(r"\D", "", a))
                              for a in TOTAL_FIRST.findall(sessions.result_text(row[0]) if row else "")]
                    if totals:
                        floor = min(totals)
        except sqlite3.Error:
            continue
    return floor


TOTAL_FIRST = re.compile(rf"^總計 ({AMOUNT})", re.M)


def not_cheaper(text, session_id, home):
    """The honest 「沒找到」 when the owner asked for cheaper and every package is not, else None."""
    totals = [int(re.sub(r"\D", "", a)) for a in TOTAL_FIRST.findall(text)]
    if not totals:
        return None
    said = sessions.owner_words(home, session_id)
    if not said or not CHEAPER.search(said[-1]):
        return None
    floor = earlier_plan_floor(session_id, home)
    if floor is None or min(totals) < floor:
        return None
    kind = "湯屋" if re.search(r"湯屋|泡湯|溫泉", said[-1]) else "地方"
    return NOT_CHEAPER.format(floor=money.money(floor), kind=kind)


def pre_tool_call(tool_name, args, task_id="", session_id="", turn_id="", **kwargs):
    if isinstance(args, dict):
        try:
            why = tamper_problem(tool_name, args) or kanban_problem(tool_name, args)
        except Exception:
            why = None
        if why:
            return {"action": "block", "message": why}
        try:
            why = request_problem(tool_name, args, session_id, os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        except Exception:  # a broken check must never stop the bot
            why = None
        if why:
            return {"action": "block", "message": why}
        try:
            why = budget_problem(tool_name, args, session_id, os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        except Exception:  # a broken check must never stop the bot
            why = None
        if why:
            return {"action": "block", "message": why}
        if tool_name == "vision_analyze" and session_id:
            VIEWED.setdefault(session_id, set()).add(photo_key(args.get("image_url")))
    if tool_name in WRITES and isinstance(args, dict):
        try:
            why = record_edit_problem(tool_name, args)
        except Exception:  # a broken check must never stop the bot
            return None
        return {"action": "block", "message": why} if why else None
    if tool_name != "clarify" or not isinstance(args, dict):
        return None
    try:
        text = question_text(args)
        if MEDIA.search(text):
            return {"action": "block", "message": MEDIA_PROBLEM}
        why = steps_problem(args, session_id, turn_id) or places_problem(args)
        home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        if not why and TOTAL.search(text) and session_id:
            output = latest_cost_output(session_id, home)
            if output is not UNKNOWN:
                why = problem(text, output)
        if not why and session_id and any(BOOKING_LINE.match(line) or MAPS.search(line)
                                          for line in text.splitlines()):
            outputs = tool_outputs(session_id, home, LINK_TOOLS)
            if outputs is not UNKNOWN:
                why = links_problem(text, outputs)
        if why:
            return {"action": "block", "message": why}
        readable = readable_questions(args)
    except Exception:  # a broken check must never stop the bot from answering
        return None
    return {"action": "modify", "args": readable} if readable else None


# 2026-11-15: a button question went unanswered for 10 minutes; clarify gave up, and the commander's
# reply then repeated the whole plan as a second message under buttons that no longer worked.
# After a timed-out question the reply says so in one line and lists the choices to type.
TIMED_OUT = sessions.Recent()  # session_id -> {"turn", "question", "choices", "minutes"}
TIMEOUT_NOTE = ("使用者還沒選，按鈕已失效。最後回覆不要重貼方案或問題（手機上已經看得到），"
                "只說一句按鈕已失效、想繼續就直接回覆編號或打字，並列出選項。")


# 2026-11-19 (Telegram): the record said 「礁溪春和泡湯官網：雙人湯屋 40 分鐘 250 元」; the commander planned on
# it, and the plan, its button question and two context summaries carried it on. The official
# page now says 699元 for 60 minutes in winter. A recorded price tells where to look, not the price.
RECORD_PRICE = money.PRICE
RECORD_NOTE = ("\n\n[旅遊紀錄提醒] 上面紀錄裡的價格只是去哪裡查的線索，可能已經過期"
               "（2026-11-19：紀錄寫春和雙人湯屋 40 分鐘 250 元，官網現在是冬季 60 分鐘 699 元）。"
               "排方案、比便宜之前，先跑 page-prices.py <官網網址> 看現在的價格，只用它印出的價格。"
               "紀錄裡的設施（例如要不要自備毛巾）也一樣，要以官網現在寫的為準。")


def record_note(tool_name, args, result):
    """The reminder to add to a read of opportunities.json that shows prices, else None."""
    if tool_name not in ("read_file", "search_files") or not isinstance(args, dict):
        return None
    where = str(args.get("path") or args.get("file_path") or "") + " " + str(args.get("file_glob") or "")
    if RECORDS not in where.replace("\\", "/") or not RECORD_PRICE.search(result) or RECORD_NOTE in result:
        return None
    return result + RECORD_NOTE


def transform_tool_result(tool_name="", args=None, result=None, session_id="", turn_id="", **kwargs):
    if isinstance(result, str):
        try:
            noted = record_note(tool_name, args, result)
        except Exception:
            noted = None
        if noted:
            return noted
    if tool_name != "clarify" or not session_id or not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("outcome") == "submitted" and turn_id:
            ANSWERED[session_id] = turn_id
        if not isinstance(data, dict) or data.get("outcome") != "timed_out":
            TIMED_OUT.pop(session_id, None)
            return None
        responses = [r for r in data.get("responses") or [] if isinstance(r, dict)]
        minutes = re.search(r"(\d+)\s*m\]", str(data.get("notice", "")))
        TIMED_OUT[session_id] = {
            "turn": turn_id,
            "question": "\n".join(str(r.get("question", "")) for r in responses),
            "choices": [str(c) for r in responses for c in r.get("choices_offered") or []],
            "minutes": minutes.group(1) if minutes else "",
        }
        return json.dumps({**data, "note": TIMEOUT_NOTE}, ensure_ascii=False)
    except Exception:
        return None


def repeats(reply, question):
    """Whether the reply re-sends the question's content (three of its lines, or its total)."""
    asked = {line.strip() for line in question.splitlines() if len(line.strip()) >= 4}
    said = [line.strip() for line in reply.splitlines() if line.strip()]
    if any(TOTAL.search(line) and line in asked for line in said):
        return True
    return sum(line in asked for line in said) >= 3


def timeout_reply(reply, pending):
    photos = [line.strip() for line in reply.splitlines() if MEDIA.match(line)]
    waited = f"{pending['minutes']} 分鐘" if pending["minutes"] else "太久"
    lines = photos + [f"上面的按鈕問題 {waited}沒選，按鈕已失效。想繼續就直接回覆編號或打字："]
    lines += [f"{n}. {choice}" for n, choice in enumerate(pending["choices"], start=1)]
    return "\n".join(lines)


# 2026-11-19: with fewer button questions, the plan went out as an ordinary reply, which the
# clarify checks never saw: it said "總計 NT$1,600（預算剩 NT$400；含未確認 NT$820）" and listed
# 公車 NT$60 and 餐飲 NT$760 without 未確認. An ordinary reply cannot be sent back, so its total
# line is replaced with the tool's headline and each unconfirmed amount gets "（未確認）".
TOTAL_AMOUNT = re.compile(rf"總計\s*({AMOUNT})")


ITEM_LINE = re.compile(rf"^\s*[•・\-*]\s*(?P<name>.+?)\s*(?P<amount>{AMOUNT})")


def item_key(line):
    """「• 溪景湯屋平日 NT$600（每1小時）」 -> ("溪景湯屋平日", "NT$600"); None for other lines."""
    found = ITEM_LINE.match(line or "")
    return (re.sub(r"\s+", "", found["name"]), found["amount"]) if found else None


def corrected_costs(text, outputs):
    """text with each 總計 line set to the headline of the trip-cost.py run with that same total,
    and those runs' unconfirmed amounts marked. A total no run printed is left alone."""
    runs = {}
    for output in outputs:  # oldest first, so the latest run with a total wins
        found = TOTAL_AMOUNT.search(output.splitlines()[0])
        if found:
            runs[found.group(1)] = output
    used = [runs[m] for m in TOTAL_AMOUNT.findall(text) if m in runs]
    if not used:
        return text
    amounts = sorted({a for output in used for a in unconfirmed_amounts(output)}, key=len, reverse=True)
    # One pass, so "NT$60（NT$30×2）" is marked once, after its unit price, and NT$30 is not re-marked.
    marked = re.compile("(?:" + "|".join(map(re.escape, amounts)) + r")(?![\d,])(?:（[^（）\n]*）)?") \
        if amounts else None
    # 2026-11-19 (Telegram): the reply listed the items under the photo, not under 總計; the tool's
    # lines went in under 總計 and the reply's own copy stayed, so every item showed twice, and the
    # confirmed 「溪景湯屋 NT$600」 was marked 未確認 because the meal was also NT$600.
    tool_items = {item_key(item) for output in used for item in output.splitlines()[1:] if item_key(item)}
    lines, items, placed = [], None, False
    for line in text.splitlines():
        total = TOTAL_AMOUNT.search(line)
        if items is not None:
            # The cost lines right under a total are the tool's own item lines (2026-11-19: 「捷運及公車
            # NT$240」 merged a confirmed NT$180 with an unconfirmed NT$60).
            if "NT$" in line and not total:
                continue
            lines.extend(items)
            items, placed = None, True
        if total:
            if total.group(1) in runs:
                run = runs[total.group(1)].splitlines()
                line = line[:total.start()] + run[0].strip()
                items = [item.strip() for item in run[1:] if item.strip().startswith("•")]
        elif item_key(line) in tool_items:
            if placed:
                continue  # the tool's line is already under 總計
        elif marked and "未確認" not in line:
            line = marked.sub(lambda m: m.group(0) + "（未確認）", line)
        lines.append(line)
    if items is not None:
        lines.extend(items)
    return "\n".join(lines)


# 2026-11-19: 「未確認就不要傳給我，傳給我確認過的就好」. With "show_unconfirmed": false in
# preferences.json, lines about anything unconfirmed are left out of the reply.
UNSURE_LINE = re.compile(rf"未確認|待確認|估算|未取得|約\s*{money.CURRENCY_BEFORE}|{AMOUNT}\s*起?左右")


def confirmed_only(home):
    home = Path(home)
    for root in (home, home.parent.parent):
        path = root / "shared" / "travel-workflow" / "preferences.json"
        try:
            prefs = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        return isinstance(prefs, dict) and prefs.get("show_unconfirmed") is False
    return False



# 2026-11-21: with show_unconfirmed off, only amounts trip-cost.py printed survived, so a plan from a
# Kanban card lost every price line on the way out: twice the user got only 「來源為 Google Flights 與
# Hotels，訂票與付款由你自行確認。」. Amounts in a card this conversation looked at are confirmed when the
# card is done and its reviewer worker really passed it (a run the dispatcher spawned, not one closed by
# hand): its attachments and run summaries are the reviewed result.
amounts_in = money.amounts
CARD_ID = re.compile(r"(?<![\w-])t_[0-9a-f]{8}(?![\w-])")


def _board(home):
    home = Path(home)
    for root in (home, home.parent.parent):
        path = root / "kanban" / "boards" / "command-center" / "kanban.db"
        if path.is_file():
            return path
    return None


def session_cards(session_id, home):
    """Card IDs named anywhere in this conversation (tool calls and results), oldest first."""
    for path in sessions.databases(home):
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            rows = db.execute("SELECT content, tool_calls FROM messages WHERE session_id = ? ORDER BY id",
                              (session_id,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            db.close()
        if rows:
            seen = []
            for content, calls in rows:
                for card in CARD_ID.findall(f"{content or ''} {calls or ''}"):
                    if card not in seen:
                        seen.append(card)
            return seen
    return []


def card_bodies(said, home):
    """The bodies of the cards whose notices (「看板任務 t_… 已完成」) came into this conversation. A card's
    budget was checked against the user's words when it was made, so its report may repeat it: the user
    said 「整趟預算 4 萬」 in another conversation, and the report arrives in a new one. Only cards in those
    notices count, not every card a `kanban list` printed (one of those had a 2000 of its own)."""
    board = _board(home)
    cards = list(dict.fromkeys(c for text in said for c in CARD_ID.findall(text))) if board else []
    if not cards:
        return []
    try:
        with sessions.read_only(board) as db:
            marks = ",".join("?" * len(cards))
            bodies = [row[0] or "" for row in db.execute(f"SELECT body FROM tasks WHERE id IN ({marks})", cards)]
    except sqlite3.Error:
        return []
    # Only the card's budget, not its other figures (a room price, a year): Antigravity's review of db4a2e0.
    return [written for body in bodies for written, _ in money.stated_budgets(body)]


def heard(home, session_id):
    """What backs a budget in this conversation: the user's words and the cards it looked at; None if unknown."""
    said = sessions.owner_words(home, session_id)
    return None if said is None else said + card_bodies(said, home)


def reviewed_card_amounts(session_id, home):
    """Amounts in the attachments and run summaries of this conversation's reviewed, done cards."""
    board = _board(home)
    cards = session_cards(session_id, home) if board else []
    if not cards:
        return set()
    found = set()
    try:
        db = sqlite3.connect(f"file:{board.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return found
    try:
        for card in cards:
            row = db.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
            run = db.execute("SELECT id FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
                             "ORDER BY id DESC LIMIT 1", (card,)).fetchone()
            if not row or row[0] != "done" or not run or not db.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'spawned'",
                    (card, run[0])).fetchone():
                continue
            texts = [s or "" for (s,) in db.execute("SELECT summary FROM task_runs WHERE task_id = ?", (card,))]
            attachments = board.parent / "attachments" / card
            for (stored,) in db.execute("SELECT stored_path FROM task_attachments WHERE task_id = ?", (card,)):
                path = Path(stored or "")
                try:
                    if path.resolve().is_relative_to(attachments.resolve()) and path.is_file():
                        texts.append(path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    continue
            for text in texts:
                found.update(amounts_in(text))
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return found


def without_unconfirmed(text, outputs, reviewed=()):
    """text without lines that state unconfirmed things: 「未確認」, 「待確認：…」, estimates, and
    amounts neither the latest trip-cost.py run nor a reviewed card printed (*reviewed*). The tool's
    own lines stay."""
    printed = {line.strip() for output in outputs[-1:] for line in output.splitlines() if line.strip()}
    known = {a for line in printed for a in money.amounts(line)}
    known |= set(reviewed)
    kept = []
    for line in text.splitlines():
        if line.strip() in printed:
            kept.append(line)
            continue
        if UNSURE_LINE.search(line):
            continue
        if any(a not in known for a in money.amounts(line)):
            continue
        kept.append(line)
    return "\n".join(kept)


# 2026-11-20: the words beside a photo on a page do not say what it shows (玫瑰園's 「溫泉湯屋」 photo
# was the shop front). A saved photo goes out only after the agent looked at it with vision_analyze.
VIEWED = sessions.Recent()  # session_id -> photo paths the agent looked at
PHOTO = re.compile(r"^\s*MEDIA:\s*(?P<path>\S+)")
UNSEEN = "照片：未取得"


def photo_key(path):
    return str(path or "").strip().strip("\"'").replace("\\", "/").lower()


def without_unseen_photos(text, session_id):
    """text without saved photos the agent never looked at (and their 📷 caption)."""
    seen = VIEWED.get(session_id, set())
    if not any(PHOTO.match(line) for line in text.splitlines()):
        return text
    kept, dropped = [], False
    for line in text.splitlines():
        match = PHOTO.match(line)
        if match and "travel-workflow/photos/" in photo_key(match["path"]) and photo_key(match["path"]) not in seen:
            kept.append(UNSEEN)
            dropped = True
            continue
        if dropped and line.lstrip().startswith("📷"):
            dropped = False
            continue
        dropped = False
        kept.append(line)
    return "\n".join(kept)


def without_unsaid_budgets(text, session_id, home):
    """A reply's budget the user never gave becomes 「預算未確認」 (2026-11-25)."""
    if not money.stated_budgets(text):
        return text
    said = heard(home, session_id)
    if said is None:
        return text
    for written, _ in money.unsaid_budgets(text, said, owner_preferences(home)):
        text = text.replace(written, "預算未確認")
    return text


# 2026-11-26 (exam): refused by format-plan.py, the bot wrote 「方案 1｜和平島…」 and 「① 雲頂溫泉行館…」 by
# hand, with prices nobody checked. A package goes out only as format-plan.py printed it.
PACKAGE_LINE = re.compile(r"^\s*(?:[①②③④⑤]|方案\s*[1-5一二三四五][｜|：:])")
HAND_WRITTEN = ("這次沒組成能直接訂的方案：有幾個地方的價格沒能在官網確認，所以先不給你。"
                "回我一聲，我就換別的地方重查。")


# Only a numbered entry with a price is a package (Antigravity's review): 「① 先決定日期 ② 再挑地方」 is not.
def package_heads(text):
    """Head lines of the numbered entries that carry a price within their own block."""
    lines, heads = text.splitlines(), []
    starts = [i for i, line in enumerate(lines) if PACKAGE_LINE.match(line)]
    for at, end in zip(starts, starts[1:] + [len(lines)]):
        if money.PRICE.search("\n".join(lines[at:end])):
            heads.append(lines[at].strip())
    return heads


def hand_written(text, session_id, home):
    """True when the reply has packages that no format-plan.py run in this conversation printed."""
    heads = package_heads(text)
    if not heads:
        return False
    printed = tool_outputs(session_id, home, "format-plan.py")
    if printed is UNKNOWN:
        return False
    made = "\n".join(printed)
    return any(head not in made for head in heads)


def transform_llm_output(response_text="", session_id="", turn_id="", **kwargs):
    if not session_id or not isinstance(response_text, str):
        return None
    pending = TIMED_OUT.pop(session_id, None)
    try:
        if pending and not (pending["turn"] and turn_id and pending["turn"] != turn_id) \
                and repeats(response_text, pending["question"]):
            return timeout_reply(response_text, pending)
        home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        if hand_written(response_text, session_id, home):
            return HAND_WRITTEN
        honest = not_cheaper(response_text, session_id, home)
        if honest:
            return honest
        fixed = without_unseen_photos(response_text, session_id)
        fixed = without_unsaid_budgets(fixed, session_id, home)
        hide = confirmed_only(home)
        if hide or TOTAL_AMOUNT.search(fixed):
            outputs = tool_outputs(session_id, home, COST_TOOLS)
            runs = [] if outputs is UNKNOWN else [blocks for o in outputs if (blocks := cost_blocks(o))]
            blocks = [b for run in runs for b in run]  # one per package of a format-plan.py --package run
            fixed = corrected_costs(fixed, blocks) if blocks else fixed
            if hide:  # every package of the latest run is printed, not only its first
                latest = ["\n".join(runs[-1])] if runs else []
                fixed = without_unconfirmed(fixed, latest, reviewed_card_amounts(session_id, home))
        return fixed if fixed != response_text else None
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("transform_llm_output", transform_llm_output)
