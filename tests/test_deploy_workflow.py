"""
tests/test_deploy_workflow.py
防止 deploy.yml 的关键安全 / 性能属性被悄悄改掉。

不依赖 PyYAML，用 stdlib 简单 parser（GitHub Actions 的 workflow 总是顶层 keys + 2 空格缩进）。
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"


class TestDeployWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.lines = cls.text.splitlines()

    def test_file_exists(self):
        self.assertTrue(WORKFLOW.exists())

    def test_trigger_on_main_push(self):
        # 简单检查 on.push.branches 含 main
        self.assertRegex(self.text, r"branches:\s*\[\s*main\s*\]")
        self.assertIn("workflow_dispatch", self.text)

    def test_has_concurrency_block(self):
        """没有 concurrency 的话，连按多次会 3 个 deploy 抢 gh-pages 分支。"""
        # 用 \n 锚定（assertRegex 默认不开启 re.MULTILINE）
        self.assertRegex(self.text, r"\nconcurrency:\s*\n", "missing concurrency: block")
        # 紧跟 group + cancel-in-progress
        self.assertRegex(self.text, r"group:\s*deploy")
        self.assertRegex(self.text, r"cancel-in-progress:\s*true")

    def test_has_timeout_minutes(self):
        """mkdocs gh-deploy 网络抽风能挂几小时；不设超时整个 job 会僵死。"""
        self.assertRegex(self.text, r"timeout-minutes:\s*\d+")

    def test_timeout_is_reasonable(self):
        """超时设 1 分钟太短，60 分钟太长；5-30 分钟合理。"""
        m = re.search(r"timeout-minutes:\s*(\d+)", self.text)
        self.assertIsNotNone(m)
        n = int(m.group(1))
        self.assertGreaterEqual(n, 5, f"timeout {n}min 太短")
        self.assertLessEqual(n, 30, f"timeout {n}min 太长")

    def test_minimal_permissions(self):
        """只需要 contents: write。其它 permission 不该给。"""
        self.assertRegex(self.text, r"contents:\s*write")
        # 确认没给 id-token / packages / pages 之类
        bad = re.findall(r"^\s*(id-token|packages|pages):", self.text, re.M)
        self.assertEqual(bad, [], f"unexpected permissions: {bad}")

    def test_runs_sync_before_install(self):
        """sync.py 不依赖 mkdocs（只 stdlib），放最前是合理的；不强制但记录。"""
        sync_idx = self.text.find("Sync CSV")
        install_idx = self.text.find("Install dependencies")
        self.assertGreater(sync_idx, 0)
        self.assertGreater(install_idx, sync_idx,
            "sync.py should run before pip install (sync doesn't need mkdocs)")

    def test_uses_force_and_clean(self):
        """mkdocs gh-deploy --force --clean 是已知正确的组合。"""
        self.assertIn("mkdocs gh-deploy", self.text)
        self.assertIn("--force", self.text)
        self.assertIn("--clean", self.text)


if __name__ == "__main__":
    unittest.main()
