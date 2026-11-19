"""Check that a page you opened, or an opportunity record taken from it, shows a given value.

2026-11-14 (Telegram test): "山口溫泉飯店週六雙人房=1600x1@<Booking page>" counted as confirmed,
though no page was opened that turn and the record for that page showed NT$1,100. A source counts
only if that page, as opened (web_extract or the browser, in the last few hours) or as recorded in
opportunities.json, shows the value. Used by trip-cost.py (prices) and booking-links.py (phones).
"""
from contextlib import closing
import hashlib
from html.parser import HTMLParser
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
    """The text of the latest page read for that page, in the last few hours, that shows the
    value: a page-prices.py snapshot, else a page-tool result. None if there is none."""
    since = now - RECENT_HOURS * 3600
    snapshot = snapshot_text(hermes_root, key, since)
    if snapshot is not None and pattern.search(snapshot):
        return snapshot
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


# 2026-11-19 (Telegram): trip-cost.py said only 「來源網頁上沒看到這個價格」 for 春和's 「雙人池 40分鐘
# NT$250」 (a price from an old record), and 小居 spent half an hour in state.db and the checkers'
# code to find out why. page-prices.py opens the page itself, keeps what a visitor sees as a
# snapshot here, and prints its prices (「冬季(10-4月)：699元 / 60分鐘」); trip-cost.py checks a
# price against that snapshot and, when it is not there, names the prices the page does show.
def snapshot_dir(hermes_root):
    return Path(hermes_root) / "shared" / "travel-workflow" / "pages"


def snapshot_path(hermes_root, key):
    return snapshot_dir(hermes_root) / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"


def save_snapshot(hermes_root, url, text, headings=(), now=None):
    path = snapshot_path(hermes_root, page_key(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "fetched_at": now or time.time(), "text": text,
                                "headings": list(headings)},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def snapshot_text(hermes_root, key, since):
    """The page's text as page-prices.py last read it, if that was after `since`."""
    try:
        data = json.loads(snapshot_path(hermes_root, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or page_key(str(data.get("url", ""))) != key:
        return None
    if float(data.get("fetched_at") or 0) < since:
        return None
    return str(data.get("text") or "")


ANY = re.compile(r".", re.S)


def latest_page_text(hermes_root, source, now=None):
    """What the page `source` showed when last read in the last few hours, whatever it showed."""
    return opened_page_text(Path(hermes_root), page_key(source), ANY, now or time.time())


PRICE = re.compile(r"(?:NT\$|NTD|TWD|\$)\s*\d[\d,]*|\d[\d,]*\s*(?:元|TWD)")
CJK = re.compile(r"[一-鿿]")


def price_lines(text, headings=(), limit=40):
    """The lines of a page's text that show a price, each once and in page order, each after the
    heading it sits under: 「溪景湯屋｜平日特價 $600 / 1小時」 (湯布苑 lists four rooms' prices
    under their names)."""
    seen, lines, heading, names = set(), [], "", set(headings)
    just_named = False
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line in names:
            # 2026-11-20: 春和's 「價位資訊」 heading is followed by its English 「Price」, and 小居 named
            # the item 「Price 60分鐘」. An English heading right after a Chinese one is its translation.
            if not (just_named and CJK.search(heading) and not CJK.search(line)):
                heading = line
            just_named = True
            continue
        if line:
            just_named = False
        if not line or not PRICE.search(line) or len(line) > 160:
            continue
        shown = f"{heading}｜{line}" if heading and heading not in line else line
        if shown not in seen:
            seen.add(shown)
            lines.append(shown)
    return lines[:limit]


class _Visible(HTMLParser):
    """What a visitor reads: block elements become lines; table cells are written out with their
    row and column headers, rowspan and colspan included, so 「699元」 reads
    「冬季(10-4月)：699元 / 60分鐘」. Scripts, styles, comments and hidden elements are left out."""
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "tr",
             "dt", "dd", "header", "footer", "ul", "ol", "table", "form", "label"}
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "select", "option"}

    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "base", "col", "embed", "wbr", "track"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skipping, self.tables = [], [], []  # skipping: the open skipped tags
        self.headings, self.heading_at = [], None

    @property
    def skip(self):
        return bool(self.skipping)

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip:
            self.write("\n")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or re.search(r"display\s*:\s*none", attrs.get("style") or "", re.I)
        if tag in self.VOID:
            if tag == "br" and not self.skip:
                self.write("\n")
            return
        if self.skip or tag in self.SKIP or hidden:
            self.skipping.append(tag)  # every tag inside a skipped one, so the end tags pair up
            return
        if tag in ("h1", "h2", "h3", "h4") and not self.tables:
            self.write("\n")
            self.heading_at = len(self.out)
            return
        if tag == "table":
            self.tables.append({"rows": [], "row": None, "cell": None})
        elif self.tables and tag == "tr":
            self.tables[-1]["row"] = []
        elif self.tables and tag in ("td", "th"):
            span = lambda name: max(1, min(20, int(re.sub(r"\D", "", attrs.get(name) or "") or 1)))
            self.tables[-1]["cell"] = {"text": [], "rows": span("rowspan"), "cols": span("colspan")}
        elif tag in self.BLOCK:
            self.write("\n")

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in self.skipping:  # close it and anything left unclosed inside it (<option>)
                del self.skipping[len(self.skipping) - 1 - self.skipping[::-1].index(tag):]
            return
        if tag in ("h1", "h2", "h3", "h4") and self.heading_at is not None:
            name = re.sub(r"\s+", " ", "".join(self.out[self.heading_at:])).strip()
            if name and not re.fullmatch(r"B?\d+F|[|｜/\d\s./-]*", name):
                self.headings.append(name)
            self.heading_at = None
            self.write("\n")
            return
        if self.tables and tag in ("td", "th"):
            table = self.tables[-1]
            if table["cell"] is not None:
                if table["row"] is None:
                    table["row"] = []
                table["row"].append(table["cell"])
                table["cell"] = None
        elif self.tables and tag == "tr":
            table = self.tables[-1]
            if table["row"] is not None:
                table["rows"].append(table["row"])
            table["row"] = None
        elif self.tables and tag == "table":
            self.write("\n" + "\n".join(table_lines(self.tables.pop()["rows"])) + "\n")
        elif tag in self.BLOCK:
            self.write("\n")

    def handle_data(self, data):
        if not self.skip:
            self.write(data)

    def write(self, text):
        if self.tables and self.tables[-1]["cell"] is not None:
            self.tables[-1]["cell"]["text"].append(text)
        elif self.tables and text.strip():
            self.tables[-1].setdefault("loose", []).append(text)
        else:
            self.out.append(text)


def table_lines(rows):
    """Lay out a table's cells on a grid (rowspan, colspan), then write each priced cell with its
    row label and column header: 「冬季(10-4月)：699元 / 60分鐘」. Other rows are joined with ｜."""
    grid, pending = [], {}  # pending: (row, column) -> text of a cell spanning down into it
    for r, row in enumerate(rows):
        line, c = {}, 0
        for cell in row:
            while (r, c) in pending:
                line[c] = pending.pop((r, c))
                c += 1
            text = re.sub(r"\s+", " ", "".join(cell["text"])).strip()
            for dr in range(cell["rows"]):
                for dc in range(cell["cols"]):
                    if dr == 0:
                        line[c + dc] = text
                    else:
                        pending[(r + dr, c + dc)] = text
            c += cell["cols"]
        for spot in sorted(k for k in pending if k[0] == r):
            line[spot[1]] = pending.pop(spot)
        grid.append([line.get(i, "") for i in range(max(line) + 1)] if line else [])
    if not grid:
        return []
    header = grid[0] if not any(PRICE.search(t) for t in grid[0]) else []
    lines = [" ｜ ".join(t for t in header if t)] if header else []
    for line in grid[1 if header else 0:]:
        label = line[0] if line and not PRICE.search(line[0]) else ""
        priced = [(i, t) for i, t in enumerate(line) if PRICE.search(t)]
        if not priced:
            lines.append(" ｜ ".join(t for t in line if t))
            continue
        for i, text in priced:
            column = header[i] if i < len(header) else ""
            lines.append(f"{label + '：' if label else ''}{text}{' / ' + column if column else ''}")
    return [l for l in lines if l.strip()]


def visible_text(html):
    """The text a visitor reads on a page, one block per line, tables written out cell by cell."""
    return read_page(html)[0]


def read_page(html):
    """(visible text, its headings)."""
    parser = _Visible()
    parser.feed(re.sub(r"(?s)<!--.*?-->", " ", html))
    parser.close()
    while parser.tables:  # an unclosed table
        parser.out.append("\n" + "\n".join(table_lines(parser.tables.pop()["rows"])) + "\n")
    lines = (re.sub(r"[ \t\r\f\v　]+", " ", l).strip() for l in "".join(parser.out).split("\n"))
    return "\n".join(l for l in lines if l), parser.headings


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
        # 2026-11-20: 春和 「夏季 (5-9月) 599元 / 冬季 (10-4月) 699元」 for 60 minutes.
        label = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
        season = MONTHS.search(label)
        months = (int(season["a"]), int(season["b"])) if season else None
        if unit or day or months:
            return {"unit": unit, "day": day, "months": months}
    return {"unit": None, "day": None, "months": None}


MONTHS = re.compile(r"(?P<a>1[0-2]|0?[1-9])\s*月?\s*[-–~至到]\s*(?P<b>1[0-2]|0?[1-9])\s*月")


def in_months(month, months):
    """True when `month` is inside a range like (5, 9) or the wrap-around (10, 4)."""
    a, b = months
    return a <= month <= b if a <= b else month >= a or month <= b


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
