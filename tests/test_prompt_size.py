"""Every agent's opening prompt stays small (2026-11-22: 「做說明書瘦身」).

Trip opened each card with ~37K tokens (tool descriptions 15.6K, its role 7.9K, auto-loaded
skills 6.1K, a 60-skill index 2.7K) and still compressed twice in one 8-minute card. A new rule
goes in by tightening an old one, not by growing the file past these limits.
"""
import os
from pathlib import Path
import unittest

import setup

ROOT = Path(setup.__file__).parent
# Bytes (UTF-8) after expanding shared includes. V2 keeps roles genuinely slim (< 6.5 KB).
ROLE_LIMITS = {"trip": 6_000, "travel-commander": 6_500, "schedule-manager": 4_000}
# Tools a role tells the agent to call; deferring one would hide it from the opening prompt.
ROLE_TOOLS = {"trip": ("terminal", "browser_exec", "clarify", "vision_analyze", "kanban_request_review",
                       "kanban_show", "kanban_complete", "read_file", "patch", "web_search"),
              "travel-commander": ("terminal", "browser_exec", "clarify", "vision_analyze", "kanban_create",
                                   "kanban_request_changes", "kanban_complete", "read_file", "patch"),
              "schedule-manager": ("terminal", "clarify", "read_file", "patch")}
HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles"
# The live profiles' own SOULs (characters): slimmed down >60% in V2.
LIVE_SOUL_LIMITS = {"trip": 4_000, "project-manager": 4_500}


class RoleSizeTest(unittest.TestCase):
    def test_roles_stay_under_their_limits(self):
        for role, limit in ROLE_LIMITS.items():
            with self.subTest(role=role):
                expanded = setup.expand_includes((ROOT / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                size = len(expanded.encode("utf-8"))
                self.assertLessEqual(size, limit, f"roles/{role}.md (expanded) is {size} bytes: tighten an old rule first")

    def test_schedule_manager_does_not_carry_price_or_photo_rules(self):
        expanded = setup.expand_includes((ROOT / "roles" / "schedule-manager.md").read_text(encoding="utf-8"))
        for unused in ("page-prices.py", "place-photo.py", "booking-links.py"):
            self.assertNotIn(unused, expanded)

    def test_travel_planning_skill_uses_vision_photo_flow(self):
        skill = (ROOT / "skills" / "travel-planning" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("vision_analyze", skill)
        self.assertNotIn("先 `--list`", skill)

    def test_card_rules_keep_their_point_without_the_whole_story(self):
        text = setup.expand_includes((ROOT / "roles" / "trip.md").read_text(encoding="utf-8"))
        for rule in ("travel.py overseas", 'travel.py" local', "不讀 `trip-bot` 程式碼除錯", "未確認"):
            self.assertIn(rule, text)
        longest = max(len(line) for line in text.splitlines())
        self.assertLess(longest, 500, "one paragraph of trip.md grew back into a wall of text")


class ToolOutputSizeTest(unittest.TestCase):
    def test_existing_cards_are_listed_as_text(self):
        # 2026-11-22: `kanban list --json` printed the whole board (57,736 characters in the reply,
        # 118K by then) and compressed a 3-minute hand-off; the text list is 3.7K and has every card.
        for path in [ROOT / "roles" / "travel-commander.md",
                     *([HERMES / "project-manager" / "SOUL.md"] if (HERMES / "project-manager" / "SOUL.md").is_file() else [])]:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("list --sort created-desc", text)
                self.assertIn("不加 `--json`", text)
                self.assertNotIn("list --json", text)


class DeferredToolsTest(unittest.TestCase):
    def test_unused_tools_wait_behind_tool_search(self):
        for profile in setup.PROFILES:
            with self.subTest(profile=profile):
                deferred = setup.DEFERRED_TOOLS[profile]
                # An explicit list replaces Hermes' default, so the default must stay in it.
                self.assertTrue(set(setup.HERMES_DEFERRED) <= set(deferred))
                for tool in ("delegate_task", "skill_manage", "memory", "kanban_heartbeat"):
                    self.assertIn(tool, deferred)
                for tool in ROLE_TOOLS[profile]:
                    self.assertNotIn(tool, deferred, f"{profile}'s role tells it to call {tool}")
        self.assertIn("kanban_create", setup.DEFERRED_TOOLS["trip"])
        self.assertIn("kanban_request_changes", setup.DEFERRED_TOOLS["trip"])

    def test_a_deferred_tool_the_role_names_says_how_to_reach_it(self):
        for role in ROLE_LIMITS:
            with self.subTest(role=role):
                text = setup.expand_includes((ROOT / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                if "用 `memory` 記到 `user`" in text:
                    self.assertIn("memory 是延後載入的工具，先用 `tool_search` 找到它", text)

    def test_setup_writes_the_list(self):
        source = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"tools.tool_search.defer"', source)
        self.assertIn("json.dumps(list(DEFERRED_TOOLS[profile]))", source)


@unittest.skipUnless((HERMES / "trip" / "SOUL.md").is_file(), "no live Hermes profiles on this machine")
class LiveProfilesTest(unittest.TestCase):
    """The running bots (their profiles are not installed by setup.py)."""

    def test_live_souls_stay_small(self):
        for profile, limit in LIVE_SOUL_LIMITS.items():
            with self.subTest(profile=profile):
                size = len((HERMES / profile / "SOUL.md").read_text(encoding="utf-8").replace("\r", ""))
                self.assertLessEqual(size, limit, f"{profile} SOUL is {size} characters: tighten an old rule first")

    def test_live_souls_are_built_from_the_repo(self):
        # 2026-11-26: the hand-kept SOULs had drifted (the live trip still pasted trip-cost.py by hand).
        # Fix a difference with `py -3 setup.py live-souls --root <hermes> --write` (it backs up first).
        import re
        for profile in setup.LIVE_SOULS:
            head = HERMES / profile / setup.LIVE_HEAD
            if not head.is_file():
                continue
            with self.subTest(profile=profile):
                current = (HERMES / profile / "SOUL.md").read_bytes().decode("utf-8").replace("\r\n", "\n")
                # The interpreter that runs the travel tools, not another one the SOUL names (the academic venv).
                python = re.search(r'"([^"]*python[^"]*\.exe)" "[^"]*/shared/travel-workflow/', current)
                wanted = setup.live_soul(head, HERMES.parent, python.group(1) if python else None)
                self.assertEqual(current, wanted, f"{profile} SOUL.md drifted from SOUL.head.md + roles/_shared")

    @staticmethod
    def yaml_list(profile, *path):
        """The `- item` list at a key path of a profile's config.yaml (no PyYAML on this machine)."""
        lines = (HERMES / profile / "config.yaml").read_text(encoding="utf-8").splitlines()
        depth, i = 0, 0
        for key in path:
            want = f"{'  ' * depth}{key}:"
            while lines[i].rstrip() != want:
                i += 1
                if i == len(lines):
                    raise AssertionError(f"{profile} config.yaml has no {'.'.join(path)}")
            i, depth = i + 1, depth + 1
        items = []
        while i < len(lines) and lines[i].lstrip().startswith("- ") and len(lines[i]) - len(lines[i].lstrip()) >= 2 * (depth - 1):
            items.append(lines[i].strip()[2:].strip("'\""))
            i += 1
        return items

    def test_live_profiles_defer_their_unused_tools(self):
        wanted = {"trip": ("kanban_create", "delegate_task", "skill_manage", "memory", "kanban_request_changes",
                           "kanban_link", "kanban_attach", "kanban_attach_url", "kanban_heartbeat"),
                  "project-manager": ("delegate_task", "skill_manage", "memory"),
                  "commander": ("kanban_link", "kanban_attach", "kanban_attach_url", "kanban_heartbeat")}
        for profile, tools in wanted.items():
            with self.subTest(profile=profile):
                deferred = set(self.yaml_list(profile, "tools", "tool_search", "defer"))
                self.assertTrue(set(setup.HERMES_DEFERRED) <= deferred)
                self.assertTrue(set(tools) <= deferred)
                self.assertFalse({"terminal", "kanban_request_review", "kanban_complete", "clarify"} & deferred)

    def test_trip_carries_only_travel_skills_on_cards_and_telegram(self):
        auto_load = self.yaml_list("trip", "skills", "auto_load")
        self.assertNotIn("coding-delegate", auto_load)  # Trip is told not to debug code
        self.assertIn("user-action-confirmation", auto_load)
        for platform in ("cli", "telegram"):
            with self.subTest(platform=platform):
                hidden = set(self.yaml_list("trip", "skills", "platform_disabled", platform))
                self.assertTrue({"coding-delegate", "songwriting-and-ai-music", "test-driven-development"} <= hidden)
                self.assertFalse({"flight-deals", "travel-planning", "travel-booking", "preference-workflow",
                                  "secure-vault", "user-action-confirmation", "maps"} & hidden)


if __name__ == "__main__":
    unittest.main()
