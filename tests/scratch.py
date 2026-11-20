"""Folders for test fixtures, removed when the test run ends.

Until 2026-11-20 every test made its own folder in the system temp directory and left it there
(about 2,900 had piled up), because cleaning up means deleting folders recursively. The owner
agreed that the tests may delete what they made themselves. To keep that to one known path:

- each run makes a single folder, `hermes-tests-<random>`, in the temp directory;
- every fixture folder is made inside it with `new_dir()`;
- when the run ends, that one folder is deleted, and only if it is still the run's own folder
  directly inside the temp directory.

Use `new_dir()` instead of `tempfile.mkdtemp()` in tests (test_scratch.py checks this).
"""
import atexit
from pathlib import Path
import shutil
import sys
import tempfile

_RUN_PREFIX = "hermes-tests-"
_run = None


def run_dir():
    """This run's one folder, made on first use."""
    global _run
    if _run is None:
        _run = Path(tempfile.mkdtemp(prefix=_RUN_PREFIX)).resolve()
        atexit.register(_remove_run)
    return _run


def new_dir(prefix="fixture-"):
    """A new empty folder inside this run's folder, as tempfile.mkdtemp() returns it (a str)."""
    return tempfile.mkdtemp(prefix=prefix, dir=run_dir())


def _remove_run():
    run = _run
    if run is None or not run.is_dir():
        return
    temp = Path(tempfile.gettempdir()).resolve()
    if run.parent != temp or not run.name.startswith(_RUN_PREFIX):
        print(f"scratch: not removing {run}: not this run's folder in {temp}", file=sys.stderr)
        return
    left = []
    shutil.rmtree(run, onerror=lambda _f, path, _e: left.append(path))
    if left:  # e.g. a file a test still holds open on Windows
        print(f"scratch: {len(left)} item(s) could not be removed under {run}", file=sys.stderr)
