"""The price format lives in scripts/travel_money.py alone (2026-11-20 refactor).

trip-cost.py prints amounts and the reply-check plugin reads them back; page_evidence.py and the
plugin's record reminder both recognise prices. Each had its own copy of the patterns, and the
copies had drifted (the plugin missed 「NTD 600」 and 「600 TWD」). These tests keep it one copy.
"""
import importlib.util
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import travel_money  # noqa: E402

PLUGIN = REPO / "plugins" / "travel-reply-check" / "__init__.py"
SHARED = REPO / "scripts" / "travel_money.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FormatTests(unittest.TestCase):
    def test_money_round_trips_through_amounts(self):
        for value in (0, 60, 1600, 12345, 1234567):
            self.assertEqual(travel_money.amounts(f"總計 {travel_money.money(value)}"), [travel_money.money(value)])

    def test_price_finds_every_written_currency(self):
        for text in ("NT$600", "NT$ 1,600", "NTD 600", "TWD600", "$600", "600元", "600 元", "600 TWD"):
            self.assertTrue(travel_money.PRICE.search(text), text)
        for text in ("60分鐘", "2026-11-20", "第3間"):
            self.assertFalse(travel_money.PRICE.search(text), text)


class OneCopyTests(unittest.TestCase):
    """The rule the refactor set: no file writes its own currency pattern or NT$ format."""

    def test_no_other_file_writes_the_currency(self):
        hand_written = re.compile(r"NT\\\$|f\"NT\$\{|f'NT\$\{")
        found = []
        for path in [*sorted((REPO / "scripts").glob("*.py")), PLUGIN, REPO / "setup.py"]:
            if path == SHARED:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if hand_written.search(code):
                    found.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
        self.assertEqual(found, [], "use travel_money (PRICE, AMOUNT_PATTERN, CURRENCY_BEFORE, money) instead")

    def test_tools_share_the_same_objects(self):
        evidence = load(REPO / "scripts" / "page_evidence.py", "page_evidence_one_copy")
        self.assertEqual(evidence.PRICE.pattern, travel_money.PRICE.pattern)
        plugin = load(PLUGIN, "reply_check_one_copy")
        self.assertEqual(plugin.RECORD_PRICE.pattern, travel_money.PRICE.pattern)


class InstalledPluginTests(unittest.TestCase):
    """setup.py copies travel_money.py into the plugin folder; the plugin must load from there."""

    def installed(self, with_money):
        # Retain fixtures: the user's policy forbids recursive cleanup.
        folder = Path(tempfile.mkdtemp(prefix="hermes-plugin-load-")) / "profiles" / "p" / "plugins" / "travel-reply-check"
        folder.mkdir(parents=True)
        shutil.copyfile(PLUGIN, folder / "__init__.py")
        shutil.copyfile(REPO / "scripts" / "hermes_sessions.py", folder / "hermes_sessions.py")
        if with_money:
            shutil.copyfile(SHARED, folder / "travel_money.py")
        return folder

    def test_loads_from_the_installed_copy(self):
        folder = self.installed(with_money=True)
        plugin = load(folder / "__init__.py", "reply_check_installed")
        self.assertEqual(Path(plugin.money.__file__).resolve(), (folder / "travel_money.py").resolve())

    def test_missing_copy_fails_at_load(self):
        folder = self.installed(with_money=False)
        with self.assertRaises(ImportError):
            load(folder / "__init__.py", "reply_check_missing")

    def test_setup_installs_it_in_both_places(self):
        text = (REPO / "setup.py").read_text(encoding="utf-8")
        self.assertIn('SHARED_MODULES = ("travel_money.py", "hermes_sessions.py")', text)
        self.assertIn('f"shared/travel-workflow/{name}") for name in SHARED_MODULES', text)
        self.assertIn('(PACKAGE / "scripts" / name).read_bytes()', text)
        plugin = load(PLUGIN, "reply_check_shared")
        self.assertEqual(sorted(f"{name}.py" for name in plugin.SHARED), sorted(load(REPO / "setup.py", "setup_shared").SHARED_MODULES))


class BehaviourTests(unittest.TestCase):
    def test_record_reminder_now_sees_ntd_and_twd(self):
        plugin = load(PLUGIN, "reply_check_records")
        for price in ("NTD 600", "600 TWD"):
            result = f'{{"note": "湯屋 {price}"}}'
            noted = plugin.record_note("read_file", {"path": "shared/travel-workflow/opportunities.json"}, result)
            self.assertIsNotNone(noted, price)

    def test_trip_cost_accepts_any_written_currency(self):
        cost = load(REPO / "scripts" / "trip-cost.py", "trip_cost_currency")
        for item in ("餐飲=800", "餐飲=NT$800", "餐飲=NTD 800", "餐飲=TWD800", "餐飲=$800"):
            self.assertEqual(cost.ITEM.match(item)["price"], "800", item)


if __name__ == "__main__":
    unittest.main()
