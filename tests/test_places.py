"""places.py: the place pool the bot picks from (2026-11-26: always the same three hot springs)."""
import json
from pathlib import Path
import sqlite3
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


class PlacesSqliteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-places-db-"))
        self.db = self.root / "places.db"
        self.history = self.root / "recommended.json"

    def run_places(self, *args, html=None, now=None, places_path=None):
        extra = ["--places", str(places_path or self.db), "--history", str(self.history), "--hermes-root", str(self.root)]
        if html is not None:
            path = self.root / "page.html"
            path.write_text(html, encoding="utf-8")
            extra += ["--html-file", str(path)]
        if now is not None:
            extra += ["--now", str(now)]
        result = subprocess.run([sys.executable, str(REPO / "scripts/places.py"), *extra, *args],
                                capture_output=True, text=True, encoding="utf-8")
        return result.returncode, result.stdout, result.stderr

    def test_add_pick_list_and_backup_in_sqlite_places_db(self):
        rc, out, err = self.run_places(
            "add", "--name", "甲湯屋", "--area", "礁溪", "--kind", "溫泉",
            "--official", "https://a.example/spa",
            html=page("甲湯屋", "<p>雙人湯屋 NT$500 / 1小時</p>"),
        )
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.db.exists())
        con = sqlite3.connect(str(self.db))
        try:
            rows = con.execute("SELECT name, area, kind, country_code, status FROM places").fetchall()
            self.assertEqual(rows, [("甲湯屋", "礁溪", "溫泉", "tw", "ok")])
        finally:
            con.close()

        # Second write creates online WAL-safe backup places.backup.db
        rc2, _, err2 = self.run_places(
            "add", "--name", "乙海灘", "--area", "福隆", "--kind", "海邊",
            "--official", "https://b.example/beach",
            html=page("乙海灘", "<p>免費入園參觀</p>"),
        )
        self.assertEqual(rc2, 0, err2)
        self.assertTrue((self.root / "places.backup.db").exists())

        rc_pick, pick_out, pick_err = self.run_places("pick", "--kind", "溫泉", "--area", "礁溪")
        self.assertEqual(rc_pick, 0, pick_err)
        self.assertIn("甲湯屋", pick_out)

        rc_list, list_out, list_err = self.run_places("list")
        self.assertEqual(rc_list, 0, list_err)
        self.assertIn("甲湯屋", list_out)
        self.assertIn("乙海灘", list_out)

    def test_auto_migrates_sibling_json_and_explicit_migrate_command(self):
        legacy = self.root / "places.json"
        legacy.write_text(json.dumps({
            "schema_version": 1,
            "places": [{
                "name": "老街湯屋", "area": "烏來", "kind": "溫泉",
                "official": "https://u.example/spa", "price_page": "https://u.example/spa",
                "phone": "02-2661-0000", "prices": ["雙人湯屋 NT$600"], "free": False,
                "checked_at": "2026-11-28", "added_at": "2026-11-28", "status": "ok",
            }],
        }, ensure_ascii=False), encoding="utf-8")

        # Reading places.db auto-migrates sibling places.json transparently
        rc, out, err = self.run_places("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("老街湯屋", out)
        self.assertTrue(self.db.exists())
        self.assertTrue(legacy.exists())  # original JSON kept intact

        # Explicit migrate command into a second DB path
        db2 = self.root / "migrated.db"
        rc2, out2, err2 = self.run_places("migrate", "--from", str(legacy), places_path=db2)
        self.assertEqual(rc2, 0, err2)
        self.assertIn("已將 1 筆台灣地點", out2)
        self.assertTrue(db2.exists())

    def test_corrupt_sqlite_db_raises_instead_of_returning_empty_list(self):
        # Regression (Claude review): read_places must never swallow sqlite3.Error as [], or add() would wipe the DB.
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not a valid sqlite file")
        rc, _, err = self.run_places(
            "add", "--name", "甲湯屋", "--area", "礁溪", "--kind", "溫泉",
            "--official", "https://a.example/spa",
            html=page("甲湯屋", "<p>雙人湯屋 NT$500 / 1小時</p>"),
            places_path=corrupt,
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("DatabaseError", err)

    def test_opendata_parsers_and_harvest_checked_incremental_tracking(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("places_mod", REPO / "scripts" / "places.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Expanded open-public classification keeps real northern open spots while still skipping paid parks
        self.assertEqual(mod.classify_taiwan_attraction("西門町商圈", "台北"), ("老街、小吃、夜市", "台北萬華"))
        self.assertEqual(mod.classify_taiwan_attraction("冬山河親水公園", "宜蘭"), ("自然步道、看風景", "冬山"))
        self.assertEqual(mod.classify_taiwan_attraction("南方澳", "宜蘭"), ("海邊", "蘇澳"))
        self.assertEqual(mod.classify_taiwan_attraction("臺北龍山寺", "台北"), ("其他", "台北"))
        # Regression (Claude review): riverside wharves are NOT beaches; ambiguous districts keep county prefix
        self.assertNotEqual(mod.classify_taiwan_attraction("大稻埕碼頭", "台北")[0], "海邊")
        self.assertEqual(mod.classify_taiwan_attraction("中正紀念堂", "台北"), ("其他", "台北中正"))
        self.assertEqual(mod.classify_taiwan_attraction("中正公園", "基隆"), ("自然步道、看風景", "基隆中正"))

        # Tourism V2.1 Attraction parser: enforces HTTPS, skips social links, never guesses paid spots as free
        attr_items = [
            {"AttractionName": "潮境智能海洋館", "PostalAddress": {"City": "基隆市", "Town": "中正區"},
             "WebsiteURL": "https://iocean.nmmst.gov.tw/", "IsAccessibleForFree": 1, "FeeInfo": "全票300元"},
            {"AttractionName": "菌寶貝博物館", "PostalAddress": {"City": "宜蘭縣", "Town": "宜蘭市"},
             "WebsiteURL": "https://www.junbaby.com.tw/", "IsAccessibleForFree": 1, "FeeInfo": "全館免費入場"},
            {"AttractionName": "免費停車另計園區", "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
             "WebsiteURL": "https://park.example/", "IsAccessibleForFree": 1, "FeeInfo": "免費停車，門票另計"},
            {"AttractionName": "臉書粉專景點", "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
             "WebsiteURL": "https://www.facebook.com/foo", "IsAccessibleForFree": 1, "FeeInfo": "免費"},
            {"AttractionName": "南部景點", "PostalAddress": {"City": "高雄市", "Town": "左營區"},
             "WebsiteURL": "https://kh.example/", "IsAccessibleForFree": 1, "FeeInfo": "免費"},
        ]
        parsed_attr = mod.parse_opendata_attractions(attr_items, ["基隆", "宜蘭", "新北"])
        by_name = {x["name"]: x for x in parsed_attr}
        self.assertEqual(set(by_name), {"潮境智能海洋館", "菌寶貝博物館", "免費停車另計園區"})
        self.assertFalse(by_name["潮境智能海洋館"]["no_fee_listed"])  # FeeInfo has 300元 -> must read live price
        self.assertTrue(by_name["菌寶貝博物館"]["no_fee_listed"])     # explicit free with no price
        self.assertFalse(by_name["免費停車另計園區"]["no_fee_listed"])  # "免費停車，門票另計" must NOT count as free

        # Tourism V2.1 Hotel parser: prioritizes hot springs, enforces no_fee_listed=False, respects skip_urls
        hotel_items = [
            {"HotelName": "一般商旅", "PostalAddress": {"City": "桃園市", "Town": "中壢區"},
             "WebsiteURL": "https://biz.example/", "Description": "商務住宿"},
            {"HotelName": "礁溪小旅宿", "PostalAddress": {"City": "宜蘭縣", "Town": "礁溪鄉"},
             "WebsiteURL": "https://near-spa.example/", "Description": "鄰近礁溪溫泉區步行5分鐘即可抵達"},
            {"HotelName": "礁溪溫泉飯店", "PostalAddress": {"City": "宜蘭縣", "Town": "礁溪鄉"},
             "WebsiteURL": "https://spa.example/", "Description": "館內溫泉與附設湯屋"},
            {"HotelName": "已測過旅館", "PostalAddress": {"City": "臺北市", "Town": "中正區"},
             "WebsiteURL": "https://checked.example/", "Description": "飯店"},
            {"HotelName": "HTTP舊站", "PostalAddress": {"City": "新北市", "Town": "淡水區"},
             "WebsiteURL": "http://insecure.example/", "Description": "飯店"},
        ]
        parsed_hot = mod.parse_opendata_hotels(hotel_items, ["桃園", "宜蘭", "台北", "新北"], skip_urls={"https://checked.example/"})
        by_h = {x["name"]: x for x in parsed_hot}
        self.assertEqual(parsed_hot[0]["name"], "礁溪溫泉飯店")
        self.assertEqual(by_h["礁溪溫泉飯店"]["kind"], "溫泉")
        self.assertEqual(by_h["礁溪溫泉飯店"]["area"], "礁溪")
        # Mentioning "鄰近礁溪溫泉區" in description must NOT tag a plain hotel as 溫泉
        self.assertEqual(by_h["礁溪小旅宿"]["kind"], "住宿")
        self.assertEqual(by_h["一般商旅"]["kind"], "住宿")
        self.assertFalse(by_h["一般商旅"]["no_fee_listed"])

        # harvest_checked remembers non-ok URLs for 30 days, expires older ones, and lets ok URLs refresh
        mod.write_places(self.db, [])
        mod.record_harvest_checked(self.db, [
            ("https://old.example/", "舊站", "no_price"),
            ("https://recent.example/", "近站", "no_price"),
            ("https://ok.example/", "成功站", "ok"),
        ], "2026-11-25")
        mod.record_harvest_checked(self.db, [("https://old.example/", "舊站", "no_price")], "2026-09-01")
        now_ts = time.mktime((2026, 11, 28, 12, 0, 0, 0, 0, -1))
        checked = mod.read_harvest_checked(self.db, max_age_days=30, now=now_ts)
        self.assertIn("https://recent.example/", checked)
        self.assertNotIn("https://old.example/", checked)
        self.assertNotIn("https://ok.example/", checked)

        # End-to-end harvest() flow with mocked Open Data ZIP + live reader: verifies dry-run, --no-opendata, and harvest_checked persistence
        from types import SimpleNamespace
        from unittest import mock

        mock_hotels = [
            {"HotelName": "真房價飯店", "PostalAddress": {"City": "宜蘭縣", "Town": "礁溪鄉"},
             "WebsiteURL": "https://good-stay.example/", "Description": "溫泉套房"},
            {"HotelName": "無價飯店", "PostalAddress": {"City": "宜蘭縣", "Town": "礁溪鄉"},
             "WebsiteURL": "https://no-price-stay.example/", "Description": "民宿"},
        ]
        def fake_fetch_zip(url, timeout=30):
            return [] if "Attraction" in url else mock_hotels

        def fake_read_page(url, args):
            if url == "https://good-stay.example/":
                return ("真房價飯店", ["暖陽雙人房｜NT$ 3,600 起／晚"], ["03-988-0001"], False), None
            return ("無價飯店", ["加床，平日$800元/人"], [], False), None

        empty_cat = self.root / "empty-cat.json"
        empty_cat.write_text('{"places": []}', encoding="utf-8")
        base_args = dict(
            places=self.db, history=self.history, hermes_root=self.root,
            html_file=None, now=now_ts, timeout=5, counties="宜蘭",
            catalog=str(empty_cat), max_per_county=5, max_attractions=5, max_hotels=5,
        )
        with mock.patch.object(mod, "crawl_taiwan_county", return_value=[]), \
             mock.patch.object(mod, "fetch_tourism_zip_json", side_effect=fake_fetch_zip), \
             mock.patch.object(mod, "read_page", side_effect=fake_read_page):
            # 1. --dry-run writes neither places nor harvest_checked
            self.assertIsNone(mod.harvest(SimpleNamespace(**base_args, opendata=True, dry_run=True)))
            self.assertEqual(mod.read_places(self.db), [])
            self.assertNotIn("https://no-price-stay.example/", mod.read_harvest_checked(self.db, now=now_ts))
            # 2. --no-opendata skips Open Data completely
            self.assertIsNotNone(mod.harvest(SimpleNamespace(**base_args, opendata=False, dry_run=False)))
            # 3. Real harvest persists good-stay in places and records no-price-stay in harvest_checked
            self.assertIsNone(mod.harvest(SimpleNamespace(**base_args, opendata=True, dry_run=False)))
            saved = mod.read_places(self.db)
            self.assertEqual([p["name"] for p in saved], ["真房價飯店"])
            self.assertIn("https://no-price-stay.example/", mod.read_harvest_checked(self.db, now=now_ts))

        # Quality filter rejects hotel surcharges/fines/subsidies and souvenir-shop shipping/cart prices
        self.assertEqual(
            mod.filter_opendata_price_lines("opendata_hotel", [
                "暖陽雙人房｜NT$ 3,600 起／晚",
                "加床，平日$800元/人",
                "退房時間11:00，延遲退房每小時$1000",
                "禁菸違規罰鍰新台幣10,000元",
                "2026旅展住宿券每張NT$3,500起",
            ]),
            ["暖陽雙人房｜NT$ 3,600 起／晚"],
        )
        self.assertEqual(
            mod.filter_opendata_price_lines("opendata_attraction", [
                "烏來台車｜全票單程50元",
                "0 個商品 - NT$0",
                "本島滿$3000免運費",
                "熱門商品｜NT$690",
            ]),
            ["烏來台車｜全票單程50元"],
        )

    def test_city_portal_and_web_discover(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest import mock

        spec = importlib.util.spec_from_file_location("places_mod", REPO / "scripts" / "places.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        now_ts = 1_800_000_000.0

        # 1. New Taipei official portal URL derivation & paid-attraction exclusion
        ntpc_records = [
            {
                "AttractionID": "Attraction_382000000A_110088",
                "AttractionName": "不厭亭",
                "WebsiteURL": "",
                "FeeInfo": "",
                "PostalAddress": {"City": "新北市", "Town": "雙溪區"},
            },
            {
                "AttractionID": "Attraction_382000000A_109992",
                "AttractionName": "陰陽海",
                "WebsiteURL": "",
                "FeeInfo": "免費參觀",
                "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
            },
            {
                "AttractionID": "Attraction_382000000A_999001",
                "AttractionName": "某收費秘境步道",
                "WebsiteURL": "",
                "FeeInfo": "全票門票200元",
                "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
            },
            {
                "AttractionID": "Attraction_382000000A_999004",
                "AttractionName": "某部分免費古道",
                "WebsiteURL": "",
                "FeeInfo": "需購買門票，兒童免費",
                "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
            },
            {
                "AttractionID": "Attraction_382000000A_999005",
                "AttractionName": "跳轉首頁假步道",
                "WebsiteURL": "",
                "FeeInfo": "",
                "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
            },
            {
                "AttractionID": "Attraction_382000000A_999002",
                "AttractionName": "某市立博物館",
                "WebsiteURL": "",
                "FeeInfo": "",
                "PostalAddress": {"City": "新北市", "Town": "板橋區"},
            },
            {
                "AttractionID": "Attraction_382000000A_999003",
                "AttractionName": "已有官網步道",
                "WebsiteURL": "https://already.example/",
                "FeeInfo": "",
                "PostalAddress": {"City": "新北市", "Town": "瑞芳區"},
            },
        ]
        city_cands = mod.parse_city_portal_attractions(ntpc_records, ["新北"], max_items=10)
        # Paid and partial-free ("需購買門票，兒童免費") trails are excluded; open spots are kept
        self.assertEqual([c["name"] for c in city_cands], ["跳轉首頁假步道", "陰陽海", "不厭亭"])
        by_city_name = {c["name"]: c for c in city_cands}
        self.assertEqual(by_city_name["不厭亭"]["price_page"], "https://newtaipei.travel/zh-tw/attractions/detail/110088")
        self.assertEqual(by_city_name["陰陽海"]["kind"], "海邊")
        self.assertTrue(by_city_name["不厭亭"]["no_fee_listed"])

        # Distinct newtaipei.travel IDs must NOT collapse, and homepage/404 redirects are rejected as bad_page
        empty_cat = self.root / "empty-cat-city.json"
        empty_cat.write_text('{"places": []}', encoding="utf-8")

        def fake_city_read(url, args):
            if "110088" in url:
                return ("不厭亭 | 新北市觀光旅遊網", [], ["02-2960-3456"], False), None
            if "999005" in url:
                return ("新北市觀光旅遊網", [], ["02-2960-3456"], False), None  # homepage redirect mismatch
            return ("陰陽海 | 新北市觀光旅遊網", [], ["02-2960-3456"], False), None

        city_args = SimpleNamespace(
            places=self.db, history=self.history, hermes_root=self.root,
            html_file=None, now=now_ts, timeout=5, counties="新北",
            catalog=str(empty_cat), max_per_county=5, max_attractions=0,
            max_city_spots=10, max_hotels=0, opendata=True, dry_run=False,
        )
        with mock.patch.object(mod, "crawl_taiwan_county", return_value=[]), \
             mock.patch.object(mod, "fetch_tourism_zip_json", return_value=ntpc_records), \
             mock.patch.object(mod, "read_page", side_effect=fake_city_read):
            self.assertIsNone(mod.harvest(city_args))
        saved_city = mod.read_places(self.db)
        self.assertEqual({p["name"] for p in saved_city}, {"不厭亭", "陰陽海"})
        self.assertIn("https://newtaipei.travel/zh-tw/attractions/detail/999005", mod.read_harvest_checked(self.db, now=now_ts))

        # 2. DuckDuckGo HTML parser + official venue URL filter + title cleaning
        ddg_html = (
            '<div class="results">'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.suispring.com%2Fspring&amp;rut=1">呆水溫泉｜礁溪湯屋</a>'
            '<a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=foo">廣告連結</a>'
            '</div>'
        )
        parser = mod._DDGResultParser()
        parser.feed(ddg_html)
        unwrapped = [(mod._unwrap_ddg_url(h), t) for h, t in parser.results]
        self.assertEqual(unwrapped[0], ("https://www.suispring.com/spring", "呆水溫泉｜礁溪湯屋"))
        self.assertTrue(mod.is_official_venue_url(*unwrapped[0]))
        self.assertFalse(mod.is_official_venue_url(*unwrapped[1]))

        self.assertTrue(mod.is_official_venue_url("https://www.suispring.com/", "呆水溫泉 Sui Spring｜礁溪溫泉"))
        self.assertTrue(mod.is_official_venue_url("https://www.kagaya.com.tw/spring/5f/", "日勝生加賀屋 - 5F 雙人湯屋"))
        self.assertTrue(mod.is_official_venue_url("https://www.gep.ntpc.gov.tw/x", "新北市立黃金博物館"))
        self.assertFalse(mod.is_official_venue_url("https://duckduckgo.com/y.js?ad=1", "廣告"))
        self.assertFalse(mod.is_official_venue_url("https://www.travel.taipei/zh-tw/attraction/details/1", "北投溫泉 | 臺北旅遊網"))
        self.assertFalse(mod.is_official_venue_url("https://www.klook.com/zh-TW/activity/123", "礁溪湯屋"))
        self.assertFalse(mod.is_official_venue_url("https://www.agoda.com/hotel/123", "礁溪飯店"))
        self.assertFalse(mod.is_official_venue_url("https://www.trivago.com.tw/zh-Hant-TW/odr/x", "淡水區飯店，起"))
        self.assertFalse(mod.is_official_venue_url("https://travel.pixnet.net/blog/post/1", "2026礁溪湯屋"))
        self.assertFalse(mod.is_official_venue_url("https://random-venue.tw/", "2026北投10間湯屋推薦懶人包"))
        self.assertFalse(mod.is_official_venue_url("https://dramastory2019.com/farm/", "富田休閒農場。2026門票資訊。餵可愛動物。"))
        self.assertFalse(mod.is_official_venue_url("https://www.osaka-hotel.jp/", "大阪溫泉飯店"))
        self.assertEqual(mod.clean_discovered_name("呆水溫泉 Sui Spring｜礁溪頂級溫泉旅宿"), "呆水溫泉")
        self.assertEqual(mod.clean_discovered_name("價目表 - 烏來璞石麗緻溫泉會館"), "烏來璞石麗緻溫泉會館")
        # Live-audit regressions: HTML entities, U+4E28/U+2502/hyphen separators, subpage-before-brand titles, leading 歡迎來到
        self.assertEqual(mod.clean_discovered_name("&#x7368;&#x7ACB;&#x6E6F;&#x5C4B; | 北投老爺酒店"), "北投老爺酒店")
        self.assertEqual(mod.clean_discovered_name("景觀溫泉/湯屋 &#8211; 陽明溫泉渡假村丨菁山遊憩區"), "陽明溫泉渡假村")
        self.assertEqual(mod.clean_discovered_name("北投湯屋首選⟫5F 雙人獨立湯屋 - 日勝生加賀屋國際溫泉飯店"), "日勝生加賀屋國際溫泉飯店")
        self.assertEqual(mod.clean_discovered_name("歡迎來到礁溪寒沐酒店"), "礁溪寒沐酒店")
        self.assertEqual(mod.clean_discovered_name("票價與交通資訊-埔心牧場"), "埔心牧場")
        self.assertEqual(mod.clean_discovered_name("Xpark │ 票價&購票"), "Xpark")
        self.assertEqual(mod.clean_discovered_name("某某溫泉 - 首頁"), "某某溫泉")
        self.assertFalse(any(s in "某某溫泉 - 首頁" for s in mod._DISCOVER_SKIP_TITLES))
        # FeeInfo refinement regressions: "不收費"/"無收費"/"部分路段濕滑" kept; "停車另收費" & "需購買門票，兒童免費" excluded
        self.assertFalse(mod._city_portal_is_paid("免費參觀"))
        self.assertFalse(mod._city_portal_is_paid("不收費"))
        self.assertFalse(mod._city_portal_is_paid("無收費"))
        self.assertFalse(mod._city_portal_is_paid("部分路段濕滑，免費開放"))
        self.assertTrue(mod._city_portal_is_paid("免費參觀，停車另收費"))
        self.assertTrue(mod._city_portal_is_paid("需購買門票，兒童免費"))
        # Area verification regressions: 02 phone alone never confirms district; loose body "台北" does not override Hualien address
        self.assertIsNone(mod._verify_discovered_area("神秘湯屋", "神秘湯屋", "", ["02-2891-1234"], "淡水"))
        self.assertIsNone(mod._verify_discovered_area("神秘湯屋", "神秘湯屋", "從台北出發約2小時，花蓮縣瑞穗鄉溫泉路1號", [], "宜蘭"))
        self.assertIsNone(mod._verify_discovered_area("神秘湯屋", "神秘湯屋", "地址：宜蘭縣礁溪鄉某路1號", ["04-2222-3333"], "礁溪"))
        self.assertEqual(mod._verify_discovered_area("斑比山丘", "斑比山丘", "歡迎光臨，宜蘭縣冬山鄉下湖路285號", [], "宜蘭"), "冬山")
        # Live-audit regression: third-person surcharge rules and COVID quarantine notices must not count as rates
        self.assertEqual(
            mod.filter_opendata_price_lines("opendata_hotel", [
                "5F 雙人獨立湯屋｜【兒童、第三、四人加價規定】第三人加價費用NT$800+10%/人",
                "資訊及公告｜本旅館為台北防疫旅館，僅提供居家檢疫者入住，每晚房價2500元起",
                "【湯屋價格】｜森山系列 │4坪│ 定價NT$2,300+10%元",
            ]),
            ["【湯屋價格】｜森山系列 │4坪│ 定價NT$2,300+10%元"],
        )
        # Live-audit regression: commuter stations and visitor service centers must not enter city_portal
        self.assertEqual(
            mod.parse_city_portal_attractions([
                {"AttractionID": "Attraction_382000000A_109635", "AttractionName": "板橋火車站旅遊服務中心",
                 "WebsiteURL": "", "FeeInfo": "", "PostalAddress": {"City": "新北市", "Town": "板橋區"}},
                {"AttractionID": "Attraction_382000000A_110901", "AttractionName": "板橋車站",
                 "WebsiteURL": "", "FeeInfo": "", "PostalAddress": {"City": "新北市", "Town": "板橋區"}},
            ], ["新北"], max_items=10),
            [],
        )

        # 3. End-to-end discover(): only real-priced, area-verified official venues saved
        def fake_ddg(query, timeout=12):
            return [
                ("https://www.klook.com/zh-TW/activity/99", "礁溪溫泉湯屋"),
                ("https://www.suispring.com/spring", "呆水溫泉 Sui Spring｜礁溪溫泉"),
                ("https://www.noprice-spa.example/spring", "沒寫價湯屋｜礁溪溫泉"),
                ("https://www.blog-list.example/", "2026礁溪10間必泡湯屋推薦"),
                ("https://www.qingjing-farm.example/ticket", "票價營業資訊-清境農場"),
                ("https://www.hualien-spa.example/spring", "神秘湯屋會館"),
                ("https://www.mommyblog.example/farm", "某秘密農場門票資訊"),
                ("https://www.js-ticket.example/ticket", "某水族館 │ 票價&購票"),
            ]

        def fake_discover_read(url, args):
            if "suispring.com" in url:
                return ("呆水溫泉 Sui Spring｜礁溪雙人湯屋", ["雙人湯屋｜平日 NT$ 2,800／90分鐘"], ["03-988-3566"], False), None
            if "qingjing-farm" in url:
                return ("票價營業資訊-清境農場", ["入園票價｜全票 270元"], [], False), None
            if "hualien-spa" in url:
                return ("神秘湯屋會館", ["雙人湯屋｜平日 NT$ 1,800／90分鐘"], [], False), None
            if "mommyblog" in url:
                return ("某秘密農場門票資訊", ["票價：全票200元，輸入KLOOK購票碼ABC99享優惠"], [], False), None
            if "js-ticket" in url:
                return ("某水族館 │ 票價&購票", [], [], True), None  # child-free notice on JS ticket page must NOT pass
            return ("沒寫價湯屋", ["免費停車", "加床平日$800元"], [], True), None

        def fake_page_text(args, url):
            if "hualien-spa" in url:
                return "神秘湯屋會館 地址：花蓮縣瑞穗鄉溫泉路一段1號"
            if "suispring.com" in url:
                return "呆水溫泉 地址：宜蘭縣礁溪鄉五峰路89-6號"
            return ""

        disc_args = SimpleNamespace(
            places=self.db, history=self.history, hermes_root=self.root,
            html_file=None, now=now_ts, timeout=5,
            max_queries=1, max_candidates=10, delay=0.0, offset=0, dry_run=True,
        )
        with mock.patch.object(mod, "ddg_search", side_effect=fake_ddg), \
             mock.patch.object(mod, "read_page", side_effect=fake_discover_read), \
             mock.patch.object(mod, "_read_page_text", side_effect=fake_page_text):
            # dry-run writes nothing
            self.assertIsNone(mod.discover(disc_args))
            self.assertEqual(len(mod.read_places(self.db)), 2)
            self.assertNotIn("https://www.noprice-spa.example/spring", mod.read_harvest_checked(self.db, now=now_ts))
            # real run saves 呆水溫泉 only, and records rejected candidates (incl. Hualien address mismatch) in harvest_checked
            disc_args.dry_run = False
            self.assertIsNone(mod.discover(disc_args))

        saved_all = mod.read_places(self.db)
        names = [p["name"] for p in saved_all]
        self.assertIn("呆水溫泉", names)
        self.assertNotIn("沒寫價湯屋", names)
        self.assertNotIn("清境農場", names)
        self.assertNotIn("神秘湯屋會館", names)
        self.assertNotIn("某秘密農場門票資訊", names)
        self.assertNotIn("某水族館", names)
        sui = next(p for p in saved_all if p["name"] == "呆水溫泉")
        self.assertEqual(sui["area"], "礁溪")
        self.assertEqual(sui["kind"], "溫泉")
        probed = mod.read_harvest_checked(self.db, now=now_ts)
        self.assertIn("https://www.noprice-spa.example/spring", probed)
        self.assertIn("https://www.qingjing-farm.example/ticket", probed)
        self.assertIn("https://www.hualien-spa.example/spring", probed)
        self.assertIn("https://www.mommyblog.example/farm", probed)
        self.assertIn("https://www.js-ticket.example/ticket", probed)

        # 4. Real non-mocked read_page + page_evidence cache integration test inside discover()
        hualien_html = self.root / "hualien-live.html"
        hualien_html.write_text(
            page("神秘山林湯屋", "<p>雙人湯屋｜平日 NT$ 1,800 / 90分鐘</p><p>地址：花蓮縣瑞穗鄉溫泉路一段1號</p>"),
            encoding="utf-8",
        )
        yilan_html = self.root / "yilan-live.html"
        yilan_html.write_text(
            page("山谷秘境湯屋", "<p>雙人湯屋｜平日 NT$ 2,200 / 90分鐘</p><p>地址：宜蘭縣礁溪鄉五峰路10號</p>"),
            encoding="utf-8",
        )
        for html_path, test_url, expect_saved in (
            (hualien_html, "https://hualien-real-cache.example/spa", False),
            (yilan_html, "https://yilan-real-cache.example/spa", True),
        ):
            live_args = SimpleNamespace(
                places=self.db, history=self.history, hermes_root=self.root,
                html_file=str(html_path), now=now_ts, timeout=5,
                max_queries=1, max_candidates=5, delay=0.0, offset=0, dry_run=False,
            )
            with mock.patch.object(mod, "ddg_search", return_value=[(test_url, "秘境湯屋")]):
                # NO mock on read_page and NO mock on _read_page_text: proves page_evidence cache actually carries the page text
                self.assertIsNone(mod.discover(live_args))
            names_now = [p["name"] for p in mod.read_places(self.db)]
            if expect_saved:
                self.assertIn("山谷秘境湯屋", names_now)
            else:
                self.assertNotIn("神秘山林湯屋", names_now)
                self.assertIn(test_url, mod.read_harvest_checked(self.db, now=now_ts))


if __name__ == "__main__":
    unittest.main()
