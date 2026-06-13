"""
tests/test_cli_config.py

CLI 配置命令 + get_user hint 测试.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def setup_env() -> dict:
    tmp = tempfile.mkdtemp()
    repo = Path(tmp) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"],
                   check=True, capture_output=True)
    (repo / "contests.csv").write_text(
        "slug,name,date,solved,total,problems,link,tags\n", encoding="utf-8",
    )
    (repo / "docs" / "contests").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    cfg = Path(tmp) / "cfg"
    (cfg / "cookies").mkdir(parents=True)
    return {"tmp_root": Path(tmp), "repo": repo, "cfg": cfg}


class ConfigCLITestBase(unittest.TestCase):
    def setUp(self):
        self.env = setup_env()
        os.environ["REPO_PATH"] = str(self.env["repo"])
        os.environ["CONFIG_DIR"] = str(self.env["cfg"])
        os.environ["CODES_DIR"] = str(self.env["tmp_root"] / "codes")
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        from click.testing import CliRunner
        self.runner = CliRunner()
        import cli_main
        cli_main.app.init()

    def tearDown(self):
        for k in ["REPO_PATH", "CONFIG_DIR", "CODES_DIR"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.env["tmp_root"], ignore_errors=True)

    def invoke(self, *args, **kwargs):
        from cli_main import cli
        return self.runner.invoke(cli, list(args), **kwargs)


class TestConfigShow(ConfigCLITestBase):
    def test_show_empty(self):
        r = self.invoke("config", "show")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("(空", r.output)
        self.assertIn("创建方法", r.output)

    def test_show_existing(self):
        cfg_path = self.env["cfg"] / "config.json"
        cfg_path.write_text(json.dumps({"default_user": {"qoj": "tarjen"}}),
                          encoding="utf-8")
        r = self.invoke("config", "show")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("tarjen", r.output)


class TestConfigSet(ConfigCLITestBase):
    def test_set_simple(self):
        r = self.invoke("config", "set", "qoj_username", "alice")
        self.assertEqual(r.exit_code, 0, r.output)
        cfg_path = self.env["cfg"] / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["qoj_username"], "alice")

    def test_set_nested(self):
        r = self.invoke("config", "set", "default_user.qoj", "bob")
        self.assertEqual(r.exit_code, 0, r.output)
        cfg_path = self.env["cfg"] / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["default_user"]["qoj"], "bob")

    def test_set_preserves_existing(self):
        cfg_path = self.env["cfg"] / "config.json"
        cfg_path.write_text(json.dumps({"existing_key": "value"}),
                          encoding="utf-8")
        r = self.invoke("config", "set", "new_key", "new_value")
        self.assertEqual(r.exit_code, 0)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["existing_key"], "value")
        self.assertEqual(cfg["new_key"], "new_value")


class TestConfigGet(ConfigCLITestBase):
    def test_get_simple(self):
        cfg_path = self.env["cfg"] / "config.json"
        cfg_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        r = self.invoke("config", "get", "foo")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("bar", r.output)

    def test_get_nested(self):
        cfg_path = self.env["cfg"] / "config.json"
        cfg_path.write_text(json.dumps({"a": {"b": {"c": "deep"}}}),
                          encoding="utf-8")
        r = self.invoke("config", "get", "a.b.c")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("deep", r.output)

    def test_get_missing(self):
        r = self.invoke("config", "get", "nope")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("未设置", r.output)


class TestConfigPath(ConfigCLITestBase):
    def test_path(self):
        r = self.invoke("config", "path")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("config.json", r.output)


class TestGetUserHint(ConfigCLITestBase):
    def test_no_user_shows_hint(self):
        """没 user 时 get_user 退出 + 给出 hint."""
        # 没有 config, 也没 --user
        r = self.invoke("update", "2564", "--dry-run")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("✗", r.output)
        # 应该给出修复提示
        self.assertIn("修复方式", r.output)
        self.assertIn("wiki config set default_user.qoj", r.output)
        self.assertIn("--user", r.output)

    def test_user_override_works(self):
        """--user 临时覆盖."""
        cfg_path = self.env["cfg"] / "config.json"
        cfg_path.write_text(json.dumps({"default_user": {"qoj": "tarjen"}}),
                          encoding="utf-8")
        r = self.invoke("update", "2564", "--user", "alice", "--dry-run")
        # 没 cookie, 应该走到 cookie 错
        self.assertIn("cookie", r.output.lower())


class TestUpdateShowsUser(ConfigCLITestBase):
    def test_update_displays_user(self):
        cfg_path = self.env["cfg"] / "config.json"
        cfg_path.write_text(json.dumps({"default_user": {"qoj": "alice"}}),
                          encoding="utf-8")
        r = self.invoke("update", "2564", "--dry-run")
        # 应该显示 user 和 platform
        self.assertIn("用户: alice", r.output)
        self.assertIn("platform: qoj", r.output)


if __name__ == "__main__":
    unittest.main()