"""The installed antigravity-oauth provider stops a model call at its request timeout.

2026-11-21: one call of the Trip worker sat 8 minutes (Hermes' own default for it is 1800 s) until
agy gave up at its output limit. The plugin took Hermes' ``httpx.Timeout`` for "no timeout", and its
deadline was only checked when agy printed a line. The local patch in the travel profiles' copies
reads the timeout and stops the process on a timer; this checks that patch where it is installed
(skipped elsewhere, e.g. CI). setup.py sets the timeout: ``providers.antigravity-oauth.request_timeout_seconds``.
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

HERMES = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
PROFILES = ("trip", "project-manager", "commander")


def plugin_dir(profile):
    return HERMES / "profiles" / profile / "plugins" / "antigravity-oauth"


def load(profile, name):
    # The plugin imports one helper from Hermes itself, absent outside its runtime.
    sys.modules.setdefault("hermes_constants", SimpleNamespace(get_hermes_home=lambda: HERMES))
    path = str(plugin_dir(profile))
    sys.path.insert(0, path)
    try:
        for module in ("stream", "client", "process", "prompt", "native_tools", "models", "soul", "auth"):
            sys.modules.pop(module, None)
        return __import__(name)
    finally:
        sys.path.remove(path)


@unittest.skipUnless(all(plugin_dir(p).is_dir() for p in PROFILES), "antigravity-oauth is not installed here")
class DeadlineTests(unittest.TestCase):
    def test_every_travel_profile_has_the_patch(self):
        for profile in PROFILES:
            stream = (plugin_dir(profile) / "stream.py").read_text(encoding="utf-8")
            client = (plugin_dir(profile) / "client.py").read_text(encoding="utf-8")
            self.assertIn("deadline_timer = threading.Timer(", stream, profile)
            self.assertIn("deadline_timer.cancel()", stream, profile)
            self.assertIn("effective_timeout = _timeout_seconds(timeout)", client, profile)

    def test_timeout_is_read_from_hermes_timeout_object(self):
        for profile in PROFILES:
            seconds = load(profile, "client")._timeout_seconds
            self.assertEqual(seconds(SimpleNamespace(connect=60.0, read=180.0, write=180.0, pool=60.0)), 180.0)
            self.assertEqual(seconds(42), 42.0)
            self.assertEqual(seconds(None), 300.0)
            self.assertEqual(seconds(True), 300.0)

    def test_a_silent_model_is_stopped_at_the_timeout(self):
        for profile in PROFILES:
            stream = load(profile, "stream")
            process = load(profile, "process")
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            client = SimpleNamespace(_lock=threading.Lock(), _active_processes={proc}, _worker_proc=None,
                                     _isolated_gemini_dir=None, _terminate_process=process.terminate_process)
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, r"within 2s"):
                    list(stream.AntigravityStream(proc=proc, client=client, model="gemini-test", timeout=2.0))
                self.assertLess(time.monotonic() - started, 15, profile)
                self.assertIsNotNone(proc.poll(), "the agy process was stopped")
            finally:
                if proc.poll() is None:
                    proc.kill()


class SettingTests(unittest.TestCase):
    def test_setup_sets_the_request_timeout(self):
        source = (Path(__file__).resolve().parents[1] / "setup.py").read_text(encoding="utf-8")
        self.assertIn('("providers.antigravity-oauth.request_timeout_seconds", "180")', source)


if __name__ == "__main__":
    unittest.main()
