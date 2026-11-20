"""Tests leave nothing in the temp directory (2026-11-20: about 2,900 fixture folders had piled up)."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import scratch

TESTS = Path(__file__).resolve().parent


class ScratchTests(unittest.TestCase):
    def test_tests_make_folders_only_through_scratch(self):
        made_directly = re.compile(r"tempfile\.mkdtemp\(|\bmkdtemp\(")
        found = [f"{path.name}:{n}" for path in sorted(TESTS.glob("*.py")) if path.name != "scratch.py"
                 for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if made_directly.search(line.split("#", 1)[0]) and "made_directly" not in line]
        self.assertEqual(found, [], "use scratch.new_dir() so the folder is removed after the run")

    def test_new_dir_is_inside_the_run_folder(self):
        folder = Path(scratch.new_dir("check-")).resolve()
        self.assertTrue(folder.is_dir())
        self.assertEqual(folder.parent, scratch.run_dir())
        self.assertEqual(scratch.run_dir().parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(scratch.run_dir().name.startswith("hermes-tests-"))

    def run_python(self, code):
        return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(TESTS)!r})\n{code}"],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_a_run_removes_its_folder_when_it_ends(self):
        done = self.run_python("import scratch, pathlib\n"
                               "d = pathlib.Path(scratch.new_dir('x-'))\n"
                               "(d / 'sub').mkdir(); (d / 'sub' / 'f.txt').write_text('x')\n"
                               "print(scratch.run_dir())")
        self.assertEqual(done.returncode, 0, done.stderr)
        run = Path(done.stdout.strip())
        self.assertTrue(run.name.startswith("hermes-tests-"))
        self.assertFalse(run.exists())

    def test_removal_refuses_a_folder_outside_the_temp_directory(self):
        outside = Path(scratch.new_dir("outside-"))  # stands in for a folder elsewhere
        done = self.run_python("import scratch, pathlib\n"
                               f"scratch._run = pathlib.Path({str(outside)!r})\n"
                               "scratch._remove_run()")
        self.assertIn("not removing", done.stderr)
        self.assertTrue(outside.is_dir())


if __name__ == "__main__":
    unittest.main()
