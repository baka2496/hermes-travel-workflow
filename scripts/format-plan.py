"""Assemble a verified, phone-ready Telegram travel plan from structured inputs.

The one way to write a plan reply: it runs trip-cost.py, booking-links.py and phone-check.py in
one step, keeps every verified figure and link verbatim, and validates phone readability before
printing. Never stitch those tools' outputs together by hand.

Three ready-to-book packages in one reply (2026-11-26: the owner only wants to pay, never to plan).
Each `--package` starts one package; options before the first are shared (header, dates, guests):
  format-plan.py --header "12/14–15 礁溪 2人" --date 2026-12-14 --checkout 2026-12-15 --guests 2 \\
      --package --place "範例溫泉" --room "和室雙人房" --transport "開車約 50 分鐘" \\
          --item "和室雙人房=3200@https://www.example.com/rooms" --official https://www.example.com/rooms \\
          --phone 03-0000000 --phone-source https://www.example.com/rooms \\
      --package ... --package ...
Packages come out cheapest first. Each needs a confirmed total, its transport and a way to book
(official page, booking-site page or phone). Fewer than three needs --few-reason 「只有…符合…」.

One package in full (after the owner picks one, or for a scheduled weekend plan), example with a
made-up place; never copy it into a reply:
  format-plan.py --header "12/14 礁溪泡湯 2人" --date 2026-12-14 \\
      --item "範例湯屋 60分鐘=699x1@https://www.example.com/hotspring.html" \\
      --place "範例溫泉" --official https://www.example.com/hotspring.html \\
      --phone 03-0000000 --phone-source https://www.example.com/hotspring.html \\
      --note "開車約 50 分鐘"
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tool(script, args):
    result = subprocess.run([sys.executable, str(HERE / script), *args],
                            capture_output=True, text=True, encoding="utf-8")
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def run_costs(args, notes):
    """trip-cost.py's output for args.item: (output, problem). Output is "" when nothing is confirmed."""
    # No --budget when the user gave none (2026-11-25): trip-cost.py then prints the total alone.
    cost_args = [] if args.budget is None else ["--budget", str(args.budget)]
    if args.date:
        cost_args += ["--date", args.date]
    if args.hermes_root:
        cost_args += ["--hermes-root", str(args.hermes_root)]
    if args.records:
        cost_args += ["--records", str(args.records)]
    if args.preferences:
        cost_args += ["--preferences", str(args.preferences)]
    cost_args += list(args.item)
    rc, out, err = run_tool("trip-cost.py", cost_args)
    if err:
        notes.append(err)
    if rc != 0:
        return None, err or f"trip-cost.py 失敗（exit {rc}）"
    return ("" if not out or out.startswith("沒有確認過的價格") else out), None


def run_links(args):
    """booking-links.py's output for args.place: (output, problem)."""
    link_args = [args.place]
    if args.official:
        link_args += ["--official", args.official]
    if args.booking:
        link_args += ["--booking", args.booking]
    if args.booking_label:
        link_args += ["--booking-label", args.booking_label]
    if getattr(args, "checkin", None) or args.date:
        link_args += ["--checkin", getattr(args, "checkin", None) or args.date]
    if getattr(args, "checkout", None):
        link_args += ["--checkout", args.checkout]
    if getattr(args, "guests", None):
        link_args += ["--guests", str(args.guests)]
    if getattr(args, "route", None):
        link_args += ["--route", *args.route]
    if getattr(args, "reservation_script", False):
        link_args.append("--reservation-script")
        if getattr(args, "time_text", None):
            link_args += ["--time", args.time_text]
        if getattr(args, "script_item", None):
            link_args += ["--item", args.script_item]
    if args.phone:
        link_args += ["--phone", args.phone]
    if args.phone_source:
        link_args += ["--phone-source", args.phone_source]
    if args.hermes_root:
        link_args += ["--hermes-root", str(args.hermes_root)]
    if args.records:
        link_args += ["--records", str(args.records)]
    rc, out, err = run_tool("booking-links.py", link_args)
    if rc != 0:
        return None, err or f"booking-links.py 失敗（exit {rc}）"
    return out, None


def checked(message, for_clarify=False):
    """phone-check.py's problems for message, formatted for stderr."""
    problems = load_module("phone_check", "phone-check.py").check(message)["problems"]
    if for_clarify:
        # In clarify questions MEDIA is intentionally omitted; ignore missing_photo only in this mode.
        problems = [p for p in problems if p.get("kind") != "missing_photo"]
    return [f"{p['kind']}: {p.get('text', '')} {p.get('hint', '')}".strip() for p in problems]


def build_plan(args):
    """Build the plan text and validate it. Returns (text, problems, stderr_notes)."""
    notes = []
    sections = [args.header.strip()]

    # 1. Verified costs via trip-cost.py
    if args.item:
        out, why = run_costs(args, notes)
        if why:
            return None, [why], notes
        if out:
            sections.append(out)

    # 2. Optional weather & short notes (traffic, highlights, pros/cons)
    if getattr(args, "weather_place", None) and args.date:
        rc, w_out, _ = run_tool("weather-check.py", [args.weather_place, "--date", args.date])
        if rc == 0 and w_out and "未確認" not in w_out:
            sections.append(w_out)
    for note in args.note or []:
        text = note.strip()
        if text:
            sections.append(text)

    # 3. Photo (verified record) or explicit fallback when recommending a place
    if args.photo:
        photo_path = Path(args.photo)
        sections.append(f"MEDIA:{photo_path.as_posix()}")
        if args.photo_caption:
            caption = args.photo_caption.strip()
            sections.append(caption if caption.startswith("📷") else f"📷 {caption}")
    elif args.place and not args.for_clarify:
        sections.append("照片：未取得")

    # 4. Verified booking links, one-tap route, phone & reservation script via booking-links.py
    if args.place:
        out, why = run_links(args)
        if why:
            return None, [why], notes
        if out:
            sections.append(out)
        if not args.phone:
            sections.append("電話：未取得")

    message = "\n".join(sections)
    # 5. Phone readability & photo/phone completeness check
    return message, checked(message, args.for_clarify), notes


MARKS = "①②③"
# 2026-11-25: 「只推一間給我選擇也太少」. Fewer than three only when the reply says why (phone-check.FEW_FIT).
FEW_FIT = re.compile(r"只有.{0,12}(符合|可行|排得進|有空|來得及)")
SHORT_LABELS = (("Google 地圖・查空房", "地圖"), ("官網訂房", "官網"), ("一鍵多點導航", "導航"))
NO_PHONE = "電話：未取得"


def one_line_links(output):
    """booking-links.py's lines as one short phone line: 「📍 [地圖](…) 🏨 [官網](…) 📞 03-…」."""
    line = " ".join(part.strip() for part in output.splitlines() if part.strip() and not part.startswith("💬"))
    for long, short in SHORT_LABELS:
        line = line.replace(f"[{long}]", f"[{short}]")
    return line


def total_of(cost_output):
    found = load_module("travel_money", "travel_money.py").amounts(cost_output.splitlines()[0])
    return int(found[0][3:].replace(",", "")) if found else 0


# 2026-11-26 (exam): 「我想出去玩」「我想去泡溫泉」「這週末想去泡溫泉」 all got the same 玫瑰園／瀧乃湯／湯布苑,
# because page-prices.py --near reopens the pages read before and the cheapest old ones always won; one
# reply's three packages were three rooms of one 龍山溫泉旅館. The owner: 「這是一個很嚴重的問題」.
# Three packages are three different places, and at most one of them was recommended in the last 14 days
# (packages shown in the last 30 minutes are the same conversation's follow-up, so they do not count).
REPEAT_DAYS = 14
SAME_CONVERSATION_S = 30 * 60
MAX_REPEATS = 1


def place_identity(name, official=None, phone=None):
    """What tells one place from another: its name without spaces, its phone digits, its page's site."""
    from urllib.parse import urlsplit
    host = urlsplit(official or "").netloc.lower().removeprefix("www.")
    path = urlsplit(official or "").path.rsplit("/", 1)[0]
    return {"name": re.sub(r"\s+", "", name or ""), "phone": re.sub(r"\D", "", phone or ""),
            "site": f"{host}{path}" if host else ""}


def same_place(a, b):
    """Same name (one inside the other: 瀧乃湯 and 北投瀧乃湯浴室), same phone, or same site and folder."""
    names = sorted((a.get("name", ""), b.get("name", "")), key=len)
    return bool((len(names[0]) >= 3 and names[0] in names[1])
                or (a.get("phone") and len(a["phone"]) >= 7 and a["phone"] == b.get("phone"))
                or (a.get("site") and a["site"] == b.get("site")))


def read_history(path):
    import json
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def recent(history, now):
    """Places recommended in the last REPEAT_DAYS days, not counting this conversation's last half hour."""
    return [e for e in history
            if SAME_CONVERSATION_S < now - float(e.get("at", 0)) < REPEAT_DAYS * 86400]


def repeat_problems(identities, history, now, few):
    """Why these packages are not fresh enough: the same place twice, or too many recent repeats."""
    problems = []
    for i, a in enumerate(identities):
        for b in identities[:i]:
            if same_place(a, b):
                problems.append(f"「{b['name']}」和「{a['name']}」是同一個地方：三個方案要三個不同的地方"
                                "（同一間的不同房型只算一個），再找別家官網")
    if few:  # the reason says why only these fit (a named place, the cheapest there)
        return problems
    seen = recent(history, now)
    repeats = [a["name"] for a in identities if any(same_place(a, e) for e in seen)]
    if len(repeats) > MAX_REPEATS:
        problems.append(f"最近 {REPEAT_DAYS} 天推過：{'、'.join(repeats)}。三個方案最多重複 {MAX_REPEATS} 個，"
                        "其他用 web_search 找這次新的官網再跑 page-prices.py（--near 只用來補比價）；"
                        "真的只有這些符合才加 --few-reason 「只有…符合，因為…」")
    return problems


def remember(path, identities, header, now):
    """Add this reply's places to the recommendation log (kept to the last REPEAT_DAYS days)."""
    import json
    history = [e for e in read_history(path) if now - float(e.get("at", 0)) < REPEAT_DAYS * 86400]
    history += [{**identity, "header": header, "at": round(now)} for identity in identities]
    try:
        Path(path).write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def build_packages(common, packages):
    """Up to three ready-to-book packages in one phone-width reply. Returns (text, problems, stderr_notes)."""
    notes, problems, built = [], [], []
    if len(packages) > len(MARKS):
        return None, [f"最多 {len(MARKS)} 個方案，現在有 {len(packages)} 個：留最便宜、最符合的 {len(MARKS)} 個"], notes
    few = (common.few_reason or "").strip()
    if len(packages) < len(MARKS) and not FEW_FIT.search(few):
        return None, [f"要給 {len(MARKS)} 個能直接訂的方案，現在只有 {len(packages)} 個：再找符合日期與交通方式、"
                      "這次查過價格的地方；真的只有一兩個符合，加 --few-reason 「只有…符合…，因為…」"], notes
    import time
    now = time.time()
    identities = [place_identity(p.place, p.official, p.phone) for p in packages]
    history_path = common.history or Path(common.hermes_root or HERE) / "recommended.json"
    problems += repeat_problems(identities, read_history(history_path), now, few)
    if problems:
        return None, problems, notes
    for number, package in enumerate(packages, 1):
        name = package.place or f"第 {number} 個方案"
        if not package.place or not package.transport or not package.item:
            problems.append(f"{name}：每個方案都要 --place、--transport（交通方式與時間）和至少一個 --item")
            continue
        if not (package.official or package.booking or package.phone):
            problems.append(f"{name}：沒有能訂的地方（--official 官網、--booking 訂房頁或 --phone 電話），換一個能直接訂的")
            continue
        own = {key: value for key, value in vars(package).items() if value not in (None, [])}
        args = argparse.Namespace(**{**vars(common), **own})
        cost, why = run_costs(args, notes)
        if why:
            problems.append(f"{name}：{why}")
            continue
        # With show_unconfirmed off nothing is printed; otherwise the headline says 全部未確認 (Antigravity's review).
        if not cost or "（全部未確認）" in cost.splitlines()[0]:
            problems.append(f"{name}：沒有確認過的價格，不能當成能直接訂的方案；換一家官網或訂房頁有標價的地方")
            continue
        links, why = run_links(args)
        if why:
            problems.append(f"{name}：{why}")
            continue
        built.append((total_of(cost), package, cost.splitlines()[0].strip(), one_line_links(links)))
    if problems:
        return None, problems, notes
    built.sort(key=lambda entry: entry[0])  # cheapest first: the recommended one unless its note says why not
    header = common.header.strip()
    lines = [f"{header}｜{few}" if few else f"{header}｜{len(built)} 個方案，點連結就能訂"]
    for mark, (_, package, total, links) in zip(MARKS, built):
        lines.append(f"{mark} {package.place.strip()}" + (f"｜{package.room.strip()}" if package.room else ""))
        lines.append(total)
        lines.append("｜".join(p.strip() for p in (package.dates, f"交通：{package.transport}", package.note)
                              if p and p.strip()))
        if package.photo:
            lines.append(f"MEDIA:{Path(package.photo).as_posix()}")
            if package.photo_caption:
                caption = package.photo_caption.strip()
                lines.append(caption if caption.startswith("📷") else f"📷 {caption}")
        lines.append(links)
        if not package.phone:  # its own line: with show_unconfirmed off the plugin drops it, not the links
            lines.append(NO_PHONE)
    message = "\n".join(lines)
    problems = checked(message, common.for_clarify)
    if not problems:
        remember(history_path, identities, header, now)
    return message, problems, notes


def package_parser():
    parser = argparse.ArgumentParser(prog="format-plan.py ... --package", add_help=False)
    parser.add_argument("--place", help="the place's name as on its own page")
    parser.add_argument("--room", help="room or bath being booked, as its page names it")
    parser.add_argument("--transport", help="how to get there and how long, e.g. 開車約 50 分鐘")
    parser.add_argument("--dates", help="this package's dates when they differ from the header")
    parser.add_argument("--note", help="one short point: the key pro or con")
    parser.add_argument("--item", action="append", default=[], help="cost item in trip-cost.py format (repeatable)")
    for option in ("--official", "--booking", "--booking-label", "--phone", "--phone-source", "--photo",
                   "--photo-caption", "--date", "--checkin", "--checkout"):
        parser.add_argument(option)
    return parser


def split_packages(argv):
    """argv before the first --package, and each package's own argv."""
    chunks = [[]]
    for token in argv:
        if token == "--package":
            chunks.append([])
        else:
            chunks[-1].append(token)
    return chunks[0], chunks[1:]


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--header", required=True, help="first line: date, destination, party size")
    parser.add_argument("--budget", type=int, help="whole-trip budget in NT$, only one the user gave")
    parser.add_argument("--date", help="trip date YYYY-MM-DD")
    parser.add_argument("--item", action="append", default=[], help="cost item in trip-cost.py format (repeatable)")
    parser.add_argument("--place", help="recommended place name for booking-links.py")
    parser.add_argument("--official", help="official page URL")
    parser.add_argument("--booking", help="booking-site page URL")
    parser.add_argument("--booking-label", help="label for --booking (e.g. Agoda)")
    parser.add_argument("--checkin", help="check-in YYYY-MM-DD for prefilled booking link")
    parser.add_argument("--checkout", help="check-out YYYY-MM-DD for prefilled booking link")
    parser.add_argument("--guests", type=int, help="adult guest count for prefilled booking link / script")
    parser.add_argument("--route", nargs="+", metavar="STOP", help="stops for one-tap Google Maps navigation")
    parser.add_argument("--reservation-script", action="store_true", help="include copy-paste reservation script")
    parser.add_argument("--time", dest="time_text", help="arrival time for --reservation-script")
    parser.add_argument("--script-item", help="room/bath name for --reservation-script")
    parser.add_argument("--weather-place", help="append verified weather line for this place on --date")
    parser.add_argument("--phone", help="verified phone number")
    parser.add_argument("--phone-source", help="page URL showing --phone")
    parser.add_argument("--photo", help="path to verified photo saved by place-photo.py")
    parser.add_argument("--photo-caption", help="caption line for --photo")
    parser.add_argument("--note", action="append", default=[], help="short note line (repeatable)")
    parser.add_argument("--for-clarify", action="store_true",
                        help="omit MEDIA line for a clarify button question (photo sent after user chooses)")
    parser.add_argument("--few-reason", help="with fewer than three --package: 「只有…符合…，因為…」")
    parser.add_argument("--records", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-root", help=argparse.SUPPRESS)
    parser.add_argument("--preferences", help=argparse.SUPPRESS)
    parser.add_argument("--history", help=argparse.SUPPRESS)  # tests only: the recommendation log
    shared, packages = split_packages(sys.argv[1:])
    args = parser.parse_args(shared)
    if packages:
        each = package_parser()
        message, problems, notes = build_packages(args, [each.parse_args(chunk) for chunk in packages])
    else:
        message, problems, notes = build_plan(args)
    for note in notes:
        print(note, file=sys.stderr)
    if problems:
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("已通過 phone-check.py 手機排版檢查，可直接原樣送出，不用再重跑 phone-check.py。", file=sys.stderr)
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
