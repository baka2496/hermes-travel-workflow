#!/usr/bin/env python3
"""Install the travel workflow into an explicit, separate Hermes home."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parent
PROFILES = ("trip", "travel-commander", "schedule-manager")
PROFILE_NAMES = ("Trip Agent", "Travel Commander", "Schedule Manager")
REPLY_CHECK = "travel-reply-check"
REPLY_CHECK_FILES = ("plugin.yaml", "__init__.py")
# Modules the scripts and the plugin share (the price format; how Hermes stores conversations):
# one source each, installed beside the scripts and inside every profile's plugin folder.
SHARED_MODULES = ("travel_money.py", "hermes_sessions.py")
JOBS = (("trip", "travel-workflow/scanner"),
        ("travel-commander", "travel-workflow/weekly"),
        ("travel-commander", "travel-workflow/alerts"))
DELIVERED_JOBS = JOBS[1:]
RECEIPT = ".travel-workflow-install.json"
# Applied once to each newly created profile. Compress before small-window providers reject the
# request (one rejected ~75K tokens), but above the ~40K the role, tools and skills already use: at
# 50K one reply compressed six times and re-ran the same searches after each. Prune old tool output
# first, and keep each web page read short. No "♻️ 閘道已上線" ping in the chat after every gateway
# restart (2026-11-12: the user kept getting it). Start a fresh conversation with the first message
# after 04:00 instead of compressing one endless chat (needs the hermes-session-reset-policy plugin;
# the roles carry unanswered button questions over in pending_question). Later user changes to these
# settings are left alone.
PROFILE_SETTINGS = (("compression.threshold_tokens", "64000"),
                    ("compression.proactive_prune_tokens", "45000"),
                    ("web.extract_char_limit", "5000"),
                    ("telegram.gateway_restart_notification", "false"),
                    # 2026-11-20: a chat that ran all day sat near the 64K limit, so one question
                    # compressed twice (2.5 of its 6 minutes). A new question after an idle hour
                    # starts fresh; open questions carry over through `pending_question`.
                    ("session_reset.mode", "both"),
                    ("session_reset.at_hour", "4"),
                    ("session_reset.idle_minutes", "60"),
                    # The same reply: each compression took 67-92 s with the medium-effort model.
                    ("auxiliary.compression.provider", "antigravity-oauth"),
                    ("auxiliary.compression.base_url", "agy://local"),
                    ("auxiliary.compression.model", "gemini-3.8-flash-low"),
                    # 2026-11-20: the antigravity-oauth plugin sends messages as plain text, so its
                    # models never receive an image ("目前收到的訊息中並未附帶任何圖片"). Photos
                    # are checked through the OpenAI Codex login, which sees them (6 s a photo).
                    ("auxiliary.vision.provider", "openai-codex"),
                    ("auxiliary.vision.base_url", ""),
                    ("auxiliary.vision.model", "gpt-6-luna"),
                    # Buttons stay usable for 10 minutes; Hermes' default 90 s expired them before
                    # the user had read the plan on a phone (Telegram test, 2026-11-14).
                    ("agent.clarify_timeout", "600"),
                    # A stalled model call waited 900 s for its first byte before retrying; one
                    # 2026-11-19 reply sat 8 minutes on a single call (normal calls take 4-7 s).
                    ("agent.local_stream_stale_timeout", "180"),
                    # 2026-11-21: a Trip call ran 8 minutes, the model thinking until agy hit its output
                    # limit; that wait guards only a silent start, and Hermes' own cap for a call is 1800 s.
                    # 99% of calls take under 93 s, the longest good one 192 s. Needs the antigravity-oauth
                    # timeout patch (tests/test_antigravity_deadline.py), which otherwise ignores this.
                    ("providers.antigravity-oauth.request_timeout_seconds", "180"))

# Tools each profile keeps out of its opening prompt: Hermes lists them behind `tool_search` and
# loads one only when it is asked for. 2026-11-22: Trip opened every card with ~37K tokens, 15.6K
# of them tool descriptions, and still compressed twice in an 8-minute card; in 30 days it never
# called the tools below (6.1K tokens). The first list is Hermes' own default, which an explicit
# `defer` replaces wholesale.
HERMES_DEFERRED = ("computer_use", "session_search", "image_generate", "todo_list", "process_manage",
                   "cronjob_manage", "browser_vault", "text_to_speech", "manage_connections",
                   "drive_preview", "gui_tour", "desktop_preview", "annotate_preview", "manage_catalog",
                   "show_tip", "desktop_project", "close_terminal", "set_theme", "read_terminal",
                   "read_window_below", "focus_pane")
UNUSED_TOOLS = ("delegate_task", "skill_manage", "memory", "kanban_link", "kanban_attach",
                "kanban_attach_url", "kanban_heartbeat")
# Trip only works cards it is given: it neither creates cards nor sends one back.
DEFERRED_TOOLS = {"trip": HERMES_DEFERRED + UNUSED_TOOLS + ("kanban_create", "kanban_request_changes"),
                  "travel-commander": HERMES_DEFERRED + UNUSED_TOOLS,
                  "schedule-manager": HERMES_DEFERRED + UNUSED_TOOLS}


# Keys pointing at personal/local model logins; skipped when --no-model-defaults is passed.
MODEL_SETTING_KEYS = {
    "auxiliary.compression.provider", "auxiliary.compression.base_url", "auxiliary.compression.model",
    "auxiliary.vision.provider", "auxiliary.vision.base_url", "auxiliary.vision.model",
}

PACKAGE_SCRIPTS = (
    ("scripts/travel-scout.py", "profiles/travel-commander/scripts/travel-scout.py"),
    ("scripts/scan-trip.py", "profiles/trip/scripts/scan-trip.py"),
    ("scripts/travel.py", "shared/travel-workflow/travel.py"),
    ("scripts/phone-check.py", "shared/travel-workflow/phone-check.py"),
    ("scripts/booking-links.py", "shared/travel-workflow/booking-links.py"),
    ("scripts/trip-cost.py", "shared/travel-workflow/trip-cost.py"),
    ("scripts/format-plan.py", "shared/travel-workflow/format-plan.py"),
    ("scripts/weather-check.py", "shared/travel-workflow/weather-check.py"),
    ("scripts/trip-reminders.py", "shared/travel-workflow/trip-reminders.py"),
    ("scripts/weekend-scout.py", "shared/travel-workflow/weekend-scout.py"),
    ("scripts/archive-opportunities.py", "shared/travel-workflow/archive-opportunities.py"),
    ("scripts/page_evidence.py", "shared/travel-workflow/page_evidence.py"),
    *((f"scripts/{name}", f"shared/travel-workflow/{name}") for name in SHARED_MODULES),
    ("scripts/calendar-check.py", "shared/travel-workflow/calendar-check.py"),
    ("scripts/place-photo.py", "shared/travel-workflow/place-photo.py"),
    ("scripts/page-prices.py", "shared/travel-workflow/page-prices.py"),
    ("scripts/places.py", "shared/travel-workflow/places.py"),
    ("skills/travel-planning/SKILL.md", "profiles/travel-commander/skills/travel-planning/SKILL.md"),
)

INCLUDE_DIRECTIVE = re.compile(r"^\{\{INCLUDE:([^}\n]+)\}\}\s*$", re.M)


def profile_settings(include_models=True):
    if include_models:
        return PROFILE_SETTINGS
    return tuple((k, v) for k, v in PROFILE_SETTINGS if k not in MODEL_SETTING_KEYS)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(root, *args):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(root)
    result = subprocess.run(["hermes", *args], env=env, text=True,
                            encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Hermes command failed: {args!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as out:
        out.write(data)


def same_or_create(path, data, *, preserve=False):
    if path.exists():
        if not preserve and path.read_bytes() != data:
            raise RuntimeError(f"Existing file differs; refusing to overwrite: {path}")
        return
    write_new(path, data)


def expand_includes(text):
    """Expand {{INCLUDE:relative/path}} lines from the package directory."""
    def replace(match):
        rel = match.group(1).strip()
        target = (PACKAGE / rel).resolve()
        if PACKAGE not in target.parents or not target.is_file():
            raise RuntimeError(f"Invalid include target: {rel}")
        return target.read_text(encoding="utf-8").rstrip("\n")
    return INCLUDE_DIRECTIVE.sub(replace, text)


def rendered(path, root):
    text = expand_includes(path.read_text(encoding="utf-8"))
    return (text
            .replace("{{ROOT}}", root.as_posix())
            .replace("{{PYTHON}}", Path(sys.executable).resolve().as_posix()).encode("utf-8"))


# The running bots' own profiles, which `install` does not manage. Until 2026-11-26 their SOUL.md was kept
# by hand and drifted from roles/ (the live trip still said to paste trip-cost.py by hand). Each now keeps
# only its own rules (who it is, its paths and IDs, which stay out of this public repo) in SOUL.head.md,
# with {{INCLUDE:roles/_shared/…}} lines for the shared rules; `live-souls` builds SOUL.md from the two.
LIVE_SOULS = ("trip", "project-manager")
LIVE_PLUGINS = ("project-manager", "trip", "us-market")
LIVE_HEAD = "SOUL.head.md"


def sync_live(root, *, python=None):
    """Single-source-of-truth sync from this repo into a live Hermes root.

    Updates shared/travel-workflow scripts, live profile scripts, travel-reply-check plugins,
    and rebuilds SOUL.md from SOUL.head.md + roles/_shared without touching user state JSON files.
    """
    root = Path(root).expanduser().resolve()
    updated = unchanged = 0
    for source, target in PACKAGE_SCRIPTS:
        if target.startswith("shared/travel-workflow/"):
            dest = root / target
        elif source == "scripts/travel-scout.py" and (root / "profiles/project-manager").is_dir():
            dest = root / "profiles/project-manager/scripts/travel-scout.py"
        elif source == "scripts/scan-trip.py" and (root / "profiles/trip").is_dir():
            dest = root / "profiles/trip/scripts/scan-trip.py"
        else:
            continue
        data = rendered(PACKAGE / source, root)
        if dest.is_file() and dest.read_bytes() == data:
            unchanged += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            updated += 1
    for profile in LIVE_PLUGINS:
        if not (root / "profiles" / profile).is_dir():
            continue
        for name in REPLY_CHECK_FILES:
            dest = root / "profiles" / profile / "plugins" / REPLY_CHECK / name
            data = (PACKAGE / "plugins" / REPLY_CHECK / name).read_bytes()
            if dest.is_file() and dest.read_bytes() == data:
                unchanged += 1
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                updated += 1
        for name in SHARED_MODULES:
            dest = root / "profiles" / profile / "plugins" / REPLY_CHECK / name
            data = (PACKAGE / "scripts" / name).read_bytes()
            if dest.is_file() and dest.read_bytes() == data:
                unchanged += 1
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                updated += 1
    live_souls(root, write=True, python=python)
    print(f"Synced live root {root}: {updated} file(s) updated, {unchanged} unchanged.")
    return 0


def live_soul(head, root, python=None):
    """SOUL.md text for a live profile: its head with the repo's shared rules filled in."""
    text = expand_includes(head.read_text(encoding="utf-8").replace("\r\n", "\n"))
    python = python or Path(sys.executable).resolve().as_posix()
    return text.replace("{{ROOT}}", root.as_posix()).replace("{{PYTHON}}", python)


def live_souls(root, *, write=False, python=None, stamp=None):
    """Report (or with *write*, fix) each live profile whose SOUL.md is not its head plus the shared rules.
    Returns 1 when one differs or has no head and nothing was written, else 0."""
    import datetime
    import difflib
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    status = 0
    for profile in LIVE_SOULS:
        home = root / "profiles" / profile
        head, soul = home / LIVE_HEAD, home / "SOUL.md"
        if not head.is_file():
            print(f"{profile}: no {LIVE_HEAD}; SOUL.md is still kept by hand")
            status = 1
            continue
        wanted = live_soul(head, root, python)
        current = soul.read_text(encoding="utf-8").replace("\r\n", "\n") if soul.is_file() else ""
        if current == wanted:
            print(f"{profile}: SOUL.md matches {LIVE_HEAD} + roles/_shared ({len(wanted):,} characters)")
            continue
        changed = sum(1 for line in difflib.unified_diff(current.splitlines(), wanted.splitlines(), lineterm="", n=0)
                      if line[:1] in "+-" and line[:3] not in ("+++", "---"))
        if not write:
            print(f"{profile}: SOUL.md differs from {LIVE_HEAD} + roles/_shared ({changed} lines); run with --write")
            status = 1
            continue
        if soul.is_file():
            backup = home / f"SOUL.before-generated-{stamp}.md"
            if backup.exists():
                raise RuntimeError(f"backup already exists: {backup}")
            shutil.copy2(soul, backup)
            print(f"{profile}: backed up SOUL.md to {backup.name}")
        with open(soul, "w", encoding="utf-8", newline="\n") as out:
            out.write(wanted)
        print(f"{profile}: SOUL.md written ({changed} lines changed, {len(wanted):,} characters)")
    return status


def managed_files(root):
    """All package-owned files as (relative_posix_path, bytes) for `root`."""
    entries = []
    for profile in PROFILES:
        entries.append((f"profiles/{profile}/SOUL.md", rendered(PACKAGE / "roles" / f"{profile}.md", root)))
    for source, target in PACKAGE_SCRIPTS:
        entries.append((target, rendered(PACKAGE / source, root)))
    for profile in PROFILES:
        for name in REPLY_CHECK_FILES:
            entries.append((f"profiles/{profile}/plugins/{REPLY_CHECK}/{name}",
                            (PACKAGE / "plugins" / REPLY_CHECK / name).read_bytes()))
        for name in SHARED_MODULES:
            entries.append((f"profiles/{profile}/plugins/{REPLY_CHECK}/{name}",
                            (PACKAGE / "scripts" / name).read_bytes()))
    return entries


def save_receipt(path, receipt):
    # Receipt is package-owned metadata, never user state.
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        raise RuntimeError(f"Interrupted receipt update; inspect {tmp}")
    write_new(tmp, (json.dumps(receipt, indent=2) + "\n").encode())
    os.replace(tmp, path)


def jobs(root, profile):
    path = root / "profiles" / profile / "cron" / "jobs.json"
    if not path.exists():
        return []
    data = read_json(path)
    if not isinstance(data.get("jobs"), list):
        raise RuntimeError(f"Unrecognized Hermes jobs format: {path}")
    return data["jobs"]


def ensure_job(root, profile, name, schedule, prompt, deliver, *, script=None,
               monitor=None, continuity=False):
    prompt = prompt.strip()
    matches = [j for j in jobs(root, profile) if j.get("name") == name]
    created = not matches
    python = str(Path(sys.executable).resolve())
    expected = {"prompt": prompt, "deliver": deliver, "script": script,
                "monitor_script": monitor, "no_agent": bool(script),
                "monitor_url": None, "timezone": "Asia/Taipei", "catch_up": False,
                "context_from": ["self"] if continuity else None,
                "interpreter": python if script or monitor else None,
                "failure_deliver": "local" if continuity else None,
                "model": None, "provider": None, "base_url": None,
                "skills": [], "enabled_toolsets": None, "workdir": None}
    if created:
        args = ["-p", profile, "cron", "create", schedule]
        if prompt:
            args.append(prompt)
        args += ["--name", name, "--deliver", deliver, "--catch-up", "off",
                 "--timezone", "Asia/Taipei", "--paused"]
        if script:
            args += ["--script", script, "--no-agent", "--interpreter", python]
        if monitor:
            args += ["--monitor-script", monitor, "--interpreter", python]
        if continuity:
            args += ["--continuity", "--failure-deliver", "local"]
        run(root, *args)
        matches = [j for j in jobs(root, profile) if j.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {name} job in {profile}, found {len(matches)}")
    job = matches[0]
    if (job.get("schedule", {}).get("kind") != "cron"
            or job.get("schedule", {}).get("expr") != schedule
            or job.get("repeat", {}).get("times") is not None
            or any(job.get(key) != value for key, value in expected.items())):
        hint = (f"; to switch delivery run: setup.py delivery {deliver} --root <root>"
                if job.get("deliver") != deliver and name != "travel-workflow/scanner" else "")
        raise RuntimeError(f"Existing job differs; refusing to change: {name}{hint}")
    if created and (job.get("state") != "paused" or job.get("enabled") is not False):
        raise RuntimeError(f"Hermes did not create {name} paused")


def install(root, delivery, *, include_models=True):
    root = root.expanduser().resolve()
    if root == PACKAGE or PACKAGE in root.parents:
        raise RuntimeError("Choose a Hermes root outside the Git checkout")
    node = shutil.which("node")
    if not node or not shutil.which("hermes"):
        raise RuntimeError("Node and Hermes must be available on PATH")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < 24:
        raise RuntimeError(f"Node 24 or newer is required; found {version}")
    scanner = (PACKAGE / "scanner").resolve()
    required = [PACKAGE / "roles" / f"{p}.md" for p in PROFILES]
    required += [PACKAGE / "prompts" / f"{p}.md" for p in ("weekly", "alert")]
    required += [PACKAGE / source for source, _ in PACKAGE_SCRIPTS]
    required += [PACKAGE / "state" / f"{p}.json" for p in ("calendar", "preferences", "opportunities")]
    required += [PACKAGE / "plugins" / REPLY_CHECK / f for f in REPLY_CHECK_FILES]
    missing = [str(p) for p in required if not p.is_file()]
    if missing or not scanner.is_dir():
        raise RuntimeError(f"Package files missing: {missing}; scanner={scanner}")
    receipt_path = root / RECEIPT
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt.get("version") != 1:
            raise RuntimeError("Unrecognized install receipt")
    else:
        if root.exists() and any(root.iterdir()):
            raise RuntimeError(f"Root must be new or already managed: {root}")
        root.mkdir(parents=True, exist_ok=True)
        receipt = {"version": 1, "profiles": {}, "files": {}, "settings": []}
        save_receipt(receipt_path, receipt)
    receipt.setdefault("files", {})
    receipt.setdefault("settings", [])
    delivery = delivery_for(receipt, delivery)
    applied_settings = profile_settings(include_models)
    for profile, display_name in zip(PROFILES, PROFILE_NAMES):
        home = root / "profiles" / profile
        soul = home / "SOUL.md"
        wanted = rendered(PACKAGE / "roles" / f"{profile}.md", root)
        if not home.exists():
            run(root, "profile", "create", profile, "--no-alias", "--no-skills",
                "--description", display_name)
            if not soul.is_file():
                raise RuntimeError(f"Native profile has no scaffold SOUL: {soul}")
            receipt["profiles"][profile] = digest(soul.read_bytes())
            save_receipt(receipt_path, receipt)
            for key, value in applied_settings:
                run(root, "-p", profile, "config", "set", key, value)
            run(root, "-p", profile, "config", "set", "tools.tool_search.defer",
                json.dumps(list(DEFERRED_TOOLS[profile])))
        scaffold = receipt["profiles"].get(profile)
        if not scaffold:
            raise RuntimeError(f"Existing unowned profile: {home}")
        if soul.exists() and soul.read_bytes() != wanted:
            if digest(soul.read_bytes()) != scaffold:
                raise RuntimeError(f"Profile SOUL modified; refusing overwrite: {soul}")
            soul.write_bytes(wanted)
        elif not soul.exists():
            write_new(soul, wanted)
        receipt["files"][f"profiles/{profile}/SOUL.md"] = digest(wanted)
    for source, target in PACKAGE_SCRIPTS:
        data = rendered(PACKAGE / source, root)
        same_or_create(root / target, data)
        receipt["files"][target] = digest(data)
    # 2026-11-14: a button question went out with a total the cost tool had not printed; this
    # plugin holds such a question back until it matches (plugins/travel-reply-check).
    for profile in PROFILES:
        for name in REPLY_CHECK_FILES:
            rel = f"profiles/{profile}/plugins/{REPLY_CHECK}/{name}"
            data = (PACKAGE / "plugins" / REPLY_CHECK / name).read_bytes()
            same_or_create(root / rel, data)
            receipt["files"][rel] = digest(data)
        for name in SHARED_MODULES:
            rel = f"profiles/{profile}/plugins/{REPLY_CHECK}/{name}"
            data = (PACKAGE / "scripts" / name).read_bytes()
            same_or_create(root / rel, data)
            receipt["files"][rel] = digest(data)
        run(root, "-p", profile, "plugins", "enable", REPLY_CHECK)
    for name, target in [("calendar", "shared/annual-schedule/calendar.json"),
                         ("preferences", "shared/travel-workflow/preferences.json"),
                         ("opportunities", "shared/travel-workflow/opportunities.json")]:
        same_or_create(root / target, rendered(PACKAGE / "state" / f"{name}.json", root), preserve=True)
    runtime = {"scanner_dir": str(scanner), "timezone": "Asia/Taipei", "node": str(Path(node).resolve())}
    same_or_create(root / "shared/travel-workflow/runtime.json",
                   (json.dumps(runtime, indent=2, ensure_ascii=False) + "\n").encode())
    boards = json.loads(run(root, "kanban", "boards", "list", "--json", "--all"))
    matches = [b for b in boards if b.get("slug") == "command-center"]
    if len(matches) > 1 or (matches and matches[0].get("archived")):
        raise RuntimeError("Ambiguous or archived command-center board")
    if not matches:
        run(root, "kanban", "boards", "create", "command-center")
    weekly = rendered(PACKAGE / "prompts/weekly.md", root).decode("utf-8")
    alert = rendered(PACKAGE / "prompts/alert.md", root).decode("utf-8")
    ensure_job(root, "trip", "travel-workflow/scanner", "*/15 * * * *", "", "local", script="scan-trip.py")
    ensure_job(root, "travel-commander", "travel-workflow/weekly", "0 9 * * 1", weekly, delivery, continuity=True)
    ensure_job(root, "travel-commander", "travel-workflow/alerts", "*/15 * * * *", alert, delivery,
               monitor="travel-scout.py", continuity=True)
    receipt["settings"] = sorted(set(receipt.get("settings", [])) | {k for k, _ in applied_settings})
    receipt["delivery"] = delivery
    save_receipt(receipt_path, receipt)
    print(f"Installed in {root}. New jobs are paused; existing job state was preserved.")
    if not (scanner / "node_modules/tsx/dist/cli.mjs").is_file():
        print("Scanner dependencies are not installed yet: npm --prefix scanner ci --ignore-scripts")


def upgrade(root, *, force=False, include_models=True):
    """Update package-owned roles, scripts, skills, plugin files and new settings in a managed root.

    User state files (calendar.json, preferences.json, opportunities.json) are never touched.
    Files modified by hand are left alone and reported unless --force is passed.
    """
    root, receipt = managed(root)
    recorded = dict(receipt.get("files") or {})
    conflicts, plan = [], []
    for rel, wanted in managed_files(root):
        path = root / rel
        wanted_hash = digest(wanted)
        if not path.exists():
            plan.append(("create", rel, path, wanted, wanted_hash))
            continue
        current = path.read_bytes()
        current_hash = digest(current)
        if current_hash == wanted_hash:
            plan.append(("unchanged", rel, path, wanted, wanted_hash))
            continue
        known = {recorded.get(rel)}
        if rel.startswith("profiles/") and rel.endswith("/SOUL.md"):
            profile = rel.split("/")[1]
            known.add(receipt.get("profiles", {}).get(profile))
        known.discard(None)
        if force or current_hash in known:
            plan.append(("update", rel, path, wanted, wanted_hash))
        else:
            conflicts.append(rel)
    if conflicts:
        joined = ", ".join(conflicts)
        raise RuntimeError(f"Modified files would be overwritten ({joined}); inspect them or re-run with --force")
    counts = {"create": 0, "update": 0, "unchanged": 0}
    for action, rel, path, wanted, wanted_hash in plan:
        counts[action] += 1
        if action in ("create", "update"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wanted)
        recorded[rel] = wanted_hash
    applied = set(receipt.get("settings") or [])
    new_settings = [(k, v) for k, v in profile_settings(include_models) if k not in applied]
    for profile in PROFILES:
        if not (root / "profiles" / profile).is_dir():
            continue
        for key, value in new_settings:
            run(root, "-p", profile, "config", "set", key, value)
        run(root, "-p", profile, "config", "set", "tools.tool_search.defer",
            json.dumps(list(DEFERRED_TOOLS[profile])))
        run(root, "-p", profile, "plugins", "enable", REPLY_CHECK)
    receipt["files"] = recorded
    receipt["settings"] = sorted(applied | {k for k, _ in new_settings})
    save_receipt(root / RECEIPT, receipt)
    print(f"Upgraded {root}: {counts['update']} updated, {counts['create']} created, "
          f"{counts['unchanged']} unchanged, {len(new_settings)} new setting(s).")
    return counts


def delivery_for(receipt, requested):
    """An explicit --deliver wins; otherwise keep what this root was installed with."""
    return requested or receipt.get("delivery") or "local"


def managed(root):
    root = root.expanduser().resolve()
    path = root / RECEIPT
    if not path.is_file() or read_json(path).get("version") != 1:
        raise RuntimeError(f"Root is not managed by this installer: {root}")
    return root, read_json(path)


def managed_job(root, profile, name):
    matches = [j for j in jobs(root, profile) if j.get("name") == name]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        raise RuntimeError(f"Expected exactly one {name} job in {profile}, found {len(matches)}; run install first")
    return matches[0]


def set_state(root, action):
    root, _ = managed(root)
    found = [(profile, managed_job(root, profile, name)) for profile, name in JOBS]
    for profile, job in found:
        if (job.get("enabled") is True) == (action == "resume"):
            print(f"{job['name']}: already {'running' if action == 'resume' else 'paused'}")
            continue
        run(root, "-p", profile, "cron", action, job["id"])
        print(f"{job['name']}: {'resumed' if action == 'resume' else 'paused'}")


def set_delivery(root, target):
    root, receipt = managed(root)
    found = [(profile, managed_job(root, profile, name)) for profile, name in DELIVERED_JOBS]
    for profile, job in found:
        if job.get("deliver") != target:
            run(root, "-p", profile, "cron", "edit", job["id"], "--deliver", target)
        print(f"{job['name']}: deliver {target}")
    receipt["delivery"] = target
    save_receipt(root / RECEIPT, receipt)


def gateway_serving(root, profile):
    """True when a Hermes gateway is serving this profile's cron jobs."""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(root)
    result = subprocess.run(["hermes", "-p", profile, "cron", "status"], env=env, text=True,
                            encoding="utf-8", errors="replace", capture_output=True)
    output = result.stdout + result.stderr
    if "No scheduler is serving" in output:
        return False
    return "Gateway is running" in output


def hermes_sessions():
    spec = importlib.util.spec_from_file_location("hermes_sessions", PACKAGE / "scripts" / "hermes_sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAX_OPPORTUNITIES_BYTES = 100_000
MAX_STATE_DB_BYTES = 250 * 1024 * 1024
MAX_PAGE_SNAPSHOTS = 50


def growth_problems(root):
    """Flag files/folders that grow unbounded over time before they slow replies down."""
    found = []
    opps = root / "shared/travel-workflow/opportunities.json"
    if opps.is_file() and opps.stat().st_size > MAX_OPPORTUNITIES_BYTES:
        kb = opps.stat().st_size // 1024
        found.append(f"opportunities.json is {kb} KB (> {MAX_OPPORTUNITIES_BYTES // 1024} KB); "
                     f"run archive-opportunities.py to move expired records into opportunities-archive.json")
    pages = root / "shared/travel-workflow/pages"
    if pages.is_dir():
        count = sum(1 for p in pages.glob("*.json") if p.is_file())
        if count > MAX_PAGE_SNAPSHOTS:
            found.append(f"shared/travel-workflow/pages has {count} snapshots (> {MAX_PAGE_SNAPSHOTS}); "
                         f"old snapshots can be cleaned up to keep --near lookups fast")
    profiles = root / "profiles"
    if profiles.is_dir():
        for db in sorted(profiles.glob("*/state.db")):
            if db.is_file() and db.stat().st_size > MAX_STATE_DB_BYTES:
                mb = db.stat().st_size // (1024 * 1024)
                found.append(f"profiles/{db.parent.name}/state.db is {mb} MB (> {MAX_STATE_DB_BYTES // (1024 * 1024)} MB); "
                             f"prune old sessions or vacuum to keep evidence lookups fast")
    return found


def check(root):
    """Read-only health report. Returns 1 when something needs attention."""
    root, _ = managed(root)
    problems = []
    # The reply checks read Hermes's conversations; a Hermes upgrade that changes their layout would
    # otherwise switch those checks off silently.
    sessions = hermes_sessions()
    for path, problem in sessions.schema_problems(root):
        problems.append(f"{path}: {problem}. The reply checks cannot read this Hermes version's conversations; "
                        f"update scripts/hermes_sessions.py (expects {', '.join(sessions.MESSAGE_COLUMNS)})")
    try:
        runtime = read_json(root / "shared/travel-workflow/runtime.json")
        scanner, node = Path(runtime["scanner_dir"]), Path(runtime["node"])
    except (OSError, ValueError, KeyError, TypeError):
        scanner = node = None
        problems.append("runtime.json missing or unreadable; run install")
    if node is not None and not node.is_file():
        problems.append(f"node not found at {node}; run install again from a shell where node 24+ is on PATH")
    if scanner is not None and not (scanner / "node_modules/tsx/dist/cli.mjs").is_file():
        problems.append(f"scanner dependencies missing in {scanner}: npm --prefix scanner ci --ignore-scripts")
    print(f"Hermes root: {root}")
    for profile, name in JOBS:
        found = [j for j in jobs(root, profile) if j.get("name") == name]
        if len(found) != 1:
            problems.append(f"{name}: expected one job in {profile}, found {len(found)}")
            continue
        job = found[0]
        state = "running" if job.get("enabled") else "paused"
        print(f"  {name} ({profile}): {state}, deliver={job.get('deliver')}, last={job.get('last_status') or 'never'}")
        error = job.get("last_error") or job.get("last_delivery_error")
        if job.get("failure_streak") or job.get("last_status") == "error" or error:
            problems.append(f"{name}: {job.get('failure_streak', 0)} failure(s) in a row; last error: {error or 'unknown'}")
    running = [name for profile, name in JOBS
               if any(j.get("name") == name and j.get("enabled") for j in jobs(root, profile))]
    serving = all(gateway_serving(root, profile) for profile in sorted({p for p, _ in JOBS}))
    print(f"  gateway: {'running' if serving else 'not running'}")
    if running and not serving:
        problems.append(f"{', '.join(running)} will not run until a Hermes gateway serves this root: "
                        f"set HERMES_HOME to it, then run hermes gateway run (or gateway install)")
    scout = root / "profiles/travel-commander/scripts/travel-scout.py"
    if scout.is_file():
        result = subprocess.run([sys.executable, str(scout), "--root", str(root)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        try:
            signal = json.loads(result.stdout)
        except ValueError:
            signal = {"status": "error"}
        detail = ", ".join(signal.get("missing", [])) if signal.get("status") == "waiting" else ""
        print(f"  alert gate: {signal.get('status')}{' (missing ' + detail + ')' if detail else ''}")
    problems.extend(growth_problems(root))
    if problems:
        print("Needs attention:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("No problems found.")
    return 0


def enable_weekend(root, delivery=None):
    """Create and enable the optional Thursday 20:00 effortless weekend proposal cron job."""
    root, receipt = managed(root)
    target = delivery or receipt.get("delivery") or "local"
    prompt = rendered(PACKAGE / "prompts/weekend.md", root).decode("utf-8")
    name = "travel-workflow/weekend"
    ensure_job(root, "travel-commander", name, "0 20 * * 4", prompt, target, continuity=True)
    job = managed_job(root, "travel-commander", name)
    if job.get("enabled") is not True:
        run(root, "-p", "travel-commander", "cron", "resume", job["id"])
    print(f"{name}: enabled (Thu 20:00, deliver={target})")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("install", help="install or verify the workflow in a Hermes root")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--deliver", choices=("local", "telegram"),
                         help="digest/alert delivery for a new root (default: local, or the root's current setting)")
    command.add_argument("--no-model-defaults", action="store_true",
                         help="skip personal auxiliary compression/vision model settings")
    command = sub.add_parser("upgrade", help="update package-owned roles, scripts, plugin files and new settings")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--force", action="store_true",
                         help="overwrite package-owned files even if modified by hand (user state is still untouched)")
    command.add_argument("--no-model-defaults", action="store_true",
                         help="skip personal auxiliary compression/vision model settings")
    for name, text in [("check", "read-only health report"), ("resume", "start the three workflow jobs"),
                       ("pause", "pause the three workflow jobs")]:
        sub.add_parser(name, help=text).add_argument("--root", type=Path, required=True)
    command = sub.add_parser("delivery", help="switch digest/alert delivery")
    command.add_argument("target", choices=("local", "telegram"))
    command.add_argument("--root", type=Path, required=True)
    command = sub.add_parser("enable-weekend", help="enable Thursday 20:00 automatic weekend trip proposal")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--deliver", choices=("local", "telegram"))
    command = sub.add_parser("live-souls", help="check (or --write) the running bots' SOUL.md against SOUL.head.md + roles/_shared")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--write", action="store_true", help="back up and rewrite each SOUL.md that differs")
    command.add_argument("--python", help="interpreter path for {{PYTHON}} (default: this one)")
    command = sub.add_parser("sync-live", help="sync scripts, plugins, and SOUL.md from this repo into a live Hermes root")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--python", help="interpreter path for {{PYTHON}} (default: this one)")
    args = parser.parse_args()
    try:
        if args.command == "sync-live":
            return sync_live(args.root, python=args.python)
        if args.command == "live-souls":
            return live_souls(args.root, write=args.write, python=args.python)
        if args.command == "check":
            return check(args.root)
        if args.command in ("resume", "pause"):
            set_state(args.root, args.command)
        elif args.command == "delivery":
            set_delivery(args.root, args.target)
        elif args.command == "enable-weekend":
            enable_weekend(args.root, args.deliver)
        elif args.command == "upgrade":
            upgrade(args.root, force=args.force, include_models=not args.no_model_defaults)
        else:
            install(args.root, args.deliver, include_models=not args.no_model_defaults)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"{args.command} stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
