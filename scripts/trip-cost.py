"""Add up a trip's costs and compare the total with the budget.

Example (made-up names; never copy them into a reply):
  trip-cost.py "範例油錢來回=300" "範例停車費=100x2@https://www.example.com/parking" \
      "範例旅館雙人房=3200@https://www.example.com/rooms" "範例餐費=800"

Each item is "name=amount" or "name=unit price x count" (x, *, or ×), followed by "@" and the
https page you opened that shows that price. An item counts as confirmed only when that price
is found on a page opened in the last few hours (web_extract or the browser); an opportunity
record does not count. When the page gives the price per hour, per person, per night, or as a
starting price (「$600 / 1小時」, 「起」), or as a 平日 or 假日 price, the printed line says so; an item whose
name gives another length of time (「2小時」 for a 「/ 1小時」 price) or the other day is unconfirmed. Text
only inside a URL-encoded link (a share button) does not count. Any other item (an estimate, a remembered
price, a meal budget, a page that does not show the price, or one marked "?") is counted as
unconfirmed, and the total says how much of it is. Include every cost of the trip, meals too. Use the printed total in
the reply exactly as shown; never add numbers up by hand. The first printed line is ready to
use as the reply's conclusion.

When preferences.json says "show_unconfirmed": false, only confirmed items are printed and
counted in the total; the others are named on stderr for the agent and must stay out of the reply.

When preferences.json says "public_transit": false (the user rides a scooter or drives), an item
for 捷運, 公車, 客運, trains or similar is refused: price fuel, parking and tolls instead.
"""
import argparse
import datetime
import json
from pathlib import Path
import re
import sys

import page_evidence
import travel_money

# "?" may follow the price or the count: "餐飲=800?x1" failed once and the agent added up by hand (2026-11-14).
ITEM = re.compile(rf"^(?P<name>.+?)=\s*(?:{travel_money.CURRENCY_BEFORE}\s*)?(?P<price>[\d,]+)\s*(?P<unsure1>[?？])?\s*(?:[x*×]\s*(?P<count>\d+))?\s*(?P<unsure2>[?？])?\s*$")
# A price counts as confirmed only with the page that shows it (2026-11-14: a day-trip plan listed
# "湯屋 NT$800" as confirmed though no page had been opened that turn).
SOURCE = re.compile(r"\s*@\s*(?P<source>https://\S+)\s*$")
PREFERENCES = page_evidence.HERE / "preferences.json"
# Admission only: 「免費停車」「免費 Wi-Fi」 on a paid hotel's page say nothing (Antigravity's review).
FREE = re.compile(r"免費入園|免費參觀|免費開放|免費入場|免門票|免收費|不收費|不需門票|無需門票|免入園費"
                  r"|(門票|入園|入場|參觀)[^。\n]{0,6}免費")
# 2026-11-19: 「我都是騎車或開車，不要再給我大眾運輸了」.
TRANSIT = re.compile(r"捷運|公車|客運|巴士|台鐵|臺鐵|火車|高鐵|區間車|自強號|莒光|普悠瑪|太魯閣號|台灣好行|臺灣好行"
                     r"|接駁車|悠遊卡|TPASS|MRT|\bbus\b|\btrain\b", re.I)


def date_arg(text):
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--date must be YYYY-MM-DD, got {text!r}")


def transit_refused(items, preferences):
    """Error text when the user does not use public transport and an item prices it, else None."""
    try:
        prefs = json.loads(Path(preferences).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(prefs, dict) or prefs.get("public_transit") is not False:
        return None
    found = [item["name"] for item in items if TRANSIT.search(item["name"])]
    if not found:
        return None
    mode = prefs.get("transport_mode") or "自己騎機車或開車"
    return (f"使用者的交通方式是「{mode}」，不要算大眾運輸：{'、'.join(found)}。"
            "交通改算油錢（估算）、停車費、國道通行費，再重跑。")


def parse(text):
    source = SOURCE.search(text)
    if source:
        text = text[:source.start()]
    elif "@" in text:
        raise argparse.ArgumentTypeError(f"a source after @ must be an https page, got {text!r}")
    match = ITEM.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"expected name=amount or name=price x count, got {text!r}")
    price = int(match["price"].replace(",", ""))
    count = int(match["count"] or 1)
    if count < 1:  # a zero count would silently drop the item and understate the total
        raise argparse.ArgumentTypeError(f"count must be at least 1, got {text!r}")
    # "餐費估算?=400x2" put the mark on the name, and the meals were counted as confirmed (2026-11-14).
    name = match["name"].strip()
    unsure_name = name.endswith(("?", "？"))
    return {"name": name.rstrip("?？").strip(), "unit": price, "count": count, "amount": price * count,
            "source": source["source"] if source else None,
            "confirmed": bool(source) and not (match["unsure1"] or match["unsure2"] or unsure_name)}


def page_says(source, hermes_root):
    """The prices the page shows, for an item whose price it does not show (2026-11-19: told only
    「沒看到這個價格」, the commander spent half an hour finding out why)."""
    lines = page_evidence.price_lines(page_evidence.latest_page_text(hermes_root, source), limit=12)
    if lines:
        return f"{source} 上寫的價格是：" + "；".join(lines) + "。要用就照這些寫，再跑一次。"
    return f"沒有 {source} 最近讀過的內容：先跑 page-prices.py {source} 看網頁上實際寫的價格。"


def check_sources(items, records, hermes_root, date=None):
    for item in items:
        if not item["confirmed"]:
            continue
        # 2026-11-26: 「為甚麼只有溫泉」 — a trail, an old street or a beach is often free; NT$0 is confirmed
        # only by a page that says so (a bare 0 is on every page).
        free = item["unit"] == 0
        pattern = FREE if free else page_evidence.price_pattern(item["unit"])
        if not page_evidence.shown(item["source"], pattern, records, hermes_root, use_records=False):
            if free:
                item["confirmed"] = False
                item["why"] = "來源網頁上沒寫免費"
                continue
            item["confirmed"] = False
            item["why"] = "來源網頁上沒看到這個價格"
            item["hint"] = page_says(item["source"], hermes_root)  # for the agent, on stderr
            continue
        details = page_evidence.details_shown(item["source"], pattern, hermes_root)
        unit, day = details["unit"], details["day"]
        months = details.get("months")
        if date and months and not page_evidence.in_months(date.month, months):
            item["confirmed"] = False
            item["why"] = f"網頁上這個價格是{months[0]}-{months[1]}月的價格，{date.month}月不適用"
            continue
        # 2026-11-19 (Telegram): 「客房泡湯2小時 NT$800」 for a page whose $800 was 「/ 1小時」 on 假日.
        page_time, own_time = minutes(unit), minutes(item["name"])
        if page_time and own_time and page_time != own_time:
            item["confirmed"] = False
            item["why"] = f"網頁上這個價格是每{unit}"
            continue
        own_day = "假日" if re.search(r"假日|週末|周末|週六|週日", item["name"]) else \
            "平日" if "平日" in item["name"] else None
        if own_day is None and date and date.weekday() >= 5:
            own_day = "假日"  # a Saturday or Sunday; a weekday may still be a holiday, so no 平日 guess
        if day and own_day and day != own_day:
            item["confirmed"] = False
            item["why"] = f"網頁上這個價格是{day}價"
            continue
        if day and not own_day:
            item["day"] = day
        bare = (unit or "").lstrip("0123456789")
        said = ("起" in item["name"]) if unit == "起價" else bool(page_time and own_time) or \
            (unit or "") in item["name"] or any(f"{mark}{bare}" in item["name"] for mark in ("每", "/", "／"))
        if unit and not said:  # 「雙人房」 holds 「人」 but does not say 「每人」
            item["per"] = unit  # 2026-11-19: 「湯屋 NT$600」 was 「$600 / 1小時」 on the page


DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>小時|hr|分鐘|分)", re.I)


def minutes(text):
    """「2小時」 -> 120, 「60分鐘」 -> 60; None when the text gives no length of time."""
    match = DURATION.search(text or "")
    if not match:
        return None
    return float(match["n"]) * (1 if match["unit"].startswith("分") else 60)


money = travel_money.money


def main():
    for stream in (sys.stdout, sys.stderr):  # item names are Chinese; errors must name them legibly too
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # 2026-11-25: a required --budget made the bot pass one the user never gave (NT$2,000). Without it
    # the tool adds up the costs and says nothing about a budget.
    parser.add_argument("--budget", type=int, help="whole-trip budget in NT$, only one the user gave")
    parser.add_argument("items", nargs="+", type=parse)
    parser.add_argument("--date", type=date_arg, help="trip date YYYY-MM-DD: a price for other months "
                        "(「夏季 (5-9月)」) or, on a Saturday or Sunday, a 平日 price is unconfirmed")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--records", default=page_evidence.RECORDS, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--hermes-root", default=page_evidence.HERMES_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--preferences", default=PREFERENCES, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args()
    refused = transit_refused(args.items, args.preferences)
    if refused:
        parser.error(refused)
    check_sources(args.items, args.records, args.hermes_root, args.date)
    # 2026-11-19: 「未確認就不要傳給我，傳給我確認過的就好」 — with "show_unconfirmed": false only
    # confirmed items are printed and counted; the rest go to stderr, for the agent only.
    hidden = []
    if not args.json and confirmed_only(args.preferences):
        hidden = [item for item in args.items if not item["confirmed"]]
        args.items = [item for item in args.items if item["confirmed"]]
        if not args.items:
            print("沒有確認過的價格：不要在回覆裡寫價格或總計。")
            report_hidden(hidden)
            report_hints(hidden)
            return 0
    total = sum(item["amount"] for item in args.items)
    left = None if args.budget is None else args.budget - total
    verdict = "" if left is None else f"，預算剩 {money(left)}" if left >= 0 else f"，超出預算 {money(-left)}"
    if args.json:
        print(json.dumps({"total": total, "budget": args.budget, "left": left, "items": args.items}, ensure_ascii=False))
        return 0
    unsure = sum(item["amount"] for item in args.items if not item["confirmed"])
    note = "（全部未確認）" if unsure == total and total else f"（{money(unsure)} 未確認）" if unsure else ""
    print(f"總計 {money(total)}{verdict}{note}")  # short: it heads a phone-width reply
    for item in args.items:
        each = f"（{money(item['unit'])}×{item['count']}）" if item["count"] > 1 else ""
        if item.get("per") or item.get("day"):  # what the page says the price is for
            per = "" if not item.get("per") else "起價" if item["per"] == "起價" else f"每{item['per']}"
            day = f"{item['day']}價" if item.get("day") else ""
            parts = [day, f"{money(item['unit'])}{per}×{item['count']}"] if item["count"] > 1 else [day, per]
            each = "（" + "・".join(p for p in parts if p) + "）"
        print(f"• {item['name']} {money(item['amount'])}{each}" + ("" if item["confirmed"] else f"，未確認（{item['why']}）" if item.get("why") else "，未確認"))
    report_hidden(hidden)
    report_hints(hidden + args.items)
    return 0


def report_hints(items):
    """What the source pages do show, for each price they did not show (agent only)."""
    for item in items:
        if item.get("hint"):
            sys.stdout.flush()
            print(f"給代理：{item['name']}：{item['hint']}", file=sys.stderr)


def confirmed_only(preferences):
    try:
        prefs = json.loads(Path(preferences).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(prefs, dict) and prefs.get("show_unconfirmed") is False


def report_hidden(hidden):
    """Tell the agent, not the user, which items were left out and why."""
    if hidden:
        sys.stdout.flush()
        names = "、".join(f"{item['name']}（{item.get('why') or '沒有網頁確認'}）" for item in hidden)
        print(f"給代理，不要貼進回覆：使用者只要確認過的價格，這些沒列出也沒算進總計：{names}。"
              "回覆裡不要提它們、不要寫估算金額或「待確認」。"
              "這是最後結果：不要再查原因或改程式，也不要換寫法重跑同一個價格；"
              "要推薦更便宜的地方，就打開另一家有標價的官網再跑一次。", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
