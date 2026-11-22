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
# Bytes (UTF-8). Raise one only after shortening something else in the same file.
ROLE_LIMITS = {"trip": 14_000, "travel-commander": 17_500, "schedule-manager": 13_500}
# Tools a role tells the agent to call; deferring one would hide it from the opening prompt.
ROLE_TOOLS = {"trip": ("terminal", "browser_exec", "clarify", "vision_analyze", "kanban_request_review",
                       "kanban_show", "kanban_complete", "read_file", "patch", "web_search"),
              "travel-commander": ("terminal", "browser_exec", "clarify", "vision_analyze", "kanban_create",
                                   "kanban_request_changes", "kanban_complete", "read_file", "patch"),
              "schedule-manager": ("terminal", "clarify", "read_file", "patch")}
HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles"
# The live profiles' own SOULs (characters): trip 8,115 and project-manager 9,702 after the slimming.
LIVE_SOUL_LIMITS = {"trip": 8_600, "project-manager": 10_200}


class RoleSizeTest(unittest.TestCase):
    def test_roles_stay_under_their_limits(self):
        for role, limit in ROLE_LIMITS.items():
            with self.subTest(role=role):
                size = len((ROOT / "roles" / f"{role}.md").read_bytes())
                self.assertLessEqual(size, limit, f"roles/{role}.md is {size} bytes: tighten an old rule first")

    def test_card_rules_keep_their_point_without_the_whole_story(self):
        text = (ROOT / "roles" / "trip.md").read_text(encoding="utf-8")
        for rule in ("不寫航空公司首頁價或自己估的「約」價", "已含它算的托運行李，不要再加", "要寫「未含托運行李」",
                     "最多重跑一次 `plan`", "不要讀 trip-bot 的程式碼、cache 或設定來除錯", "也不要自己挑沒查過價格的飯店"):
            self.assertIn(rule, text)
        longest = max(len(line) for line in text.splitlines())
        self.assertLess(longest, 1_300, "one paragraph of trip.md grew back into a wall of text")


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
                text = (ROOT / "roles" / f"{role}.md").read_text(encoding="utf-8")
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
