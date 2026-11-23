import datetime
import importlib.util
import json
from pathlib import Path
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_opportunities", REPO / "scripts/archive-opportunities.py")
arch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arch)


class ArchiveOpportunitiesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-archive-opps-"))
        self.opps = self.root / "opportunities.json"
        self.today = datetime.date(2026, 11, 23)
        self.records = [
            {"id": "opp-old-past", "status": "可行候選", "depart_date": "2026-09-10", "return_date": "2026-09-12"},
            {"id": "opp-expired", "status": "已失效", "depart_date": "2026-12-20"},
            {"id": "opp-old-pending", "status": "已失效", "depart_date": "2026-08-01",
             "pending_question": {"question": "still waiting"}},
            {"id": "opp-recent-past", "status": "已確認成行", "depart_date": "2026-11-10"},
            {"id": "opp-upcoming", "status": "待評估", "travel_window": "2026-12-14 ~ 2026-12-15"},
        ]
        self.opps.write_text(json.dumps({"schema_version": 1, "opportunities": self.records}, ensure_ascii=False),
                             encoding="utf-8")

    def test_dry_run_reports_counts_without_changing_files(self):
        kept, archived, archive_path = arch.archive_file(self.opps, days=30, today=self.today, dry_run=True)
        self.assertEqual((kept, archived), (3, 2))
        self.assertFalse(archive_path.exists())
        data = json.loads(self.opps.read_text(encoding="utf-8"))
        self.assertEqual(len(data["opportunities"]), 5)

    def test_archives_expired_and_old_records_keeping_pending_and_recent(self):
        kept, archived, archive_path = arch.archive_file(self.opps, days=30, today=self.today)
        self.assertEqual((kept, archived), (3, 2))
        main_ids = [r["id"] for r in json.loads(self.opps.read_text(encoding="utf-8"))["opportunities"]]
        arch_ids = [r["id"] for r in json.loads(archive_path.read_text(encoding="utf-8"))["opportunities"]]
        self.assertEqual(main_ids, ["opp-old-pending", "opp-recent-past", "opp-upcoming"])
        self.assertEqual(arch_ids, ["opp-old-past", "opp-expired"])

        # A second archive run appends to the existing archive without losing earlier entries.
        kept2, archived2, _ = arch.archive_file(self.opps, days=5, today=self.today)
        self.assertEqual((kept2, archived2), (2, 1))
        arch_ids2 = [r["id"] for r in json.loads(archive_path.read_text(encoding="utf-8"))["opportunities"]]
        self.assertEqual(arch_ids2, ["opp-old-past", "opp-expired", "opp-recent-past"])


if __name__ == "__main__":
    unittest.main()
