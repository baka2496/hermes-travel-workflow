"""Add up a trip's costs and compare the total with the budget.

Example (made-up names; never copy them into a reply):
  trip-cost.py --budget 5000 "範例油錢來回=300" "範例停車費=100x2@https://www.example.com/parking" \
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

When preferences.json says "public_transit": false (the user rides a scooter or drives), an item
for 捷運, 公車, 客運, trains or similar is refused: price fuel, parking and tolls instead.
"""
import argparse
import json
from pathlib import Path
import re
import sys

import page_evidence

# "?" may follow the price or the count: "餐飲=800?x1" failed once and the agent added up by hand (2026-11-14).
ITEM = re.compile(r"^(?P<name>.+?)=\s*(?:NT\$)?(?P<price>[\d,]+)\s*(?P<unsure1>[?？])?\s*(?:[x*×]\s*(?P<count>\d+))?\s*(?P<unsure2>[?？])?\s*$")
# A price counts as confirmed only with the page that shows it (2026-11-14: a day-trip plan listed
# "湯屋 NT$800" as confirmed though no page had been opened that turn).
SOURCE = re.compile(r"\s*@\s*(?P<source>https://\S+)\s*$")
PREFERENCES = page_evidence.HERE / "preferences.json"
# 2026-11-19: 「我都是騎車或開車，不要再給我大眾運輸了」.
TRANSIT = re.compile(r"捷運|公車|客運|巴士|台鐵|臺鐵|火車|高鐵|區間車|自強號|莒光|普悠瑪|太魯閣號|台灣好行|臺灣好行"
                     r"|接駁車|悠遊卡|TPASS|MRT|\bbus\b|\btrain\b", re.I)


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


def check_sources(items, records, hermes_root):
    for item in items:
        if not item["confirmed"]:
            continue
        pattern = page_evidence.price_pattern(item["unit"])
        if not page_evidence.shown(item["source"], pattern, records, hermes_root, use_records=False):
            item["confirmed"] = False
            item["why"] = "來源網頁上沒看到這個價格"
            continue
        details = page_evidence.details_shown(item["source"], pattern, hermes_root)
        unit, day = details["unit"], details["day"]
        # 2026-11-19 (Telegram): 「客房泡湯2小時 NT$800」 for a page whose $800 was 「/ 1小時」 on 假日.
        page_time, own_time = minutes(unit), minutes(item["name"])
        if page_time and own_time and page_time != own_time:
            item["confirmed"] = False
            item["why"] = f"網頁上這個價格是每{unit}"
            continue
        own_day = "假日" if re.search(r"假日|週末|周末|週六|週日", item["name"]) else \
            "平日" if "平日" in item["name"] else None
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


def money(value):
    return f"NT${value:,}"


def main():
    for stream in (sys.stdout, sys.stderr):  # item names are Chinese; errors must name them legibly too
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget", type=int, required=True, help="whole-trip budget in NT$")
    parser.add_argument("items", nargs="+", type=parse)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--records", default=page_evidence.RECORDS, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--hermes-root", default=page_evidence.HERMES_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--preferences", default=PREFERENCES, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args()
    refused = transit_refused(args.items, args.preferences)
    if refused:
        parser.error(refused)
    check_sources(args.items, args.records, args.hermes_root)
    total = sum(item["amount"] for item in args.items)
    left = args.budget - total
    verdict = f"預算剩 {money(left)}" if left >= 0 else f"超出預算 {money(-left)}"
    if args.json:
        print(json.dumps({"total": total, "budget": args.budget, "left": left, "items": args.items}, ensure_ascii=False))
        return 0
    unsure = sum(item["amount"] for item in args.items if not item["confirmed"])
    note = "（全部未確認）" if unsure == total and total else f"（{money(unsure)} 未確認）" if unsure else ""
    print(f"總計 {money(total)}，{verdict}{note}")  # short: it heads a phone-width reply
    for item in args.items:
        each = f"（{money(item['unit'])}×{item['count']}）" if item["count"] > 1 else ""
        if item.get("per") or item.get("day"):  # what the page says the price is for
            per = "" if not item.get("per") else "起價" if item["per"] == "起價" else f"每{item['per']}"
            day = f"{item['day']}價" if item.get("day") else ""
            parts = [day, f"{money(item['unit'])}{per}×{item['count']}"] if item["count"] > 1 else [day, per]
            each = "（" + "・".join(p for p in parts if p) + "）"
        print(f"• {item['name']} {money(item['amount'])}{each}" + ("" if item["confirmed"] else f"，未確認（{item['why']}）" if item.get("why") else "，未確認"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
