import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import page_evidence  # noqa: E402
sys.path.insert(0, str(REPO))
import setup  # noqa: E402

PAGE = "https://spring.example/hotspring.html"
HTML = """<html><head><title>範例溫泉</title></head><body>
<h1>價位資訊</h1>
<p>冬季 (10-4月)：699元 / 60分鐘</p>
<p>電話：03-9881892</p>
</body></html>"""


def run_format(*args):
    result = subprocess.run([sys.executable, str(REPO / "scripts/format-plan.py"), *args],
                            capture_output=True, text=True, encoding="utf-8")
    return result.returncode, result.stdout, result.stderr


class SkipPhoneCheckRuleTests(unittest.TestCase):
    def test_role_only_skips_phone_check_for_unedited_output(self):
        # 2026-11-25 (Antigravity's review of ddb1750): the role said a passing format-plan.py means
        # phone-check.py can be skipped, without "only if sent as is"; edited text went out unchecked.
        role = (REPO / "roles/_shared/telegram-research.md").read_text(encoding="utf-8")
        self.assertIn("原樣送出免重跑 `phone-check.py`，改字就重跑", role)


class FormatPlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-format-plan-"))
        text, headings = page_evidence.read_page(HTML)
        page_evidence.save_snapshot(self.root, PAGE, text, headings)
        self.prefs = self.root / "preferences.json"
        self.prefs.write_text(json.dumps({"show_unconfirmed": False, "public_transit": False}), encoding="utf-8")
        self.records = self.root / "opportunities.json"
        self.records.write_text("[]", encoding="utf-8")
        # Verified photo fixture
        self.photo = self.root / "room.jpg"
        data = b"fake-jpeg-bytes" * 100
        self.photo.write_bytes(data)
        self.photo.with_name("room.jpg.source.json").write_text(
            json.dumps({"sha256": hashlib.sha256(data).hexdigest()}), encoding="utf-8"
        )

    def test_assembles_verified_plan_passing_phone_check(self):
        rc, out, err = run_format(
            "--header", "12/14 礁溪泡湯 2人",
            "--budget", "2000",
            "--date", "2026-12-14",
            "--item", f"雙人湯屋 60分鐘=699x1@{PAGE}",
            "--place", "範例溫泉",
            "--official", PAGE,
            "--phone", "03-9881892",
            "--phone-source", PAGE,
            "--photo", str(self.photo),
            "--photo-caption", "雙人湯屋 60分鐘（spring.example）",
            "--note", "開車約 50 分鐘",
            "--hermes-root", str(self.root),
            "--records", str(self.records),
            "--preferences", str(self.prefs),
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("不用再重跑 phone-check.py", err)
        self.assertIn("12/14 礁溪泡湯 2人", out)
        self.assertIn("總計 NT$699，預算剩 NT$1,301", out)
        self.assertIn("• 雙人湯屋 60分鐘 NT$699", out)
        self.assertIn(f"MEDIA:{self.photo.as_posix()}", out)
        self.assertIn("📷 雙人湯屋 60分鐘（spring.example）", out)
        self.assertIn("📞 03-9881892", out)
        self.assertIn("Google 地圖・查空房", out)

    def test_a_plan_without_a_budget_adds_up_the_total_alone(self):
        # 2026-11-25: the user gave no budget, so there is none to pass (Antigravity's review of 0783149).
        rc, out, err = run_format(
            "--header", "12/14 礁溪泡湯 2人", "--date", "2026-12-14",
            "--item", f"雙人湯屋 60分鐘=699x1@{PAGE}", "--place", "範例溫泉", "--official", PAGE,
            "--phone", "03-9881892", "--phone-source", PAGE, "--photo", str(self.photo),
            "--photo-caption", "雙人湯屋 60分鐘（spring.example）",
            "--hermes-root", str(self.root), "--records", str(self.records), "--preferences", str(self.prefs))
        self.assertEqual(rc, 0, err)
        self.assertIn("總計 NT$699", out)
        self.assertNotIn("預算", out)

    def test_adds_fallbacks_when_phone_and_photo_absent(self):
        rc, out, err = run_format(
            "--header", "12/14 礁溪泡湯 2人",
            "--budget", "2000",
            "--date", "2026-12-14",
            "--item", f"雙人湯屋 60分鐘=699x1@{PAGE}",
            "--place", "範例溫泉",
            "--official", PAGE,
            "--hermes-root", str(self.root),
            "--records", str(self.records),
            "--preferences", str(self.prefs),
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("照片：未取得", out)
        self.assertIn("電話：未取得", out)

    def test_for_clarify_omits_photo_without_failing_missing_photo(self):
        rc, out, err = run_format(
            "--header", "12/14 礁溪泡湯 2人",
            "--budget", "2000",
            "--date", "2026-12-14",
            "--item", f"雙人湯屋 60分鐘=699x1@{PAGE}",
            "--place", "範例溫泉",
            "--phone", "03-9881892",
            "--phone-source", PAGE,
            "--for-clarify",
            "--hermes-root", str(self.root),
            "--records", str(self.records),
            "--preferences", str(self.prefs),
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn("MEDIA:", out)
        self.assertNotIn("照片：未取得", out)

    def test_rejects_unverified_photo(self):
        unverified = self.root / "no_record.jpg"
        unverified.write_bytes(b"abc")
        rc, out, err = run_format(
            "--header", "12/14 礁溪泡湯 2人",
            "--budget", "2000",
            "--date", "2026-12-14",
            "--item", f"雙人湯屋 60分鐘=699x1@{PAGE}",
            "--place", "範例溫泉",
            "--phone", "03-9881892",
            "--phone-source", PAGE,
            "--photo", str(unverified),
            "--hermes-root", str(self.root),
            "--records", str(self.records),
            "--preferences", str(self.prefs),
        )
        self.assertEqual(rc, 1)
        self.assertIn("unverified_photo", err)


PLACES = {  # page, name, room, price, phone
    "a": ("https://a.example/rooms", "甲溫泉", "和室雙人房", 3200, "03-9881111"),
    "b": ("https://b.example/rooms", "乙山莊", "雙人湯屋房", 2800, "03-9882222"),
    "c": ("https://c.example/rooms", "丙旅館", "景觀雙人房", 4100, "03-9883333"),
}


class PackageTests(unittest.TestCase):
    """2026-11-26: the owner only wants to pay. One reply, three packages that can be booked as they stand."""

    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-packages-"))
        for page, name, room, price, phone in PLACES.values():
            html = (f"<html><head><title>{name}</title></head><body><h1>{room}</h1>"
                    f"<p>{room}：{price}元 / 晚</p><p>電話：{phone}</p></body></html>")
            text, headings = page_evidence.read_page(html)
            page_evidence.save_snapshot(self.root, page, text, headings)
        self.prefs = self.root / "preferences.json"
        self.prefs.write_text(json.dumps({"show_unconfirmed": False, "public_transit": False}), encoding="utf-8")
        self.records = self.root / "opportunities.json"
        self.records.write_text("[]", encoding="utf-8")

    def package(self, key, *, phone=True, price=None, official=True, transport="開車約 50 分鐘"):
        page, name, room, real, number = PLACES[key]
        args = ["--package", "--place", name, "--room", room, "--item", f"{room} 1晚={price or real}@{page}"]
        if transport:
            args += ["--transport", transport]
        if official:
            args += ["--official", page]
        if phone:
            args += ["--phone", number, "--phone-source", page]
        return args

    def run_packages(self, *packages, shared=()):
        return run_format("--header", "12/14–15 礁溪 2人", "--date", "2026-12-14", "--checkout", "2026-12-15",
                          "--guests", "2", *shared, "--hermes-root", str(self.root), "--records", str(self.records),
                          "--preferences", str(self.prefs), *[arg for p in packages for arg in p])

    def test_three_packages_come_out_cheapest_first_and_pass_phone_check(self):
        rc, out, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"))
        self.assertEqual(rc, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "12/14–15 礁溪 2人｜3 個方案，點連結就能訂")
        marks = [line for line in lines if line[0] in "①②③"]
        self.assertEqual(marks, ["① 乙山莊｜雙人湯屋房", "② 甲溫泉｜和室雙人房", "③ 丙旅館｜景觀雙人房"])
        self.assertIn("總計 NT$2,800", lines)
        self.assertEqual(lines.count("交通：開車約 50 分鐘"), 3)
        self.assertIn("📍 [地圖](https://www.google.com/maps/search/?api=1&query=%E4%B9%99%E5%B1%B1%E8%8E%8A) "
                      "🏨 [官網](https://b.example/rooms) 📞 03-9882222", lines)
        self.assertNotIn("預算", out)  # no budget was given
        status = subprocess.run([sys.executable, str(REPO / "scripts/phone-check.py")], input=out,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(status.returncode, 0, status.stdout)

    def test_fewer_than_three_needs_a_reason(self):
        rc, _, err = self.run_packages(self.package("a"), self.package("b"))
        self.assertEqual(rc, 1)
        self.assertIn("要給 3 個能直接訂的方案", err)
        rc, out, err = self.run_packages(self.package("a"), self.package("b"),
                                         shared=("--few-reason", "只有兩間 12/14 排得進，其他客滿"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("12/14–15 礁溪 2人｜只有兩間 12/14 排得進"))

    def test_more_than_three_is_refused(self):
        rc, _, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"), self.package("a"))
        self.assertEqual(rc, 1)
        self.assertIn("最多 3 個方案", err)

    def test_a_package_without_a_confirmed_price_is_not_ready_to_book(self):
        # With show_unconfirmed off, a price the page does not show leaves nothing to book on.
        rc, _, err = self.run_packages(self.package("a", price=1234), self.package("b"), self.package("c"))
        self.assertEqual(rc, 1)
        self.assertIn("甲溫泉：沒有確認過的價格", err)

    def test_every_package_needs_transport_and_a_way_to_book(self):
        rc, _, err = self.run_packages(self.package("a", transport=None), self.package("b"),
                                       self.package("c", phone=False, official=False))
        self.assertEqual(rc, 1)
        self.assertIn("甲溫泉：每個方案都要 --place、--transport", err)
        self.assertIn("丙旅館：沒有能訂的地方", err)

    def test_a_package_without_a_phone_says_so(self):
        rc, out, err = self.run_packages(self.package("a"), self.package("b"), self.package("c", phone=False))
        self.assertEqual(rc, 0, err)
        self.assertIn("🏨 [官網](https://c.example/rooms)\n電話：未取得", out)

    def test_a_budget_the_owner_gave_is_compared_per_package(self):
        rc, out, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"),
                                         shared=("--budget", "3000"))
        self.assertEqual(rc, 0, err)
        self.assertIn("總計 NT$2,800，預算剩 NT$200", out)
        self.assertIn("總計 NT$4,100，超出預算 NT$1,100", out)


class OneWayTests(unittest.TestCase):
    """format-plan.py is the one way to write a plan reply (2026-11-26)."""

    def test_roles_name_no_other_way_to_assemble_a_plan(self):
        for role in ("trip", "travel-commander"):
            with self.subTest(role=role):
                text = setup.expand_includes((REPO / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                self.assertIn("format-plan.py", text)
                self.assertIn("--package", text)
                for other in ("trip-cost.py", "booking-links.py"):
                    self.assertNotIn(other, text, f"{role} still tells the bot to run {other} by hand")

    def test_scheduled_prompts_use_it_too(self):
        for name in ("weekly", "weekend"):
            with self.subTest(prompt=name):
                text = (REPO / "prompts" / f"{name}.md").read_text(encoding="utf-8")
                self.assertIn("format-plan.py", text)
                for other in ("trip-cost.py", "booking-links.py"):
                    self.assertNotIn(other, text)


if __name__ == "__main__":
    unittest.main()
