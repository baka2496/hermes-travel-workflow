"""places.py: the place pool the bot picks from (2026-11-26: always the same three hot springs)."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
DAY = 86400


def page(title, body):
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-places-"))
        self.places = self.root / "places.json"
        self.history = self.root / "recommended.json"

    def run_places(self, *args, html=None, now=None):
        extra = ["--places", str(self.places), "--history", str(self.history), "--hermes-root", str(self.root)]
        if html is not None:
            path = self.root / "page.html"
            path.write_text(html, encoding="utf-8")
            extra += ["--html-file", str(path)]
        if now is not None:
            extra += ["--now", str(now)]
        result = subprocess.run([sys.executable, str(REPO / "scripts/places.py"), *extra, *args],
                                capture_output=True, text=True, encoding="utf-8")
        return result.returncode, result.stdout, result.stderr

    def add(self, name, area, kind, url, body, now=None):
        return self.run_places("add", "--name", name, "--area", area, "--kind", kind, "--official", url,
                               html=page(name, body), now=now)

    def saved(self):
        return json.loads(self.places.read_text(encoding="utf-8"))["places"]

    def test_a_place_goes_in_only_when_its_page_shows_a_price_or_free(self):
        rc, out, err = self.add("甲湯屋", "礁溪", "溫泉", "https://a.example/spa", "<p>雙人湯屋 NT$500 / 1小時</p>"
                                "<p>電話：03-988-1111</p>")
        self.assertEqual(rc, 0, err)
        self.assertIn("收進地點庫：甲湯屋", out)
        rc, out, err = self.add("乙步道", "九份", "自然步道、看風景", "https://b.example/trail", "<p>開放時間 8:00 免費入園</p>")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.add("丙老街", "大溪", "老街、小吃、夜市", "https://c.example/", "<p>歡迎光臨 免費停車</p>")
        self.assertEqual(rc, 1)
        self.assertIn("讀不到價格", err)
        saved = self.saved()
        self.assertEqual([p["name"] for p in saved], ["甲湯屋", "乙步道"])
        self.assertEqual(saved[0]["phone"], "03-988-1111")
        self.assertTrue(saved[1]["free"])
        self.assertEqual(saved[0]["price_page"], "https://a.example/spa")

    def test_a_beach_whose_page_lists_no_fee_goes_in_only_when_asked_and_never_as_free(self):
        rc, _, err = self.add("白沙灣海水浴場", "石門", "海邊", "https://beach.example/", "<p>開放時間 09:00-17:00</p>")
        self.assertEqual(rc, 1)
        rc, out, err = self.run_places("add", "--name", "白沙灣海水浴場", "--area", "石門", "--kind", "海邊",
                                       "--official", "https://beach.example/", "--no-fee-listed",
                                       html=page("白沙灣", "<p>開放時間 09:00-17:00</p>"))
        self.assertEqual(rc, 0, err)
        self.assertIn("官方頁沒列收費", out)
        rc, out, _ = self.run_places("pick", "--kind", "海邊")
        self.assertIn("白沙灣海水浴場｜石門・海邊｜沒推過｜官方頁沒列收費", out)
        self.assertNotIn("免費", out)

    def test_the_same_place_again_is_updated_not_doubled(self):
        self.add("甲湯屋", "礁溪", "溫泉", "https://a.example/spa", "<p>雙人湯屋 NT$500 / 1小時</p>")
        rc, out, _ = self.add("甲湯屋 本館", "礁溪", "溫泉", "https://a.example/spa", "<p>雙人湯屋 NT$550 / 1小時</p>")
        self.assertIn("更新", out)
        self.assertEqual(len(self.saved()), 1)
        self.assertTrue((self.root / "places.backup.json").exists())

    def test_an_unknown_kind_is_refused(self):
        rc, _, err = self.add("甲", "礁溪", "購物", "https://a.example/", "<p>NT$500</p>")
        self.assertEqual(rc, 1)
        self.assertIn("--kind 要是其中一種", err)
        rc, _, err = self.add("甲", "礁溪", "", "https://a.example/", "<p>NT$500</p>")
        self.assertEqual(rc, 1)
        self.assertIn("--kind 要是其中一種", err)
        rc, _, err = self.run_places("pick", "--kind", "購物")
        self.assertEqual(rc, 1)
        self.assertIn("--kind 要是其中一種", err)

    def test_staying_overnight_picks_hot_spring_hotels_too(self):
        # 2026-11-27 (exam overnight-budget): pick --kind 住宿 said 「地點庫沒有」 though seven 礁溪 hot-spring
        # hotels were kept as 溫泉, then add --kind 住宿 was refused.
        self.add("甲溫泉飯店", "礁溪", "溫泉", "https://a.example/room", "<p>雙人房 假日 NT$4,200</p>")
        rc, out, err = self.add("乙旅館", "礁溪", "住宿", "https://b.example/room", "<p>雙人房 假日 NT$1,800</p>")
        self.assertEqual(rc, 0, err)
        self.add("丙步道", "礁溪", "自然步道、看風景", "https://c.example/trail", "<p>免費入園</p>")
        rc, out, err = self.run_places("pick", "--kind", "住宿", "--area", "礁溪")
        self.assertEqual(rc, 0, err)
        names = sorted(line.split("｜")[0].lstrip("• ") for line in out.splitlines() if line.startswith("•"))
        self.assertEqual(names, ["乙旅館", "甲溫泉飯店"])
        rc, out, _ = self.run_places("pick", "--kind", "溫泉", "--area", "礁溪")
        self.assertNotIn("乙旅館", out)

    def test_pick_offers_the_least_recently_recommended_first_and_never_stale_ones(self):
        now = time.time()
        for key, name in (("a", "甲湯屋"), ("b", "乙湯屋"), ("c", "丙湯屋")):
            self.add(name, "礁溪", "溫泉", f"https://{key}.example/spa", "<p>雙人湯屋 NT$500 / 1小時</p>")
        self.add("丁步道", "九份", "自然步道、看風景", "https://d.example/trail", "<p>免費入園</p>")
        self.history.write_text(json.dumps([{"name": "甲湯屋", "at": now - DAY}, {"name": "乙湯屋", "at": now - 5 * DAY}],
                                           ensure_ascii=False), encoding="utf-8")
        rc, out, err = self.run_places("pick", "--kind", "溫泉")
        self.assertEqual(rc, 0, err)
        order = [line.split("｜")[0].lstrip("• ") for line in out.splitlines() if line.startswith("•")]
        self.assertEqual(order, ["丙湯屋", "乙湯屋", "甲湯屋"])
        self.assertIn("沒推過", out)
        rc, out, _ = self.run_places("pick", "--area", "九份")
        self.assertIn("丁步道｜九份・自然步道、看風景｜沒推過｜免費", out)
        # Prices not read again for over 30 days are not offered.
        rc, out, err = self.run_places("pick", "--kind", "溫泉", now=now + 31 * DAY)
        self.assertEqual(rc, 1)
        self.assertIn("地點庫沒有溫泉的地方", err)

    def test_recheck_marks_a_page_that_lost_its_price_broken_and_keeps_it(self):
        now = time.time()
        self.add("甲湯屋", "礁溪", "溫泉", "https://a.example/spa", "<p>雙人湯屋 NT$500 / 1小時</p>")
        rc, out, _ = self.run_places("recheck", html=page("甲湯屋", "<p>網站改版中</p>"), now=now + 8 * DAY)
        self.assertIn("失效：甲湯屋", out)
        self.assertEqual(self.saved()[0]["status"], "broken")
        rc, out, _ = self.run_places("pick", "--kind", "溫泉", now=now + 8 * DAY)
        self.assertEqual(rc, 1)
        rc, out, _ = self.run_places("recheck", html=page("甲湯屋", "<p>雙人湯屋 NT$520 / 1小時</p>"), now=now + 9 * DAY)
        self.assertIn("重查：甲湯屋", out)
        self.assertEqual(self.saved()[0]["status"], "ok")

    def test_harvest_catalog_enforces_https_unlisted_and_deduplicates_distinct_taiwan_ids(self):
        # 1. Paid venue catalog + non-HTTPS rejection
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({
            "schema_version": 1,
            "places": [
                {"name": "甲湯屋", "area": "北投", "kind": "溫泉",
                 "official": "https://a.example/", "price_page": "https://a.example/spa"},
                {"name": "不安全http", "area": "金山", "kind": "溫泉",
                 "official": "http://bad.example/", "price_page": "http://bad.example/spa"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        spa_html = page("甲湯屋", "<p>雙人湯屋 NT$800 / 60分鐘</p><p>電話 02-2891-1111</p>")
        rc, out, err = self.run_places(
            "harvest", "--counties", "none", "--catalog", str(catalog), "--dry-run", html=spa_html
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("預覽模式", out)
        self.assertFalse(self.places.exists())

        rc, out, err = self.run_places(
            "harvest", "--counties", "none", "--catalog", str(catalog), html=spa_html
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual([p["name"] for p in self.saved()], ["甲湯屋"])
        self.assertEqual(self.saved()[0]["phone"], "02-2891-1111")

        # Updating when page temporarily has no phone preserves existing phone
        spa_no_phone = page("甲湯屋", "<p>雙人湯屋 NT$850 / 60分鐘</p>")
        rc, _, err = self.run_places(
            "harvest", "--counties", "none", "--catalog", str(catalog), html=spa_no_phone
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.saved()[0]["phone"], "02-2891-1111")

        # 2. Public old streets / night markets with no prices -> free == "unlisted" & distinct taiwan.net.tw ids kept
        tw_cat = self.root / "tw-catalog.json"
        tw_cat.write_text(json.dumps({
            "schema_version": 1,
            "places": [
                {"name": "夜市一", "area": "士林", "kind": "老街、小吃、夜市",
                 "official": "https://www.taiwan.net.tw/m1.aspx?sNo=0001090&id=154",
                 "price_page": "https://www.taiwan.net.tw/m1.aspx?sNo=0001090&id=154", "no_fee_listed": True},
                {"name": "夜市二", "area": "台北萬華", "kind": "老街、小吃、夜市",
                 "official": "https://www.taiwan.net.tw/m1.aspx?sNo=0001090&id=18",
                 "price_page": "https://www.taiwan.net.tw/m1.aspx?sNo=0001090&id=18", "no_fee_listed": True},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        no_price_html = page("觀光介紹", "<p>人氣夜市美食</p><p>電話 (02)2349-1500</p>")
        rc, _, err = self.run_places(
            "harvest", "--counties", "none", "--catalog", str(tw_cat), html=no_price_html
        )
        self.assertEqual(rc, 0, err)
        saved = {p["name"]: p for p in self.saved()}
        self.assertIn("夜市一", saved)
        self.assertIn("夜市二", saved)
        self.assertEqual(saved["夜市一"]["free"], "unlisted")
        self.assertEqual(saved["夜市一"]["prices"], [])

    def test_classify_and_crawl_taiwan_county_skips_paid_forest_parks_and_keeps_public_spots(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("places_mod", REPO / "scripts" / "places.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        fake_listing = (
            '<a href="m1.aspx?sNo=0001091&amp;id=19" title="淡水老街">淡水老街</a>'
            '<a href="m1.aspx?sNo=0001091&amp;id=721" title="鼻頭角步道">鼻頭角步道</a>'
            '<a href="m1.aspx?sNo=0001091&amp;id=9658" title="內洞國家森林遊樂區">內洞國家森林遊樂區</a>'
            '<a href="m1.aspx?sNo=0001091&amp;id=2890" title="野柳地質公園">野柳地質公園</a>'
            '<a href="m1.aspx?sNo=0001106&amp;id=605" title="大溪蜜月灣">大溪蜜月灣</a>'
        )
        items = mod.crawl_taiwan_county("新北", html_text=fake_listing)
        names = [x["name"] for x in items]
        self.assertIn("淡水老街", names)
        self.assertIn("鼻頭角步道", names)
        # Paid ticketed parks must NOT be crawled as unlisted/no-fee from overview pages
        self.assertNotIn("內洞國家森林遊樂區", names)
        self.assertNotIn("野柳地質公園", names)
        self.assertEqual(mod.classify_taiwan_attraction("大溪蜜月灣", "宜蘭"), ("海邊", "宜蘭頭城"))


class StrftimeTests(unittest.TestCase):
    def test_no_strftime_format_holds_chinese(self):
        # 2026-11-26: time.strftime("%m/%d 推過") passed here but raised UnicodeEncodeError on CI's locale.
        # Read as code (Antigravity's review): a call over several lines or `datetime.strftime(d, fmt)` counts,
        # a comment does not.
        import ast
        found = []
        for path in [*REPO.glob("scripts/*.py"), *REPO.glob("tools/*.py"), *REPO.glob("plugins/*/*.py"),
                     *REPO.glob("tests/*.py"), REPO / "setup.py"]:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None))
                        == "strftime"):
                    continue
                for arg in node.args:
                    texts = [n.value for n in ast.walk(arg) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                    if any(ord(ch) > 127 for text in texts for ch in text):
                        found.append(f"{path.relative_to(REPO)}:{node.lineno}")
        self.assertEqual(found, [], "put the Chinese outside strftime: strftime('%m/%d') + ' 推過'")


if __name__ == "__main__":
    unittest.main()
