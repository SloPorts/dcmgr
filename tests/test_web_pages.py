"""Static checks on the web pages: unique element ids, every $("id") the scripts use exists (or is one
they create), and the scripts parse (when Node.js is available)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter

from rig import ROOT

PAGES = {
    # ids the page's own script creates at runtime
    "index.html": {"holdmin", "details", "cal-run-btn", "cal-apply", "cal-apply-manual", "cal-manual"},
    "session.html": {"video"},
}


def read(name):
    with open(os.path.join(ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


class PageTests(unittest.TestCase):
    def test_ids_are_unique(self):
        for name in PAGES:
            ids = Counter(re.findall(r'\bid="([^"$]+)"', read(name)))
            self.assertEqual([i for i, n in ids.items() if n > 1], [], name)

    def test_scripts_only_use_existing_ids(self):
        for name, dynamic in PAGES.items():
            page = read(name)
            static = set(re.findall(r'\bid="([^"$]+)"', page))
            used = set(re.findall(r'\$\("([\w-]+)"\)', page))
            self.assertEqual(sorted(used - static - dynamic), [], name)

    def test_hidden_always_wins(self):
        # a class rule like .adjust { display: grid } overrides the hidden attribute unless this exists;
        # the picture adjuster showed on page load because of that
        for name in PAGES:
            self.assertRegex(read(name), r"\[hidden\]\s*\{\s*display:\s*none\s*!important", name)

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_scripts_parse(self):
        for name in PAGES:
            scripts = re.findall(r"<script>(.*?)</script>", read(name), re.S)
            for i, js in enumerate(scripts):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                    f.write(js)
                try:
                    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
                finally:
                    os.unlink(f.name)
                self.assertEqual(r.returncode, 0, f"{name} script {i}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
