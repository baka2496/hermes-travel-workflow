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
