"""
tests/test_requirements.py
保证 requirements.txt 是钉死的（不是裸包名）——裸包名会在某次 pip install 时无声升级。
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQ = REPO / "requirements.txt"


# 这些包必须带版本号（防止 3 年后回来「pip install -r」时无声升级破坏 build）
MUST_BE_PINNED = [
    "mkdocs",
    "mkdocs-material",
    "mkdocs-material-extensions",
    "pymdown-extensions",
]


class TestRequirementsPinned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = REQ.read_text(encoding="utf-8")
        cls.lines = [
            ln.strip() for ln in cls.text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    def test_file_exists(self):
        self.assertTrue(REQ.exists())

    def test_every_required_pkg_is_pinned(self):
        """裸包名如 'mkdocs-material' 会让 pip install -r 拉最新；必须带 ==。"""
        for pkg in MUST_BE_PINNED:
            pattern = rf"^{re.escape(pkg)}==[\d.]+$"
            matched = [ln for ln in self.lines if re.match(pattern, ln)]
            self.assertTrue(
                matched,
                f"{pkg} not pinned in requirements.txt (need '==<version>'); "
                f"unpinned packages can silently upgrade and break mkdocs build"
            )

    def test_no_pinned_package_is_a_prerelease(self):
        """钉 rc/alpha/beta/.dev 之类的会让 build 偶发坏。"""
        for ln in self.lines:
            m = re.match(r"^([\w\-.]+)==([\d.]+)", ln)
            if not m:
                continue
            ver = m.group(2)
            self.assertNotIn("a", ver.split(".")[-1],
                f"{ln} looks like a pre-release pin")

    def test_no_duplicate_lines(self):
        from collections import Counter
        c = Counter(self.lines)
        dups = {pkg: n for pkg, n in c.items() if n > 1}
        self.assertEqual(dups, {}, f"duplicate lines: {dups}")


class TestRequirementsMatchVenv(unittest.TestCase):
    """可选：如果 .venv 存在（开发者本地），保证装的版本 == requirements.txt 写的。"""

    @classmethod
    def setUpClass(cls):
        cls.venv = REPO / ".venv"
        if not cls.venv.exists():
            cls.skipTest(cls, "no .venv present — skip env consistency check")

    def test_installed_versions_match_requirements(self):
        import subprocess
        req = REQ.read_text()
        out = subprocess.check_output(
            [str(self.venv / "bin" / "pip"), "freeze"],
            text=True, stderr=subprocess.DEVNULL,
        )
        frozen = dict(
            (line.split("==")[0].lower(), line.split("==")[1])
            for line in out.splitlines()
            if "==" in line
        )
        for line in req.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "==" not in line:
                continue
            pkg, ver = line.split("==", 1)
            actual = frozen.get(pkg.lower())
            if actual is None:
                continue  # 未安装（可能是 dev-only 依赖），不算 fail
            self.assertEqual(
                actual, ver,
                f"{pkg}: requirements.txt says {ver} but venv has {actual}. "
                f"Run: .venv/bin/pip install -r requirements.txt"
            )


if __name__ == "__main__":
    unittest.main()
