"""Check that a page you opened, or an opportunity record taken from it, shows a given value.

2026-11-14 (Telegram test): "山口溫泉飯店週六雙人房=1600x1@<Booking page>" counted as confirmed,
though no page was opened that turn and the record for that page showed NT$1,100. A source counts
only if that page, as opened (web_extract or the browser, in the last few hours) or as recorded in
opportunities.json, shows the value. Used by trip-cost.py (prices) and booking-links.py (phones).
"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time

HERE = Path(__file__).resolve().parent
RECORDS = HERE / "opportunities.json"  # beside the installed scripts in shared/travel-workflow
HERMES_ROOT = HERE.parent.parent       # shared/travel-workflow -> the Hermes home with profiles/

PAGE_TOOLS = ("web_extract", "browser")  # tools that open pages; not terminal, whose args echo our own items
RECENT_HOURS = 6
URL = re.compile(r"https?://[^\s\\\"'<>()（）]+")


def page_key(url):
    """Compare pages without scheme, fragment, or trailing slash."""
    url = url.split("#", 1)[0].rstrip("/.,;，。")
    return re.sub(r"^https?://", "", url).lower()


def records_show(records, key, pattern):
    """True if an opportunity record whose source is that page mentions the value."""
    def walk(node):
        if isinstance(node, dict):
            source = node.get("source")
            if isinstance(source, str) and any(page_key(u) == key for u in URL.findall(source)):
                if pattern.search(json.dumps(node, ensure_ascii=False)):
                    return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False
    return walk(records)


def opened_pages_show(hermes_root, key, pattern, now):
    """True if a page tool opened that page in the last few hours and its text shows the value."""
    return opened_page_text(hermes_root, key, pattern, now) is not None


def opened_page_text(hermes_root, key, pattern, now):
    """The text of the latest page-tool result for that page, in the last few hours, that shows the
    value; None if there is none."""
    since = now - RECENT_HOURS * 3600
    found = None
    for db_path in sorted((hermes_root / "profiles").glob("*/state.db")):
        try:
            with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
                text = shows(db, since, key, pattern)
                if text is not None:
                    found = text
        except sqlite3.Error:  # a busy or foreign database is no evidence either way
            continue
    return found


def shows(db, since, key, pattern):
    """The latest page-tool result in this database that shows the value and was for that page, or None."""
    rows = db.execute("SELECT id, session_id, tool_call_id, tool_name, content FROM messages "
                      "WHERE role = 'tool' AND timestamp >= ? ORDER BY id DESC", (since,)).fetchall()
    for row_id, session, call_id, tool, content in rows:
        if not tool or not tool.startswith(PAGE_TOOLS) or not content or not pattern.search(visible(content)):
            continue
        if key in result_pages(db, session, row_id, call_id, content):
            return visible(content)
    return None


# 2026-11-19 (Telegram): 「客房泡湯2小時 NT$800」 counted as confirmed from 湯布苑's page. Its visible
# prices were 「假日特價 $800 / 1小時」; 「價格800起，平日2小時」 sat only inside a share button's
# URL-encoded link. Text a visitor cannot read is no evidence.
ENCODED = re.compile(r"[^\s\"'<>\\]*(?:%[0-9A-Fa-f]{2}){2}[^\s\"'<>\\]*")


def visible(text):
    """The text without URL-encoded runs (share links, tracking parameters)."""
    return ENCODED.sub(" ", text or "")


# 2026-11-19: the plan said 「雙人溪景湯屋 NT$600」; the official page said 「平日特價 $600 / 1小時」.
UNIT_AFTER = re.compile(r"^\s*(?:元|TWD)?\s*(?:[/／]\s*|每\s*)(?P<unit>\d*\s*(?:小時|hr|分鐘|晚|人|位|次|間|房|天|日))")
STARTING = re.compile(r"^\s*(?:元)?\s*起")
UNIT_BEFORE = re.compile(r"每\s*(?P<unit>\d*\s*(?:小時|分鐘|晚|人|位|次|間|房|天|日))\s*[:：]?\s*(?:NT\$|\$|TWD)?\s*$")


DAY = re.compile(r"平日|假日|週末|周末|週六|週日|例假日")


def price_details(text, pattern):
    """What the page says right next to the price: {"unit": 「1小時」, 「人」, 「起價」 or None,
    "day": 「平日」, 「假日」 or None}, from the first place the price appears with either."""
    for match in pattern.finditer(text or ""):
        after, before = text[match.end():match.end() + 20], text[max(0, match.start() - 20):match.start()]
        unit = UNIT_AFTER.match(after) or UNIT_BEFORE.search(before)
        unit = re.sub(r"\s+", "", unit["unit"]) if unit else "起價" if STARTING.match(after) else None
        # The label nearest the price: 「平日特價 $600 / 1小時 | 假日特價 $800」 puts both before 800.
        line = re.split(r"[\n|｜]", before)[-1]
        days = DAY.findall(line)
        day = ("平日" if days[-1] == "平日" else "假日") if days else None
        if unit or day:
            return {"unit": unit, "day": day}
    return {"unit": None, "day": None}


def price_unit(text, pattern):
    """What the page says the price is for, right next to it: 「1小時」, 「人」, or 「起價」; else None."""
    return price_details(text, pattern)["unit"]


def details_shown(source, pattern, hermes_root=HERMES_ROOT, now=None):
    """What an opened page says next to this price (see price_details)."""
    text = opened_page_text(Path(hermes_root), page_key(source), pattern, now or time.time())
    return price_details(text, pattern)


def own_call_pages(db, session, row_id, call_id):
    """Pages named in the arguments of the call this result answers, e.g. new_tab('<url>')."""
    if not call_id:
        return set()
    row = db.execute("SELECT tool_calls FROM messages WHERE session_id = ? AND id < ? AND tool_calls LIKE ? "
                     "ORDER BY id DESC LIMIT 1", (session, row_id, f"%{call_id}%")).fetchone()
    for call in json.loads(row[0]) if row else []:
        if (call.get("id") or call.get("call_id")) == call_id:
            return {page_key(u) for u in URL.findall(str(call.get("function", call).get("arguments", "")))}
    return set()


def result_pages(db, session, row_id, call_id, content):
    """The pages a page-tool result may have come from.

    1. The pages its own call opened (a browser script with new_tab('<url>'), web_extract's urls).
       Parallel calls each open their own page, so another call's page never counts.
    2. Else the last address in the result itself.
    3. Else the page the browser was left on by the page-tool calls just before it
       (a script that only reads the current page names no address)."""
    own = own_call_pages(db, session, row_id, call_id)
    if own:
        return own
    page = current_page(db, session, row_id, content)
    return {page} if page else set()


def current_page(db, session, row_id, content):
    """The page a page-tool result came from: the last address named in that result, or else in the
    page-tool calls and results just before it (a browser script that reads the page names no address)."""
    found = [page_key(u) for u in URL.findall(content)]
    if found:
        return found[-1]
    earlier = db.execute("SELECT tool_name, tool_calls, content FROM messages WHERE session_id = ? AND id < ? "
                         "ORDER BY id DESC LIMIT 40", (session, row_id)).fetchall()
    for tool, calls, text in earlier:
        if calls:
            args = [c.get("function", c).get("arguments", "") for c in json.loads(calls)
                    if str(c.get("function", c).get("name", "")).startswith(PAGE_TOOLS)]
            text = " ".join(str(a) for a in args)
        elif not (tool or "").startswith(PAGE_TOOLS):
            continue
        found = [page_key(u) for u in URL.findall(text or "")]
        if found:
            return found[-1]
    return None


def price_pattern(price):
    digits = f"{price:,}".replace(",", ",?")
    return re.compile(rf"(?<![\d.,]){digits}(?![\d]|,\d)")


def phone_pattern(phone):
    """03-9102000 also matches 03-910-2000 and (03)910 2000."""
    digits = re.sub(r"\D", "", phone)
    return re.compile(r"(?<!\d)" + r"[\s\-–()]*".join(digits) + r"(?!\d)") if digits else None


def shown(source, pattern, records=RECORDS, hermes_root=HERMES_ROOT, now=None, use_records=True):
    """True if the page `source` shows text matching `pattern`.

    use_records=False: only a page opened in the last few hours counts. Prices need this
    (2026-11-19): a record sourced to the 台北捷運 fare page mentioned 「TWD 120」 for 輔大↔台北車站
    (two people), and that confirmed 「捷運公車來回 NT$120×2」 to 烏來, which the page never showed.
    A record summarises several fares and rooms, so a number in it does not say which item it
    prices, and it may be days old. A phone number is specific enough for a record to vouch for it."""
    key = page_key(source)
    if use_records:
        try:
            data = json.loads(Path(records).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if records_show(data, key, pattern):
            return True
    return opened_pages_show(Path(hermes_root), key, pattern, now or time.time())
