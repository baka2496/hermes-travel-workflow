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
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote

TOTAL = re.compile(r"總計 NT\$[\d,]+[^\n]*")
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
                                       "ORDER BY id", (session_id, f"%{TOOL}%")):
                for call in json.loads(calls or "[]"):
                    function = call.get("function", call)
                    if TOOL in str(function.get("arguments", "")):
                        ids.append(call.get("id") or call.get("call_id"))
            for call_id in reversed(ids):
                row = db.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'tool' "
                                 "AND tool_call_id = ? ORDER BY id DESC LIMIT 1", (session_id, call_id)).fetchone()
                if not row:
                    continue
                try:
                    output = json.loads(row[0]).get("output", "")
                except (ValueError, AttributeError):
                    output = row[0] or ""
                if output.lstrip().startswith("總計"):
                    return output.replace("\r\n", "\n").strip()
            return None
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
            amounts.update(re.findall(r"NT\$[\d,]+", line.split("，未確認")[0]))
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


def readable_questions(args):
    """args with every question's markdown links written out, or None when there are none."""
    questions = args.get("questions")
    if isinstance(questions, list):
        changed = [dict(q, question=plain_links(str(q.get("question", "")))) if isinstance(q, dict) else q
                   for q in questions]
        return {"questions": changed} if changed != questions else None
    if isinstance(args.get("question"), str) and LINK.search(args["question"]):
        return {"question": plain_links(args["question"])}
    return None


def pre_tool_call(tool_name, args, task_id="", session_id="", turn_id="", **kwargs):
    if tool_name in ("patch", "write_file") and isinstance(args, dict):
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
        why = None
        if TOTAL.search(text) and session_id:
            home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
            output = latest_cost_output(session_id, home)
            if output is not UNKNOWN:
                why = problem(text, output)
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


def transform_tool_result(tool_name="", args=None, result=None, session_id="", turn_id="", **kwargs):
    if tool_name != "clarify" or not session_id or not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
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


def transform_llm_output(response_text="", session_id="", turn_id="", **kwargs):
    pending = TIMED_OUT.pop(session_id, None) if session_id else None
    if not pending or not isinstance(response_text, str):
        return None
    if pending["turn"] and turn_id and pending["turn"] != turn_id:
        return None
    try:
        return timeout_reply(response_text, pending) if repeats(response_text, pending["question"]) else None
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("transform_llm_output", transform_llm_output)
