"""
tests/test_bootstrap.py
保证 bootstrap.sh 存在、可执行，且能正确解析参数 + 不会触碰 GitHub PAT。
不真跑它（会装 venv），只做静态检查。
"""
import os
import re
import stat
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "bootstrap.sh"


class TestBootstrapScript(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(BOOTSTRAP.exists(), "bootstrap.sh missing")

    def test_is_executable(self):
        mode = BOOTSTRAP.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "bootstrap.sh must be executable (chmod +x)")

    def test_shebang_is_bash(self):
        first = BOOTSTRAP.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("#!") and "bash" in first,
            f"first line should be a bash shebang, got: {first!r}")

    def test_handles_known_flags(self):
        text = BOOTSTRAP.read_text(encoding="utf-8")
        for flag in ("--serve", "--no-sync", "--no-cli", "--clean", "-h", "--help"):
            self.assertIn(flag, text, f"bootstrap.sh should handle {flag}")

    def test_does_not_ask_for_or_write_github_token(self):
        """安全约束：PAT 绝不能进脚本流程。Dev #4 把 token 操作挪到浏览器，bootstrap 也不能回退。"""
        text = BOOTSTRAP.read_text(encoding="utf-8")
        # 不应该出现 "gh_pat_" "github.com/settings/tokens" "PAT" 之类的引导
        self.assertNotIn("gh_pat_", text, "bootstrap should not prompt for a PAT literal")
        self.assertNotIn("settings/tokens", text, "bootstrap should not link to PAT settings")
        # 也不应该写任何 .token / .pat / .gh-token 文件
        bad_paths = re.findall(r'>\s*["\']?\S*(?:\.token|\.pat|\.gh[-_]token)', text)
        self.assertEqual(bad_paths, [], f"bootstrap should not write token files: {bad_paths}")

    def test_runs_python_via_venv_not_system(self):
        """避免污染系统 python。"""
        text = BOOTSTRAP.read_text(encoding="utf-8")
        # 必须用 "$VENV/bin/python" 或 "$VENV/bin/pip"
        self.assertRegex(text, r'\$VENV/bin/(?:python|pip)')
        # 避免直接 pip install（应该用 venv 里的 pip）
        self.assertNotIn("pip3 install", text)

    def test_help_flag_prints_usage(self):
        """`./bootstrap.sh --help` 应成功并打印用法。"""
        if not BOOTSTRAP.exists():
            self.skipTest("bootstrap.sh missing")
        result = subprocess.run(
            [str(BOOTSTRAP), "--help"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, f"--help failed: {result.stderr}")
        self.assertIn("用法", result.stdout, "help output should contain '用法' (usage)")

    def test_unknown_flag_fails_cleanly(self):
        result = subprocess.run(
            [str(BOOTSTRAP), "--definitely-not-a-flag"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(result.returncode, 0, "unknown flag should fail")
        combined = result.stdout + result.stderr
        self.assertIn("未知参数", combined, "should print '未知参数' (unknown argument)")


if __name__ == "__main__":
    unittest.main()
