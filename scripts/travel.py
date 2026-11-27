#!/usr/bin/env python3
"""Unified deterministic travel engine for the Hermes travel workflow.

One entry point for all travel operations so AI roles stay small and never hand-stitch
prices, flight times, room types, or clickable booking links:

  travel.py overseas <城市> <出發 YYYY-MM-DD> <回程 YYYY-MM-DD> [--guests 2] [--budget 45000]
  travel.py local [--area 礁溪] [--kind 溫泉] [--date YYYY-MM-DD] [--nights 0] [--guests 2] [--budget 2000]
  travel.py detail <format-plan.py flags...>
  travel.py add-place <places.py add flags...>
  travel.py photo <place-photo.py flags...>
  travel.py prices <page-prices.py flags...>
  travel.py schedule --from <YYYY-MM-DD> --to <YYYY-MM-DD>
  travel.py remind <trip-reminders.py flags...>
  travel.py check <phone-check.py flags...>
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


def load_module(name, filename):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_hermes_root():
    # Installed under <root>/shared/travel-workflow/travel.py: HERE is authoritative
    # because Hermes sets HERMES_HOME to <root>/profiles/<profile> inside a profile's terminal.
    if HERE.name == "travel-workflow" and HERE.parent.name == "shared":
        return HERE.parent.parent
    if os.environ.get("HERMES_HOME"):
        home = Path(os.environ["HERMES_HOME"])
        if home.parent.name == "profiles":
            return home.parent.parent
        return home
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "hermes").is_dir():
        return Path(local) / "hermes"
    return HERE.parent.parent


def read_json_safe(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


_MONEY = load_module("travel_money", "travel_money.py")


def money(value):
    return _MONEY.money(int(round(value)))


def parse_amount(text):
    found = _MONEY.amounts(text or "")
    return int(re.sub(r"\D", "", found[0])) if found else None


def short_date(iso_date):
    try:
        d = dt.date.fromisoformat(iso_date)
        return f"{d.month:02d}/{d.day:02d}"
    except ValueError:
        return iso_date


def nights_between(start_iso, end_iso):
    d1 = dt.date.fromisoformat(start_iso)
    d2 = dt.date.fromisoformat(end_iso)
    return (d2 - d1).days


def next_saturday(today=None):
    base = today or dt.date.today()
    offset = (5 - base.weekday()) % 7
    if offset == 0:
        offset = 7
    return (base + dt.timedelta(days=offset)).isoformat()


def check_calendar_conflict(start_iso, end_iso, hermes_root):
    """Best-effort calendar conflict summary; returns None when free or unavailable."""
    cal_script = HERE / "calendar-check.py"
    if not cal_script.is_file():
        return None
    cal_file = Path(hermes_root) / "shared" / "annual-schedule" / "calendar.json"
    if not cal_file.is_file():
        return None
    try:
        res = subprocess.run(
            [sys.executable, str(cal_script), "--from", start_iso, "--to", end_iso,
             "--calendar", str(cal_file)],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or "").strip()
    if not out:
        return None
    first = out.splitlines()[0].strip()
    if "沒有排定的事" in first:
        return None
    # Keep only actual event lines (drop redundant header "12/6–12/6 行事曆：" and "未知：...")
    events = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s or s.startswith("未知") or s.endswith("行事曆："):
            continue
        events.append(s.lstrip("•・-* ").strip())
    if not events:
        return None
    joined = "；".join(events[:2])
    return (joined[:22] + "…") if len(joined) > 23 else joined


# ---------------------------------------------------------------------------
# Overseas: scanner runner + deterministic parser + phone-ready card renderer
# ---------------------------------------------------------------------------

def resolve_scanner_cmd(hermes_root):
    """Return command prefix list to run scanner CLI (`... cli.ts`)."""
    root = Path(hermes_root)
    # 1. Live trip-bot project (preferred on the user's running Hermes home)
    trip_bot = root / "profiles" / "trip" / "projects" / "trip-bot"
    node_exe = shutil.which("node") or (
        r"C:\Program Files\nodejs\node.exe" if Path(r"C:\Program Files\nodejs\node.exe").is_file() else None
    )
    for candidate in (trip_bot,):
        tsx = candidate / "node_modules" / "tsx" / "dist" / "cli.mjs"
        cli = candidate / "src" / "cli.ts"
        if node_exe and tsx.is_file() and cli.is_file():
            return [node_exe, "--disable-warning=ExperimentalWarning", str(tsx), str(cli)], candidate

    # 2. runtime.json installed by setup.py
    runtime = read_json_safe(root / "shared" / "travel-workflow" / "runtime.json")
    if runtime.get("scanner_dir") and runtime.get("node"):
        sdir = Path(runtime["scanner_dir"])
        tsx = sdir / "node_modules" / "tsx" / "dist" / "cli.mjs"
        cli = sdir / "src" / "cli.ts"
        if Path(runtime["node"]).is_file() and tsx.is_file() and cli.is_file():
            return [runtime["node"], "--disable-warning=ExperimentalWarning", str(tsx), str(cli)], sdir

    # 3. Repo checkout sibling `scanner/`
    repo_scanner = REPO_ROOT / "scanner"
    tsx = repo_scanner / "node_modules" / "tsx" / "dist" / "cli.mjs"
    cli = repo_scanner / "src" / "cli.ts"
    if node_exe and tsx.is_file() and cli.is_file():
        return [node_exe, "--disable-warning=ExperimentalWarning", str(tsx), str(cli)], repo_scanner

    return None, None


MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
HOTEL_START_RE = re.compile(r"^(\d+)\.\s+\[([^\]]+)\]\((https?://[^)\s]+)\)\s*$")


def parse_deals_plan(raw_text, city, outbound_date, return_date, guests):
    """Deterministically parse `deals plan` stdout into structured flight + hotel data."""
    text = (raw_text or "").replace("\r\n", "\n")
    # Strip PLAN_TOTAL_NOTE footer first so its instructions ("寫「這次沒讀到訂票頁」", "有「＋托運行李」那行")
    # never falsely trigger no_booking_page or overwrite bag_estimate_per_person!
    text = text.split("「含機票」總價＝", 1)[0]
    nights = max(1, nights_between(outbound_date, return_date))
    data = {
        "city": city,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "nights": nights,
        "guests": guests,
        "status": "ok",
        "reason": None,
        "flight": {
            "outbound_text": None,
            "inbound_text": None,
            "outbound_url": None,
            "inbound_url": None,
            "separate_tickets": False,
            "per_person": None,
            "bag_estimate_per_person": None,
            "baggage_status": "unknown",  # included | estimated | excluded | unknown
            "all_flights_url": None,
            "market": None,
        },
        "hotels": [],
        "all_hotels_url": None,
    }

    if "Google 暫時限流" in text:
        data["status"] = "rate_limited"
        data["reason"] = "Google 暫時限流"
        return data
    if "這次沒讀到航班" in text:
        data["status"] = "no_flights"
        data["reason"] = "這次沒讀到航班"
        return data

    lines = [ln.strip() for ln in text.splitlines()]
    fl = data["flight"]
    no_booking_page = ("這次沒讀到訂票頁" in text) or ("沒有符合時段的航班" in text)

    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln:
            i += 1
            continue

        # Hotel entry starts here
        hm = HOTEL_START_RE.match(ln)
        if hm:
            hotel = {
                "rank": int(hm.group(1)),
                "name": hm.group(2).strip(),
                "google_url": hm.group(3).strip(),
                "rating": None,
                "room_type": None,
                "room_confirmed": False,
                "public_bath": False,
                "area": None,
                "nightly": None,
                "site_name": None,
                "booking_url": hm.group(3).strip(),
                "stay_total": None,
                "combined_total": None,
                "per_person_total": None,
            }
            j = i + 1
            while j < len(lines):
                sub = lines[j]
                if not sub:
                    j += 1
                    continue
                if HOTEL_START_RE.match(sub) or sub.startswith("[看全部住宿]") or sub.startswith("[在 Google Flights") or sub.startswith("「含機票」"):
                    break
                if "⭐" in sub:
                    rm = re.search(r"⭐\s*([\d.]+)", sub)
                    if rm:
                        hotel["rating"] = rm.group(1)
                if "♨️" in sub:
                    hotel["public_bath"] = True
                if "📍" in sub:
                    am = re.search(r"📍\s*([^・\n]+)", sub)
                    if am:
                        area = am.group(1).strip()
                        if area != "位置未確認":
                            hotel["area"] = area
                if "房型未確認" in sub:
                    hotel["room_type"] = "房型未確認"
                    hotel["room_confirmed"] = False
                elif hotel["room_type"] is None:
                    for part in sub.split("・"):
                        p = part.strip()
                        if any(k in p for k in ("雙人房", "雙床房", "小雙人房", "三人房", "家庭房", "套房", "和室", "單人房", "標準房", "客房", "私人房")) or re.search(r"\b(double|twin|queen|king|single|suite|studio|standard)\b", p, re.I):
                            cleaned = p.lstrip("✅❔ ").strip()
                            hotel["room_type"] = cleaned
                            hotel["room_confirmed"] = ("未確認" not in p) and (not p.startswith("❔"))
                            break
                if sub.startswith("每晚含稅"):
                    hotel["nightly"] = parse_amount(sub)
                elif sub.startswith("在 ["):
                    lm = MD_LINK_RE.search(sub)
                    if lm:
                        hotel["site_name"] = lm.group(1).strip()
                        hotel["booking_url"] = lm.group(2).strip()
                elif re.match(r"^\d+晚", sub):
                    hotel["stay_total"] = parse_amount(sub)
                    if "含機票" in sub:
                        amts = _MONEY.amounts(sub)
                        if len(amts) >= 2:
                            hotel["combined_total"] = parse_amount(amts[1])
                elif "人含機票" in sub:
                    hotel["combined_total"] = parse_amount(sub)
                elif sub.startswith("每人 "):
                    hotel["per_person_total"] = parse_amount(sub)
                j += 1
            if hotel["room_type"] is None:
                hotel["room_type"] = "房型未確認"
                hotel["room_confirmed"] = False
            data["hotels"].append(hotel)
            i = j
            continue

        # Flight lines
        if ln.startswith("📊"):
            fl["market"] = ln.lstrip("📊 ").strip()
        elif ln.startswith("✈️ "):
            lm = MD_LINK_RE.search(ln)
            if lm:
                label, url = lm.group(1).strip(), lm.group(2).strip()
                if label.startswith("去 "):
                    fl["outbound_text"] = label[2:].strip()
                    fl["outbound_url"] = url
                elif "來回每人" in label:
                    fl["per_person"] = parse_amount(label)
                    fl["outbound_url"] = url
        elif ln.startswith("[回 "):
            lm = MD_LINK_RE.search(ln)
            if lm:
                fl["inbound_text"] = lm.group(1).strip()[2:].strip()
                fl["inbound_url"] = lm.group(2).strip()
                fl["separate_tickets"] = True
        elif ln.startswith("回 "):
            fl["inbound_text"] = ln[2:].strip()
        elif ln.startswith("來回每人"):
            fl["per_person"] = parse_amount(ln)
        elif "＋托運行李" in ln:
            fl["bag_estimate_per_person"] = parse_amount(ln)
        elif ln.startswith("分開訂兩張票"):
            fl["separate_tickets"] = True
        elif ln.startswith("[在 Google Flights 看全部航班]"):
            lm = MD_LINK_RE.search(ln)
            if lm:
                fl["all_flights_url"] = lm.group(2).strip()
        elif ln.startswith("[看全部住宿]"):
            lm = MD_LINK_RE.search(ln)
            if lm:
                data["all_hotels_url"] = lm.group(2).strip()
        i += 1

    # Determine baggage status (exact 3-case rule)
    if no_booking_page:
        fl["baggage_status"] = "excluded"
    elif fl["bag_estimate_per_person"] is not None:
        fl["baggage_status"] = "estimated"
    elif fl["outbound_text"] and fl["inbound_text"]:
        fl["baggage_status"] = "included"
    else:
        fl["baggage_status"] = "excluded"

    if fl["per_person"] is None and not data["hotels"]:
        data["status"] = "no_flights"
        data["reason"] = "未讀到有效航班或住宿價格"

    return data


def _baggage_label(status):
    return {
        "included": "已含托運行李",
        "estimated": "含估算托運行李",
        "excluded": "未含托運行李",
    }.get(status, "行李未確認")


def format_overseas_card(parsed, budget=None, top=2, calendar_note=None, show_unconfirmed=True):
    """Render a deterministic, phone-ready Telegram message (<= 14 phone lines) from parsed data."""
    city = parsed["city"]
    out_s = short_date(parsed["outbound_date"])
    ret_s = short_date(parsed["return_date"])
    nights = parsed["nights"]
    days = nights + 1
    guests = parsed["guests"]
    header = f"{out_s}–{ret_s} {city} {days}天{nights}夜（{guests}人）"

    if parsed["status"] == "rate_limited":
        return f"{header}\n機票與住宿：這次沒讀到價格（Google 暫時限流），未確認。"
    if parsed["status"] != "ok":
        return f"{header}\n機票與住宿：這次沒讀到價格，未確認。"

    fl = parsed["flight"]
    bag_text = _baggage_label(fl["baggage_status"])
    if not show_unconfirmed and bag_text == "行李未確認":
        bag_text = ""
    per_flight = fl["per_person"] or 0
    if fl["bag_estimate_per_person"]:
        per_flight += fl["bag_estimate_per_person"]
    total_flight = per_flight * guests if per_flight else None

    # Prefer hotels with confirmed room types; when show_unconfirmed is False, drop unconfirmed-room hotels
    hotels = list(parsed["hotels"])
    confirmed_hotels = [h for h in hotels if h.get("room_confirmed")]
    if show_unconfirmed:
        ranked = (confirmed_hotels + [h for h in hotels if not h.get("room_confirmed")])[:max(1, top)]
    else:
        ranked = confirmed_hotels[:max(1, top)]

    lines = [header]
    if calendar_note:
        lines.append(f"⚠️ 行事曆：{calendar_note}")

    # Headline total (from top hotel if available, else flight-only)
    if ranked and ranked[0].get("combined_total"):
        best_total = ranked[0]["combined_total"]
        if budget is not None:
            diff = budget - best_total
            tail = f"預算剩 {money(diff)}" if diff >= 0 else f"超出預算 {money(-diff)}"
            lines.append(f"總計 {money(best_total)}（{tail}）")
        else:
            lines.append(f"總計 {money(best_total)}")
    elif total_flight is not None:
        bag_suffix = f"（{bag_text}）" if bag_text else ""
        lines.append(f"機票 {guests}人 {money(total_flight)}{bag_suffix}")

    # Flight block (always includes both airlines when they differ, times, baggage & clickable link)
    if fl["outbound_text"] and fl["inbound_text"]:
        out_airline = fl["outbound_text"].split()[0]
        in_airline = fl["inbound_text"].split()[0]
        same_airline = (out_airline == in_airline)
        airline_hdr = out_airline if same_airline else f"{out_airline}／{in_airline}"
        flight_amt = f" {guests}人 {money(total_flight)}" if total_flight else ""
        bag_suffix = f"（{bag_text}）" if bag_text else ""
        lines.append(f"✈️ {airline_hdr}{flight_amt}{bag_suffix}")
        out_time = fl["outbound_text"][len(out_airline):].strip() if fl["outbound_text"].startswith(out_airline) else fl["outbound_text"]
        in_time = fl["inbound_text"][len(in_airline):].strip() if fl["inbound_text"].startswith(in_airline) else fl["inbound_text"]
        if same_airline:
            lines.append(f"• 去 {out_s} {out_time}｜回 {ret_s} {in_time}")
        else:
            lines.append(f"• 去 {out_s} {out_airline} {out_time}")
            lines.append(f"• 回 {ret_s} {in_airline} {in_time}")
        if fl["separate_tickets"] and fl["outbound_url"] and fl["inbound_url"]:
            link_line = f"• [去程機票]({fl['outbound_url']})／[回程機票]({fl['inbound_url']})"
        elif fl["outbound_url"]:
            link_line = f"• [機票訂票連結]({fl['outbound_url']})"
        elif fl["all_flights_url"]:
            link_line = f"• [查看航班]({fl['all_flights_url']})"
        else:
            link_line = None
        if link_line:
            if fl["all_flights_url"] and fl["all_flights_url"] != fl["outbound_url"]:
                link_line += f"｜[全部航班]({fl['all_flights_url']})"
            lines.append(link_line)
    else:
        flight_url = fl["outbound_url"] or fl["all_flights_url"]
        link_part = f"｜[看航班]({flight_url})" if flight_url else ""
        amt_part = f"{guests}人 {money(total_flight)}" if total_flight else ("價格未確認" if show_unconfirmed else "")
        bag_suffix = f"（{bag_text}）" if bag_text else ""
        if amt_part or link_part:
            lines.append(f"✈️ 機票 {amt_part}{bag_suffix}{link_part}".strip())

    # Hotel block (always includes full name, room type, verified stay/nightly price, clickable booking link)
    marks = "①②③"
    if not ranked:
        all_h = f"｜[看住宿]({parsed['all_hotels_url']})" if parsed.get("all_hotels_url") else ""
        if show_unconfirmed:
            lines.append(f"🏨 住宿：這次沒讀到價格，未確認{all_h}")
        elif all_h:
            lines.append(f"🏨 住宿：這次沒讀到確認房型{all_h}")
    else:
        for idx, h in enumerate(ranked):
            mark = marks[idx] if idx < len(marks) else f"{idx + 1}."
            room = f"（{h['room_type']}）" if h.get("room_type") else ""
            lines.append(f"🏨 {mark} {h['name']}{room}")
            parts = []
            if h.get("stay_total") is not None:
                parts.append(f"{nights}晚 {money(h['stay_total'])}")
            elif h.get("nightly") is not None:
                parts.append(f"每晚 {money(h['nightly'])}")
            elif show_unconfirmed:
                parts.append("住宿總價未確認")
            if idx > 0 and h.get("combined_total") is not None:
                parts.append(f"合計 {money(h['combined_total'])}")
            if h.get("rating"):
                parts.append(f"⭐{h['rating']}")
            if h.get("area") and idx == 0:
                parts.append(h["area"])
            site_lbl = h.get("site_name") or "訂房"
            if h.get("booking_url"):
                parts.append(f"[{site_lbl}]({h['booking_url']})")
            lines.append("• " + "｜".join(parts))

    return "\n".join(lines)


FOREIGN_CURRENCY_RE = re.compile(
    r"日圓|日幣|円|JPY|美金|美元|USD|歐元|EUR|韓元|韓幣|KRW|港幣|HKD|英鎊|GBP",
    re.I,
)


def owner_quoted_budget(prefs):
    """Return TWD entire-trip budget amount ONLY when preferences.json carries the owner's own quote."""
    b = prefs.get("budget") if isinstance(prefs, dict) else None
    if isinstance(b, dict) and b.get("amount") and b.get("quote"):
        raw_currency = b.get("currency")
        if raw_currency:
            currency = str(raw_currency).upper()
        else:
            if FOREIGN_CURRENCY_RE.search(str(b.get("quote"))):
                return None
            currency = "TWD"
        scope = b.get("scope") or "entire_trip"
        if currency != "TWD" or scope != "entire_trip":
            return None
        try:
            val = int(b["amount"])
            return val if val > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def cmd_overseas(args):
    root = Path(args.hermes_root or default_hermes_root())
    prefs = read_json_safe(root / "shared" / "travel-workflow" / "preferences.json")
    guests = args.guests or default_guests(prefs)
    budget = args.budget if args.budget is not None else owner_quoted_budget(prefs)
    show_unconfirmed = prefs.get("show_unconfirmed", True) is not False

    if args.raw_output:
        raw_text = Path(args.raw_output).read_text(encoding="utf-8")
        cal_note = None
    else:
        cmd_prefix, cwd = resolve_scanner_cmd(root)
        if not cmd_prefix:
            print("找不到機票住宿掃描器（trip-bot 或 scanner/）；請先安裝 scanner 相依套件。", file=sys.stderr)
            return 1
        with ThreadPoolExecutor(max_workers=2) as pool:
            cal_future = pool.submit(check_calendar_conflict, args.outbound, args.ret, root)
            env = {**os.environ, "NODE_OPTIONS": "--disable-warning=ExperimentalWarning"}
            proc = subprocess.run(
                [*cmd_prefix, "plan", args.city, args.outbound, args.ret, str(guests)],
                cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=300,
            )
            cal_note = cal_future.result()
        raw_text = (proc.stdout or "") + ("\n" + proc.stderr if proc.returncode else "")
        if proc.returncode != 0 and "Google 暫時限流" not in raw_text and "這次沒讀到航班" not in raw_text:
            print(f"掃描器執行失敗（exit {proc.returncode}）：{raw_text.strip()[:300]}", file=sys.stderr)
            return 1

    parsed = parse_deals_plan(raw_text, args.city, args.outbound, args.ret, guests)
    phone_check = load_module("phone_check", "phone-check.py")
    message = format_overseas_card(parsed, budget=budget, top=args.top, calendar_note=cal_note, show_unconfirmed=show_unconfirmed)
    report = phone_check.check(message)
    # Auto-trim to 1 hotel if 2 hotels pushed past the 14-line phone limit
    if report["problems"] and args.top > 1 and any(p.get("kind") == "too_long" for p in report["problems"]):
        shorter = format_overseas_card(parsed, budget=budget, top=1, calendar_note=cal_note, show_unconfirmed=show_unconfirmed)
        shorter_report = phone_check.check(shorter)
        if not shorter_report["problems"]:
            message, report = shorter, shorter_report

    if args.json:
        print(json.dumps({"parsed": parsed, "message": message, "phone_check": report}, ensure_ascii=False, indent=2))
        return 0 if not report["problems"] else 1
    if report["problems"]:
        for p in report["problems"]:
            print(f"- {p['kind']}: {p.get('text', '')} {p.get('hint', '')}".strip(), file=sys.stderr)
        return 1
    print("已通過 phone-check.py 手機排版檢查，可直接原樣送出。", file=sys.stderr)
    print(message)
    return 0


# ---------------------------------------------------------------------------
# Local: auto-pick from places.json + parallel live price fetch + format-plan
# ---------------------------------------------------------------------------

OVERNIGHT_HINTS = ("住宿", "雙人房", "兩人房", "標準房", "精緻", "雅緻", "和室", "客房", "一晚", "套房", "每晚")
DAY_HINTS = ("湯屋", "泡湯", "大眾池", "裸湯", "小時", "分鐘", "門票", "入場", "入園", "全票", "雙人池")
BAD_ITEM_HEADS = ("價位資訊", "收費標準", "價格表", "價目表", "費用說明", "房價表", "平假日定義", "溫泉房型", "營運時間及收費", "時間及票價")


def _extract_prices_from_line(line):
    vals = []
    for token in _MONEY.PRICE.findall(line or ""):
        digits = re.sub(r"\D", "", token)
        if digits.isdigit():
            val = int(digits)
            if 50 <= val <= 100000:
                vals.append(val)
    return vals


def _clean_room_label(line, fallback):
    parts = [p.strip() for p in (line or "").split("｜") if p.strip()]
    candidates = []
    if len(parts) >= 2:
        # Prefer the specific room/item segment after a generic section heading (e.g. 平假日定義｜山景溫泉湯屋：...)
        first_head = re.sub(r"[（(][^）)]*[）)]", "", parts[0].split("：")[0].split(":")[0]).strip()
        second_head = re.sub(r"[（(][^）)]*[）)]", "", parts[-1].split("：")[0].split(":")[0]).strip()
        if any(b in first_head for b in BAD_ITEM_HEADS) or len(first_head) > 16 or any(k in first_head for k in ("旺季", "淡季")):
            candidates.extend([second_head, first_head])
        else:
            candidates.extend([first_head, second_head])
    elif parts:
        candidates.append(re.sub(r"[（(][^）)]*[）)]", "", parts[0].split("：")[0].split(":")[0]).strip())
    for head in candidates:
        head = re.sub(r"^[■•‧・\-\s]+", "", head).strip()
        if head and len(head) <= 16 and not any(b in head for b in BAD_ITEM_HEADS) and not _MONEY.PRICE.search(head):
            return head
    return fallback


PURE_NOISE_HINTS = (
    "加價", "加購", "加人", "加床", "加時", "逾時", "延時", "每加",
    "毛巾", "礦泉水", "沐浴品", "清潔費", "押金", "折抵", "憑券",
    "車資", "單程", "退款", "退訂", "處理費", "熱水", "冷水", "其他收費",
    "優待票", "敬老", "孩童", "兒童票", "愛心票",
)
MEAL_SUBITEM_HINTS = ("享用", "套餐", "贈送")
MONTH_RANGE_RE = re.compile(r"(?P<a>1[0-2]|0?[1-9])\s*月?\s*[-–~至到]\s*(?P<b>1[0-2]|0?[1-9])\s*月")
DATE_RANGE_RE = re.compile(
    r"(?:(?P<y1>\d{4})[/-])?(?P<m1>1[0-2]|0?[1-9])/(?P<d1>\d{1,2})\s*[-–~至到]\s*"
    r"(?:(?P<y2>\d{4})[/-])?(?P<m2>1[0-2]|0?[1-9])/(?P<d2>\d{1,2})"
)


def _is_subitem_or_noise(line):
    if any(k in line for k in PURE_NOISE_HINTS):
        return True
    if any(k in line for k in MEAL_SUBITEM_HINTS):
        if not any(k in line for k in OVERNIGHT_HINTS) and not any(k in line for k in DAY_HINTS):
            return True
    return False


def _in_month_range(month, start_m, end_m):
    return start_m <= month <= end_m if start_m <= end_m else (month >= start_m or month <= end_m)


def _explicit_month_ranges(line):
    ranges = [(int(m["a"]), int(m["b"])) for m in MONTH_RANGE_RE.finditer(line or "")]
    for m in DATE_RANGE_RE.finditer(line or ""):
        ranges.append((int(m["m1"]), int(m["m2"])))
    return ranges


def _is_expired_promo(line, trip_date):
    if not trip_date:
        return False
    end_dates = []
    for m in DATE_RANGE_RE.finditer(line or ""):
        y2 = m.group("y2") or m.group("y1")
        if y2:
            try:
                end_dates.append(dt.date(int(y2), int(m.group("m2")), int(m.group("d2"))))
            except ValueError:
                continue
    return bool(end_dates) and all(trip_date > end_d for end_d in end_dates)


def _price_for_day_type(line, is_weekend):
    """Extract the right price from a line like '旺季平日$2,600元、假日$3,000元' (ignoring section heading before ｜)."""
    body = (line or "").split("｜")[-1]
    norm = body.replace("非假日", "平日")
    if is_weekend and "假日" in norm:
        after = norm.split("假日", 1)[1]
        found = _extract_prices_from_line(after)
        if found:
            return found[0]
    if (not is_weekend) and "平日" in norm:
        after = norm.split("平日", 1)[1]
        # Stop before '假日' if both appear on one line
        after = after.split("假日", 1)[0]
        found = _extract_prices_from_line(after)
        if found:
            return found[0]
    prices = _extract_prices_from_line(norm)
    return min(prices) if prices else None


def _room_capacity(line):
    if any(k in line for k in ("四人房", "4人房", "家庭房", "四人", "家庭湯屋")):
        return 4
    if any(k in line for k in ("三人房", "3人房", "三人")):
        return 3
    if any(k in line for k in ("單人房", "1人房")):
        return 1
    return 2


def select_best_price_line(lines, *, nights=0, guests=2, is_weekend=True, month=None, trip_date=None,
                           cheaper_than=None, budget=None, kind=""):
    """Pick the best matching (label, unit_price, qty) from page-prices.py lines without guessing seasons."""
    if isinstance(trip_date, str):
        trip_date = dt.date.fromisoformat(trip_date)
    if trip_date is not None and month is None:
        month = trip_date.month

    # Detect ambiguous un-dated 淡季/旺季 or 夏季/冬季 pairs so we never guess a venue's season months
    undated_peak = any(("旺季" in ln or "冬季" in ln) and not _explicit_month_ranges(ln) for ln in (lines or []))
    undated_off = any(("淡季" in ln or "夏季" in ln) and not _explicit_month_ranges(ln) for ln in (lines or []))
    has_ambiguous_seasons = undated_peak and undated_off

    scored = []
    for ln in lines or []:
        if _is_subitem_or_noise(ln):
            continue
        if _is_expired_promo(ln, trip_date):
            continue
        unit = _price_for_day_type(ln, is_weekend)
        if unit is None:
            continue

        # Season check: ONLY filter by explicit month/date ranges printed on the page; never guess!
        ranges = _explicit_month_ranges(ln)
        if month is not None and ranges:
            if not any(_in_month_range(month, a, b) for a, b in ranges):
                continue
        elif month is not None and has_ambiguous_seasons and not ranges:
            if any(k in ln for k in ("淡季", "旺季", "夏季", "冬季")):
                continue

        # Day-of-week check on the price body (after ｜) so a heading like '平假日定義' doesn't mask '/ 平日'
        body = ln.split("｜")[-1]
        norm = body.replace("非假日", "平日")
        weekend_pen = 0
        if is_weekend and "平日" in norm and "假日" not in norm and "平假日" not in norm:
            weekend_pen = 2
        elif (not is_weekend) and "假日" in norm and "平日" not in norm and "平假日" not in norm:
            weekend_pen = 2

        mode_score = 0
        if nights > 0:
            if any(k in ln for k in OVERNIGHT_HINTS):
                mode_score = -2
            elif any(k in ln for k in ("湯屋", "分鐘", "小時", "大眾池")):
                mode_score = 3
        else:
            if any(k in ln for k in DAY_HINTS):
                mode_score = -2
            elif any(k in ln for k in ("住宿", "雙人房", "一晚", "客房")):
                mode_score = 2

        per_person = (
            any(k in ln for k in ("/人", "每人", "一位", "成人", "全票", "每張", "單人"))
            and not any(k in ln for k in ("雙人", "2人", "兩人", "四人", "4人", "三人", "3人", "家庭"))
        )
        if per_person:
            qty = max(1, guests) * (nights if nights > 0 else 1)
        else:
            cap = _room_capacity(ln)
            rooms = max(1, (guests + cap - 1) // cap)
            qty = rooms * (nights if nights > 0 else 1)
        total = unit * qty
        if cheaper_than is not None and total >= cheaper_than:
            continue
        if budget is not None and total > budget * 1.5:
            continue
        fallback = "雙人房" if nights > 0 else ("雙人湯屋" if "溫泉" in kind else "門票")
        label = _clean_room_label(ln, fallback)
        scored.append((mode_score + weekend_pen, total, label, unit, qty))

    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    _, _, label, unit, qty = scored[0]
    return label, unit, qty


def default_guests(prefs):
    if isinstance(prefs, dict):
        for key in ("traveler_count", "party_size"):
            val = prefs.get(key)
            if isinstance(val, int) and val > 0:
                return val
    return 2


def transport_text(prefs, area):
    if not isinstance(prefs, dict):
        return f"開車或大眾運輸前往{area or '目的地'}"
    no_transit = (
        prefs.get("public_transit") is False
        or (isinstance(prefs.get("transport_mode"), dict) and prefs["transport_mode"].get("public_transit") is False)
        or (isinstance(prefs.get("transport_mode"), str) and "不搭大眾運輸" in prefs["transport_mode"])
    )
    origin = prefs.get("origin") or (prefs["transport_mode"].get("origin") if isinstance(prefs.get("transport_mode"), dict) else None)
    short_origin = re.sub(r"（[^）]*）", "", origin or "").strip()
    if no_transit:
        prefix = f"從{short_origin}" if short_origin else ""
        return f"{prefix}開車／騎車前往{area or '目的地'}"
    return f"開車或大眾運輸前往{area or '目的地'}"


def pick_mixed_candidates(places_mod, all_places, history, *, kind=None, area=None, interests=None, now=None, limit=9):
    if kind:
        return places_mod.candidates(all_places, history, kind=kind, area=area, now=now)[:limit]
    # Mix across preferred interests so 3 packages aren't all the same kind
    buckets = []
    kinds_order = list(interests or []) or list(places_mod.KINDS)
    seen_kinds = set()
    for k in kinds_order:
        if k in seen_kinds:
            continue
        seen_kinds.add(k)
        c = places_mod.candidates(all_places, history, kind=k, area=area, now=now)
        if c:
            buckets.append(c)
    # Also add remaining usable places in case interests didn't cover everything
    rest = places_mod.candidates(all_places, history, kind=None, area=area, now=now)
    picked, seen_names = [], set()
    idx = 0
    while len(picked) < limit and any(idx < len(b) for b in buckets):
        for b in buckets:
            if idx < len(b):
                p = b[idx]
                key = p.get("name", "")
                if key not in seen_names:
                    seen_names.add(key)
                    picked.append(p)
                    if len(picked) >= limit:
                        break
        idx += 1
    for p in rest:
        if len(picked) >= limit:
            break
        key = p.get("name", "")
        if key not in seen_names:
            seen_names.add(key)
            picked.append(p)
    return picked


KIND_ALIASES = {
    "泡湯": "溫泉", "湯屋": "溫泉", "溫泉湯屋": "溫泉", "泡溫泉": "溫泉", "溫泉泡湯": "溫泉",
    "過夜": "住宿", "飯店": "住宿", "民宿": "住宿", "旅館": "住宿", "住一晚": "住宿",
    "步道": "自然步道、看風景", "爬山": "自然步道、看風景", "健行": "自然步道、看風景",
    "踏青": "自然步道、看風景", "風景": "自然步道、看風景", "自然步道": "自然步道、看風景", "看風景": "自然步道、看風景",
    "海灘": "海邊", "沙灘": "海邊", "看海": "海邊", "玩水": "海邊",
    "老街": "老街、小吃、夜市", "小吃": "老街、小吃、夜市", "夜市": "老街、小吃、夜市", "美食": "老街、小吃、夜市",
}


def normalize_kind(kind):
    if not kind:
        return None
    k = kind.strip()
    if k in KIND_ALIASES:
        return KIND_ALIASES[k]
    for alias, canonical in KIND_ALIASES.items():
        if alias in k:
            return canonical
    return k


def cmd_local(args):
    root = Path(args.hermes_root or default_hermes_root())
    shared = root / "shared" / "travel-workflow"
    prefs_path = Path(args.preferences) if args.preferences else shared / "preferences.json"
    places_path = Path(args.places) if args.places else shared / "places.json"
    history_path = Path(args.history) if args.history else shared / "recommended.json"

    prefs = read_json_safe(prefs_path)
    guests = args.guests or default_guests(prefs)
    date_iso = args.date or next_saturday()
    nights = max(0, int(args.nights or 0))
    checkout_iso = (dt.date.fromisoformat(date_iso) + dt.timedelta(days=nights)).isoformat() if nights > 0 else None
    is_weekend = dt.date.fromisoformat(date_iso).weekday() >= 5
    budget = args.budget if args.budget is not None else owner_quoted_budget(prefs)

    places_mod = load_module("places", "places.py")
    prices_mod = load_module("page_prices", "page-prices.py")
    format_mod = load_module("format_plan", "format-plan.py")

    effective_kind = normalize_kind(args.kind) or ("住宿" if nights > 0 else None)
    all_places = places_mod.read_places(places_path)
    if nights > 0:
        disq = (prefs.get("hot_spring_stay") or {}).get("disqualified") or []
        disq_names = [re.split(r"[（(]", str(d))[0].strip() for d in disq if str(d).strip()]
        if disq_names:
            all_places = [p for p in all_places if not any(dn and dn in (p.get("name") or "") for dn in disq_names)]
    history = format_mod.read_history(history_path)
    cands = pick_mixed_candidates(
        places_mod, all_places, history,
        kind=effective_kind, area=args.area,
        interests=prefs.get("interests") if not effective_kind else None,
        limit=max(6, args.limit),
    )
    if not cands:
        print(f"地點庫沒有符合條件的地方（地區={args.area or '不限'}、類型={effective_kind or '不限'}）；"
              "請用 web_search 找官網後用 travel.py add-place 收錄。", file=sys.stderr)
        return 1

    cal_note = check_calendar_conflict(date_iso, checkout_iso or date_iso, root)

    # Fetch live pages in parallel
    fetch_args = SimpleNamespace(hermes_root=root, html_file=args.html_file, timeout=args.timeout)
    urls = [c["price_page"] for c in cands]
    fetched = prices_mod.read_all(urls, fetch_args)

    # Build format-plan.py package objects from verified live pages
    def make_pkg(place, room, transport, item, no_fee_listed, official, phone, phone_source):
        return SimpleNamespace(
            place=place, room=room, transport=transport, dates=None, note=None,
            item=item, no_fee_listed=no_fee_listed, official=official,
            booking=None, booking_label=None, phone=phone, phone_source=phone_source,
            photo=None, photo_caption=None, date=None, checkin=None, checkout=None,
        )

    all_pkg_objs = []
    skip_reasons = set()
    for cand, (url, page, err) in zip(cands, fetched):
        if err or page is None:
            skip_reasons.add("其他候選頁今天讀不到價格")
            continue
        _title, price_lines, phones = page
        # Only use a phone number verified on THIS opened page today
        phone = (phones[0].split()[-1] if phones else "") or None
        phone_source = url if phone else None
        transport = transport_text(prefs, cand.get("area"))

        trip_date_obj = dt.date.fromisoformat(date_iso)
        picked_price = select_best_price_line(
            price_lines, nights=nights, guests=guests, is_weekend=is_weekend,
            trip_date=trip_date_obj, cheaper_than=args.cheaper_than, budget=budget, kind=cand.get("kind", ""),
        )
        if picked_price:
            label, unit, qty = picked_price
            item_spec = f"{label}={unit}x{qty}@{url}" if qty > 1 else f"{label}={unit}@{url}"
            all_pkg_objs.append(make_pkg(
                cand["name"], label, transport, [item_spec], None,
                cand.get("official") or url, phone, phone_source,
            ))
        elif cand.get("free") is True and nights == 0:
            all_pkg_objs.append(make_pkg(
                cand["name"], "免費入園", transport, [f"門票=0@{url}"], None,
                cand.get("official") or url, phone, phone_source,
            ))
        elif cand.get("free") == "unlisted" and nights == 0:
            all_pkg_objs.append(make_pkg(
                cand["name"], "未列收費景點", transport, [], url,
                cand.get("official") or url, phone, phone_source,
            ))
        else:
            if args.cheaper_than is not None:
                skip_reasons.add("其他候選比原方案貴或未符合價格門檻")
            else:
                skip_reasons.add("其他候選頁今天沒找到符合條件的價格")

    if not all_pkg_objs:
        print("地點庫候選頁今天都讀不到符合條件的即時價格；請用 web_search 找新官網並用 travel.py add-place 收錄。", file=sys.stderr)
        return 1

    # Filter out any single package whose cost/link validation fails, keeping up to 3 valid ones
    common_base = dict(
        budget=budget, date=date_iso, checkin=date_iso, checkout=checkout_iso,
        guests=guests, for_clarify=args.for_clarify,
        hermes_root=root, records=None, preferences=prefs_path if prefs_path.is_file() else None,
        history=history_path, official=None, booking=None, booking_label=None,
        phone=None, phone_source=None,
    )
    pkg_objs = []
    seen_identities = []
    for cand_pkg in all_pkg_objs:
        if len(pkg_objs) >= 3:
            break
        ident = format_mod.place_identity(cand_pkg.place, cand_pkg.official, cand_pkg.phone)
        if any(format_mod.same_place(ident, prev) for prev in seen_identities):
            skip_reasons.add("其餘候選為重複地點")
            continue
        probe_args = argparse.Namespace(**{**common_base, **{k: v for k, v in vars(cand_pkg).items() if v not in (None, [])}})
        if cand_pkg.no_fee_listed:
            if format_mod.no_fee_problem(cand_pkg.no_fee_listed, SimpleNamespace(**common_base)):
                skip_reasons.add("其餘候選未通過官網核對")
                continue
        else:
            cost_out, why = format_mod.run_costs(probe_args, [])
            if why or not cost_out or "（全部未確認）" in cost_out.splitlines()[0]:
                skip_reasons.add("其餘候選未通過價格核對")
                continue
        _, link_why = format_mod.run_links(probe_args)
        if link_why:
            skip_reasons.add("其餘候選連結未通過核對")
            continue
        pkg_objs.append(cand_pkg)
        seen_identities.append(ident)

    if not pkg_objs:
        print("地點庫候選頁的即時價格都未通過核對；請用 web_search 找新官網並用 travel.py add-place 收錄。", file=sys.stderr)
        return 1

    # Header & concise user-facing few_reason (no internal jargon, single primary reason)
    date_label = f"{short_date(date_iso)}–{short_date(checkout_iso)}" if checkout_iso else short_date(date_iso)
    area_label = args.area or (effective_kind or "週末出遊")
    header = f"{date_label} {area_label} {guests}人"
    primary_reason = next(iter(sorted(skip_reasons)), "目前能查到官網價的僅此幾家")
    few_reason = f"只有 {len(pkg_objs)} 間符合，因為{primary_reason}" if len(pkg_objs) < 3 else None

    common = SimpleNamespace(header=header, few_reason=few_reason, **common_base)
    message, problems, notes = format_mod.build_packages(common, pkg_objs)
    for note in notes:
        print(note, file=sys.stderr)
    if problems:
        for p in problems:
            print(f"- {p}", file=sys.stderr)
        return 1
    if cal_note:
        phone_check = load_module("phone_check", "phone-check.py")

        def _try_attach_cal(msg_text):
            for cal_line in (f"⚠️ 行事曆：{cal_note}", "⚠️ 行事曆有衝突，請先確認"):
                cand_msg = cal_line + "\n" + msg_text
                cal_problems = phone_check.check(cand_msg)["problems"]
                if args.for_clarify:
                    cal_problems = [p for p in cal_problems if p.get("kind") != "missing_photo"]
                if not cal_problems:
                    return cand_msg
            return None

        with_cal = _try_attach_cal(message)
        if with_cal is None and len(pkg_objs) > 1:
            trimmed_objs = pkg_objs[:-1]
            trimmed_common = SimpleNamespace(
                header=header,
                few_reason=f"行事曆有提醒，先列 {len(trimmed_objs)} 間方便手機閱讀",
                **common_base,
            )
            trimmed_msg, trimmed_probs, _ = format_mod.build_packages(trimmed_common, trimmed_objs)
            if not trimmed_probs:
                with_cal = _try_attach_cal(trimmed_msg)
                if with_cal is not None:
                    pkg_objs = trimmed_objs
        if with_cal is not None:
            message = with_cal
        else:
            print(f"⚠️ 行事曆：{cal_note}", file=sys.stderr)
    if args.json:
        print(json.dumps({"header": header, "packages_count": len(pkg_objs), "message": message}, ensure_ascii=False, indent=2))
        return 0
    print("已通過 phone-check.py 手機排版檢查，可直接原樣送出。", file=sys.stderr)
    print(message)
    return 0


# ---------------------------------------------------------------------------
# Unified CLI dispatch
# ---------------------------------------------------------------------------

PASSTHROUGH = {
    "detail": ("format-plan.py", []),
    "add-place": ("places.py", ["add"]),
    "places": ("places.py", []),
    "photo": ("place-photo.py", []),
    "prices": ("page-prices.py", []),
    "schedule": ("calendar-check.py", []),
    "remind": ("trip-reminders.py", []),
    "check": ("phone-check.py", []),
}


def run_passthrough(command, rest):
    script_name, prefix = PASSTHROUGH[command]
    args_list = list(rest)
    if command == "add-place":
        for i, tok in enumerate(args_list):
            if tok == "--kind" and i + 1 < len(args_list):
                args_list[i + 1] = normalize_kind(args_list[i + 1]) or args_list[i + 1]
            elif tok.startswith("--kind="):
                val = tok.split("=", 1)[1]
                args_list[i] = "--kind=" + (normalize_kind(val) or val)
    proc = subprocess.run([sys.executable, str(HERE / script_name), *prefix, *args_list])
    return proc.returncode


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASSTHROUGH:
        return run_passthrough(argv[0], argv[1:])

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    ov = sub.add_parser("overseas", help="fetch flights + hotels directly and render phone-ready Telegram card")
    ov.add_argument("city", help="destination city, e.g. 福岡, 東京, 大阪")
    ov.add_argument("outbound", help="outbound date YYYY-MM-DD")
    ov.add_argument("ret", help="return date YYYY-MM-DD")
    ov.add_argument("--guests", type=int, help="number of travelers (default from preferences.json or 2)")
    ov.add_argument("--budget", type=int, help="whole-trip budget in NT$ (only pass if owner stated it)")
    ov.add_argument("--top", type=int, default=2, help="number of top hotels to show (default 2)")
    ov.add_argument("--json", action="store_true", help="output structured JSON")
    ov.add_argument("--hermes-root", help=argparse.SUPPRESS)
    ov.add_argument("--raw-output", help=argparse.SUPPRESS)  # deterministic tests

    loc = sub.add_parser("local", help="build 3 verified ready-to-book local packages in one step")
    loc.add_argument("--area", help="destination area, e.g. 礁溪, 烏來, 北投")
    loc.add_argument("--kind", help="category: 溫泉, 住宿, 自然步道、看風景, 海邊, 老街、小吃、夜市, 其他")
    loc.add_argument("--date", help="trip date YYYY-MM-DD (default: upcoming Saturday)")
    loc.add_argument("--nights", type=int, default=0, help="number of nights (0 = day trip)")
    loc.add_argument("--guests", type=int, help="number of travelers (default from preferences.json or 2)")
    loc.add_argument("--budget", type=int, help="whole-trip budget in NT$ (only pass if owner stated it)")
    loc.add_argument("--cheaper-than", type=int, help="only pick options cheaper than this NT$ amount")
    loc.add_argument("--limit", type=int, default=8, help="max candidate pages to check")
    loc.add_argument("--for-clarify", action="store_true", help="format for clarify button question")
    loc.add_argument("--json", action="store_true", help="output structured JSON")
    loc.add_argument("--hermes-root", help=argparse.SUPPRESS)
    loc.add_argument("--places", help=argparse.SUPPRESS)
    loc.add_argument("--history", help=argparse.SUPPRESS)
    loc.add_argument("--preferences", help=argparse.SUPPRESS)
    loc.add_argument("--html-file", help=argparse.SUPPRESS)
    loc.add_argument("--timeout", type=float, default=10, help=argparse.SUPPRESS)

    # Also register passthrough names in --help
    for name, (script, _) in PASSTHROUGH.items():
        sub.add_parser(name, help=f"delegate to {script}")

    args = parser.parse_args(argv)
    if args.command == "overseas":
        return cmd_overseas(args)
    if args.command == "local":
        return cmd_local(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
