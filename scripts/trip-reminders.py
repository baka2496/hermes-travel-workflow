"""Schedule automatic one-shot pre-trip reminders after a trip is booked/confirmed.

Creates up to three one-shot Hermes cron jobs (skipping any time already in the past):
  1. 24h before free-cancellation deadline (09:00): ask if still going or want to cancel free.
  2. Evening before departure (20:00): short packing list, booking code, and weather check.
  3. 1 hour before departure: one-tap Google Maps navigation link to head out.

Example (made-up trip):
  trip-reminders.py --title "烏來湯布苑泡湯" --date 2026-12-19 --depart-time 15:00 \\
      --place "湯布苑溫泉館" --route "台北車站" "湯布苑溫泉館" \\
      --free-cancel-until 2026-12-17 --packing "毛巾、泳衣" --booking-code "BK-12345" --dry-run
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode

TZ = datetime.timezone(datetime.timedelta(hours=8))


def nav_link(place, route):
    stops = [s.strip() for s in (route or []) if s and s.strip()]
    if stops:
        params = {"api": 1, "destination": stops[-1], "travelmode": "driving"}
        if len(stops) >= 2:
            params["origin"] = stops[0]
        if len(stops) >= 3:
            params["waypoints"] = "|".join(stops[1:-1])
        return "https://www.google.com/maps/dir/?" + urlencode(params)
    return "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "destination": place, "travelmode": "driving"})


def cron_expr(dt):
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"


def build_reminders(title, date, *, depart_time="09:00", place="", route=None,
                    free_cancel_until=None, packing="", booking_code="", now=None):
    """Return list of {"kind", "name", "at", "cron", "prompt"} for future reminders."""
    current = now or datetime.datetime.now(TZ)
    hour, minute = (int(x) for x in depart_time.split(":", 1))
    depart_dt = datetime.datetime(date.year, date.month, date.day, hour, minute, tzinfo=TZ)
    slug = f"{date.isoformat()}-{place or title}"[:36]
    items = []

    if free_cancel_until:
        cancel_dt = datetime.datetime(free_cancel_until.year, free_cancel_until.month, free_cancel_until.day,
                                      9, 0, tzinfo=TZ) - datetime.timedelta(days=1)
        if cancel_dt > current:
            code_note = f"（訂位代號：{booking_code}）" if booking_code else ""
            prompt = (f"傳一則簡短提醒給使用者：{title}{code_note} 免費取消期限是 "
                      f"{free_cancel_until.month}/{free_cancel_until.day}，確認是否照原計畫出發。")
            items.append({"kind": "free_cancel", "name": f"trip-reminder/cancel/{slug}",
                          "at": cancel_dt.isoformat(), "cron": cron_expr(cancel_dt), "prompt": prompt})

    eve_dt = datetime.datetime(date.year, date.month, date.day, 20, 0, tzinfo=TZ) - datetime.timedelta(days=1)
    if eve_dt > current:
        parts = [f"傳一則 5 行內的行前提醒給使用者：明天 {date.month}/{date.day} {depart_time} 出發「{title}」。"]
        if place:
            parts.append(f"先跑 weather-check.py {place} --date {date.isoformat()} 附上明天氣溫與降雨建議。")
        if packing:
            parts.append(f"必帶物品：{packing}。")
        if booking_code:
            parts.append(f"訂位代號：{booking_code}。")
        items.append({"kind": "eve", "name": f"trip-reminder/eve/{slug}",
                      "at": eve_dt.isoformat(), "cron": cron_expr(eve_dt), "prompt": " ".join(parts)})

    go_dt = depart_dt - datetime.timedelta(hours=1)
    if go_dt > current:
        link = nav_link(place or title, route)
        code_note = f"訂位代號：{booking_code}。" if booking_code else ""
        prompt = (f"傳一則簡短出發提醒給使用者：1 小時後（{depart_time}）出發「{title}」。{code_note}"
                  f"附上導航連結：🧭 [一鍵開始導航]({link})")
        items.append({"kind": "depart", "name": f"trip-reminder/go/{slug}",
                      "at": go_dt.isoformat(), "cron": cron_expr(go_dt), "prompt": prompt})

    return items


def calendar_event(title, date, depart_time="09:00", place="", booking_code="", free_cancel_until=None):
    return {
        "id": f"trip-{date.isoformat()}-{(place or title)[:16]}",
        "title": title,
        "type": "travel",
        "start": f"{date.isoformat()}T{depart_time}:00+08:00",
        "timezone": "Asia/Taipei",
        "status": "confirmed",
        "location": place or title,
        "booking_code": booking_code or None,
        "free_cancel_until": free_cancel_until.isoformat() if free_cancel_until else None,
        "source": "user_confirmed_booking",
    }


def schedule_reminder(reminder, *, profile, deliver, hermes_root=None):
    env = os.environ.copy()
    if hermes_root:
        env["HERMES_HOME"] = str(hermes_root)
    args = ["hermes", "-p", profile, "cron", "create", reminder["cron"], reminder["prompt"],
            "--name", reminder["name"], "--repeat", "1", "--deliver", deliver,
            "--timezone", "Asia/Taipei", "--no-wrap-response"]
    result = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Failed to schedule {reminder['name']}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", required=True, help="trip title")
    parser.add_argument("--date", required=True, type=datetime.date.fromisoformat, help="departure date YYYY-MM-DD")
    parser.add_argument("--depart-time", default="09:00", help="departure time HH:MM (default: 09:00)")
    parser.add_argument("--place", default="", help="destination name")
    parser.add_argument("--route", nargs="+", metavar="STOP", help="stops for Google Maps navigation")
    parser.add_argument("--free-cancel-until", type=datetime.date.fromisoformat, help="free cancellation deadline YYYY-MM-DD")
    parser.add_argument("--packing", default="", help="required items to pack")
    parser.add_argument("--booking-code", default="", help="booking confirmation code")
    parser.add_argument("--profile", default="travel-commander", help="Hermes profile to create cron jobs under")
    parser.add_argument("--deliver", default="telegram", help="cron delivery target (default: telegram)")
    parser.add_argument("--hermes-root", type=Path, help="explicit HERMES_HOME")
    parser.add_argument("--calendar-json", action="store_true", help="also print a calendar event JSON block")
    parser.add_argument("--dry-run", action="store_true", help="preview reminders without calling hermes")
    args = parser.parse_args()

    reminders = build_reminders(
        args.title, args.date, depart_time=args.depart_time, place=args.place, route=args.route,
        free_cancel_until=args.free_cancel_until, packing=args.packing, booking_code=args.booking_code,
    )
    if not reminders:
        print("沒有尚未到期的提醒需要排程。")
        return 0
    for item in reminders:
        if args.dry_run:
            print(f"[dry-run] {item['kind']} @ {item['at']} ({item['cron']}) -> {item['name']}")
        else:
            schedule_reminder(item, profile=args.profile, deliver=args.deliver, hermes_root=args.hermes_root)
            print(f"已排程 {item['kind']} @ {item['at']} -> {item['name']}")
    if args.calendar_json:
        event = calendar_event(args.title, args.date, args.depart_time, args.place, args.booking_code, args.free_cancel_until)
        print(json.dumps(event, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
