"""Behavior tests for the V2 unified travel engine (scripts/travel.py) and setup.py sync-live."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import scratch
import setup

REPO = Path(__file__).resolve().parents[1]
TRAVEL = REPO / "scripts" / "travel.py"


def load_travel():
    spec = importlib.util.spec_from_file_location("travel_cli", TRAVEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE_FULL_SERVICE = """🏨 *福岡 住宿*
06/05(六)–06/09(三)・4 晚
📊 機票 Google ➖ 一般
✈️ [去 華航 16:40→20:00](https://www.google.com/travel/flights/booking?tfs=abc)
飛 2h20・約 21:15 到飯店
回 華航 11:00→12:30
飛 2h30・08:30 就要離開飯店
來回每人 NT$13,754
去回同一張票・去程連結一次訂

🏷 住宿：都是一般價

1. [MIYUKIHOUSE 1番館](https://www.google.com/travel/search?q=miyuki)
⭐4.5・❔ 房型未確認・📍Kasuga
每晚含稅 NT$1,984
在 [Ikyu](https://www.google.com/travel/search?q=miyuki-ikyu) 訂
4晚 NT$7,936
2人含機票 NT$35,444
每人 NT$17,722

2. [Fukuoka UBL Hotel](https://www.google.com/travel/search?q=ubl)
⭐4.3・雙人房・📍福岡中央區
每晚含稅 NT$1,990
在 [Hotels](https://www.google.com/travel/search?q=ubl-hotels) 訂
4晚 NT$7,960
2人含機票 NT$35,468
每人 NT$17,734

3. [Hotel Hakata Place](https://www.google.com/travel/search?q=hakata)
⭐4.1・雙人房・📍福岡博多區
每晚含稅 NT$1,996
在 [Booking](https://www.google.com/travel/search?q=hakata-booking) 訂
4晚 NT$7,984
2人含機票 NT$35,492
每人 NT$17,746

[看全部住宿](https://www.google.com/travel/search?q=all-hotels)
[在 Google Flights 看全部航班](https://www.google.com/travel/flights?q=all-flights)
"""


SAMPLE_LCC_SEPARATE = """🏨 *大阪 住宿*
05/15(六)–05/19(三)・4 晚
✈️ [去 台灣虎航 06:40→10:15](https://www.tigerairtw.com/out)
[回 台灣虎航 11:20→13:15](https://www.tigerairtw.com/back)
來回每人 NT$9,007
＋托運行李約 NT$1,800（估算）
分開訂兩張票・省 NT$500

1. [FAWN HOTEL](https://www.google.com/travel/search?q=fawn)
⭐4.2・雙人房・📍大阪浪速區
每晚含稅 NT$1,421
在 [Booking](https://www.booking.com/hotel/jp/fawn) 訂
4晚 NT$5,684
2人含機票 NT$27,298
每人 NT$13,649
"""


SAMPLE_NO_BOOKING_PAGE = """🏨 *東京 住宿*
07/10(六)–07/14(三)・4 晚
✈️ [東京 來回每人 NT$12,000](https://www.google.com/travel/flights?q=tokyo)
這次沒讀到訂票頁

1. [APA Hotel Shinjuku](https://www.google.com/travel/search?q=apa)
⭐4.0・雙人房・📍新宿
每晚含稅 NT$2,500
在 [Booking](https://www.booking.com/hotel/jp/apa) 訂
4晚 NT$10,000
2人含機票 NT$34,000
每人 NT$17,000
"""


class OverseasTravelTests(unittest.TestCase):
    def run_overseas(self, raw_text, *extra_args, city="福岡", out="2027-06-05", ret="2027-06-09"):
        d = Path(scratch.new_dir("travel-overseas"))
        raw = d / "raw.txt"
        raw.write_text(raw_text, encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(TRAVEL), "overseas", city, out, ret,
             "--raw-output", str(raw), "--hermes-root", str(d), *extra_args],
            capture_output=True, text=True, encoding="utf-8",
        )
        return res.returncode, res.stdout, res.stderr

    def test_full_service_prefers_confirmed_rooms_and_includes_times_and_links(self):
        rc, out, err = self.run_overseas(SAMPLE_FULL_SERVICE, "--guests", "2", "--budget", "45000")
        self.assertEqual(rc, 0, err)
        self.assertIn("已通過 phone-check.py", err)
        self.assertIn("總計 NT$35,468（預算剩 NT$9,532）", out)
        self.assertIn("華航 2人 NT$27,508（已含托運行李）", out)
        self.assertIn("16:40→20:00", out)
        self.assertIn("11:00→12:30", out)
        self.assertIn("[機票訂票連結](https://www.google.com/travel/flights/booking?tfs=abc)", out)
        self.assertIn("Fukuoka UBL Hotel（雙人房）", out)
        self.assertIn("[Hotels](https://www.google.com/travel/search?q=ubl-hotels)", out)
        self.assertIn("Hotel Hakata Place（雙人房）", out)
        self.assertIn("[Booking](https://www.google.com/travel/search?q=hakata-booking)", out)
        # MIYUKIHOUSE (room unconfirmed) should NOT beat confirmed hotels for top 2
        self.assertNotIn("MIYUKIHOUSE", out)

    def test_no_budget_passed_omits_budget_wording(self):
        rc, out, err = self.run_overseas(SAMPLE_FULL_SERVICE, "--guests", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("總計 NT$35,468", out)
        self.assertNotIn("預算", out)

    def test_over_budget_states_exact_overage(self):
        rc, out, err = self.run_overseas(SAMPLE_FULL_SERVICE, "--guests", "2", "--budget", "30000")
        self.assertEqual(rc, 0, err)
        self.assertIn("超出預算 NT$5,468", out)

    def test_lcc_separate_tickets_links_both_legs_and_marks_baggage_estimated(self):
        rc, out, err = self.run_overseas(
            SAMPLE_LCC_SEPARATE, "--guests", "2", "--budget", "45000",
            city="大阪", out="2027-05-15", ret="2027-05-19",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("含估算托運行李", out)
        self.assertIn("[去程機票](https://www.tigerairtw.com/out)", out)
        self.assertIn("[回程機票](https://www.tigerairtw.com/back)", out)
        self.assertIn("FAWN HOTEL（雙人房）", out)

    def test_no_booking_page_marks_baggage_excluded(self):
        rc, out, err = self.run_overseas(
            SAMPLE_NO_BOOKING_PAGE, "--guests", "2",
            city="東京", out="2027-07-10", ret="2027-07-14",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("未含托運行李", out)
        self.assertIn("[看航班](https://www.google.com/travel/flights?q=tokyo)", out)

    def test_rate_limit_and_no_flights_report_unconfirmed_honestly(self):
        rc, out, _ = self.run_overseas("Google 暫時限流：每個查詢都回 Oops")
        self.assertEqual(rc, 0)
        self.assertIn("未確認", out)
        self.assertIn("限流", out)
        self.assertNotIn("開賣", out)

        rc, out, _ = self.run_overseas("這次沒讀到航班，可以稍後再試")
        self.assertEqual(rc, 0)
        self.assertIn("未確認", out)
        self.assertNotIn("開賣", out)

    def test_different_airlines_unknown_room_and_no_stay_total_estimation(self):
        raw = """🏨 *大阪 住宿*
05/15(六)–05/19(三)・4 晚
✈️ [去 台灣虎航 06:40→10:15](https://www.tigerairtw.com/out)
[回 樂桃 11:20→13:15](https://www.flypeach.com/back)
來回每人 NT$9,000
＋托運行李約 NT$1,800（估算）

1. [Mystery Hotel](https://www.google.com/travel/search?q=mystery)
⭐4.2・📍大阪
每晚含稅 NT$2,000
在 [Booking](https://www.booking.com/hotel/jp/mystery) 訂
"""
        rc, out, err = self.run_overseas(raw, "--guests", "2", city="大阪", out="2027-05-15", ret="2027-05-19")
        self.assertEqual(rc, 0, err)
        self.assertIn("台灣虎航／樂桃", out)
        self.assertIn("去 05/15 台灣虎航 06:40→10:15", out)
        self.assertIn("回 05/19 樂桃 11:20→13:15", out)
        self.assertIn("Mystery Hotel（房型未確認）", out)
        # Must NOT fabricate 4晚 NT$8,000 when scanner only printed nightly rate
        self.assertNotIn("NT$8,000", out)
        self.assertIn("每晚 NT$2,000", out)

    def test_show_unconfirmed_false_drops_unconfirmed_hotels_and_ignores_non_twd_budget(self):
        d = Path(scratch.new_dir("travel-overseas-unconf"))
        shared = d / "shared" / "travel-workflow"
        shared.mkdir(parents=True)
        (shared / "preferences.json").write_text(json.dumps({
            "show_unconfirmed": False,
            "budget": {"amount": 50000, "currency": "JPY", "scope": "entire_trip", "quote": "5萬日圓"},
        }, ensure_ascii=False), encoding="utf-8")
        raw = d / "raw.txt"
        raw.write_text(SAMPLE_FULL_SERVICE, encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(TRAVEL), "overseas", "福岡", "2027-06-05", "2027-06-09",
             "--guests", "2", "--raw-output", str(raw), "--hermes-root", str(d)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("MIYUKIHOUSE", res.stdout)
        self.assertNotIn("預算", res.stdout)  # JPY budget must not be treated as TWD

    def test_four_guests_scaling_for_local_prices(self):
        travel = load_travel()
        # Per-person ticket scales with 4 guests
        label, unit, qty = travel.select_best_price_line(["全票：200元 / 每人"], nights=0, guests=4)
        self.assertEqual((unit, qty), (200, 4))
        # Overnight 2 nights for 4 guests needs 2 rooms x 2 nights = 4 room-nights
        label, unit, qty = travel.select_best_price_line(["標準雙人房：3000元"], nights=2, guests=4)
        self.assertEqual((unit, qty), (3000, 4))


class LocalTravelTests(unittest.TestCase):
    def test_local_builds_three_packages_with_owner_transport_and_no_disqualified(self):
        d = Path(scratch.new_dir("travel-local"))
        shared = d / "shared" / "travel-workflow"
        shared.mkdir(parents=True)
        prefs = {
            "origin": "新北市新莊區（輔大一帶）",
            "public_transit": False,
            "traveler_count": 2,
            "show_unconfirmed": False,
            "interests": ["溫泉", "自然步道、看風景"],
            "hot_spring_stay": {"disqualified": ["三光礁溪溫泉旅館（無房內浴池）"]},
        }
        (shared / "preferences.json").write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
        html = d / "sample.html"
        html.write_text(
            "<html><head><title>範例溫泉</title></head><body>"
            "<h1>雙人湯屋</h1><p>雙人湯屋 60分鐘 NT$600</p>"
            "</body></html>",
            encoding="utf-8",
        )
        places = {
            "schema_version": 1,
            "places": [
                {"name": "甲溫泉", "area": "礁溪", "kind": "溫泉",
                 "official": "https://a.example/bath", "price_page": "https://a.example/bath",
                 "phone": "", "prices": ["雙人湯屋 NT$600"], "free": False,
                 "checked_at": "2026-11-27", "status": "ok"},
                {"name": "乙溫泉", "area": "礁溪", "kind": "溫泉",
                 "official": "https://b.example/bath", "price_page": "https://b.example/bath",
                 "phone": "", "prices": ["雙人湯屋 NT$600"], "free": False,
                 "checked_at": "2026-11-27", "status": "ok"},
                {"name": "丙溫泉", "area": "礁溪", "kind": "溫泉",
                 "official": "https://c.example/bath", "price_page": "https://c.example/bath",
                 "phone": "", "prices": ["雙人湯屋 NT$600"], "free": False,
                 "checked_at": "2026-11-27", "status": "ok"},
            ],
        }
        (shared / "places.json").write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
        (shared / "recommended.json").write_text("[]", encoding="utf-8")

        res = subprocess.run(
            [sys.executable, str(TRAVEL), "local", "--area", "礁溪", "--kind", "泡湯",
             "--date", "2026-12-05", "--budget", "2000",
             "--hermes-root", str(d), "--html-file", str(html)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("已通過 phone-check.py", res.stderr)
        for name in ("甲溫泉", "乙溫泉", "丙溫泉"):
            self.assertIn(name, res.stdout)
        self.assertIn("① ", res.stdout)
        self.assertIn("② ", res.stdout)
        self.assertIn("③ ", res.stdout)
        self.assertIn("開車／騎車前往礁溪", res.stdout)
        self.assertNotIn("大眾運輸", res.stdout)
        self.assertIn("總計 NT$600，預算剩 NT$1,400", res.stdout)

    def test_profile_hermes_home_resolves_shared_root(self):
        travel = load_travel()
        import os
        old = os.environ.get("HERMES_HOME")
        try:
            os.environ["HERMES_HOME"] = r"C:\fake\hermes\profiles\project-manager"
            self.assertEqual(travel.default_hermes_root(), Path(r"C:\fake\hermes"))
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


class SyncLiveTests(unittest.TestCase):
    def test_sync_live_deploys_only_existing_plugins_backs_up_and_keeps_state(self):
        root = Path(scratch.new_dir("sync-live"))
        for prof in ("project-manager", "trip", "us-market"):
            (root / "profiles" / prof).mkdir(parents=True)
        # Only project-manager already has travel-reply-check installed
        pm_plugin = root / "profiles/project-manager/plugins/travel-reply-check"
        pm_plugin.mkdir(parents=True)
        (pm_plugin / "__init__.py").write_text("# old plugin\n", encoding="utf-8")
        (root / "profiles/trip/SOUL.head.md").write_text(
            "# Trip\n{{INCLUDE:roles/_shared/trip-telegram.md}}\n", encoding="utf-8"
        )
        (root / "profiles/project-manager/SOUL.head.md").write_text(
            "# PM\n{{INCLUDE:roles/_shared/commander-cards.md}}\n", encoding="utf-8"
        )
        shared = root / "shared/travel-workflow"
        shared.mkdir(parents=True)
        user_prefs = '{"budget": null, "custom": true}\n'
        (shared / "preferences.json").write_text(user_prefs, encoding="utf-8")

        rc = setup.sync_live(root, python="C:/py/python.exe", stamp="20261127T120000")
        self.assertEqual(rc, 0)
        self.assertTrue((shared / "travel.py").is_file())
        self.assertTrue((pm_plugin / "__init__.py").is_file())
        # Old plugin file was backed up before overwrite
        self.assertTrue((pm_plugin / "__init__.py.before-sync-20261127T120000").is_file())
        # us-market had no travel-reply-check plugin, so sync-live did NOT inject one
        self.assertFalse((root / "profiles/us-market/plugins/travel-reply-check").exists())
        self.assertTrue((root / "profiles/trip/SOUL.md").is_file())
        self.assertIn("travel.py overseas", (root / "profiles/project-manager/SOUL.md").read_text(encoding="utf-8"))
        # User state is untouched
        self.assertEqual((shared / "preferences.json").read_text(encoding="utf-8"), user_prefs)

    def test_sync_live_propagates_missing_soul_head_error(self):
        root = Path(scratch.new_dir("sync-live-err"))
        (root / "profiles/trip").mkdir(parents=True)
        rc = setup.sync_live(root, python="C:/py/python.exe", stamp="20261127T120001")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
