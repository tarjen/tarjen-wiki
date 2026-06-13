#!/usr/bin/env python3
"""
tools/cli_main.py — Wiki CLI

直接调用各模块, 不通过 HTTP. 因为所有数据/操作都是本地的:
  - contests.csv (本地)
  - git (本地仓库)
  - QOJ (唯一网络出口)

用法:
  wiki doctor                         # 健康检查
  wiki list [--since] [--tag] [--sort]  # 列比赛
  wiki show <slug>                    # 看一场
  wiki update <cid> [--user X] [--dry-run] [-y]
  wiki upsolve <slug|cid> [--user X] [--dry-run] [-y]
  wiki set <slug> --status A=O B=Ø    # 改字段
  wiki rm <slug>
  wiki edit <slug>                    # $EDITOR 改 md 详情页
  wiki codes <cid> [--only-mine] [--sample N] [-y]   # 抓代码
  wiki cookies status | import <file>
  wiki watchlist list | add X | remove X
  wiki serve                          # 可选: 跑 mkdocs preview

设计原则:
  - 直接 import tools/* 模块, 不发 HTTP
  - 默认每个 mutating 命令有 Y/n 确认 (--yes 跳过)
  - 错误打印到 stderr, exit code != 0
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# tools/ 不是 package, 让 module 形式的调用也能找到兄弟模块
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import click  # noqa: E402

from csv_store import Contest, CsvStore  # noqa: E402
from md_store import MdStore  # noqa: E402
from git_ops import GitOps, GitPushError  # noqa: E402
from watchlist import Watchlist  # noqa: E402
from codes_store import CodesStore, ensure_gitignore  # noqa: E402
from codes_logic import FetchRequest, fetch_codes  # noqa: E402
from import_logic import (  # noqa: E402
    ApplyResult, UpdatePreview, UpsolvePreview,
    apply_update, apply_upsolve, build_update_preview, build_upsolve_preview,
    make_client, _slug_from_meta,
)
from platforms import get_client_class  # noqa: E402


# === 配置 ===

def repo_path() -> Path:
    p = os.environ.get("REPO_PATH", "").strip()
    return Path(p).expanduser() if p else Path.cwd()


def config_dir() -> Path:
    p = os.environ.get("CONFIG_DIR", "").strip()
    if p:
        return Path(p).expanduser()
    return Path.home() / ".config" / "wiki"


def codes_dir() -> Path:
    p = os.environ.get("CODES_DIR", "").strip()
    if p:
        return Path(p).expanduser()
    return Path.home() / ".local" / "share" / "wiki" / "codes"


# === 共享 state (per-process) ===

class App:
    """CLI 运行时的全局 state. 启动时初始化一次."""

    def __init__(self):
        self.repo_path: Path = repo_path()
        self.config_dir: Path = config_dir()
        self.codes_dir: Path = codes_dir()
        self.csv: CsvStore | None = None
        self.md: MdStore | None = None
        self.git: GitOps | None = None
        self.watchlist_obj: Watchlist | None = None
        self.codes_store: CodesStore | None = None

    def init(self, verbose: bool = False):
        # 重新读 env (每次都读, 支持 REPO_PATH 等环境变量在测试间变化)
        self.repo_path: Path = repo_path()
        self.config_dir: Path = config_dir()
        self.codes_dir: Path = codes_dir()

        self.csv = CsvStore(self.repo_path / "contests.csv")
        warnings = self.csv.load()
        self.md = MdStore(self.repo_path / "docs" / "contests")
        self.git = GitOps(self.repo_path)
        self.watchlist_obj = Watchlist(self.config_dir / "watchlist.txt")
        self.watchlist_obj.load()
        self.codes_store = CodesStore(self.codes_dir)
        ensure_gitignore(self.codes_dir)

        # CSV warnings 默认静默, --verbose 时显示
        self._csv_warnings = warnings
        if verbose and warnings:
            click.echo(f"\n⚠ CSV 警告 ({len(warnings)} 条):", err=True)
            for w in warnings:
                click.echo(f"  {w}", err=True)


app = App()


# === 工具 ===

def confirm(prompt: str, default_no: bool = True) -> bool:
    suffix = "[Y/n]" if not default_no else "[y/N]"
    resp = click.prompt(f"{prompt} {suffix}", default="", show_default=False)
    resp = resp.strip().lower()
    if not resp:
        return not default_no
    return resp in ("y", "yes")


def load_config_json() -> dict:
    cfg_path = app.config_dir / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def get_user(override: str | None, platform: str = "qoj") -> str:
    """从 CLI 参数或 config 读 user.

    没找到时打印可执行 hint (不是死错), 引导用户修复.
    """
    if override:
        return override
    cfg = load_config_json()
    users = cfg.get("default_user", {})
    if isinstance(users, dict):
        u = users.get(platform)
        if u:
            return u
    # fallback: 顶层字段 (兼容老 config)
    legacy = cfg.get(f"{platform}_username")
    if legacy:
        return legacy

    # 没找到: 给出可执行 hint
    cfg_path = app.config_dir / "config.json"
    click.echo(f"✗ 没指定 user 且 config 里没 default_user.{platform}", err=True)
    click.echo("", err=True)
    click.echo("修复方式 (任选一种):", err=True)
    click.echo(f"  1. 设默认值 (推荐, 以后不用每次传 --user):", err=True)
    click.echo(f"       wiki config set default_user.{platform} tarjen", err=True)
    click.echo(f"  2. 临时指定:", err=True)
    click.echo(f"       wiki update 2564 --user tarjen", err=True)
    click.echo("", err=True)
    click.echo(f"当前 config: {cfg_path}", err=True)
    sys.exit(1)


# === CLI group ===

@click.group()
@click.option("--verbose", "-v", is_flag=True, help="显示 CSV 警告等详细信息")
@click.pass_context
def cli(ctx, verbose):
    """Wiki backend CLI."""
    ctx.ensure_object(dict)
    app.init(verbose=verbose)


# === doctor ===

@cli.command()
@click.option("--verbose", "-v", is_flag=True, help="详细诊断 (Python 版本, 依赖, 文件权限...)")
def doctor(verbose):
    """健康检查."""
    import sys as _sys
    import platform

    click.echo(f"✓ 数据 store 加载完成")
    click.echo(f"  仓库: {app.repo_path}")
    click.echo(f"  比赛: {len(app.csv)}")
    click.echo(f"  watchlist: {len(app.watchlist_obj)} 人")

    repo_status = app.git.status()
    if repo_status.clean:
        click.echo(f"  git: {repo_status.branch} clean, ahead={repo_status.ahead}")
    else:
        click.echo(f"  ⚠ git dirty / ahead={repo_status.ahead} behind={repo_status.behind}")

    # QOJ cookie status
    cookie_path = app.config_dir / "cookies" / "qoj.txt"
    if cookie_path.exists():
        click.echo(f"  QOJ cookie: {cookie_path} ({cookie_path.stat().st_size} bytes)")
    else:
        click.echo(f"  QOJ cookie: ✗ 未配置 (跑 wiki cookies import ...)")

    if verbose:
        click.echo()
        click.echo("── 详细诊断 ──")

        # Python
        click.echo(f"  Python: {_sys.version.split()[0]} ({platform.python_implementation()})")
        click.echo(f"  Platform: {platform.system()} {platform.release()}")

        # 关键依赖
        deps = ["mkdocs", "click", "pymdownx", "material"]
        for dep in deps:
            try:
                mod = __import__(dep)
                ver = getattr(mod, "__version__", "?")
                click.echo(f"  {dep}: {ver} ✓")
            except ImportError:
                click.echo(f"  {dep}: ✗ NOT INSTALLED")

        # Git
        try:
            r = subprocess.run(["git", "--version"], capture_output=True, text=True,
                               timeout=5)
            click.echo(f"  git: {r.stdout.strip()} ✓")
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            click.echo(f"  git: ✗ {e}")

        # venv
        click.echo(f"  venv: {'✓' if '.venv' in str(Path(_sys.executable)) else '?'} ({_sys.executable})")

        # 文件权限 (Unix only, Windows 会跳过)
        if platform.system() != "Windows":
            for p in [cookie_path, app.config_dir / "watchlist.txt"]:
                if p.exists():
                    mode = oct(p.stat().st_mode)[-3:]
                    click.echo(f"  {p}: mode={mode} (期望 600 for cookie)")

        # contests.csv 校验
        try:
            n_total = len(app.csv)
            n_with_body = sum(1 for c in app.csv if app.md.exists(c.slug))
            click.echo(f"  contests.csv: {n_total} 行, {n_with_body} 有 md 详情页")

            # 找出没 md 的
            missing = [c.slug for c in app.csv if not app.md.exists(c.slug)][:5]
            if missing:
                click.echo(f"  ⚠ 缺 md 的 slug (前 5): {missing}")
        except Exception as e:
            click.echo(f"  ⚠ contests.csv 校验失败: {e}")

        # 配置示例
        click.echo()
        click.echo("── 配置示例 ──")
        click.echo(f"  {{\"default_user\": {{\"qoj\": \"tarjen\"}}}}  →  {app.config_dir / 'config.json'}")
        click.echo(f"  watchlist: {app.config_dir / 'watchlist.txt'} (一行一个用户名)")


# === list / show ===

@cli.command("list")
@click.option("--since", help="YYYY-MM-DD")
@click.option("--until", help="YYYY-MM-DD")
@click.option("--tag", multiple=True)
@click.option("--solved-min", type=int)
@click.option("--sort", default="date", type=click.Choice(["date", "solved", "rate", "total"]))
@click.option("--order", default="desc", type=click.Choice(["asc", "desc"]))
@click.option("--limit", type=int)
@click.option("--json", "json_out", is_flag=True, help="输出 JSON")
def list_cmd(since, until, tag, solved_min, sort, order, limit, json_out):
    """列出比赛."""
    contests = list(app.csv.all())
    if since:
        contests = [c for c in contests if c.iso_date >= since]
    if until:
        contests = [c for c in contests if c.iso_date <= until]
    if tag:
        norm_tag = set(t.lstrip("#") for t in tag)
        contests = [
            c for c in contests
            if norm_tag.intersection(t.lstrip("#") for t in c.tags_list)
        ]
    if solved_min is not None:
        contests = [c for c in contests if c.solved >= solved_min]

    if sort == "date":
        key = lambda c: c.iso_date
    elif sort == "solved":
        key = lambda c: c.solved
    elif sort == "rate":
        key = lambda c: c.solved / c.total if c.total else 0
    elif sort == "total":
        key = lambda c: c.total
    contests.sort(key=key, reverse=(order == "desc"))
    if limit:
        contests = contests[:limit]

    if json_out:
        click.echo(json.dumps([{
            "slug": c.slug, "name": c.name, "date": c.date,
            "solved": c.solved, "in_contest": c.in_contest_solved,
            "total": c.total, "tags": c.tags_list,
        } for c in contests], ensure_ascii=False, indent=2))
        return

    click.echo(f"共 {len(contests)} 场")
    for c in contests:
        body_mark = "📝" if app.md.exists(c.slug) else "  "
        click.echo(
            f"  {c.date:12} {c.slug:35} "
            f"{c.solved}/{c.in_contest_solved}/{c.total:2}  {body_mark}"
        )


@cli.command()
@click.argument("slug")
@click.option("--body/--no-body", default=True)
def show(slug, body):
    """查看一场比赛."""
    c = app.csv.get(slug)
    if c is None:
        click.echo(f"✗ slug 不存在: {slug}", err=True)
        sys.exit(1)
    click.echo(f"slug: {c.slug}")
    click.echo(f"name: {c.name}")
    click.echo(f"date: {c.date}")
    click.echo(f"solved: {c.solved} (赛中 {c.in_contest_solved}, 补题 {c.upsolved}) / {c.total}")
    click.echo(f"tags: {' '.join(c.tags_list) or '(无)'}")
    click.echo(f"link: {c.link or '(无)'}")
    click.echo(f"problems: {';'.join(c.problems)}")
    if body and app.md.exists(slug):
        click.echo("\n--- body ---")
        click.echo(app.md.read(slug))


# === CRUD: add / set / rm / edit ===

@cli.command()
@click.option("--slug", required=True)
@click.option("--name", required=True)
@click.option("--date", required=True, help="YYYY.M.D")
@click.option("--total", required=True, type=int)
@click.option("--problems", required=True, help='分号分隔, 例 "O;O;.;O"')
@click.option("--tags", default="")
@click.option("--link", default="")
@click.option("--body", default=None, help="md 详情页内容")
@click.option("--yes", "-y", is_flag=True)
def add(slug, name, date, total, problems, tags, link, body, yes):
    """手工新增一场比赛 (不走 QOJ)."""
    from csv_store import parse_problems
    probs = parse_problems(problems)
    contest = Contest(
        slug=slug, name=name, date=date, solved=0, total=total,
        problems=probs, link=link, tags=tags,
    )
    if not yes and not confirm("确认写入?"):
        click.echo("已取消")
        return

    app.csv.add(contest)
    app.csv.save()

    if body:
        app.md.write(slug, body)
    elif not app.md.exists(slug):
        app.md.write(slug, app.md.placeholder(contest))

    try:
        _run_sync()
    except Exception as e:
        click.echo(f"  ⚠ sync.py 失败: {e}", err=True)

    try:
        sha, pushed = app.git.commit_and_push(
            f"add({slug}): via cli", ["contests.csv", f"docs/contests/{slug}.md"]
        )
    except GitPushError as e:
        click.echo(f"⚠ commit 成功但 push 失败: {e}", err=True)
        click.echo(f"✓ {slug} 已写入 (commit 本地成功, 稍后手动 wiki push)")
        return

    if not pushed and sha:
        click.echo(f"⚠ push 失败但 commit 成功 ({sha[:8]}), 稍后手动 wiki push",
                  err=True)
    click.echo(f"✓ {slug} 已写入 (commit {sha[:8] if sha else 'none'}, pushed={pushed})")


@cli.command()
@click.argument("slug")
@click.option("--name")
@click.option("--date")
@click.option("--total", type=int)
@click.option("--problems", help='分号分隔, 例 "O;O;.;O"')
@click.option("--tags")
@click.option("--link")
@click.option("--status", multiple=True, help="批量改状态: A=O B=Ø")
@click.option("--yes", "-y", is_flag=True)
def set(slug, name, date, total, problems, tags, link, status, yes):
    """改一场的字段. --status 多次指定如: --status A=O --status B=Ø."""
    from csv_store import parse_problems

    fields = {}
    if name: fields["name"] = name
    if date: fields["date"] = date
    if total: fields["total"] = total
    if problems:
        fields["problems"] = parse_problems(problems)
    if tags is not None: fields["tags"] = tags
    if link is not None: fields["link"] = link

    # --status A=O B=Ø 形式
    if status:
        # 拿现有 problems
        c = app.csv.get(slug)
        if c is None:
            click.echo(f"✗ slug 不存在: {slug}", err=True)
            sys.exit(1)
        new_probs = list(c.problems)
        for s in status:
            if "=" not in s:
                click.echo(f"✗ --status 格式错误: {s} (要 LETTER=STATUS)", err=True)
                sys.exit(1)
            letter, st = s.split("=", 1)
            idx = ord(letter.upper()) - ord("A")
            if 0 <= idx < len(new_probs):
                new_probs[idx] = st
            else:
                click.echo(f"⚠ 跳过 {letter} (超出题目范围)", err=True)
        fields["problems"] = new_probs

    if not fields:
        click.echo("✗ 没指定任何字段", err=True)
        sys.exit(1)

    if not yes and not confirm(f"确认更新 {slug} ({len(fields)} 个字段)?"):
        click.echo("已取消")
        return

    app.csv.update(slug, **fields)
    app.csv.save()
    try:
        _run_sync()
    except Exception:
        pass
    try:
        sha, pushed = app.git.commit_and_push(
            f"update({slug}): via cli", ["contests.csv"]
        )
    except GitPushError as e:
        click.echo(f"⚠ push 失败但 commit 成功: {e}", err=True)
        click.echo(f"✓ {slug} 已更新 (commit 本地成功)")
        return
    if not pushed and sha:
        click.echo(f"⚠ push 失败但 commit 成功 ({sha[:8]})", err=True)
    click.echo(f"✓ {slug} 已更新 (commit {sha[:8] if sha else 'none'})")


@cli.command()
@click.argument("slug")
@click.option("--keep-body", is_flag=True)
@click.option("--yes", "-y", is_flag=True)
def rm(slug, keep_body, yes):
    """删除一场比赛."""
    if app.csv.get(slug) is None:
        click.echo(f"✗ slug 不存在: {slug}", err=True)
        sys.exit(1)
    if not yes and not confirm(f"确认删除 {slug}?"):
        click.echo("已取消")
        return
    app.csv.delete(slug)
    app.csv.save()
    body_removed = False
    if not keep_body:
        body_removed = app.md.delete(slug)
    try:
        _run_sync()
    except Exception:
        pass
    paths = ["contests.csv"]
    if body_removed:
        paths.append(f"docs/contests/{slug}.md")
    try:
        sha, pushed = app.git.commit_and_push(f"remove({slug}): via cli", paths)
    except GitPushError as e:
        click.echo(f"⚠ push 失败但 commit 成功: {e}", err=True)
        click.echo(f"✓ {slug} 已删除 (本地)")
        return
    click.echo(f"✓ {slug} 已删除")


@cli.command()
@click.argument("slug")
def edit(slug):
    """用 $EDITOR 改 md 详情页, 保存后自动 commit + push."""
    if app.csv.get(slug) is None:
        click.echo(f"✗ slug 不存在: {slug}", err=True)
        sys.exit(1)
    target = app.md._path(slug)
    if not target.exists():
        # 用占位模板初始化
        target.write_text(app.md.placeholder(app.csv.get(slug)), encoding="utf-8")
    editor = os.environ.get("EDITOR", "vi")
    click.echo(f"打开 {editor} {target} ...")
    subprocess.run([editor, str(target)], check=True)

    try:
        sha, pushed = app.git.commit_and_push(
            f"update({slug}): body via editor", [f"docs/contests/{slug}.md"]
        )
    except GitPushError as e:
        click.echo(f"⚠ push 失败但 commit 成功: {e}", err=True)
        click.echo(f"✓ 已更新 (本地)")
        return
    click.echo(f"✓ 已更新 (commit {sha[:8] if sha else 'none'})")


# === update / upsolve (核心) ===

@cli.command()
@click.argument("cid")
@click.option("--platform", default="qoj")
@click.option("--user", default=None)
@click.option("--yes", "-y", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--slug", default=None)
def update(cid, platform, user, yes, dry_run, slug):
    """从 OJ 导入一场比赛 (赛时表现)."""
    user = get_user(user, platform)
    click.echo(f"用户: {user} (platform: {platform})")

    try:
        preview = build_update_preview(
            platform=platform,
            contest_id=str(cid),
            user=user,
            csv_store=app.csv,
            config_dir=app.config_dir,
            slug_override=slug,
        )
    except ValueError as e:
        msg = str(e)
        if "cookie_missing" in msg:
            click.echo(f"✗ cookie 未配置: {app.config_dir}/cookies/{platform}.txt", err=True)
        else:
            click.echo(f"✗ {msg}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"✗ {type(e).__name__}: {e}", err=True)
        sys.exit(1)

    _show_update_preview(preview)

    if dry_run:
        click.echo("(dry-run, 不写)")
        return

    if not yes and not confirm("确认提交?", default_no=True):
        click.echo("已取消")
        return

    result = apply_update(
        preview=preview, csv_store=app.csv, md_store=app.md,
        git_ops=app.git, create_body=True, run_sync=True, push=True,
    )
    click.echo(f"✓ {result.slug} ({result.record_state})")
    if result.commit_sha:
        click.echo(f"  commit: {result.commit_sha} (pushed={result.pushed})")
    if result.body_written:
        click.echo(f"  body: {result.body_written}")


@cli.command()
@click.argument("cid_or_slug")
@click.option("--platform", default="qoj")
@click.option("--user", default=None)
@click.option("--yes", "-y", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--since", default=None)
def upsolve(cid_or_slug, platform, user, yes, dry_run, since):
    """检测赛后补题, 更新 contests.csv."""
    user = get_user(user, platform)
    click.echo(f"用户: {user} (platform: {platform})")

    if cid_or_slug.isdigit():
        contest_id, slug = cid_or_slug, None
    else:
        contest_id, slug = None, cid_or_slug

    try:
        preview = build_upsolve_preview(
            platform=platform, contest_id=contest_id, slug=slug, user=user,
            csv_store=app.csv, config_dir=app.config_dir,
            since_override=since,
        )
    except ValueError as e:
        msg = str(e)
        if "slug_not_found_in_csv" in msg:
            click.echo(f"✗ slug 在 CSV 里找不到, 先跑 wiki update <cid>", err=True)
        else:
            click.echo(f"✗ {msg}", err=True)
        sys.exit(1)

    click.echo(f"slug: {preview.slug}")
    click.echo(f"当前: {''.join(preview.current_problems)}")
    if not preview.changes:
        click.echo("(无变化)")
        return

    click.echo(f"\n检测到 {len(preview.changes)} 题变化:")
    for ch in preview.changes:
        click.echo(f"  {ch['letter']} {ch['before']} -> {ch['after']} ({ch['verdict']}, {ch.get('submitted_at', '?')})")

    if dry_run:
        click.echo("(dry-run, 不写)")
        return

    if not yes and not confirm("确认更新?", default_no=True):
        click.echo("已取消")
        return

    result = apply_upsolve(
        preview=preview, csv_store=app.csv, md_store=app.md,
        git_ops=app.git, push=True,
    )
    click.echo(f"✓ {result.slug}")
    if result.commit_sha:
        click.echo(f"  commit: {result.commit_sha}")


def _show_update_preview(preview) -> None:
    c = preview.contest
    click.echo(f"\n=== {'CREATE NEW' if preview.record_state=='create_new' else 'UPDATE EXISTING'} ===")
    click.echo(f"比赛: {c['title']} ({c['problem_count']} 题)")
    click.echo(f"slug: {preview.slug} {'(已存在)' if preview.slug_exists else ''}")
    click.echo(f"date: {preview.suggested.get('date', '?')}")
    click.echo(f"link: {preview.suggested.get('link', '')}")
    click.echo()
    for p in preview.problems:
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
    s = preview.summary
    click.echo(f"\n汇总: O×{s['O']}, !×{s['!']}, .×{s['.']}")


# === codes (抓代码) ===

@cli.command()
@click.argument("cid")
@click.option("--platform", default="qoj")
@click.option("--user", default=None)
@click.option("--only-mine", is_flag=True)
@click.option("--only-watchlist", is_flag=True)
@click.option("--no-watchlist", is_flag=True)
@click.option("--sample", "sample_n", default=1, type=int)
@click.option("--problem", multiple=True)
@click.option("--status", default="AC")
@click.option("--refresh", is_flag=True)
@click.option("--yes", "-y", is_flag=True)
def codes(cid, platform, user, only_mine, only_watchlist, no_watchlist,
          sample_n, problem, status, refresh, yes):
    """抓代码. 默认: 自己 + watchlist + 每题最快 1 个 AC."""
    user = get_user(user, platform)

    req = FetchRequest(
        platform=platform, cid=str(cid), username=user,
        fetch_self=not only_watchlist,
        fetch_watchlist=not (only_mine or no_watchlist),
        fetch_others="none" if only_mine else (
            "none" if only_watchlist else "top_n_fastest"),
        others_n=sample_n,
        problems=list(problem) if problem else None,
        skip_existing=not refresh,
        request_interval=1.5,
    )

    client_factory = lambda p: make_client(p, app.config_dir)
    progress = {"fetched": 0, "total": 0}

    def on_progress(p):
        progress.update(p)

    click.echo(f"抓取 {platform}/contest/{cid} (用户 {user})...")

    try:
        result = fetch_codes(req, client_factory, app.codes_store,
                            app.watchlist_obj, progress_callback=on_progress)
    except Exception as e:
        click.echo(f"✗ {type(e).__name__}: {e}", err=True)
        sys.exit(1)

    click.echo(f"\n完成: 抓 {result.fetched}, 跳过 {result.skipped_existing}, "
              f"错误 {result.errors} ({result.duration_seconds:.1f}s)")
    # by source
    by_src = {}
    for f in result.files:
        by_src.setdefault(f["source"], 0)
        by_src[f["source"]] += 1
    for src, n in sorted(by_src.items()):
        click.echo(f"  [{src}] {n} 份")


@cli.command("codes-list")
@click.argument("cid")
@click.option("--problem", multiple=True)
@click.option("--user", multiple=True)
@click.option("--source", type=click.Choice(["mine", "watchlist", "sample", "other"]))
def codes_list(cid, problem, user, source):
    """列出已抓的代码."""
    files = app.codes_store.list_files(
        str(cid), problem=list(problem) or None, user=list(user) or None,
        source=source,
    )
    if not files:
        click.echo("(空)")
        return
    # 按题号 + user 分组
    from collections import defaultdict
    by_prob = defaultdict(list)
    for f in files:
        by_prob[f.problem].append(f)
    for prob in sorted(by_prob.keys()):
        click.echo(f"\n{prob} 题 ({len(by_prob[prob])} 份):")
        for f in sorted(by_prob[prob], key=lambda x: x.user):
            click.echo(f"  [{f.source:9}] {f.user:15} {f.problem}.{_ext(f.path)} "
                      f"({f.size}B)")


@cli.command("codes-show")
@click.argument("cid")
@click.argument("user")
@click.argument("problem")
def codes_show(cid, user, problem):
    """显示某份代码."""
    code = app.codes_store.read(str(cid), user, problem)
    if code is None:
        click.echo(f"✗ 没找到 {cid}/{user}/{problem}", err=True)
        sys.exit(1)
    pager = os.environ.get("PAGER", "less")
    subprocess.run([pager], input=code, text=True)


def _ext(path_str: str) -> str:
    return Path(path_str).suffix.lstrip(".")


# === cookies ===

@cli.group()
def cookies():
    pass


@cookies.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--platform", default="qoj")
def cookies_import(file, platform):
    """导入 Netscape cookie jar."""
    import shutil
    target = app.config_dir / "cookies" / f"{platform}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(file, target)
    # chmod 600 仅在 Unix 上有效, Windows 会忽略 OSError
    try:
        target.chmod(0o600)
    except (OSError, NotImplementedError):
        pass
    click.echo(f"✓ 已导入 → {target}")


@cookies.command("status")
@click.option("--platform", default="qoj")
def cookies_status(platform):
    """看 cookie 状态."""
    p = app.config_dir / "cookies" / f"{platform}.txt"
    if not p.exists():
        click.echo(f"✗ {platform} 没 cookie 配置 (跑: wiki cookies import <file>)")
        return
    age = "? 天"
    click.echo(f"✓ {platform}: {p} ({p.stat().st_size} bytes)")


# === watchlist ===

@cli.group()
def watchlist():
    pass


@watchlist.command("list")
def watchlist_list():
    """看当前名单."""
    users = app.watchlist_obj.users()
    if not users:
        click.echo("(空)")
    for u in users:
        click.echo(u)


@watchlist.command("add")
@click.argument("users", nargs=-1)
def watchlist_add(users):
    """加用户."""
    added = app.watchlist_obj.add(list(users))
    for u in added:
        click.echo(f"+ {u}")


@watchlist.command("remove")
@click.argument("users", nargs=-1)
@click.option("--all", "rm_all", is_flag=True)
def watchlist_remove(users, rm_all):
    """删用户."""
    if rm_all:
        users = app.watchlist_obj.users()
    removed = app.watchlist_obj.remove(list(users))
    for u in removed:
        click.echo(f"- {u}")


# === sync ===

@cli.command()
def sync():
    """跑 tools/sync.py (重新生成 docs/index.md + data/contests.json)."""
    _run_sync()
    click.echo("✓ sync.py 完成")


def _run_sync():
    sync_py = Path(__file__).resolve().parent / "sync.py"
    if not sync_py.exists():
        return
    subprocess.run(
        [sys.executable, str(sync_py)],
        cwd=str(app.repo_path),
        check=False,
        capture_output=True,
        timeout=30,
    )


# === config ===

@cli.group()
def config():
    """管理 ~/.config/wiki/config.json."""
    pass


@config.command("show")
def config_show():
    """显示当前 config."""
    cfg = load_config_json()
    if not cfg:
        cfg_path = app.config_dir / "config.json"
        click.echo(f"(空, 配置文件不存在: {cfg_path})")
        click.echo("")
        click.echo("创建方法:")
        click.echo(f'  mkdir -p {app.config_dir}')
        click.echo(f'  echo \'{{"default_user": {{"qoj": "tarjen"}}}}\' > {cfg_path}')
        return
    click.echo(json.dumps(cfg, ensure_ascii=False, indent=2))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """设一个 config 项. 例: wiki config set default_user.qoj tarjen."""
    cfg_path = app.config_dir / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config_json()

    # 支持点路径: default_user.qoj → {"default_user": {"qoj": ...}}
    keys = key.split(".")
    cur = cfg
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value

    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    click.echo(f"✓ {key} = {value!r} 写入 {cfg_path}")
    # 显示更新后的相关部分
    click.echo(f"  当前值: {cfg}")


@config.command("get")
@click.argument("key")
def config_get(key):
    """读一个 config 项."""
    cfg = load_config_json()
    keys = key.split(".")
    cur = cfg
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            click.echo(f"(未设置)", err=True)
            sys.exit(1)
    if isinstance(cur, (dict, list)):
        click.echo(json.dumps(cur, ensure_ascii=False, indent=2))
    else:
        click.echo(cur)


@config.command("path")
def config_path():
    """显示 config 文件路径."""
    cfg_path = app.config_dir / "config.json"
    click.echo(str(cfg_path))
    if cfg_path.exists():
        click.echo("(存在)")
    else:
        click.echo("(不存在, 跑 wiki config set ... 创建)")


# === serve (mkdocs preview, 不是后端) ===

@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000, type=int)
def serve(host, port):
    """跑 mkdocs preview (前端静态页)."""
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "mkdocs"
    if not venv_py.exists():
        click.echo(f"✗ 找不到 {venv_py}, 跑 ./bootstrap.sh 装 venv", err=True)
        sys.exit(1)
    subprocess.run([str(venv_py), "serve", "-a", f"{host}:{port}"],
                   cwd=str(app.repo_path))


if __name__ == "__main__":
    cli(obj={})