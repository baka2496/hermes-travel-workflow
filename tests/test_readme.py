"""README's install steps point at the repository this checkout came from.

2026-11-20: the account was renamed (baka9881 -> baka2496, 2026-11-18) and the README's
`git clone` line still named the old one.
"""
from pathlib import Path
import re
import subprocess
import unittest

REPO = Path(__file__).resolve().parents[1]


def origin_repo():
    """"owner/name" of this checkout's GitHub origin, or None when there is none."""
    try:
        url = subprocess.run(["git", "-C", str(REPO), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return found.group(1).lower() if found else None


class ReadmeTests(unittest.TestCase):
    def test_clone_url_is_this_repository(self):
        clones = re.findall(r"git clone https://github\.com/([^/\s]+/[^/\s]+?)(?:\.git)?\s", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertTrue(clones, "README has no git clone line")
        expected = origin_repo()
        if expected is None:
            self.skipTest("no GitHub origin to compare with")
        self.assertEqual({c.lower() for c in clones}, {expected})


if __name__ == "__main__":
    unittest.main()
