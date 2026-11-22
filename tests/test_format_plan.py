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
        self.assertIn("12/14 礁溪泡湯 2人", out)
        self.assertIn("總計 NT$699，預算剩 NT$1,301", out)
        self.assertIn("• 雙人湯屋 60分鐘 NT$699", out)
        self.assertIn(f"MEDIA:{self.photo.as_posix()}", out)
        self.assertIn("📷 雙人湯屋 60分鐘（spring.example）", out)
        self.assertIn("📞 03-9881892", out)
        self.assertIn("Google 地圖・查空房", out)

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


if __name__ == "__main__":
    unittest.main()
