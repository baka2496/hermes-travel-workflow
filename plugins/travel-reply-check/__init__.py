"""Hold back a button question whose cost figures do not match trip-cost.py's output.

2026-11-14 (Telegram test 10): 小居 sent its clarify in the same batch as trip-cost.py, so the
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


def _load_money():
    """travel_money.py, the price format shared with trip-cost.py: installed beside this file,
    or in the checkout's scripts/ folder. A missing copy fails loudly here, at load, rather than
    leaving every later check to fail inside a hook that swallows its errors."""
    here = Path(__file__).resolve().parent
    for path in (here / "travel_money.py", here.parents[1] / "scripts" / "travel_money.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location("travel_money", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"travel_money.py not found beside {here}")


money = _load_money()
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


def databases(home):
    home = Path(home)
    found = [home / "state.db"] if (home / "state.db").is_file() else []
    return found + sorted((home / "profiles").glob("*/state.db"))


UNKNOWN = object()  # the conversation is in no readable database: nothing to check against


def latest_cost_output(session_id, home):
    """Output of the last trip-cost.py run in this conversation, None if it has none yet,
    or UNKNOWN if the conversation cannot be found."""
    outputs = tool_outputs(session_id, home, TOOL)
    if outputs is UNKNOWN:
        return UNKNOWN
    headed = [o for o in outputs if o.startswith("總計")]
    return headed[-1] if headed else None


def tool_outputs(session_id, home, tool):
    """Outputs of every run of `tool` in this conversation, oldest first (empty if none),
    or UNKNOWN if the conversation cannot be found."""
    for path in databases(home):
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            if not db.execute("SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)).fetchone():
                continue
            ids = []
            for (calls,) in db.execute("SELECT tool_calls FROM messages WHERE session_id = ? AND tool_calls LIKE ? "
                                       "ORDER BY id", (session_id, f"%{tool}%")):
                for call in json.loads(calls or "[]"):
                    function = call.get("function", call)
                    if tool in str(function.get("arguments", "")):
                        ids.append(call.get("id") or call.get("call_id"))
            outputs = []
            for call_id in ids:
                row = db.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'tool' "
                                 "AND tool_call_id = ? ORDER BY id DESC LIMIT 1", (session_id, call_id)).fetchone()
                if not row:
                    continue
                try:
                    output = json.loads(row[0]).get("output", "")
                except (ValueError, AttributeError):
                    output = row[0] or ""
                output = str(output or "").replace("\r\n", "\n").strip()
                if output:
                    outputs.append(output)
            return outputs
        except sqlite3.Error:
            continue
        finally:
            db.close()
    return UNKNOWN


def unconfirmed_amounts(output):
    """NT$ figures (item total and unit price) of each item trip-cost.py marked 未確認."""
    amounts = set()
    for line in output.splitlines()[1:]:
        if "未確認" in line:
            amounts.update(money.amounts(line.split("，未確認")[0]))
    return amounts


def problem(text, output):
    """Why this question must not go out yet, or None."""
    totals = TOTAL.findall(text)
    if not totals:
        return None
    if output is None:
        return (f"這則有「總計」，但這次對話還沒有 {TOOL} 的結果。先單獨執行 {TOOL}，等結果回來，"
                "再用 clarify 問，總計照它印的第一行寫。")
    headline = output.splitlines()[0].strip()
    for total in totals:
        if total.strip() != headline:
            return f"總計要照 {TOOL} 最後印的第一行寫：「{headline}」，不是「{total.strip()}」。改好再問。"
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


# 2026-11-19: 「我要跟小居去泡溫泉」 took three button questions in a row — keep the plan that was
# already set, then "看費用明細", then another "下一步". A choice is a decision, never a way to
# see information, and one message from the user gets at most one button question.
INFO_CHOICE = re.compile(r"^\s*(?:\d+[.、]\s*)?(?:看|查看|顯示|列出|展開|瀏覽)")
INFO_PROBLEM = ("按鈕選項只能放不同的做法，不能放「{choice}」這種看資訊的選項：費用每項一行、交通或行程重點直接寫進方案。"
                "拿掉這個選項；剩下的如果不是真的要使用者選，就不用按鈕，直接把結果講完。")
ANSWERED = {}  # session_id -> turn_id whose button question the user already answered
SECOND_PROBLEM = ("這則訊息已經問過一次按鈕，使用者也選了。照選的做完，用一般回覆講結果就結束，"
                  "不要再用按鈕問下一步；之後要做什麼讓使用者自己說。")


def choices_of(args):
    questions = args.get("questions")
    if isinstance(questions, list):
        return [str(c) for q in questions if isinstance(q, dict) for c in q.get("choices") or []]
    return [str(c) for c in args.get("choices") or []]


def steps_problem(args, session_id, turn_id):
    if session_id and turn_id and ANSWERED.get(session_id) == turn_id:
        return SECOND_PROBLEM
    info = [c for c in choices_of(args) if INFO_CHOICE.match(c)]
    return INFO_PROBLEM.format(choice=info[0].strip()) if info else None


# 2026-11-16: booking-links.py printed the map link for 「山口溫泉飯店」, and the question showed
# one for 「山口溫泉館」 that 小居 had typed itself. Booking lines are pasted as printed.
LINKS_TOOL = "booking-links.py"
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


# 2026-11-19 (Telegram): asked for a cheaper hot spring, 小居 took 42 minutes. trip-cost.py
# rightly left out 「雙人池 40分鐘 NT$250」 (the official page shows no price; its price page was
# 404), and 小居 spent half an hour querying state.db, reading the checkers' code and patching
# page_evidence.py to make the price pass; the failed patch's warning reached the user's phone.
# The checkers' verdict is final: the workflow's own scripts, this plugin and the session
# databases are off limits to the bot.
# travel-workflow/pages holds page-prices.py's snapshots of what pages showed: the evidence itself.
GUARDED = re.compile(r"(?:shared[\\/]+travel-workflow[\\/]+(?:[\w.-]+\.py|pages\b)|travel-reply-check[\\/]|travel[-_]rules"
                     r"|\.db\b|sqlite3|profiles[\\/]+[\w-]+[\\/]+cache\b|page_cache|sys\.path\.(?:append|insert)|spec_from_file_location"
                     r"|\bimport\s+(?:page_evidence|travel_money|trip_cost|place_photo|booking_links|phone_check))", re.I)
WRITES = ("patch", "write_file")
# `python …/trip-cost.py …`: the script right after a Python interpreter is being run, not read.
RUN_TOOL = re.compile(r"""((?:^|[\s;&|(])["']?[^\s"';&|]*python[\w.]*["']?\s+)["']?[^\s"']*"""
                      r"""shared[\\/]+travel-workflow[\\/]+[\w.-]+\.py["']?(?=\s|$)""", re.I)
TAMPER_PROBLEM = ("不能查或改檢查程式（shared/travel-workflow 的 .py、travel-reply-check 外掛、state.db）。"
                  "檢查結果就是結論：價格沒通過，就表示開過的官網上沒有這個價格。"
                  "要知道網頁上實際寫什麼價格，就跑 page-prices.py <官網網址>；"
                  "直接照工具輸出寫回覆：改推薦官網上有標價的地方，或不寫這個價格。"
                  "工具用法照說明書寫的跑。")


def tamper_problem(tool_name, args):
    if tool_name in WRITES:
        path = str(args.get("path") or args.get("file_path") or "")
        return TAMPER_PROBLEM if GUARDED.search(path) else None
    if tool_name == "terminal":
        command = str(args.get("command") or "")
        # Running the tools (python …/trip-cost.py …) is fine; reading their insides is not.
        inside = RUN_TOOL.sub(r"\1 ", command)
        return TAMPER_PROBLEM if GUARDED.search(inside) else None
    if tool_name in ("read_file", "search_files"):
        path = str(args.get("path") or args.get("file_path") or "")
        return TAMPER_PROBLEM if GUARDED.search(path) else None
    return None


def pre_tool_call(tool_name, args, task_id="", session_id="", turn_id="", **kwargs):
    if isinstance(args, dict):
        try:
            why = tamper_problem(tool_name, args)
        except Exception:
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
        why = steps_problem(args, session_id, turn_id)
        home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        if not why and TOTAL.search(text) and session_id:
            output = latest_cost_output(session_id, home)
            if output is not UNKNOWN:
                why = problem(text, output)
        if not why and session_id and any(BOOKING_LINE.match(line) or MAPS.search(line)
                                          for line in text.splitlines()):
            outputs = tool_outputs(session_id, home, LINKS_TOOL)
            if outputs is not UNKNOWN:
                why = links_problem(text, outputs)
        if why:
            return {"action": "block", "message": why}
        readable = readable_questions(args)
    except Exception:  # a broken check must never stop the bot from answering
        return None
    return {"action": "modify", "args": readable} if readable else None


# 2026-11-15: a button question went unanswered for 10 minutes; clarify gave up, and 小居's
# reply then repeated the whole plan as a second message under buttons that no longer worked.
# After a timed-out question the reply says so in one line and lists the choices to type.
TIMED_OUT = {}  # session_id -> {"turn", "question", "choices", "minutes"}
TIMEOUT_NOTE = ("使用者還沒選，按鈕已失效。最後回覆不要重貼方案或問題（手機上已經看得到），"
                "只說一句按鈕已失效、想繼續就直接回覆編號或打字，並列出選項。")


# 2026-11-19 (Telegram): the record said 「礁溪春和泡湯官網：雙人湯屋 40 分鐘 250 元」; 小居 planned on
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


def without_unconfirmed(text, outputs):
    """text without lines that state unconfirmed things: 「未確認」, 「待確認：…」, estimates, and
    amounts the latest trip-cost.py run did not print. The tool's own lines stay."""
    printed = {line.strip() for output in outputs[-1:] for line in output.splitlines() if line.strip()}
    known = {a for line in printed for a in money.amounts(line)}
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
VIEWED = {}  # session_id -> photo paths the agent looked at
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


def transform_llm_output(response_text="", session_id="", turn_id="", **kwargs):
    if not session_id or not isinstance(response_text, str):
        return None
    pending = TIMED_OUT.pop(session_id, None)
    try:
        if pending and not (pending["turn"] and turn_id and pending["turn"] != turn_id) \
                and repeats(response_text, pending["question"]):
            return timeout_reply(response_text, pending)
        fixed = without_unseen_photos(response_text, session_id)
        home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        hide = confirmed_only(home)
        if hide or TOTAL_AMOUNT.search(fixed):
            outputs = tool_outputs(session_id, home, TOOL)
            outputs = [] if outputs is UNKNOWN else [o for o in outputs if o.startswith("總計")]
            fixed = corrected_costs(fixed, outputs) if outputs else fixed
            if hide:
                fixed = without_unconfirmed(fixed, outputs)
        return fixed if fixed != response_text else None
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("transform_llm_output", transform_llm_output)
