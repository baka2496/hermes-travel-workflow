"""Check upcoming weekend availability and weather for Thursday's effortless weekend proposal.

Reads calendar.json and preferences.json under HERMES_HOME, checks whether Saturday or Sunday
is free of confirmed events, and optionally fetches weather for nearby weekend spots.
Output is stable JSON (no wall-clock timestamps) so it can serve as a Hermes cron monitor script.

Usage:
  weekend-scout.py [--root HERMES_HOME] [--today YYYY-MM-DD] [--no-weather]
"""
import argparse
import datetime
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent if HERE.name == "travel-workflow" else HERE.parents[2]
WEEKEND_SPOTS = ("礁溪", "烏來", "台北")


def upcoming_weekend(today):
    """Return (saturday, sunday) for the upcoming weekend from `today`."""
    # Monday=0 .. Saturday=5, Sunday=6. On Sunday, look at next weekend.
    days_to_sat = (5 - today.weekday()) % 7
    if today.weekday() == 6:
        days_to_sat = 6
    sat = today + datetime.timedelta(days=days_to_sat)
    return sat, sat + datetime.timedelta(days=1)


def busy_dates(calendar_path, dates):
    """Set of ISO date strings among `dates` that have a non-cancelled, blocking event."""
    if not calendar_path.is_file():
        return set()
    try:
        data = json.loads(calendar_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    events = data.get("events", []) if isinstance(data, dict) else []
    target_set = {d.isoformat() for d in dates}
    busy = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        status = str(ev.get("status") or "").lower()
        if status in ("cancelled", "canceled", "proposed"):
            continue
        start = str(ev.get("start") or ev.get("date") or "")[:10]
        end = str(ev.get("end") or start)[:10]
        for day_str in target_set:
            if start and end and start <= day_str <= end:
                busy.add(day_str)
    return busy


def load_weather_module():
    for candidate in (HERE / "weather-check.py", Path(__file__).resolve().parent / "weather-check.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("weather_check", candidate)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def evaluate(root, *, today=None, include_weather=True, weather_checker=None):
    root = Path(root)
    now = today or datetime.date.today()
    sat, sun = upcoming_weekend(now)
    cal_path = root / "shared/annual-schedule/calendar.json"
    prefs_path = root / "shared/travel-workflow/preferences.json"

    busy = busy_dates(cal_path, (sat, sun))
    free_days = [d.isoformat() for d in (sat, sun) if d.isoformat() not in busy]

    prefs = {}
    if prefs_path.is_file():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            prefs = {}

    if not free_days:
        return {
            "status": "busy",
            "weekend": [sat.isoformat(), sun.isoformat()],
            "free_days": [],
        }

    weather = {}
    if include_weather:
        checker = weather_checker
        if checker is None:
            mod = load_weather_module()
            checker = (lambda place, d: mod.check_weather(place, d, today=now)) if mod else None
        if checker:
            for day_str in free_days:
                d = datetime.date.fromisoformat(day_str)
                weather[day_str] = {spot: checker(spot, d).get("summary", "天氣：未確認") for spot in WEEKEND_SPOTS}

    return {
        "status": "ready",
        "weekend": [sat.isoformat(), sun.isoformat()],
        "free_days": free_days,
        "budget": prefs.get("budget"),
        "transport_mode": prefs.get("transport_mode"),
        "public_transit": prefs.get("public_transit"),
        "weather": weather,
    }


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Hermes root directory")
    parser.add_argument("--today", type=datetime.date.fromisoformat, help="override today YYYY-MM-DD (tests)")
    parser.add_argument("--no-weather", action="store_true", help="skip Open-Meteo weather lookup")
    args = parser.parse_args()
    result = evaluate(args.root, today=args.today, include_weather=not args.no_weather)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
