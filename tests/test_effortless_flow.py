import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import page_evidence  # noqa: E402


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


booking_links = load_script("booking_links_mod", "booking-links.py")
weather_check = load_script("weather_check_mod", "weather-check.py")
trip_reminders = load_script("trip_reminders_mod", "trip-reminders.py")
weekend_scout = load_script("weekend_scout_mod", "weekend-scout.py")


class BookingDeepLinkAndNavTests(unittest.TestCase):
    def test_prefills_booking_and_agoda_dates_and_guests(self):
        ci, co = datetime.date(2026, 12, 19), datetime.date(2026, 12, 21)
        b_url = booking_links.prefill_booking_url("https://www.booking.com/hotel/tw/demo.html", ci, co, 2)
        self.assertIn("checkin=2026-12-19", b_url)
        self.assertIn("checkout=2026-12-21", b_url)
        self.assertIn("group_adults=2", b_url)

        a_url = booking_links.prefill_booking_url("https://www.agoda.com/demo/hotel/taipei.html", ci, co, 2)
        self.assertIn("checkIn=2026-12-19", a_url)
        self.assertIn("los=2", a_url)
        self.assertIn("adults=2", a_url)

        # Unrelated URLs stay untouched.
        other = "https://www.example.com/rooms"
        self.assertEqual(booking_links.prefill_booking_url(other, ci, co, 2), other)

    def test_builds_multi_stop_navigation_url_and_reservation_script(self):
        nav = booking_links.route_url(["台北車站", "烏來立體停車場", "湯布苑溫泉館"])
        self.assertIn("https://www.google.com/maps/dir/?", nav)
        self.assertIn("origin=", nav)
        self.assertIn("waypoints=", nav)
        self.assertIn("destination=", nav)
        self.assertIn("travelmode=driving", nav)

        script = booking_links.reservation_script(datetime.date(2026, 12, 19), "16:00", 2, "溪景湯屋 1小時")
        self.assertIn("12/19（六） 16:00", script)
        self.assertIn("2位", script)
        self.assertIn("溪景湯屋 1小時", script)

    def test_format_plan_with_route_and_script_passes_phone_check(self):
        root = Path(scratch.new_dir("hermes-effortless-fmt-"))
        page = "https://spring.example/rooms"
        text, headings = page_evidence.read_page("<html><body><p>假日 800元 / 1小時 電話 02-2661-8001</p></body></html>")
        page_evidence.save_snapshot(root, page, text, headings)
        prefs = root / "preferences.json"
        prefs.write_text(json.dumps({"show_unconfirmed": False, "public_transit": False}), encoding="utf-8")
        records = root / "opportunities.json"
        records.write_text("[]", encoding="utf-8")
        res = subprocess.run([
            sys.executable, str(REPO / "scripts/format-plan.py"),
            "--header", "12/19 烏來泡湯 2人",
            "--budget", "2000",
            "--date", "2026-12-19",
            "--item", f"溪景湯屋 1小時=800x1@{page}",
            "--place", "範例溫泉館",
            "--official", page,
            "--route", "台北車站", "範例溫泉館",
            "--guests", "2",
            "--reservation-script", "--time", "16:00", "--script-item", "溪景湯屋 1小時",
            "--phone", "02-2661-8001", "--phone-source", page,
            "--hermes-root", str(root), "--records", str(records), "--preferences", str(prefs),
        ], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("一鍵多點導航", res.stdout)
        self.assertIn("💬 預約", res.stdout)


class WeatherCheckTests(unittest.TestCase):
    def test_sunny_rainy_and_out_of_range(self):
        today = datetime.date(2026, 11, 23)
        sunny = weather_check.check_weather(
            "礁溪", datetime.date(2026, 11, 28), today=today,
            fetcher=lambda lat, lon, d: {"daily": {"temperature_2m_min": [17.2], "temperature_2m_max": [22.8],
                                                   "precipitation_probability_max": [15]}},
        )
        self.assertEqual(sunny["status"], "ok")
        self.assertFalse(sunny["rainy"])
        self.assertIn("適合出遊", sunny["summary"])

        rainy = weather_check.check_weather(
            "烏來", datetime.date(2026, 11, 28), today=today,
            fetcher=lambda lat, lon, d: {"daily": {"temperature_2m_min": [15.0], "temperature_2m_max": [18.4],
                                                   "precipitation_probability_max": [75]}},
        )
        self.assertTrue(rainy["rainy"])
        self.assertIn("室內雨備", rainy["summary"])

        far = weather_check.check_weather("礁溪", datetime.date(2026, 12, 25), today=today)
        self.assertEqual(far["status"], "out_of_range")
        self.assertIn("未確認", far["summary"])


class TripRemindersTests(unittest.TestCase):
    def test_builds_three_future_reminders_and_skips_past_ones(self):
        tz = trip_reminders.TZ
        now = datetime.datetime(2026, 12, 10, 12, 0, tzinfo=tz)
        items = trip_reminders.build_reminders(
            "烏來湯布苑泡湯", datetime.date(2026, 12, 19), depart_time="15:00",
            place="湯布苑溫泉館", route=["台北車站", "湯布苑溫泉館"],
            free_cancel_until=datetime.date(2026, 12, 17), packing="毛巾、泳衣", booking_code="BK-99", now=now,
        )
        self.assertEqual([i["kind"] for i in items], ["free_cancel", "eve", "depart"])
        self.assertEqual(items[0]["cron"], "0 9 16 12 *")
        self.assertEqual(items[1]["cron"], "0 20 18 12 *")
        self.assertEqual(items[2]["cron"], "0 14 19 12 *")
        self.assertIn("BK-99", items[1]["prompt"])
        self.assertIn("https://www.google.com/maps/dir/", items[2]["prompt"])

        # When now is already the morning of departure, only the 1h-before reminder remains.
        same_day = datetime.datetime(2026, 12, 19, 10, 0, tzinfo=tz)
        later = trip_reminders.build_reminders(
            "烏來湯布苑泡湯", datetime.date(2026, 12, 19), depart_time="15:00",
            free_cancel_until=datetime.date(2026, 12, 17), now=same_day,
        )
        self.assertEqual([i["kind"] for i in later], ["depart"])


class WeekendScoutTests(unittest.TestCase):
    def test_finds_free_weekend_days_and_reports_busy_when_both_taken(self):
        root = Path(scratch.new_dir("hermes-weekend-scout-"))
        cal = root / "shared/annual-schedule/calendar.json"
        cal.parent.mkdir(parents=True)
        prefs = root / "shared/travel-workflow/preferences.json"
        prefs.parent.mkdir(parents=True)
        prefs.write_text(json.dumps({"budget": {"amount": 2000, "currency": "TWD"}, "public_transit": False}),
                         encoding="utf-8")

        # Thursday 2026-11-26 -> upcoming weekend is Sat 2026-11-28 & Sun 2026-11-29.
        cal.write_text(json.dumps({"events": [
            {"id": "e1", "status": "confirmed", "start": "2026-11-28T10:00:00+08:00", "end": "2026-11-28T16:00:00+08:00"},
            {"id": "e2", "status": "cancelled", "start": "2026-11-29T10:00:00+08:00"},
        ]}), encoding="utf-8")
        res = weekend_scout.evaluate(
            root, today=datetime.date(2026, 11, 26),
            weather_checker=lambda place, d: {"summary": f"🌤 {place} 晴天"},
        )
        self.assertEqual(res["status"], "ready")
        self.assertEqual(res["free_days"], ["2026-11-29"])
        self.assertIn("礁溪", res["weather"]["2026-11-29"])

        # Both days busy -> status=busy.
        cal.write_text(json.dumps({"events": [
            {"id": "e1", "status": "confirmed", "start": "2026-11-28T10:00:00+08:00"},
            {"id": "e2", "status": "confirmed", "start": "2026-11-29T10:00:00+08:00"},
        ]}), encoding="utf-8")
        res_busy = weekend_scout.evaluate(root, today=datetime.date(2026, 11, 26), include_weather=False)
        self.assertEqual(res_busy["status"], "busy")
        self.assertEqual(res_busy["free_days"], [])


if __name__ == "__main__":
    unittest.main()
