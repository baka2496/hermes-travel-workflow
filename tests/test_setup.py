import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest

import scratch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workflow_setup", REPO / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class FakeHermes:
    """Stands in for the Hermes CLI by editing each profile's jobs.json."""

    def __init__(self, root):
        self.root = root
        self.calls = []

    def path(self, profile):
        return self.root / "profiles" / profile / "cron" / "jobs.json"

    def add(self, profile, job_id, name, **fields):
        path = self.path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"jobs": []}
        data["jobs"].append({"id": job_id, "name": name, "enabled": False, "state": "paused",
                             "deliver": "local", "last_status": None, "last_error": None,
                             "last_delivery_error": None, "failure_streak": 0, **fields})
        path.write_text(json.dumps(data), encoding="utf-8")

    def __call__(self, root, *args):
        self.calls.append(args)
        profile, verb, job_id = args[1], args[3], args[4]
        path = self.path(profile)
        data = json.loads(path.read_text(encoding="utf-8"))
        job = next(j for j in data["jobs"] if j["id"] == job_id)
        if verb == "resume":
            job.update(enabled=True, state="scheduled")
        elif verb == "pause":
            job.update(enabled=False, state="paused")
        elif verb == "edit":
            job["deliver"] = args[args.index("--deliver") + 1]
        path.write_text(json.dumps(data), encoding="utf-8")
        return ""


class SetupCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-setup-test-"))
        (self.root / ".travel-workflow-install.json").write_text(json.dumps({"version": 1, "profiles": {}}), encoding="utf-8")
        self.hermes = FakeHermes(self.root)
        self.hermes.add("trip", "s1", "travel-workflow/scanner")
        self.hermes.add("travel-commander", "w1", "travel-workflow/weekly")
        self.hermes.add("travel-commander", "a1", "travel-workflow/alerts")
        self.hermes.add("travel-commander", "x1", "someone-else/job")
        self.original_run, self.original_serving = setup.run, setup.gateway_serving
        setup.run = self.hermes
        self.serving = False
        setup.gateway_serving = lambda root, profile: self.serving

    def tearDown(self):
        setup.run, setup.gateway_serving = self.original_run, self.original_serving

    def ready_runtime(self):
        scanner = self.root / "scanner"
        (scanner / "node_modules/tsx/dist").mkdir(parents=True)
        (scanner / "node_modules/tsx/dist/cli.mjs").write_text("", encoding="utf-8")
        runtime = self.root / "shared/travel-workflow/runtime.json"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(json.dumps({"scanner_dir": str(scanner), "node": __file__}), encoding="utf-8")

    def test_check_flags_running_jobs_without_a_gateway(self):
        self.ready_runtime()
        status, report = self.quiet(setup.check, self.root)
        self.assertEqual(status, 0, report)
        self.assertIn("gateway: not running", report)
        self.quiet(setup.set_state, self.root, "resume")
        status, report = self.quiet(setup.check, self.root)
        self.assertEqual(status, 1)
        self.assertIn("will not run until a Hermes gateway", report)
        self.serving = True
        status, report = self.quiet(setup.check, self.root)
        self.assertEqual(status, 0, report)
        self.assertIn("gateway: running", report)

    def quiet(self, function, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = function(*args)
        return result, out.getvalue()

    def receipt(self):
        return json.loads((self.root / ".travel-workflow-install.json").read_text(encoding="utf-8"))

    def test_resume_and_pause_touch_only_managed_jobs_once(self):
        self.quiet(setup.set_state, self.root, "resume")
        self.assertEqual(sorted(c[4] for c in self.hermes.calls), ["a1", "s1", "w1"])
        self.quiet(setup.set_state, self.root, "resume")
        self.assertEqual(len(self.hermes.calls), 3)
        self.quiet(setup.set_state, self.root, "pause")
        self.assertEqual([c[3] for c in self.hermes.calls[3:]], ["pause"] * 3)

    def test_delivery_changes_digest_jobs_and_is_remembered(self):
        self.quiet(setup.set_delivery, self.root, "telegram")
        self.assertEqual(sorted(c[4] for c in self.hermes.calls), ["a1", "w1"])
        self.assertEqual(self.receipt()["delivery"], "telegram")
        self.assertEqual(setup.delivery_for(self.receipt(), None), "telegram")
        self.assertEqual(setup.delivery_for({"version": 1}, None), "local")
        self.assertEqual(setup.delivery_for(self.receipt(), "local"), "local")

    def test_unmanaged_root_is_refused(self):
        other = Path(scratch.new_dir("hermes-setup-unmanaged-"))
        with self.assertRaisesRegex(RuntimeError, "not managed"):
            setup.set_state(other, "resume")
        self.assertEqual(self.hermes.calls, [])

    def test_check_reports_missing_dependencies_and_failing_jobs(self):
        runtime = self.root / "shared/travel-workflow/runtime.json"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(json.dumps({"scanner_dir": str(self.root / "no-scanner"), "node": str(self.root / "no-node")}), encoding="utf-8")
        data = json.loads(self.hermes.path("travel-commander").read_text(encoding="utf-8"))
        data["jobs"][1].update(last_status="error", last_error="model quota", failure_streak=3)
        self.hermes.path("travel-commander").write_text(json.dumps(data), encoding="utf-8")
        status, report = self.quiet(setup.check, self.root)
        self.assertEqual(status, 1)
        self.assertIn("scanner dependencies", report)
        self.assertIn("node", report)
        self.assertIn("travel-workflow/alerts", report)
        self.assertIn("model quota", report)
        self.assertIn("paused", report)
        self.assertEqual(self.hermes.calls, [])


class ProfileSettingsTests(unittest.TestCase):
    def test_new_profiles_do_not_ping_the_chat_after_each_restart(self):
        # 2026-11-12: "他還是會一直傳訊息" — every bot sent "♻️ 閘道已上線" after each gateway restart.
        self.assertIn(("telegram.gateway_restart_notification", "false"), setup.PROFILE_SETTINGS)

    def test_a_fresh_conversation_starts_each_day_and_picks_up_open_questions(self):
        # 2026-11-12: one Telegram chat grew past 100K tokens and kept compressing.
        # 2026-11-20: a chat open all day compressed twice in one 6-minute reply.
        self.assertIn(("session_reset.mode", "both"), setup.PROFILE_SETTINGS)
        self.assertIn(("session_reset.at_hour", "4"), setup.PROFILE_SETTINGS)
        self.assertIn(("session_reset.idle_minutes", "60"), setup.PROFILE_SETTINGS)
        self.assertIn(("auxiliary.compression.model", "gemini-3.8-flash-low"), setup.PROFILE_SETTINGS)
        self.assertIn(("auxiliary.vision.provider", "openai-codex"), setup.PROFILE_SETTINGS)  # antigravity drops images
        for role in ("travel-commander", "trip", "schedule-manager"):
            with self.subTest(role=role):
                text = setup.expand_includes((Path(setup.__file__).parent / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                self.assertIn("04:00 或閒置 1 小時後開新對話", text)
                self.assertIn("pending_question", text)
        commander = setup.expand_includes((Path(setup.__file__).parent / "roles" / "travel-commander.md").read_text(encoding="utf-8"))
        self.assertIn("寫入 `pending_question`", commander)
        self.assertIn("不覆蓋已有的 `source` 與 `queried_at`", commander)

    def test_the_answer_goes_out_with_its_buttons(self):
        self.assertIn(("agent.clarify_timeout", "600"), setup.PROFILE_SETTINGS)
        self.assertIn(("agent.local_stream_stale_timeout", "180"), setup.PROFILE_SETTINGS)
        for role in ("travel-commander", "trip", "schedule-manager"):
            with self.subTest(role=role):
                text = setup.expand_includes((Path(setup.__file__).parent / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                self.assertIn("`clarify` 按鈕（單獨呼叫、全文放 question、不含 `MEDIA:`）", text)
                self.assertIn("每則訊息最多問一次按鈕", text)
                self.assertIn("日期矛盾", text)
                self.assertIn("預算只寫使用者親口說過的", text)
                self.assertIn("不讀工具原始碼或 `state.db`", text)
        for role in ("travel-commander", "trip"):
            with self.subTest(role=role):
                text = setup.expand_includes((Path(setup.__file__).parent / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                self.assertIn("travel.py\" local", text)
                self.assertIn("travel.py\" overseas", text)
                self.assertIn("travel.py photo", text)
                self.assertIn("vision_analyze", text)
                self.assertIn('travel.py" detail', text)
                self.assertIn("travel.py remind", text)
                self.assertIn("直接原樣貼上輸出", text)

    def test_commands_are_written_for_the_bash_terminal(self):
        # 2026-11-14: every turn's first command failed with "/usr/bin/bash: syntax error near `&'":
        # the roles wrote PowerShell's call operator, but Hermes' terminal tool runs bash.
        root = Path(setup.__file__).parent
        for path in [*(root / "roles").glob("*.md"), *(root / "prompts").glob("*.md")]:
            with self.subTest(path=path.name):
                self.assertNotIn('`& "', path.read_text(encoding="utf-8"))

    def test_tool_examples_use_made_up_places(self):
        # 2026-11-14: the examples named real hotels from past plans (山口溫泉飯店 經濟雙人房), so a
        # reply could not show whether a place came from a lookup or from a tool's help text.
        for script in ("trip-cost.py", "booking-links.py", "place-photo.py"):
            with self.subTest(script=script):
                doc = (Path(setup.__file__).parent / "scripts" / script).read_text(encoding="utf-8").split('"""')[1]
                example = doc.split("Example", 1)[1].split("\n\n", 1)[0]
                self.assertIn("範例", example)
                for real in ("山口", "三光", "萬國別館", "國光"):
                    self.assertNotIn(real, example)

    def test_older_talks_are_searched_and_lasting_preferences_remembered(self):
        for role in ("travel-commander", "trip", "schedule-manager"):
            with self.subTest(role=role):
                text = setup.expand_includes((Path(setup.__file__).parent / "roles" / f"{role}.md").read_text(encoding="utf-8"))
                self.assertIn("用 `memory`（延後載入，先 `tool_search`）記到 `user`", text)
        commander = setup.expand_includes((Path(setup.__file__).parent / "roles" / "travel-commander.md").read_text(encoding="utf-8"))
        self.assertIn("session_search", commander)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-upgrade-test-"))
        for profile in setup.PROFILES:
            (self.root / "profiles" / profile).mkdir(parents=True, exist_ok=True)
        # Seed a managed root with an outdated script and a user state file.
        old_script = b"# old version\n"
        target = self.root / "shared/travel-workflow/page-prices.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old_script)
        self.prefs = self.root / "shared/travel-workflow/preferences.json"
        self.prefs.write_text('{"budget": {"amount": 9999}}\n', encoding="utf-8")
        receipt = {
            "version": 1,
            "profiles": {},
            "files": {"shared/travel-workflow/page-prices.py": setup.digest(old_script)},
            "settings": ["compression.threshold_tokens"],
        }
        (self.root / setup.RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
        self.calls = []
        self.original_run = setup.run
        setup.run = lambda root, *args: (self.calls.append(args), "")[1]

    def tearDown(self):
        setup.run = self.original_run

    def test_upgrade_updates_unmodified_files_creates_missing_and_keeps_user_state(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            counts = setup.upgrade(self.root)
        self.assertEqual(counts["update"], 1)
        self.assertGreater(counts["create"], 0)
        # User preferences are never touched.
        self.assertEqual(json.loads(self.prefs.read_text(encoding="utf-8")), {"budget": {"amount": 9999}})
        # Role includes were expanded in installed SOUL.md.
        soul = (self.root / "profiles/schedule-manager/SOUL.md").read_text(encoding="utf-8")
        self.assertNotIn("{{INCLUDE:", soul)
        self.assertIn('travel.py" schedule', soul)
        self.assertNotIn("page-prices.py", soul)
        # travel.py and format-plan.py were installed.
        self.assertTrue((self.root / "shared/travel-workflow/travel.py").is_file())
        self.assertTrue((self.root / "shared/travel-workflow/format-plan.py").is_file())
        # Previously applied setting was not re-applied, while new settings were.
        set_keys = [c[4] for c in self.calls if len(c) >= 5 and c[2] == "config" and c[3] == "set"]
        self.assertNotIn("compression.threshold_tokens", set_keys)
        self.assertIn("agent.clarify_timeout", set_keys)
        self.assertIn("tools.tool_search.defer", set_keys)

    def test_upgrade_refuses_hand_modified_file_unless_forced(self):
        target = self.root / "shared/travel-workflow/page-prices.py"
        target.write_text("# user edited this by hand\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Modified files would be overwritten"):
            setup.upgrade(self.root)
        self.assertEqual(target.read_text(encoding="utf-8"), "# user edited this by hand\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            counts = setup.upgrade(self.root, force=True)
        self.assertEqual(counts["update"], 1)
        self.assertIn("ThreadPoolExecutor", target.read_text(encoding="utf-8"))

    def test_no_model_defaults_skips_personal_auxiliary_models(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.upgrade(self.root, include_models=False)
        set_keys = {c[4] for c in self.calls if len(c) >= 5 and c[2] == "config" and c[3] == "set"}
        self.assertFalse(set_keys & setup.MODEL_SETTING_KEYS)
        self.assertIn("agent.clarify_timeout", set_keys)

    def test_growth_problems_flags_bloated_opportunities_and_snapshots(self):
        opps = self.root / "shared/travel-workflow/opportunities.json"
        opps.write_text(" " * (setup.MAX_OPPORTUNITIES_BYTES + 1024), encoding="utf-8")
        pages = self.root / "shared/travel-workflow/pages"
        pages.mkdir(parents=True, exist_ok=True)
        for i in range(setup.MAX_PAGE_SNAPSHOTS + 1):
            (pages / f"p{i}.json").write_text("{}", encoding="utf-8")
        problems = setup.growth_problems(self.root)
        self.assertTrue(any("opportunities.json" in p for p in problems), problems)
        self.assertTrue(any("snapshots" in p for p in problems), problems)

    def test_no_personal_bot_nicknames_in_public_repo(self):
        # Keep personal Telegram bot nicknames out of the public GitHub repository.
        forbidden = ("\u5c0f\u5c45", "\u60f3\u51fa\u53bb\u73a9\u5c45", "\u8a8d\u771f\u7528\u529f\u5c45")
        hits = []
        for path in REPO.rglob("*"):
            if not path.is_file() or any(part in {".git", "node_modules", "__pycache__", ".claude"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(word in text for word in forbidden):
                hits.append(str(path.relative_to(REPO)))
        self.assertEqual(hits, [], f"Personal bot nickname leaked into: {hits}")


OLD_SOUL = "手寫的舊版\n"


class LiveSoulTests(unittest.TestCase):
    """2026-11-26: the running bots' SOUL.md is built from their own SOUL.head.md and the shared rules."""

    def setUp(self):
        self.root = Path(scratch.new_dir("hermes-live-souls-"))
        self.home = self.root / "profiles" / "trip"
        self.home.mkdir(parents=True)
        (self.home / "SOUL.head.md").write_text("# 自己的規則\n路徑 {{ROOT}}\n{{INCLUDE:roles/_shared/telegram-core.md}}\n",
                                                encoding="utf-8")
        (self.home / "SOUL.md").write_text(OLD_SOUL, encoding="utf-8")

    def run_live(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = setup.live_souls(self.root, python="C:/py/python.exe", stamp="20261126T120000", **kwargs)
        return status, out.getvalue()

    def test_a_drifted_soul_is_reported_and_left_alone(self):
        status, out = self.run_live()
        self.assertEqual(status, 1)
        self.assertIn("trip: SOUL.md differs", out)
        self.assertIn("project-manager: no SOUL.head.md", out)
        self.assertEqual((self.home / "SOUL.md").read_text(encoding="utf-8"), OLD_SOUL)
        self.assertFalse(list(self.home.glob("SOUL.before-*")))

    def test_write_backs_up_then_builds_from_head_and_shared_rules(self):
        self.run_live(write=True)
        backup = self.home / "SOUL.before-generated-20261126T120000.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), OLD_SOUL)
        soul = (self.home / "SOUL.md").read_bytes().decode("utf-8")
        self.assertTrue(soul.startswith(f"# 自己的規則\n路徑 {self.root.as_posix()}\n"))
        self.assertIn((REPO / "roles/_shared/telegram-core.md").read_text(encoding="utf-8").splitlines()[0], soul)
        self.assertIn('"C:/py/python.exe"', soul)
        self.assertNotIn("\r", soul)
        self.assertNotIn("{{", soul)
        status, out = self.run_live()
        self.assertIn("trip: SOUL.md matches", out)

    def test_an_existing_backup_is_never_overwritten(self):
        (self.home / "SOUL.before-generated-20261126T120000.md").write_text("更早的備份", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.run_live(write=True)
        self.assertEqual((self.home / "SOUL.md").read_text(encoding="utf-8"), OLD_SOUL)


if __name__ == "__main__":
    unittest.main()
