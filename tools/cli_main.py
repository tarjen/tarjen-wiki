#!/usr/bin/env python3
"""
tools/cli_main.py — Wiki CLI

用法:
  python3 -m tools.cli_main <command> [args]
或装到 PATH:
  ln -s $(pwd)/.venv/bin/python /usr/local/bin/wiki -t /usr/local/bin
  # 或者 bootstrap.sh 加 wrapper

设计原则:
  - 调 HTTP API (localhost:8001), 不直连 store
  - 服务没起时报清晰错误 (而非 traceback)
  - 默认每个 mutating 命令有确认 (--yes 跳过)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# tools/ 不是 package, 让 module 形式的调用也能找到兄弟模块
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import click
import httpx

# === 配置 ===

DEFAULT_BASE_URL = os.environ.get("WIKI_API", "http://127.0.0.1:8001")
DEFAULT_TIMEOUT = 30


def get_client(base_url: str = DEFAULT_BASE_URL) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=DEFAULT_TIMEOUT)


def call_api(method: str, path: str, **kwargs) -> dict:
    """调 API. 错误时打印并退出."""
    try:
        r = get_client().request(method, path, **kwargs)
        if r.status_code >= 400:
            data = r.json()
            err = data.get("error", {})
            code = err.get("code", "unknown")
            msg = err.get("message", r.text)
            click.echo(f"✗ {code}: {msg}", err=True)
            sys.exit(1)
        return r.json()
    except httpx.ConnectError:
        click.echo("✗ 无法连接后端 (server 没起?). 跑: wiki serve --install", err=True)
        sys.exit(1)
    except httpx.HTTPError as e:
        click.echo(f"✗ HTTP error: {e}", err=True)
        sys.exit(1)


def confirm(prompt: str, default_no: bool = True) -> bool:
    """Y/n 确认. default_no=True 时回车=N."""
    suffix = "[Y/n]" if not default_no else "[y/N]"
    resp = click.prompt(f"{prompt} {suffix}", default="", show_default=False)
    resp = resp.strip().lower()
    if not resp:
        return not default_no
    return resp in ("y", "yes")


# === CLI group ===

@click.group()
@click.option("--api", default=DEFAULT_BASE_URL, help="Backend base URL")
@click.pass_context
def cli(ctx, api):
    """Wiki backend CLI."""
    ctx.ensure_object(dict)
    ctx.obj["api"] = api


# === serve ===

@cli.command()
@click.option("--install", is_flag=True, help="装 systemd 单元")
@click.option("--uninstall", is_flag=True, help="卸 systemd 单元")
@click.option("--status", "status_flag", is_flag=True, help="看状态")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8001, type=int)
def serve(install, uninstall, status_flag, host, port):
    """管理后端服务."""
    if install:
        # 调用 bootstrap.sh --install-service (TODO: 后续 phase)
        click.echo("✗ 还没实现: 跑 bootstrap.sh --install-service")
        sys.exit(1)
    if uninstall:
        click.echo("✗ 还没实现")
        sys.exit(1)
    if status_flag:
        # systemctl --user status wiki-backend
        click.echo("✗ 还没实现")
        sys.exit(1)
    # 否则前台启动
    import uvicorn
    os.environ["BIND"] = host
    os.environ["PORT"] = str(port)
    # 设 cwd 到 repo root (为了 tools/* 相对 import)
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(str(repo_root))
    sys.path.insert(0, str(repo_root / "tools"))
    import server
    uvicorn.run(server.app, host=host, port=port, log_level="info")


# === doctor ===

@cli.command()
def doctor():
    """健康检查."""
    data = call_api("GET", "/healthz")
    click.echo(f"✓ 后端在跑 (uptime {data.get('uptime_seconds', '?')}s)")
    click.echo(f"  仓库: {data['config']['repo_path']}")
    click.echo(f"  比赛数: {data['csv']['contests']}")
    click.echo(f"  watchlist: {data['watchlist_count']} 人")
    if data.get("repo"):
        r = data["repo"]
        if r["clean"]:
            click.echo(f"  git: {r['branch']} clean, ahead={r['ahead']}")
        else:
            click.echo(f"  ⚠ git dirty / ahead={r['ahead']} behind={r['behind']}")


# === list / show ===

@cli.command("list")
@click.option("--since", help="YYYY-MM-DD")
@click.option("--tag", multiple=True)
@click.option("--solved-min", type=int)
@click.option("--sort", default="date", type=click.Choice(["date", "solved", "rate", "total"]))
@click.option("--limit", type=int)
@click.option("--json", "json_out", is_flag=True, help="输出 JSON")
def list_cmd(since, tag, solved_min, sort, limit, json_out):
    """列出比赛."""
    params = {}
    if since: params["since"] = since
    if tag: params["tag"] = list(tag)
    if solved_min is not None: params["solved_min"] = solved_min
    if sort: params["sort"] = sort
    if limit: params["limit"] = limit
    data = call_api("GET", "/contests", params=params)
    if json_out:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return

    click.echo(f"共 {data['count']} 场")
    for c in data["contests"]:
        body_mark = "📝" if c["body_exists"] else "  "
        click.echo(
            f"  {c['date']:12} {c['slug']:35} "
            f"{c['solved']}/{c['in_contest']}/{c['total']:2}  {body_mark}"
        )


@cli.command()
@click.argument("slug")
@click.option("--body/--no-body", default=True)
def show(slug, body):
    """查看一场比赛."""
    data = call_api("GET", f"/contests/{slug}")
    click.echo(f"slug: {data['slug']}")
    click.echo(f"name: {data['name']}")
    click.echo(f"date: {data['date']}")
    click.echo(f"solved: {data['solved']} (赛中 {data['in_contest']}, 补题 {data['upsolved']}) / {data['total']}")
    click.echo(f"tags: {' '.join(data['tags'])}")
    click.echo(f"link: {data['link']}")
    click.echo(f"problems: {''.join(data['problems'])}")
    if body and data.get("body"):
        click.echo("\n--- body ---")
        click.echo(data["body"])


# === update / upsolve (核心) ===

@cli.command()
@click.argument("cid")
@click.option("--platform", default="qoj")
@click.option("--user", default=None, help="QOJ 用户名 (默认从 config)")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@click.option("--dry-run", is_flag=True)
@click.option("--slug", default=None)
def update(cid, platform, user, yes, dry_run, slug):
    """从 OJ 导入一场比赛 (赛时表现).

    CID 是 OJ 比赛 ID (QOJ 用整数).

    示例:
      wiki update 2564
      wiki update 2564 --user alice
    """
    body = {"platform": platform, "contest_id": str(cid)}
    if user: body["user"] = user
    if slug: body["slug"] = slug

    click.echo(f"抓取 {platform}/contest/{cid}...")
    preview = call_api("POST", "/import/update-preview", json=body)

    # 显示预览
    _show_update_preview(preview)

    if dry_run:
        click.echo("(dry-run, 不写)")
        return

    if not yes:
        if not confirm("确认提交?", default_no=True):
            click.echo("已取消")
            return

    # apply
    apply_body = {
        "platform": platform,
        "preview": preview,
        "overrides": {},
        "options": {"create_body": True, "run_sync": True, "push": True},
    }
    result = call_api("POST", "/import/update-apply", json=apply_body)
    click.echo(f"✓ {result['slug']} ({result['record_state']})")
    if result.get("commit_sha"):
        click.echo(f"  commit: {result['commit_sha']} (pushed={result['pushed']})")
    click.echo(f"  body: {result.get('body_written', '(none)')}")


@cli.command()
@click.argument("cid_or_slug")
@click.option("--platform", default="qoj")
@click.option("--user", default=None)
@click.option("--yes", "-y", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--since", default=None, help="ISO 时间, 不传则用 contest.end_time")
def upsolve(cid_or_slug, platform, user, yes, dry_run, since):
    """检测赛后补题, 更新 contests.csv.

    CID_OR_SLUG 可以是 contest_id (会先 fetch meta 找 slug) 或直接 slug.
    """
    body = {"platform": platform}
    # 判断是 cid 还是 slug
    if cid_or_slug.isdigit():
        body["contest_id"] = cid_or_slug
    else:
        body["slug"] = cid_or_slug
    if user: body["user"] = user
    if since: body["since"] = since

    click.echo(f"检查补题 ({platform}: {cid_or_slug})...")
    preview = call_api("POST", "/import/upsolve-preview", json=body)

    click.echo(f"slug: {preview['slug']}")
    click.echo(f"当前: {''.join(preview['current_problems'])}")
    if not preview["changes"]:
        click.echo("(无变化)")
        return

    click.echo(f"\n检测到 {len(preview['changes'])} 题变化:")
    for ch in preview["changes"]:
        click.echo(f"  {ch['letter']} {ch['before']} -> {ch['after']} ({ch['verdict']}, {ch.get('submitted_at', '?')})")

    if dry_run:
        click.echo("(dry-run, 不写)")
        return

    if not yes:
        if not confirm("确认更新?", default_no=True):
            click.echo("已取消")
            return

    result = call_api("POST", "/import/upsolve-apply", json={
        "platform": platform, "preview": preview,
        "options": {"push": True},
    })
    click.echo(f"✓ 更新 {result['slug']}")
    if result.get("commit_sha"):
        click.echo(f"  commit: {result['commit_sha']}")


def _show_update_preview(preview: dict) -> None:
    """显示 update-preview 给人看."""
    c = preview["contest"]
    click.echo(f"\n=== {'CREATE NEW' if preview['record_state']=='create_new' else 'UPDATE EXISTING'} ===")
    click.echo(f"比赛: {c['title']} ({c['problem_count']} 题)")
    click.echo(f"slug: {preview['slug']} {'(已存在)' if preview['slug_exists'] else ''}")
    click.echo(f"date: {preview['suggested'].get('date', '?')}")
    click.echo(f"link: {preview['suggested'].get('link', '')}")
    click.echo()
    for p in preview["problems"]:
        if p["status"] == "O":
            mark = click.style("O", fg="green")
        elif p["status"] == "Ø":
            mark = click.style("Ø", fg="yellow")
        elif p["status"] == "!":
            mark = click.style("!", fg="red")
        else:
            mark = "."
        extra = ""
        if p.get("contest_time"):
            extra = f"  ({p['contest_time']}, {p.get('tries', '?')}x)"
        elif p.get("no_submission"):
            extra = "  (未提交)"
        click.echo(f"  {p['letter']} {mark}{extra}")
    s = preview["summary"]
    click.echo(f"\n汇总: O×{s['O']}, !×{s['!']}, .×{s['.']}")


# === cookies ===

@cli.group()
def cookies():
    """管理 OJ cookie."""
    pass


@cookies.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--platform", default="qoj")
def cookies_import(file, platform):
    """导入 Netscape cookie jar 文件."""
    with open(file, "rb") as f:
        files = {"file": (Path(file).name, f, "text/plain")}
        data = {"platform": platform}
        r = get_client().post("/import/cookies/import", files=files, data=data)
        if r.status_code >= 400:
            click.echo(f"✗ {r.json().get('error', {}).get('message', r.text)}", err=True)
            sys.exit(1)
        result = r.json()
        click.echo(f"✓ 导入 {result.get('cookies_loaded', '?')} 个 cookie")


@cookies.command("status")
@click.option("--platform", default="qoj")
def cookies_status(platform):
    """看 cookie 状态."""
    data = call_api("GET", "/import/cookies/status",
                    params={"platform": platform})
    if not data.get("cookies_loaded"):
        click.echo(f"✗ {platform} 没 cookie 配置")
        return
    age = data.get("age_days", "?")
    click.echo(f"✓ {platform}: {data['cookies_loaded']} cookies, 上次成功 {age} 天前")
    if data.get("warning"):
        click.echo(f"  ⚠ {data['warning']}")


# === entry ===

if __name__ == "__main__":
    cli(obj={})