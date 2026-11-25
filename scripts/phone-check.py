"""Check that a Telegram message reads well on a phone.

Reads the message from a file argument or stdin. Width is measured the way a
phone renders the chat bubble: about 20 full-width (CJK) characters per line,
with Latin letters and digits about 0.35 of that width (calibrated against a
Telegram iOS screenshot). Exit status 1 means a problem.
"""
import argparse
import hashlib
import json
from pathlib import Path
import math
import re
import sys
import unicodedata
import urllib.parse

import travel_money

LINE_WIDTH = 40          # 20 full-width characters per bubble line on a phone
NARROW = 0.7             # width of a Latin letter or digit, in half full-width units
SCREEN_LINES = 30        # bubble lines visible on one phone screen
MAX_MESSAGE_LINES = 14   # a reply should be readable at a glance; details on request
NARRATION = re.compile(r"^(我先|我會先|我正在|接著我|首先我)")
INTERNAL_ID = re.compile(r"(?<![\w-])(t_[0-9a-f]{6,}|opp-\d{8}[-\w]*|local-[0-9a-f]{16,})")
MAX_LINES_PER_ITEM = 2   # one bullet or sentence should wrap to at most two lines


MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE_LINK = re.compile(r"(https?://|tel:)\S+")
LOCAL_PATH = re.compile(r"(?<![\w/])([A-Za-z]:[\\/]|\\\\|~/|/Users/|/home/)\S*|\S+\.(md|json|txt|ics|py)\b")
BOOKING_LINKS = "Google 地圖・查空房"  # booking-links.py output: a place is being recommended
# 2026-11-21: the commander answered 「下一步｜派工補查機票住宿」 and ended its turn with no card made, so
# nobody looked. A reply that says work was handed on names the card it made (with --card; the user
# never sees the ID), and the card must be on the board, made in the last few hours.
# 2026-11-21: 「已為你送出福岡機票與住宿查詢」 was checked against a card ID the agent made up.
HANDOFF = re.compile(r"派工|派給|派卡|交給\s*(Trip|trip|專員|行程管理)|請\s*(Trip|trip)|轉給\s*(Trip|trip)|送出.{0,12}查詢")
CARD_ID = re.compile(r"^t_[0-9a-f]{6,}$")
# 2026-11-21: plan read no flights once (Google blocked it; a rerun had Tigerair at NT$12,281) and the user
# was told 「機票與房價尚未開賣」「預計2026年3月查價」 for a March 2027 trip. Google sells about 11 months out;
# an empty read is 未確認, never a reason the agent made up.
NOT_ON_SALE = re.compile(r"(尚未|還沒|還未|未)(開賣|開放(銷售|預訂|訂購))|開賣後|遠期")
CARD_MAX_AGE_S = 6 * 3600
# 2026-11-22: the card's budget arrived as "NT0,000": bash expanded `$4` inside a double-quoted --body,
# and Trip filled in a NT$70,000 budget of its own. An amount that lost its `$` means the body is broken.
LOST_DOLLAR = re.compile(r"NT(?=[0-9,])")


def board_db():
    """The command-center board: HERMES_KANBAN_DB, else beside this script's Hermes root."""
    import os
    if os.environ.get("HERMES_KANBAN_DB"):
        return Path(os.environ["HERMES_KANBAN_DB"])
    return Path(__file__).resolve().parents[2] / "kanban/boards/command-center/kanban.db"


def card_problem(card, now=None):
    """Why *card* does not back a handoff claim, or None when it is a recent card on the board."""
    import sqlite3
    import time
    if not card:
        return "no --card"
    if not CARD_ID.match(card):
        return f"not a card ID: {card}"
    path = board_db()
    if not path.is_file():
        return f"board not found: {path.name}"
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT created_at, assignee, body FROM tasks WHERE id = ?", (card,)).fetchone()
            watched = db.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id = ?", (card,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return f"board unreadable: {exc}"
    if row is None:
        return f"{card} is not on the board"
    if (now or time.time()) - row[0] > CARD_MAX_AGE_S:
        return f"{card} is an old card ({row[1]})"
    # 2026-11-21: a card made with `hermes kanban create` finished and nobody told the user: only the
    # native tool subscribes on its own, so the card needs notify-subscribe to wake whoever made it.
    if watched is None:
        return f"{card} has no notify-subscribe, so nobody is told when it is done"
    lost = LOST_DOLLAR.search(row[2] or "")
    if lost:
        amount = re.match(r"NT[0-9,]*", (row[2] or "")[lost.start():]).group(0)
        return (f"{card}'s body says {amount}: bash ate the `$` of a double-quoted --body; make the card again "
                "with the body in single quotes (or --body-file)")
    return None


def result_problem(card):
    """Why *card* cannot back a message that reports its results, or None when the reviewer really passed it.

    2026-11-21: the Fukuoka card's workers crashed, so the commander attached a plan itself and ran
    `hermes -p commander kanban complete` — the card said done but nobody had reviewed it. A real review
    is a reviewer worker the dispatcher spawned; a card closed by hand has no spawned run.
    """
    import sqlite3
    if not CARD_ID.match(card):
        return f"not a card ID: {card}"
    path = board_db()
    if not path.is_file():
        return f"board not found: {path.name}"
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
            run = db.execute("SELECT id, profile FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
                             "ORDER BY id DESC LIMIT 1", (card,)).fetchone()
            spawned = run and db.execute("SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'spawned'",
                                         (card, run[0])).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return f"board unreadable: {exc}"
    if row is None:
        return f"{card} is not on the board"
    if row[0] != "done":
        return f"{card} is {row[0]}, not done: say the lookup is not finished (or failed), with no prices"
    if not spawned:
        return (f"{card} was closed by hand, not by a reviewer worker: never complete a card as another profile; "
                "tell the user the review did not run")
    return None


# Not \b: Chinese counts as a word character, so 「去程16:40起飛」 had no boundary (Antigravity, 2026-11-25).
FLIGHT_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
ATTACHMENT_URL = re.compile(r"https?://[^\s)\]）]+")
EMPTY_RESULT = re.compile(r"未確認|沒讀到價格|沒查成")
PRICE = travel_money.PRICE
LEGS = ("去程", "回程")


# How a link is sorted, most specific evidence first (Antigravity's reviews of 2f15bdc, 3ed6613, e363c3c):
# 1. the address itself: flights.booking.com, expedia.com/flights, skyscanner.com/hotels, …/lodging
# 2. plain words in the link text: 「華航官網訂票」, 「東橫INN 博多口」 (official sites say neither)
# 3. the site: an airline, or a site that is mostly rooms (Agoda, Expedia: their flight pages say /flights,
#    caught in step 1). Klook, Skyscanner and the word "booking" alone are not sorted by name.
FLIGHT_WORDS = ("flight", "flights", "airline", "airlines", "airways")
STAY_WORDS = ("hotel", "hotels", "lodging", "ryokan", "hostel")
FLIGHT_TEXT = ("機票", "航班", "訂票", "航空", "班機", *FLIGHT_WORDS)
STAY_TEXT = ("住宿", "飯店", "旅館", "酒店", "民宿", "訂房", "房型", "inn", "resort", *STAY_WORDS)
AIRLINE_SITES = {"china-airlines", "evaair", "starlux", "tigerair", "tigerairtw", "jal", "ana", "flypeach", "jetstar",
                 "vietjetair", "cathaypacific", "flyscoot", "finnair", "airasia", "koreanair", "gotogate",
                 "britishairways", "singaporeair", "thaiairways", "emirates", "qatarairways", "united", "delta"}
HOTEL_ONLY_SITES = {"booking", "agoda", "expedia", "airbnb", "jalan", "ikyu", "vio", "priceline", "toyoko", "trivago", "dealbase",
                    "gracery", "japanesque"}


def _has_word(text, words):
    """A Chinese word anywhere; an English one only as a whole word (Holiday Inn, not dinner or finnair)."""
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) if w.isascii() else w in text for w in words)


def link_kind(url, name=""):
    """機票 or 住宿 for a booking/search link, else None."""
    parsed = urllib.parse.urlparse(url.lower())
    host = parsed.hostname or ""
    parts = set(re.split(r"[.\-]", host))  # whole parts: "ana" is not hotel-hana.co.jp
    address = " ".join(re.split(r"[.\-/_?=&]", host + parsed.path))
    if _has_word(address, FLIGHT_WORDS):
        return "機票"
    if _has_word(address, STAY_WORDS) or "travel/search" in parsed.path:
        return "住宿"
    name = name.lower()
    if _has_word(name, FLIGHT_TEXT):
        return "機票"
    if _has_word(name, STAY_TEXT):
        return "住宿"
    if parts & AIRLINE_SITES:
        return "機票"
    # Rakuten Travel, not Rakuten's shop (Antigravity's review of 55a7ae9: it was dropped from the list).
    if parts & HOTEL_ONLY_SITES or "inn" in parts or {"travel", "rakuten"} <= parts:
        return "住宿"
    return None


def times_in(text):
    """Flight times as HH:MM, so 9:05 and 09:05 match (Antigravity's review of 2f15bdc)."""
    return {t.zfill(5) for t in FLIGHT_TIME.findall(text)}


def leg_heading(line):
    """The leg a line heads, else None: a Markdown heading naming one leg (「### ✈️ 回程航班」「### 去程 06/05」),
    or a bare label (「**回程**：」「去程（06/05）」).

    A sentence that merely mentions a leg (「- 回程含 20kg 行李」) is not one, or the next line's 15:00
    check-in would count as a flight time (Antigravity's review of 3ed6613).
    """
    legs = [leg for leg in LEGS if leg in line]
    if len(legs) != 1:
        return None
    if line.lstrip().startswith("#") and not _has_word(line.lower(), STAY_TEXT + ("入住", "退房")):
        # 「### 去程當晚住宿」 heads a hotel, not a flight (Antigravity's review of 55a7ae9).
        return legs[0]
    core = re.sub(r"[#*\-\s:：|]", "", line)
    core = re.sub(r"[（(][^）)]*[）)]$", "", core)
    return core if core in LEGS else None


def leg_times(text):
    """The times written for each leg: on the leg's own line, or under a leg heading, up to the next
    heading or a blank line after its content (a blank line right under the heading is skipped)."""
    found = {leg: set() for leg in LEGS}
    under, seen = None, False
    for line in text.splitlines():
        legs = [leg for leg in LEGS if leg in line]
        times = times_in(line)
        heading = leg_heading(line) if not times else None
        if heading:
            under, seen = heading, False
            continue
        if legs:
            for leg in legs:
                found[leg] |= times
            if legs == [under]:  # 「- 回程含 20kg 行李」 under 「### 回程」 does not end the section
                seen = True
            else:
                under = None
        elif line.lstrip().startswith("#") or (not line.strip() and seen):
            under = None
        elif under and line.strip():
            found[under] |= times
            seen = True
    return found


def card_detail_problem(card, message):
    """When a done flight/hotel card's attachment has flight times or links, the reply must include them.

    2026-11-25: t_0569fddd's fukuoka_plan.md had CI116/CI111 times, room types and Google Flights/Hotels
    links, but the Telegram reply dropped all of them and sent only bare totals.
    """
    # A failed lookup (no price given) has no details to carry. Antigravity, 2026-11-25: any 「未確認」 used
    # to skip this, so bare totals with 「早餐未確認」 went out without times or links.
    if EMPTY_RESULT.search(message) and not PRICE.search(message):
        return None
    attach_dir = board_db().parent / "attachments" / card
    if not attach_dir.is_dir():
        return None
    texts = []
    for path in sorted(attach_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in (".md", ".txt"):
            try:
                texts.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    if not texts:
        return None
    combined = "\n".join(texts)
    missing = []
    # Each leg needs one of its own times. Antigravity, 2026-11-25: any one time passed, even the 15:00
    # check-in with the return flight left out. A leg line may also hold e.g. 「預計 21:15 抵達飯店」, so
    # one of the line's times is enough, not all.
    replied_times = times_in(message)
    legs = leg_times(combined)
    missed_legs = [leg for leg, times in legs.items() if times and not times & replied_times]
    if missed_legs:
        missing.append(("去回程" if len(missed_legs) == 2 else missed_legs[0]) + "航班時間／班號")
    elif not any(legs.values()):
        has_flight_times = bool(FLIGHT_TIME.search(combined) and ("航班" in combined or "→" in combined))
        if has_flight_times and not replied_times:
            missing.append("去回程航班時間／班號")
    # Each kind of link the attachment has (機票, 住宿) must be in the reply, not just one link.
    named = MARKDOWN_LINK.findall(combined)
    bare = ATTACHMENT_URL.findall(MARKDOWN_LINK.sub("", combined))
    needed = {link_kind(url, name) for name, url in named} | {link_kind(url) for url in bare}
    needed -= {None}
    replied = {link_kind(url, name) for name, url in MARKDOWN_LINK.findall(message)}
    if needed - replied:
        missing.append("可點擊的" + "與".join(sorted(needed - replied, key="機票住宿".index)) + "連結（[名稱](網址)）")
    elif ATTACHMENT_URL.search(combined) and not MARKDOWN_LINK.search(message):
        missing.append("可點擊的機票與住宿連結（[名稱](網址)）")
    if not missing:
        return None
    return (f"{card} 的交付檔有{'、'.join(missing)}，但回覆漏掉了；"
            "請直接附上航班時間與行李說明、飯店全名與房型，以及交付檔裡的 [機票訂票連結](網址) 與 [住宿頁面連結](網址)")


def photo_problem(path_text):
    """A MEDIA photo must have the source record place-photo.py writes, matching the file."""
    path = Path(path_text)
    record = path.with_name(path.name + ".source.json")
    if not path.is_file():
        return f"file not found: {path.name}"
    if not record.is_file():
        return f"no source record: {path.name}"
    try:
        expected = json.loads(record.read_text(encoding="utf-8")).get("sha256")
    except ValueError:
        return f"unreadable source record: {record.name}"
    if expected != hashlib.sha256(path.read_bytes()).hexdigest():
        return f"photo changed after it was saved: {path.name}"
    return None


def width(text):
    text = MARKDOWN_LINK.sub(r"\1", text)  # Telegram shows only the link text
    text = text.replace("**", "").replace("__", "")
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else NARROW for ch in text)


def wrapped(line):
    return max(1, math.ceil(width(line) / LINE_WIDTH))


def check(message, card=None):
    problems = []
    if HANDOFF.search(message):
        problem = card_problem(card)
        if problem:
            problems.append({"kind": "handoff_without_card", "text": problem,
                             "hint": "make the card first: hermes kanban --board command-center create \"<title>\" --assignee trip "
                                     "--body '<body>' --json (single quotes; no --title), then pass the id it prints with --card; "
                                     "never make an ID up; if it was not made, say the lookup was not sent"})
    elif card:
        problem = result_problem(card)
        if problem:
            problems.append({"kind": "unreviewed_result", "text": problem,
                             "hint": "report a card's results only after its reviewer worker passed it"})
        else:
            detail_problem = card_detail_problem(card, message)
            if detail_problem:
                problems.append({"kind": "card_missing_details", "text": detail_problem,
                                 "hint": "include flight times, hotel name/room type, and clickable [name](url) links from the card attachment"})
    if NOT_ON_SALE.search(message):
        problems.append({"kind": "guessed_not_on_sale", "text": NOT_ON_SALE.search(message).group(0),
                         "hint": "a lookup that read no prices was blocked or empty this time, not 'not on sale': "
                                 "write 這次沒讀到價格，未確認, and offer to look again"})
    lines = message.strip("\n").splitlines()
    phone_lines = 0
    for number, line in enumerate(lines, 1):
        if line.strip().startswith("MEDIA:"):
            problem = photo_problem(line.strip()[len("MEDIA:"):].strip())
            if problem:
                problems.append({"kind": "unverified_photo", "line": number, "text": problem,
                                 "hint": "save photos with place-photo.py from the place's own page, or write 照片：未取得"})
            continue  # Telegram sends this as a photo, not as text
        rows = wrapped(line) if line.strip() else 0.5  # a blank line is about half a text line
        phone_lines += rows
        stripped = line.strip()
        if stripped.startswith("```"):
            problems.append({"kind": "code_block", "line": number, "text": stripped[:40]})
        elif stripped.startswith("|") or stripped.count("|") >= 2 or "│" in stripped:
            problems.append({"kind": "table", "line": number, "text": stripped[:40]})
        if NARRATION.match(stripped.lstrip("•-* ")):
            problems.append({"kind": "narration", "line": number, "text": stripped[:40],
                             "hint": "report results, not the steps you are taking"})
        if INTERNAL_ID.search(stripped):
            problems.append({"kind": "internal_id", "line": number, "text": INTERNAL_ID.search(stripped).group(0),
                             "hint": "do not show card, opportunity or session IDs"})
        plain = MARKDOWN_LINK.sub(r"\1", stripped)
        if BARE_LINK.search(plain):
            problems.append({"kind": "bare_link", "line": number, "text": BARE_LINK.search(plain).group(0)[:40],
                             "hint": "paste booking-links.py output; phone numbers as plain 📞 text"})
        if LOCAL_PATH.search(plain):
            problems.append({"kind": "local_path", "line": number, "text": LOCAL_PATH.search(plain).group(0)[:40],
                             "hint": "the user reads on a phone and cannot open computer files; offer details with a button"})
        if rows > MAX_LINES_PER_ITEM and not stripped.startswith(("```", "|", "來源", "Source")) and "│" not in stripped and not stripped.startswith(("來源", "Source")):
            problems.append({"kind": "long_line", "line": number, "phone_lines": rows, "text": stripped[:40]})
    content = [l.strip() for l in lines if l.strip() and not l.strip().startswith(("來源", "Source", "MEDIA:"))]
    for line in content[-3:]:
        if re.search(r"[？?]\s*$", line):
            problems.append({"kind": "text_question", "text": line[:40],
                             "hint": "ask fixed-answer questions with clarify buttons"})
    if BOOKING_LINKS in message and "照片：未取得" not in message and not any(l.strip().startswith("MEDIA:") for l in lines):
        problems.append({"kind": "missing_photo",
                         "hint": "add a photo from place-photo.py, or write 照片：未取得 when the place's own page has none"})
    # 2026-11-20: 「電話沒附」 — a plan for 春和 left out the number its page shows.
    if BOOKING_LINKS in message and "電話：未取得" not in message and not any(l.strip().startswith("📞") for l in lines):
        problems.append({"kind": "missing_phone",
                         "hint": "page-prices.py prints the page's phone numbers: pass one to booking-links.py "
                                 "with --phone and --phone-source; write 電話：未取得 when the page has none"})
    screens = round(phone_lines / SCREEN_LINES, 2)
    if phone_lines > MAX_MESSAGE_LINES:
        problems.append({"kind": "too_long", "phone_lines": math.ceil(phone_lines),
                         "hint": f"keep to {MAX_MESSAGE_LINES} phone lines; cut repeats and offer details on request"})
    return {"phone_lines": math.ceil(phone_lines), "screens": screens, "problems": problems}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="message file (default: stdin)")
    parser.add_argument("--text", help="check this message text directly; no file needed")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--card", help="the Kanban card this message hands work to, or whose results it reports (t_...)")
    args = parser.parse_args()
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    if args.text is not None:
        message = args.text.replace("\\n", "\n")
    else:
        message = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    report = check(message, args.card)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"phone lines: {report['phone_lines']} ({report['screens']} screens)")
        for problem in report["problems"]:
            where = f" line {problem['line']}" if "line" in problem else ""
            print(f"- {problem['kind']}{where}: {problem.get('text', '')} {problem.get('hint', '')}".rstrip())
        print("OK" if not report["problems"] else "Fix the problems above before sending.")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
