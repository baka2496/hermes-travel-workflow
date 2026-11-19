import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import page_evidence  # noqa: E402

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
    """A Hermes home where page-prices.py read `html` from `page` (retained: no recursive cleanup)."""
    root = Path(tempfile.mkdtemp(prefix="hermes-page-prices-"))
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


class PagePricesToolTests(unittest.TestCase):
    def test_it_prints_the_prices_and_keeps_a_snapshot(self):
        root = tempfile.mkdtemp(prefix="hermes-page-prices-")
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
        os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-photo-")  # no preferences: show all
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


if __name__ == "__main__":
    unittest.main()
