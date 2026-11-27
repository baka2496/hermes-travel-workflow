import importlib.util
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


def load_phone_check():
    spec = importlib.util.spec_from_file_location("phone_check", REPO / "scripts/phone-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_format(*args):
    result = subprocess.run([sys.executable, str(REPO / "scripts/format-plan.py"), *args],
                            capture_output=True, text=True, encoding="utf-8")
    return result.returncode, result.stdout, result.stderr


class SkipPhoneCheckRuleTests(unittest.TestCase):
    def test_role_only_skips_phone_check_for_unedited_output(self):
        # V2: travel.py runs phone-check internally; only hand-written replies run travel.py check.
        role = (REPO / "roles/_shared/telegram-research.md").read_text(encoding="utf-8")
        self.assertIn("直接原樣貼上輸出，不手抄改寫，免重跑檢查", role)
        self.assertIn("只有非 `travel.py` 產生的手寫回覆", role)


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

    def history(self, *names, hours_ago=24):
        import time
        at = time.time() - hours_ago * 3600
        entries = [{"name": PLACES[k][1], "phone": PLACES[k][4].replace("-", ""), "site": PLACES[k][0].split("//")[1],
                    "at": at} for k in names]
        (self.root / "recommended.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def test_one_place_twice_is_not_two_packages(self):
        # 2026-11-26 (exam): three packages were three rooms of one 龍山溫泉旅館.
        twin = ["--package", "--place", "甲溫泉 本館", "--room", "雙人房", "--transport", "開車約 50 分鐘",
                "--item", "和室雙人房 1晚=3200@https://a.example/rooms", "--official", "https://a.example/rooms"]
        rc, _, err = self.run_packages(self.package("a"), twin, self.package("c"))
        self.assertEqual(rc, 1)
        self.assertIn("是同一個地方", err)

    def test_the_same_three_places_again_are_refused(self):
        # 2026-11-26 (exam): 「我想出去玩」「我想去泡溫泉」「這週末想去泡溫泉」 all got the same three places.
        self.history("a", "b", "c")
        rc, _, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"))
        self.assertEqual(rc, 1)
        self.assertIn("最近 14 天三個都推過：甲溫泉、乙山莊、丙旅館", err)
        # Two repeats pass: for 「烏來」 one repeat allowed had the bot refused eight times (exam, 2026-11-26).
        self.history("a", "b")
        rc, out, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"))
        self.assertEqual(rc, 0, err)
        # A follow-up in the same conversation (minutes ago) and an old recommendation do not count.
        for hours in (0.1, 24 * 15):
            self.history("a", "b", "c", hours_ago=hours)
            rc, _, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"))
            self.assertEqual(rc, 0, err)
        # Only these fit (a named place, the cheapest there): the reason lets them through.
        self.history("a", "b", "c")
        rc, _, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"),
                                       shared=("--few-reason", "只有這三家符合，因為礁溪雙人湯屋只有這些有標價"))
        self.assertEqual(rc, 0, err)
        # Any words are not a reason (Antigravity's review): it must say why only these fit.
        rc, _, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"),
                                       shared=("--few-reason", "推薦這三家"))
        self.assertEqual(rc, 1)
        self.assertIn("最近 14 天三個都推過", err)

    def test_a_beach_whose_page_lists_no_fee_says_so_and_never_free(self):
        # 2026-11-26: the owner chose to have such places recommended, marked 「官方頁沒列收費」.
        beach = "https://www.taiwan.net.tw/m1.aspx?sNo=0001016&id=10596"
        text, headings = page_evidence.read_page("<html><body><h1>白沙灣</h1><p>開放時間 09:00-17:00</p>"
                                                 "<p>電話：02-8635-5100</p></body></html>")
        page_evidence.save_snapshot(self.root, beach, text, headings)
        sea = ["--package", "--place", "白沙灣海水浴場", "--transport", "開車約 60 分鐘", "--no-fee-listed", beach,
               "--phone", "02-8635-5100", "--phone-source", beach]
        rc, out, err = self.run_packages(self.package("a"), sea, self.package("c"))
        self.assertEqual(rc, 0, err)
        self.assertIn("① 白沙灣海水浴場\n總計 NT$0（門票：官方頁沒列收費，未確認）", out)
        self.assertNotIn("免費", out)
        # A page that does list a price must be priced with --item.
        priced = [arg if arg != beach else PLACES["b"][0] for arg in sea]
        rc, _, err = self.run_packages(self.package("a"), priced, self.package("c"))
        self.assertEqual(rc, 1)
        self.assertIn("有列價格", err)
        # A page not read today cannot vouch for it.
        unread = [arg if arg != beach else "https://www.example.com/never-read" for arg in sea]
        rc, _, err = self.run_packages(self.package("a"), unread, self.package("c"))
        self.assertIn("先跑 page-prices.py", err)
        # With other costs (--item) the page is still checked and the fee still marked (Antigravity's review).
        paid = ["--item", "和室雙人房 1晚=3200@https://a.example/rooms"]
        rc, out, err = self.run_packages(self.package("b"), sea + paid, self.package("c"))
        self.assertEqual(rc, 0, err)
        self.assertIn("白沙灣海水浴場\n總計 NT$3,200（門票：官方頁沒列收費，未確認）", out)
        rc, _, err = self.run_packages(self.package("b"), priced + paid, self.package("c"))
        self.assertEqual(rc, 1)
        self.assertIn("有列價格", err)
        # One plan alone (the owner picked it) says the same.
        rc, out, err = run_format("--header", "12/14 白沙灣 2人", "--date", "2026-12-14", "--place", "白沙灣海水浴場",
                                  "--no-fee-listed", beach, "--phone", "02-8635-5100", "--phone-source", beach,
                                  "--hermes-root", str(self.root), "--records", str(self.records),
                                  "--preferences", str(self.prefs))
        self.assertEqual(rc, 0, err)
        self.assertIn("總計 NT$0（門票：官方頁沒列收費，未確認）", out)
        self.assertNotIn("免費", out)
        rc, _, err = run_format("--header", "12/14 白沙灣 2人", "--date", "2026-12-14", "--place", "白沙灣海水浴場",
                                "--no-fee-listed", PLACES["b"][0], "--hermes-root", str(self.root),
                                "--records", str(self.records), "--preferences", str(self.prefs))
        self.assertEqual(rc, 1)
        self.assertIn("有列價格", err)

    def test_two_attractions_on_one_tourism_site_are_two_places(self):
        load = importlib.util.spec_from_file_location("format_plan", REPO / "scripts/format-plan.py")
        plan = importlib.util.module_from_spec(load)
        load.loader.exec_module(plan)
        a = plan.place_identity("十分瀑布", "https://www.newtaipei.travel/zh-tw/attractions/detail/109609")
        b = plan.place_identity("猴硐貓村", "https://www.newtaipei.travel/zh-tw/attractions/detail/112345")
        c = plan.place_identity("步道一", "https://recreation.forest.gov.tw/Trail/RT?tr_id=001")
        d = plan.place_identity("步道二", "https://recreation.forest.gov.tw/Trail/RT?tr_id=002")
        self.assertFalse(plan.same_place(a, b))
        self.assertFalse(plan.same_place(c, d))
        self.assertTrue(plan.same_place(a, plan.place_identity("十分瀑布公園", None)))

    def test_a_sent_reply_is_remembered(self):
        rc, _, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"))
        self.assertEqual(rc, 0, err)
        log = json.loads((self.root / "recommended.json").read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in log], ["甲溫泉", "乙山莊", "丙旅館"])
        self.assertEqual(log[0]["phone"], "039881111")
        self.assertEqual(log[0]["site"], "a.example/rooms")

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

    def test_three_packages_with_a_long_header_are_not_too_long(self):
        # 2026-11-26 (exam): 「11/28 週末出遊 2人（自駕預估）」 made format-plan.py's own packages 15 phone lines,
        # refused as too long, and the bot wrote them by hand.
        rc, out, err = run_format("--header", "11/28 週末出遊 2人（自駕預估）走走看看", "--date", "2026-12-14",
                                  "--checkout", "2026-12-15", "--guests", "2", "--hermes-root", str(self.root),
                                  "--records", str(self.records), "--preferences", str(self.prefs),
                                  *self.package("a", transport="開車約 50 分鐘，國道五號雪隧假日下午容易塞車"),
                                  *self.package("b"), *self.package("c"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(sum(line[:1] in "①②③" for line in out.splitlines()), 3)
        lines = load_phone_check().check(out)["phone_lines"]
        self.assertGreater(lines, 14)  # over the old one-reply limit, the case the exam hit

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

    def test_when_one_package_fails_it_says_the_others_are_ready(self):
        # 2026-11-27 (exam overnight-budget): two hotels had prices read today, the third had none. The whole
        # run was refused, the bot hand-wrote all three, and the owner got no plan.
        rc, out, err = self.run_packages(self.package("a", price=1234), self.package("b"), self.package("c"))
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "")  # nothing to paste until every package is right
        self.assertIn("乙山莊、丙旅館 已經沒問題，不要手寫回覆", err)
        self.assertIn("--few-reason", err)
        self.assertNotIn("甲溫泉 已經沒問題", err)
        # Nothing ready at all: no such line.
        rc, _, err = self.run_packages(self.package("a", price=1234), self.package("b", price=1234),
                                       self.package("c", price=1234))
        self.assertNotIn("已經沒問題", err)

    def test_an_all_estimate_package_is_refused_when_estimates_are_shown_too(self):
        # Antigravity's review of 646df05: with show_unconfirmed on, an estimate-only total still printed.
        self.prefs.write_text(json.dumps({"public_transit": False}), encoding="utf-8")
        rc, _, err = self.run_packages(self.package("a", price=1234), self.package("b"), self.package("c"))
        self.assertEqual(rc, 1)
        self.assertIn("甲溫泉：沒有確認過的價格", err)
        rc, out, err = self.run_packages(self.package("a"), self.package("b"), self.package("c"))
        self.assertEqual(rc, 0, err)

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
    """travel.py (wrapping format-plan.py) is the one way to write a plan reply."""

    def test_roles_name_no_other_way_to_assemble_a_plan(self):
        for role in ("trip", "travel-commander"):
            with self.subTest(role=role):
                text = setup.expand_includes((REPO / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                self.assertIn("travel.py", text)
                self.assertIn("travel.py\" local", text)
                self.assertIn("travel.py\" overseas", text)
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
