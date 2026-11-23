import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import page_evidence  # noqa: E402

import scratch

spec = importlib.util.spec_from_file_location("reply_check", REPO / "plugins/travel-reply-check/__init__.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

# 2026-11-19: the shape of 春和's price table (a rowspan 定價 cell), its hidden 雙人池 caption, and
# 湯布苑's prices listed under each room's heading.
CHUNHE = """<html><head><title>春和</title><script>var p = "雙人池 250元";</script></head><body>
<!-- <div><h1>雙人池</h1><p>40分鐘 250元</p></div> -->
<h1>價位資訊</h1>
<table><tr><td></td><td>定價</td><td>60分鐘</td><td>90分鐘</td></tr>
<tr><td>夏季<br>(5-9月)</td><td rowspan="2">900元</td><td>599元</td><td>899元</td></tr>
<tr><td>冬季<br>(10-4月)</td><td>699元</td><td>999元</td></tr></table>
<p>以上為2-4人價格</p><select><option>舊價 250元<option>其他</select>
<div style="display:none">隱藏 250元</div><p>費用包含：浴巾、沐浴用品。</p></body></html>"""
YUFUIN = """<h2>溪景湯屋</h2><p>價格：</p><ul><li>平日特價　$600 / 1小時</li><li>假日特價　$800 / 1小時</li></ul>
<h2>豪華套房</h2><ul><li>平日特價　$800 / 1.5小時</li></ul>"""
PAGE = "https://spring.example/hotspring.html"


def run(script, *args):
    result = subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                            capture_output=True, text=True, encoding="utf-8")
    return result.returncode, result.stdout, result.stderr


def snapshot(html, page=PAGE, age_hours=0):
    """A Hermes home where page-prices.py read `html` from `page`."""
    root = Path(scratch.new_dir("hermes-page-prices-"))
    text, headings = page_evidence.read_page(html)
    page_evidence.save_snapshot(root, page, text, headings, now=time.time() - age_hours * 3600)
    return str(root)


class ReadPageTests(unittest.TestCase):
    def test_a_price_table_is_written_out_with_its_headers(self):
        text, headings = page_evidence.read_page(CHUNHE)
        self.assertIn("冬季 (10-4月)：699元 / 60分鐘", text)
        self.assertIn("冬季 (10-4月)：900元 / 定價", text)  # the rowspan cell belongs to both seasons
        self.assertIn("夏季 (5-9月)：599元 / 60分鐘", text)
        self.assertIn("費用包含：浴巾、沐浴用品。", text)

    def test_what_a_visitor_cannot_see_is_left_out(self):
        text, _ = page_evidence.read_page(CHUNHE)
        self.assertNotIn("250", text)  # comment, script, <option> without end tags, display:none
        self.assertIn("以上為2-4人價格", text)

    def test_prices_come_with_the_heading_they_sit_under(self):
        text, headings = page_evidence.read_page(YUFUIN)
        self.assertEqual(page_evidence.price_lines(text, headings),
                         ["溪景湯屋｜平日特價 $600 / 1小時", "溪景湯屋｜假日特價 $800 / 1小時",
                          "豪華套房｜平日特價 $800 / 1.5小時"])


    def test_an_english_heading_after_a_chinese_one_is_its_translation(self):
        # 2026-11-20: 春和's 「價位資訊」 then 「Price」, and the item became 「Price 60分鐘」.
        text, headings = page_evidence.read_page("<h1>價位資訊</h1><h2>Price</h2><p>60分鐘 699元</p>"
                                                 "<h2>Spa</h2><p>30分鐘 500元</p>")
        self.assertEqual(page_evidence.price_lines(text, headings), ["價位資訊｜60分鐘 699元", "Spa｜30分鐘 500元"])


ROSE = ["溫泉湯屋｜湯屋計價方式：兩人一間300元，加一位大人加50元，加一位小孩30元。",
        "溫泉湯屋｜泡湯時間50分鐘。另售有湯券，10張2000元，無使用期限。",
        "溫泉套房｜另有住宿方案：訂價3600元，假日2880元優惠。"]


class CompareTests(unittest.TestCase):
    # 2026-11-20: a new conversation recommended 春和's NT$699; 玫瑰園 (NT$300) was nearby.
    def test_the_price_of_one_visit_is_picked_not_extras_or_tickets(self):
        self.assertEqual(page_evidence.cheapest(ROSE)[0], 300)

    def test_other_months_and_the_other_day_are_left_out(self):
        import datetime
        text, headings = page_evidence.read_page(CHUNHE)
        lines = page_evidence.price_lines(text, headings)
        self.assertEqual(page_evidence.cheapest(lines, datetime.date(2026, 12, 14))[0], 699)
        self.assertEqual(page_evidence.cheapest(lines, datetime.date(2026, 7, 6))[0], 599)
        text, headings = page_evidence.read_page(YUFUIN)
        lines = page_evidence.price_lines(text, headings)
        self.assertEqual(page_evidence.cheapest(lines, datetime.date(2026, 12, 14))[0], 600)  # Monday
        self.assertEqual(page_evidence.cheapest(lines, datetime.date(2026, 12, 12))[0], 800)  # Saturday

    def test_a_per_person_price_counts_for_everyone(self):
        self.assertEqual(page_evidence.cheapest(["大眾池｜每人 250元 / 2小時"], people=2)[0], 500)

    def test_pages_read_before_for_the_same_area_are_found_again(self):
        root = snapshot(CHUNHE.replace("價位資訊", "礁溪 價位資訊"))
        self.assertEqual(page_evidence.known_pages(root, "礁溪"), [PAGE])
        self.assertEqual(page_evidence.known_pages(root, "烏來"), [])

    def test_the_tool_ranks_several_pages(self):
        root = scratch.new_dir("hermes-page-prices-")
        html = Path(root) / "page.html"
        html.write_text(CHUNHE, encoding="utf-8")
        status, out, err = run("page-prices.py", PAGE, "https://other.example/", "--date", "2026-12-14",
                               "--html-file", str(html), "--hermes-root", root)
        self.assertEqual(status, 0, err)
        self.assertIn("比價（一次的價格，便宜的在前）", out)
        self.assertIn("   699元｜春和｜價位資訊｜冬季 (10-4月)：699元 / 60分鐘", out)


class PhonesOnPageTests(unittest.TestCase):
    def test_the_pages_phones_are_listed_without_the_fax(self):
        # 2026-11-20: 「電話沒附」; 春和's page shows 電話, 傳真 and the big bath's own number.
        text = "營業時間：平日 8:00 電話：03-9881892#3 LINE ID：@sh026 傳真：03-9889151\n日式大浴池：03-9889715"
        self.assertEqual(page_evidence.phones_on_page(text), ["電話 03-9881892#3", "日式大浴池 03-9889715"])
        self.assertEqual(page_evidence.phones_on_page("TEL：039883364 手機 0966-677299"),
                         ["TEL 039883364", "手機 0966-677299"])
        self.assertEqual(page_evidence.phones_on_page("2026-11-20 訂價3600元"), [])


class PagePricesToolTests(unittest.TestCase):
    def test_it_prints_the_prices_and_keeps_a_snapshot(self):
        root = scratch.new_dir("hermes-page-prices-")
        html = Path(root) / "page.html"
        html.write_text(CHUNHE, encoding="utf-8")
        status, out, err = run("page-prices.py", PAGE, "--html-file", str(html), "--hermes-root", root)
        self.assertEqual(status, 0, err)
        self.assertIn("• 價位資訊｜冬季 (10-4月)：699元 / 60分鐘", out)
        self.assertNotIn("250", out)
        self.assertIn("項目名稱寫推薦的是什麼加時長或人數", out)  # so place-photo.py can match the photo
        saved = json.loads(page_evidence.snapshot_path(root, page_evidence.page_key(PAGE)).read_text(encoding="utf-8"))
        self.assertEqual(saved["url"], PAGE)

    def test_only_https(self):
        status, _, err = run("page-prices.py", "http://spring.example/")
        self.assertNotEqual(status, 0)
        self.assertIn("https", err)


class TripCostWithSnapshotTests(unittest.TestCase):
    def cost(self, root, *items, date="2026-12-07"):
        return run("trip-cost.py", "--budget", "2000", "--date", date, *items, "--hermes-root", root,
                   "--records", str(Path(root) / "none.json"), "--preferences", str(Path(root) / "none.json"))

    def test_a_price_on_the_snapshot_is_confirmed(self):
        status, out, err = self.cost(snapshot(CHUNHE), f"雙人湯屋 60分鐘=699@{PAGE}")
        self.assertEqual(status, 0, err)
        self.assertEqual(out.splitlines()[0], "總計 NT$699，預算剩 NT$1,301")

    def test_a_missing_price_names_what_the_page_shows_to_the_agent_only(self):
        # 2026-11-19: told only 「沒看到這個價格」, 小居 spent half an hour finding out why.
        status, out, err = self.cost(snapshot(CHUNHE), f"雙人池 40分鐘=250@{PAGE}")
        self.assertEqual(status, 0, err)
        self.assertIn("• 雙人池 40分鐘 NT$250，未確認（來源網頁上沒看到這個價格）", out)
        self.assertNotIn("699", out)
        self.assertIn("冬季 (10-4月)：699元 / 60分鐘", err)

    def test_a_price_for_other_months_is_not_confirmed(self):
        status, out, _ = self.cost(snapshot(CHUNHE), f"雙人湯屋 60分鐘=599@{PAGE}")
        self.assertIn("網頁上這個價格是5-9月的價格，12月不適用", out)
        status, out, _ = self.cost(snapshot(CHUNHE), f"雙人湯屋 60分鐘=599@{PAGE}", date="2026-07-06")
        self.assertNotIn("未確認", out)

    def test_a_weekday_price_on_a_saturday_is_not_confirmed(self):
        page = "https://spa.example/"
        status, out, _ = self.cost(snapshot(YUFUIN, page), f"溪景湯屋 1小時=600@{page}", date="2026-12-05")
        self.assertIn("網頁上這個價格是平日價", out)
        status, out, _ = self.cost(snapshot(YUFUIN, page), f"溪景湯屋 1小時=600@{page}", date="2026-12-07")
        self.assertNotIn("未確認", out)

    def test_an_old_snapshot_is_no_evidence(self):
        status, out, _ = self.cost(snapshot(CHUNHE, age_hours=page_evidence.RECENT_HOURS + 1),
                                   f"雙人湯屋 60分鐘=699@{PAGE}")
        self.assertIn("未確認", out)


class RecordsAndSnapshotsInThePluginTests(unittest.TestCase):
    RECORDS = "C:/hermes/shared/travel-workflow/opportunities.json"

    def test_reading_a_recorded_price_comes_with_a_reminder_to_recheck(self):
        # 2026-11-19: 小居 planned on the record's 「春和雙人湯屋 40 分鐘 250 元」.
        result = '{"content": "207| \\"jiaoxi_chunhe\\": \\"雙人湯屋 40 分鐘 250 元\\""}'
        noted = check.transform_tool_result("read_file", {"path": self.RECORDS}, result, session_id="s1")
        self.assertTrue(noted.startswith(result))
        self.assertIn("page-prices.py", noted)
        self.assertIsNone(check.transform_tool_result("read_file", {"path": "C:/notes/other.json"}, result,
                                                      session_id="s1"))
        self.assertIsNone(check.transform_tool_result("read_file", {"path": self.RECORDS}, '{"content": "沒有價格"}',
                                                      session_id="s1"))

    def test_the_snapshots_cannot_be_written_by_the_bot(self):
        result = check.pre_tool_call("write_file", {"path": "C:/hermes/shared/travel-workflow/pages/ab12.json",
                                                    "content": "{}"}, session_id="s1")
        self.assertEqual(result["action"], "block")
        run_tool = check.pre_tool_call("terminal", {"command": "python C:/hermes/shared/travel-workflow/page-prices.py "
                                                               "https://spring.example/"}, session_id="s1")
        self.assertIsNone(run_tool)

    def test_a_photo_goes_out_only_after_the_bot_looked_at_it(self):
        # 2026-11-20: 玫瑰園's photo captioned 「溫泉湯屋」 was its shop front.
        import os
        photo = "C:/Users/me/AppData/Local/hermes/shared/travel-workflow/photos/ab12.jpg"
        reply = f"12/14 礁溪 2人\nMEDIA:{photo}\n📷 溫泉湯屋 50分鐘（spa.example）\n玫瑰園原泉湯屋"
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = scratch.new_dir("hermes-photo-")  # no preferences: show all
        try:
            fixed = check.transform_llm_output(response_text=reply, session_id="photo1", turn_id="t1")
            self.assertEqual(fixed, "12/14 礁溪 2人\n照片：未取得\n玫瑰園原泉湯屋")
            self.assertIsNone(check.pre_tool_call("vision_analyze", {"image_url": photo.replace("/", "\\"),
                                                                       "question": "是湯屋嗎"}, session_id="photo1"))
            self.assertIsNone(check.transform_llm_output(response_text=reply, session_id="photo1", turn_id="t1"))
            self.assertIsNotNone(check.transform_llm_output(response_text=reply, session_id="photo2", turn_id="t1"))
            self.assertIsNone(check.transform_llm_output(response_text="沒有照片的回覆", session_id="photo3",
                                                         turn_id="t1"))
        finally:
            if old is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old

    def test_the_block_message_does_not_send_the_bot_to_the_tool_internals(self):
        # The roles say not to read the tools' code or --help; the block message must agree.
        self.assertNotIn("--help", check.TAMPER_PROBLEM)
        self.assertIn("page-prices.py", check.TAMPER_PROBLEM)


class ParallelFetchTests(unittest.TestCase):
    def test_read_all_fetches_pages_concurrently_and_keeps_order(self):
        import types
        spec_pp = importlib.util.spec_from_file_location("page_prices_mod", REPO / "scripts/page-prices.py")
        pp = importlib.util.module_from_spec(spec_pp)
        spec_pp.loader.exec_module(pp)

        urls = ["https://a.example/", "https://b.example/", "https://c.example/"]
        root = scratch.new_dir("hermes-parallel-prices-")
        args = types.SimpleNamespace(html_file=None, hermes_root=root, timeout=5)

        def fake_fetch(url, timeout=10):
            time.sleep(0.15)
            if "b.example" in url:
                raise RuntimeError("timed out")
            return f"<html><title>{url}</title><body><p>湯屋 600元 / 1小時</p></body></html>", url

        original = pp.fetch
        pp.fetch = fake_fetch
        try:
            start = time.monotonic()
            results = pp.read_all(urls, args)
            elapsed = time.monotonic() - start
        finally:
            pp.fetch = original

        self.assertLess(elapsed, 0.35, f"expected parallel fetch, took {elapsed:.2f}s")
        self.assertEqual([u for u, _, _ in results], urls)
        self.assertIsNotNone(results[0][1])
        self.assertIsNone(results[1][1])
        self.assertIn("打不開 https://b.example/", results[1][2])
        self.assertIsNotNone(results[2][1])

    def test_known_pages_caps_near_results_to_five_newest(self):
        root = Path(scratch.new_dir("hermes-known-cap-"))
        now = time.time()
        for i in range(8):
            page_evidence.save_snapshot(root, f"https://spa{i}.example/", f"礁溪 湯屋 {300 + i * 50}元",
                                        now=now - (8 - i) * 60)
        urls = page_evidence.known_pages(root, "礁溪")
        self.assertEqual(len(urls), 5)
        self.assertEqual(urls[0], "https://spa7.example/")
        self.assertEqual(urls[-1], "https://spa3.example/")


if __name__ == "__main__":
    unittest.main()
