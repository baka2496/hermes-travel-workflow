"""Print tap-to-open booking links for one place, ready to paste into a Telegram reply.

Example (a made-up place; never copy it into a reply):
  booking-links.py 範例溫泉旅館 --official https://www.example.com/rooms \
      --phone 03-0000000 --phone-source https://www.example.com/contact

A phone number is printed only with --phone-source, the page you opened that shows it
(2026-11-14: replies carried numbers that no page in that turn had shown). The number must be
found on that page as opened in the last few hours or as recorded in an opportunity record;
otherwise nothing is printed and you should leave --phone out.

Google Maps always: it opens the place itself and offers "查詢空房" with dates. A booking-site
link (--booking) and the official page (--official) are added only as exact pages you opened and
confirmed are this place. Search-result links are not used: a site that does not list the place
silently shows a similarly named one instead. Nothing is booked; links only open pages.
"""
import argparse
import datetime
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import page_evidence


def https_url(text):
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.netloc:
        raise argparse.ArgumentTypeError(f"links must be https URLs, got {text!r}")
    if parts.path.rstrip("/").endswith(("searchresults.html", "searchresults.zh-tw.html", "/search")):
        raise argparse.ArgumentTypeError("give the place's own page, not a search-results page")
    return text


def iso_date(text):
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got {text!r}")


def prefill_booking_url(url, checkin=None, checkout=None, guests=None):
    """Add check-in/out dates and guest count to Booking.com or Agoda property URLs."""
    if not (checkin or checkout or guests):
        return url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "booking.com" in host:
        if checkin:
            params["checkin"] = checkin.isoformat()
        if checkout:
            params["checkout"] = checkout.isoformat()
        if guests:
            params["group_adults"] = str(guests)
            params.setdefault("no_rooms", "1")
    elif "agoda.com" in host:
        if checkin:
            params["checkIn"] = checkin.isoformat()
        if checkin and checkout and checkout > checkin:
            params["los"] = str((checkout - checkin).days)
        if guests:
            params["adults"] = str(guests)
            params.setdefault("rooms", "1")
    else:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def route_url(stops, mode="driving"):
    """Build a one-tap Google Maps multi-stop directions URL."""
    cleaned = [s.strip() for s in stops if s and s.strip()]
    if not cleaned:
        return None
    params = {"api": 1, "destination": cleaned[-1], "travelmode": mode}
    if len(cleaned) >= 2:
        params["origin"] = cleaned[0]
    if len(cleaned) >= 3:
        params["waypoints"] = "|".join(cleaned[1:-1])
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def reservation_script(date=None, time_text=None, guests=None, item=None):
    """Short copy-paste reservation message for phone / LINE booking."""
    when = ""
    if date:
        weekday = "一二三四五六日"[date.weekday()]
        when = f"{date.month}/{date.day}（{weekday}）"
    if time_text:
        when = f"{when} {time_text}".strip()
    party = f"{guests}位" if guests else ""
    what = (item or "雙人房/湯屋").strip()
    details = " ".join(p for p in (when, party, what) if p)
    return f"💬 預約：「想訂 {details}，請問有位嗎？需自備毛巾嗎？」"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="place name as it appears on its official site or Google Maps")
    parser.add_argument("--official", type=https_url, help="official booking or room page you confirmed")
    parser.add_argument("--booking", type=https_url, help="this place's own page on a booking site you confirmed")
    parser.add_argument("--booking-label", default="訂房網站", help="link text for --booking, e.g. Agoda")
    parser.add_argument("--checkin", type=iso_date, help="prefill check-in date YYYY-MM-DD on booking link")
    parser.add_argument("--checkout", type=iso_date, help="prefill check-out date YYYY-MM-DD on booking link")
    parser.add_argument("--guests", type=int, help="prefill adult guest count on booking link / script")
    parser.add_argument("--route", nargs="+", metavar="STOP",
                        help="stops in order (origin [waypoints...] destination) for one-tap Google Maps navigation")
    parser.add_argument("--travel-mode", default="driving", choices=("driving", "two-wheeler", "walking", "transit"),
                        help="travel mode for --route (default: driving)")
    parser.add_argument("--reservation-script", action="store_true",
                        help="append a copy-paste reservation script for phone or LINE")
    parser.add_argument("--time", dest="time_text", help="arrival time for --reservation-script (e.g. 16:00)")
    parser.add_argument("--item", dest="item_text", help="room/bath name for --reservation-script")
    parser.add_argument("--phone")
    parser.add_argument("--phone-source", type=https_url, help="page you opened that shows the phone number")
    parser.add_argument("--records", default=page_evidence.RECORDS, help=argparse.SUPPRESS)  # tests only
    parser.add_argument("--hermes-root", default=page_evidence.HERMES_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.phone and not args.phone_source:
        parser.error("--phone needs --phone-source: the page you opened that shows this number; "
                     "leave the phone out if no page showed it")
    pattern = page_evidence.phone_pattern(args.phone) if args.phone else None
    if args.phone and not (pattern and page_evidence.shown(args.phone_source, pattern, args.records, args.hermes_root)):
        parser.error(f"{args.phone} is not on {args.phone_source} as opened or recorded; "
                     "leave --phone out, or open the page that shows the number and run again")

    lines = [f"📍 [Google 地圖・查空房](https://www.google.com/maps/search/?{urlencode({'api': 1, 'query': args.name})})"]
    if args.route:
        nav = route_url(args.route, mode=args.travel_mode)
        if nav:
            lines.append(f"🧭 [一鍵多點導航]({nav})")
    if args.official:
        lines.append(f"🏨 [官網訂房]({args.official})")
    if args.booking:
        booking_url = prefill_booking_url(args.booking, args.checkin, args.checkout, args.guests)
        lines.append(f"🛏 [{args.booking_label}]({booking_url})")
    if args.phone:
        lines.append(f"📞 {args.phone}")
    if args.reservation_script:
        lines.append(reservation_script(args.checkin, args.time_text, args.guests, args.item_text))
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
