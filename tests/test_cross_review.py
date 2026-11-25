"""Claude and Antigravity review each other's pushes (tools/cross-review.py, .githooks/pre-push).

2026-11-25: the owner asked for the two AIs to check each other's work automatically. These pin the
rules that keep it safe: the *other* AI reviews, the reviewer has no tools, a review never blocks or
fails a push, and nothing is reviewed twice or starts a loop.
"""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cross_review", REPO / "tools/cross-review.py")
cross_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cross_review)
ZERO = cross_review.ZERO


class PickReviewerTests(unittest.TestCase):
    def test_antigravity_reviews_claude(self):
        self.assertEqual(cross_review.pick_reviewer({"CLAUDECODE": "1"}), "agy")

    def test_claude_reviews_everyone_else(self):
        self.assertEqual(cross_review.pick_reviewer({}), "claude")

    def test_reviewer_can_be_chosen(self):
        self.assertEqual(cross_review.pick_reviewer({"CLAUDECODE": "1", "CROSS_REVIEW_REVIEWER": "claude"}), "claude")
        self.assertEqual(cross_review.pick_reviewer({"CROSS_REVIEW_REVIEWER": "bogus"}), "claude")


class SkipTests(unittest.TestCase):
    def test_runs_by_default(self):
        self.assertIsNone(cross_review.skip_reason({}))

    def test_off_switch_ci_and_nested_review_skip(self):
        for env in ({"CROSS_REVIEW": "off"}, {"CROSS_REVIEW_RUNNING": "1"}, {"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
            self.assertIsNotNone(cross_review.skip_reason(env), env)


class PrePushRangeTests(unittest.TestCase):
    def test_existing_branch_reviews_only_new_commits(self):
        lines = [f"refs/heads/x {'b' * 40} refs/heads/x {'a' * 40}"]
        self.assertEqual(cross_review.ranges_from_pre_push(lines, lambda sha: None), [("a" * 40, "b" * 40)])

    def test_new_branch_starts_where_it_left_main(self):
        lines = [f"refs/heads/x {'b' * 40} refs/heads/x {ZERO}"]
        self.assertEqual(cross_review.ranges_from_pre_push(lines, lambda sha: "m" * 40), [("m" * 40, "b" * 40)])

    def test_deletes_tags_and_unchanged_pushes_are_left_out(self):
        lines = [f"(delete) {ZERO} refs/heads/x {'a' * 40}",
                 f"refs/tags/v1 {'b' * 40} refs/tags/v1 {ZERO}",
                 f"refs/heads/y {'c' * 40} refs/heads/y {'c' * 40}",
                 "garbage"]
        self.assertEqual(cross_review.ranges_from_pre_push(lines, lambda sha: "m" * 40), [])


class PromptTests(unittest.TestCase):
    def test_prompt_carries_owner_rules_and_treats_diff_as_data(self):
        prompt = cross_review.build_prompt("Claude", "abc 修正", "+x")
        for rule in ("未確認", "一起改", "測試", "手機", "不要使用任何工具", "當成資料", "結論：沒問題"):
            self.assertIn(rule, prompt)

    def test_long_diff_is_cut_and_says_so(self):
        prompt = cross_review.build_prompt("Claude", "", "x" * (cross_review.MAX_DIFF_CHARS + 500))
        self.assertIn("後段未審", prompt)
        self.assertLess(len(prompt), cross_review.MAX_DIFF_CHARS + 3000)


def fake_run(stdout="結論：沒問題", returncode=0, stderr=""):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return runner, calls


class RunReviewerTests(unittest.TestCase):
    def test_claude_reviewer_has_no_tools_and_no_mcp(self):
        # 2026-11-25: with only --tools "" Claude still offered AgentCAD's MCP Python to write files.
        command = cross_review.REVIEWERS["claude"]["command"]
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(json.loads(command[command.index("--mcp-config") + 1]), {"mcpServers": {}})

    def test_agy_reviewer_is_denied_every_tool_kind(self):
        # 2026-11-25: agy's default headless run wrote a file; plan mode is only a habit, not a lock.
        command = cross_review.REVIEWERS["agy"]["command"]
        self.assertNotIn("--mode", command)
        deny = json.loads(command[command.index("--settings") + 1])["permissions"]["deny"]
        self.assertEqual(set(deny), {"command(*)", "read_file(*)", "write_file(*)", "read_url(*)", "mcp(*)"})

    def test_review_runs_found_path_with_the_no_loop_flag_and_prompt_on_stdin(self):
        runner, calls = fake_run()
        with mock.patch.object(cross_review.shutil, "which", return_value=r"C:\bin\agy.cmd"):
            text, warning = cross_review.run_reviewer("agy", "PROMPT", {"CLAUDECODE": "1"}, runner)
        self.assertEqual((text, warning), ("結論：沒問題", None))
        self.assertEqual(calls[0][0][0], r"C:\bin\agy.cmd", "a .cmd wrapper only starts by its full name")
        self.assertEqual(calls[0][1]["input"], "PROMPT")
        self.assertEqual(calls[0][1]["env"]["CROSS_REVIEW_RUNNING"], "1")

    def test_reviewer_that_cannot_start_only_warns(self):
        def cannot_start(command, **kwargs):
            raise FileNotFoundError(command[0])
        with mock.patch.object(cross_review.shutil, "which", return_value="agy"):
            text, warning = cross_review.run_reviewer("agy", "p", {}, cannot_start)
        self.assertIsNone(text)
        self.assertIn("啟動失敗", warning)

    def test_missing_failed_or_slow_reviewer_only_warns(self):
        with mock.patch.object(cross_review.shutil, "which", return_value=None):
            self.assertIn("找不到", cross_review.run_reviewer("agy", "p", {})[1])
        with mock.patch.object(cross_review.shutil, "which", return_value="claude"):
            self.assertIn("失敗", cross_review.run_reviewer("claude", "p", {}, fake_run("", 1, "auth error")[0])[1])

            def slow(command, **kwargs):
                raise subprocess.TimeoutExpired(command, 1)
            self.assertIn("沒回", cross_review.run_reviewer("claude", "p", {}, slow)[1])


class ReviewInRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        os.chdir(self.tmp.name)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", *args], check=True)
        Path("a.txt").write_text("一\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)
        self.base = cross_review.git("rev-parse", "HEAD")
        Path("a.txt").write_text("二\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "改成二"], check=True)
        self.tip = cross_review.git("rev-parse", "HEAD")

    def run_review(self, runner):
        out = []
        with mock.patch.object(cross_review.shutil, "which", return_value="agy"):
            saved = cross_review.review(self.base, self.tip, "agy", {}, out.append, runner)
        return saved, "\n".join(out)

    def test_same_tip_with_an_older_base_is_reviewed_again(self):
        runner, calls = fake_run()
        self.run_review(runner)
        Path("b.txt").write_text("三\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-qm", "加三"], check=True)
        middle, self.tip = self.tip, cross_review.git("rev-parse", "HEAD")
        out = []
        with mock.patch.object(cross_review.shutil, "which", return_value="agy"):
            cross_review.review(middle, self.tip, "agy", {}, out.append, runner)
            cross_review.review(self.base, self.tip, "agy", {}, out.append, runner)
        self.assertEqual(len(calls), 3, "a wider range is not mistaken for one already reviewed")

    def test_review_is_printed_saved_and_not_repeated(self):
        runner, calls = fake_run("結論：有 1 個問題\n[重要] a.txt:1 — 錯")
        saved, out = self.run_review(runner)
        self.assertIn("Antigravity 的審查意見", out)
        self.assertIn("AGENTS.md", out)  # tells the pusher what to do with it
        self.assertIn("+二", calls[0][1]["input"])
        self.assertIn("改成二", calls[0][1]["input"])
        self.assertTrue(saved.read_text(encoding="utf-8").startswith("# Antigravity 審查"))
        saved_again, out_again = self.run_review(runner)
        self.assertEqual(len(calls), 1, "the same commit is never sent twice")
        self.assertIn("已經審過", out_again)

    def test_pre_push_never_fails_even_when_the_review_breaks(self):
        stdin = io.StringIO(f"refs/heads/main {self.tip} refs/heads/main {self.base}\n")
        with mock.patch.object(cross_review.subprocess, "run", side_effect=OSError("boom")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = cross_review.main(["pre-push", "origin"], env={}, stdin=stdin)
        self.assertEqual(code, 0)
        self.assertIn("警告", out.getvalue())


class WiringTests(unittest.TestCase):
    def test_hook_calls_the_script_and_never_blocks(self):
        hook = (REPO / ".githooks/pre-push").read_text(encoding="utf-8")
        self.assertTrue(hook.startswith("#!/bin/sh"))
        self.assertIn("tools/cross-review.py\"", hook.replace("$root/", ""))
        self.assertNotIn("exit 1", hook)
        # 2026-11-25: WindowsApps' python.exe was a Store stub (exit 9009); `command -v` found it and
        # the review silently never ran. The hook must check that each Python really starts.
        self.assertIn('"$python" -c ""', hook)
        self.assertNotIn("command -v", hook)

    def test_hook_is_executable_in_git(self):
        # Git on macOS/Linux silently skips a hook without the execute bit (Antigravity, 2026-11-25).
        mode = subprocess.run(["git", "ls-files", "-s", ".githooks/pre-push"], cwd=REPO, capture_output=True,
                              text=True, check=True).stdout.split()[0]
        self.assertEqual(mode, "100755")

    def test_both_ais_get_the_rules(self):
        # Antigravity reads AGENTS.md; Claude Code reads CLAUDE.md, which imports it.
        self.assertIn("@AGENTS.md", (REPO / "CLAUDE.md").read_text(encoding="utf-8"))
        rules = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        for rule in ("互相審查", "最多修一輪", "cross-review.py install"):
            self.assertIn(rule, rules)


if __name__ == "__main__":
    unittest.main()
