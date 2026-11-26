"""地點庫：查過、確定價格讀得到的地方，給 bot 挑。

2026-11-26: 「我想出去玩」「這週末想去泡溫泉」 always got the same 玫瑰園／瀧乃湯／湯布苑: the bot searched a
few pages each time and only those few showed a price it could read. The owner: 「這是一個很嚴重的問題」.
This keeps every place found once, so the next question starts from many, the least recently recommended first.

  places.py pick --kind 溫泉 [--area 礁溪]     挑候選：最久沒推的排前面（過期、失效的不給）；
                                              要過夜用 --kind 住宿（溫泉飯店也列）
  places.py add --name … --area … --kind … --official <官網> [--price-page <價格頁>]
                                              打開網頁，價格或「免費」讀得到才收進來
  places.py recheck [--days 7]                重開超過幾天沒查的網頁；讀不到就標失效（不刪）
  places.py list

Only this script writes places.json (the plugin refuses a hand edit): the file stays valid, every place
keeps where its prices came from and when they were read.
"""
import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
KINDS = ("溫泉", "住宿", "自然步道、看風景", "海邊", "老街、小吃、夜市", "其他")
# 2026-11-27: 「礁溪住一晚」 asked pick --kind 住宿, got 「地點庫沒有」 though seven 礁溪 hot-spring hotels were kept
# as 溫泉, then add --kind 住宿 was refused. A room can be at a 溫泉 place too, so 住宿 picks both.
ALSO = {"住宿": ("溫泉",)}


def kind_problem(kind, required=False):
    """Why --kind is wrong; pick may leave it out, add may not (Antigravity's review: --kind "" got in)."""
    if (kind or required) and kind not in KINDS:
        return f"--kind 要是其中一種：{'、'.join(KINDS)}（要過夜就用 住宿，溫泉飯店也會列出來）"
    return None
STALE_DAYS = 30          # prices not read again for this long are not offered
FREE = re.compile(r"免費入園|免費參觀|免費開放|免費入場|免門票|免收費|不收費|不需門票|無需門票|免入園費"
                  r"|(門票|入園|入場|參觀)[^。\n]{0,6}免費")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_places(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    places = data.get("places") if isinstance(data, dict) else None
    return [p for p in places if isinstance(p, dict)] if isinstance(places, list) else []


def write_places(path, places):
    """Write through a temporary file, keeping the previous version as places.backup.json (one only)."""
    path = Path(path)
    # The new version first: a failed write leaves places.json as it was, never missing (Antigravity's review).
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"schema_version": 1, "places": places}, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    if path.exists():
        backup = path.with_name("places.backup.json")
        backup.write_bytes(path.read_bytes())
    temporary.replace(path)


def today(now=None):
    return dt.date.fromtimestamp(now or time.time()).isoformat()


def age_days(place, now=None):
    try:
        checked = dt.date.fromisoformat(place.get("checked_at", ""))
    except ValueError:
        return 10 ** 6
    return (dt.date.fromtimestamp(now or time.time()) - checked).days


def read_page(url, args):
    """(title, price lines, phones, says free) for one page, or (None, error)."""
    prices = load("page_prices", "page-prices.py")
    page, error = prices.read(url, args)
    if page is None:
        return None, error
    title, lines, phones = page
    evidence = load("page_evidence", "page_evidence.py")
    text = evidence.latest_page_text(args.hermes_root, url) or ""
    return (title, lines, phones, bool(FREE.search(text))), None


def identity(place):
    plan = load("format_plan", "format-plan.py")
    return plan.place_identity(place.get("name"), place.get("official"), place.get("phone"))


def last_recommended(place, history):
    """When this place was last recommended (0 when never), from format-plan.py's recommended.json."""
    plan = load("format_plan", "format-plan.py")
    me = identity(place)
    return max((float(e.get("at", 0)) for e in history if plan.same_place(me, e)), default=0)


def add(args):
    if kind_problem(args.kind, required=True):
        return kind_problem(args.kind, required=True)
    page_url = args.price_page or args.official
    found, error = read_page(page_url, args)
    if found is None:
        return error
    title, lines, phones, free = found
    if not lines and not free and args.no_fee_listed:
        free = "unlisted"  # the owner chose (2026-11-26): recommend it, saying the page lists no fee
    if not lines and not free:
        return (f"{page_url} 上讀不到價格，也沒寫免費入園：不收進地點庫。換這間官網上寫著價格的那頁（--price-page），"
                "或換一個地方")
    place = {"name": args.name.strip(), "area": args.area.strip(), "kind": args.kind, "official": args.official,
             "price_page": page_url, "phone": args.phone or (phones[0].split()[-1] if phones else ""),
             "prices": lines[:6], "free": free, "checked_at": today(args.now), "status": "ok"}
    places = read_places(args.places)
    plan = load("format_plan", "format-plan.py")
    for i, old in enumerate(places):
        if plan.same_place(identity(old), identity(place)):
            places[i] = {**old, **place, "added_at": old.get("added_at", today(args.now))}
            write_places(args.places, places)
            print(f"更新：{place['name']}（{place['area']}，{len(place['prices'])} 個價格）")
            return None
    places.append({**place, "added_at": today(args.now)})
    write_places(args.places, places)
    print(f"收進地點庫：{place['name']}（{place['area']}・{place['kind']}，"
          + ("官方頁沒列收費" if free == "unlisted" else "免費" if free and not lines
             else f"{len(place['prices'])} 個價格") + "）")
    return None


def candidates(places, history, kind=None, area=None, now=None):
    """Usable places of this kind and area, the least recently recommended first."""
    usable = [p for p in places if p.get("status") == "ok" and age_days(p, now) <= STALE_DAYS
              and (not kind or p.get("kind") in (kind, *ALSO.get(kind, ()))) and (not area or area in p.get("area", ""))]
    return sorted(usable, key=lambda p: (last_recommended(p, history), p.get("name", "")))


def pick(args):
    if kind_problem(args.kind):
        return None, kind_problem(args.kind)
    history = load("format_plan", "format-plan.py").read_history(args.history)
    found = candidates(read_places(args.places), history, args.kind, args.area, args.now)
    if not found:
        return None, (f"地點庫沒有{args.area or ''}{args.kind or ''}的地方：用 web_search 找官網，"
                      "page-prices.py 讀得到價格的，用 places.py add 收進來")
    lines = []
    for place in found[:args.limit]:
        when = last_recommended(place, history)
        # Chinese outside strftime: CI's locale cannot encode it (UnicodeEncodeError on 推).
        said = time.strftime("%m/%d", time.localtime(when)) + " 推過" if when else "沒推過"
        price = (place.get("prices") or [""])[0] or ("官方頁沒列收費" if place.get("free") == "unlisted"
                                                  else "免費" if place.get("free") else "未確認")
        lines.append(f"• {place['name']}｜{place['area']}・{place['kind']}｜{said}｜{price}"
                     f"｜價格頁 {place['price_page']}" + (f"｜📞 {place['phone']}" if place.get("phone") else ""))
    lines.append("用之前先對這幾頁跑 page-prices.py（價格以今天讀到的為準），再用 format-plan.py --package 組方案。")
    return "\n".join(lines), None


def recheck(args):
    places, changed = read_places(args.places), []
    for place in places:
        if age_days(place, args.now) < args.days:
            continue
        found, error = read_page(place["price_page"], args)
        if found is None or not (found[1] or found[3] or (place.get("free") == "unlisted" and found[0])):
            if place.get("status") != "broken":
                place["status"] = "broken"
                changed.append(f"失效：{place['name']}（{error or '讀不到價格'}）")
            continue
        free = found[3] or ("unlisted" if place.get("free") == "unlisted" and not found[1] else False)
        place.update(prices=found[1][:6], free=free, checked_at=today(args.now), status="ok")
        changed.append(f"重查：{place['name']}")
    if changed:
        write_places(args.places, places)
    print("\n".join(changed) or "都在期限內，不用重查")


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--places", default=HERE / "places.json", help=argparse.SUPPRESS)
    parser.add_argument("--history", default=HERE / "recommended.json", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-root", default=HERE.parent.parent, help=argparse.SUPPRESS)
    parser.add_argument("--html-file", help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--now", type=float, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--timeout", type=float, default=10, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("add")
    one.add_argument("--name", required=True)
    one.add_argument("--area", required=True, help="地區，例如 礁溪、烏來、北投、金山、九份")
    one.add_argument("--kind", required=True, help="、".join(KINDS))
    one.add_argument("--official", required=True, help="這個地方自己的官網")
    one.add_argument("--price-page", help="官網上寫價格（或寫免費入園）的那頁，沒給就用 --official")
    one.add_argument("--phone")
    one.add_argument("--no-fee-listed", action="store_true",
                     help="景點官方頁讀得到但沒列收費（海邊、步道）：照樣收，挑的時候寫「官方頁沒列收費」，不能寫免費")
    some = sub.add_parser("pick")
    some.add_argument("--kind")
    some.add_argument("--area")
    some.add_argument("--limit", type=int, default=6)
    again = sub.add_parser("recheck")
    again.add_argument("--days", type=int, default=7)
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "add":
        problem = add(args)
    elif args.command == "pick":
        text, problem = pick(args)
        if text:
            print(text)
    elif args.command == "recheck":
        problem = recheck(args)
    else:
        for place in read_places(args.places):
            print(f"{place.get('status', '?'):6} {place.get('checked_at', '?')} {place.get('area', '')}・"
                  f"{place.get('kind', '')} {place.get('name', '')}")
        problem = None
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
