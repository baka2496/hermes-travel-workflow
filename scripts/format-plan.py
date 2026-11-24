"""Assemble a verified, phone-ready Telegram travel plan from structured inputs.

Instead of asking the model to hand-stitch outputs from trip-cost.py, booking-links.py,
place-photo.py and phone-check.py across multiple turns, this tool runs them in one step,
keeps every verified figure and link verbatim, and validates phone readability before printing.

Example (made-up place; never copy into a reply):
  format-plan.py --header "12/14 礁溪泡湯 2人" --budget 2000 --date 2026-12-14 \\
      --item "範例湯屋 60分鐘=699x1@https://www.example.com/hotspring.html" \\
      --place "範例溫泉" --official https://www.example.com/hotspring.html \\
      --phone 03-0000000 --phone-source https://www.example.com/hotspring.html \\
      --note "開車約 50 分鐘"
"""
import argparse
import importlib.util
from pathlib import Path
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


def build_plan(args):
    """Build the plan text and validate it. Returns (text, problems, stderr_notes)."""
    notes = []
    sections = [args.header.strip()]

    # 1. Verified costs via trip-cost.py
    if args.item:
        if args.budget is None:
            return None, ["有提供 --item 時必須同時指定 --budget"], notes
        cost_args = ["--budget", str(args.budget)]
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
            return None, [err or f"trip-cost.py 失敗（exit {rc}）"], notes
        if out and not out.startswith("沒有確認過的價格"):
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
            return None, [err or f"booking-links.py 失敗（exit {rc}）"], notes
        if out:
            sections.append(out)
        if not args.phone:
            sections.append("電話：未取得")

    message = "\n".join(sections)

    # 5. Phone readability & photo/phone completeness check
    phone_check = load_module("phone_check", "phone-check.py")
    report = phone_check.check(message)
    problems = report["problems"]
    if args.for_clarify:
        # In clarify questions MEDIA is intentionally omitted; ignore missing_photo only in this mode.
        problems = [p for p in problems if p.get("kind") != "missing_photo"]
    if problems:
        formatted = [f"{p['kind']}: {p.get('text', '')} {p.get('hint', '')}".strip() for p in problems]
        return message, formatted, notes
    return message, [], notes


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--header", required=True, help="first line: date, destination, party size")
    parser.add_argument("--budget", type=int, help="whole-trip budget in NT$")
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
    parser.add_argument("--records", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-root", help=argparse.SUPPRESS)
    parser.add_argument("--preferences", help=argparse.SUPPRESS)
    args = parser.parse_args()

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
