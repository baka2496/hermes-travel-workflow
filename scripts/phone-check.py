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
# 2026-11-21: 想出去玩居 answered 「下一步｜派工補查機票住宿」 and ended its turn with no card made, so
# nobody looked. A reply that says work was handed on names the card it made (with --card; the user
# never sees the ID), and the card must be on the board, made in the last few hours.
HANDOFF = re.compile(r"派工|派給|派卡|交給\s*(Trip|trip|專員|行程管理)|請\s*(Trip|trip)|轉給\s*(Trip|trip)")
CARD_ID = re.compile(r"^t_[0-9a-f]{6,}$")
CARD_MAX_AGE_S = 6 * 3600


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
            row = db.execute("SELECT created_at, assignee FROM tasks WHERE id = ?", (card,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return f"board unreadable: {exc}"
    if row is None:
        return f"{card} is not on the board"
    if (now or time.time()) - row[0] > CARD_MAX_AGE_S:
        return f"{card} is an old card ({row[1]})"
    return None


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
                             "hint": "make the Kanban card first (hermes kanban --board command-center create), "
                                     "then pass its ID with --card; if it was not made, say the lookup was not sent"})
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
    parser.add_argument("--card", help="the Kanban card this message says the work was handed to (t_...)")
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
