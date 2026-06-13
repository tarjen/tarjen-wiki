#!/usr/bin/env python3
"""
tools/import_logic.py — 比赛导入的业务逻辑 (preview + apply)

被 tools/server.py 调用. 纯函数 + 后端 store, 不依赖 FastAPI.

核心操作:
  build_update_preview(platform, cid, user)  -> UpdatePreview
  apply_update(preview, ...)                  -> ApplyResult
  build_upsolve_preview(platform, cid, slug, user) -> UpsolvePreview
  apply_upsolve(preview, ...)                 -> ApplyResult

状态映射 (in-contest):
  AC  -> O
  其他 -> !  (WA/TLE/RE/MLE)
  没提交 -> .

upsolve 判定:
  当前 . + 赛后 AC -> Ø
  当前 ! + 赛后 AC -> Ø (赛后补过)
  其他 -> 不变
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from csv_store import Contest
from md_store import MdStore
from csv_store import CsvStore
from git_ops import GitOps

from platforms import get_client_class
from platforms.base import PlatformClient, StandingsEntry, Submission


ProblemStatus = Literal["O", "Ø", "!", "."]


# === Helpers ===

def slugify(s: str) -> str:
    """从 name 生成 slug. 例: '2025 ICPC XXX Regional' -> '2025-icpc-xxx-regional'."""
    s = s.lower()
    # 去掉特殊引号
    s = s.replace("'", "").replace("'", "").replace("`", "")
    # 非字母数字 -> '-'
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:60]


def normalize_date_for_csv(s: str | None) -> str:
    """从 ISO 时间取日期部分, 转 YYYY.M.D."""
    if not s:
        return datetime.now().strftime("%Y.%m.%d")
    # ISO: 2025-06-07T08:00:00Z or 2025-06-07 08:00:00
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y)}.{int(mo)}.{int(d)}"
    return s


def iso_to_dt(s: str | None) -> datetime | None:
    """ISO 时间字符串 -> datetime. None 入参返回 None."""
    if not s:
        return None
    try:
        # 处理 2025-06-07T08:00:00Z 这种
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# === Preview 数据类 ===

@dataclass
class UpdatePreview:
    """update-preview 的完整响应."""
    platform: str
    contest_id: str
    username: str
    type: str = "update"
    record_state: str = "create_new"     # "create_new" | "update_existing"
    slug: str = ""
    slug_exists: bool = False
    contest: dict = field(default_factory=dict)
    problems: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    suggested: dict = field(default_factory=dict)
    fetch_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "type": self.type,
            "record_state": self.record_state,
            "slug": self.slug,
            "slug_exists": self.slug_exists,
            "contest": self.contest,
            "username": self.username,
            "total_problems": len(self.problems),
            "problems": self.problems,
            "summary": self.summary,
            "suggested": self.suggested,
            "fetch_seconds": self.fetch_seconds,
        }


@dataclass
class UpsolvePreview:
    """upsolve-preview 的完整响应."""
    platform: str
    slug: str
    contest_id: str | None
    username: str
    type: str = "upsolve"
    since: str = ""
    current_problems: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "type": self.type,
            "slug": self.slug,
            "contest_id": self.contest_id,
            "username": self.username,
            "since": self.since,
            "current_problems": self.current_problems,
            "changes": self.changes,
            "summary": self.summary,
        }


# === 主逻辑 ===

def make_client(platform: str, config_dir: Path) -> PlatformClient:
    """从 config_dir/cookies/<platform>.txt 加载 cookie, 构造 client."""
    cls = get_client_class(platform)
    cookie_path = config_dir / "cookies" / f"{platform}.txt"
    cookies = {}
    if cookie_path.exists():
        # 局部 import 避免循环
        from platforms.qoj import parse_netscape_cookies
        # 通用: 任何 platform 都可以用 Netscape 格式
        cookies = parse_netscape_cookies(cookie_path.read_text(encoding="utf-8"))
    return cls(cookies=cookies)


def _filter_in_contest(subs: list[Submission], start: str | None,
                       end: str | None) -> list[Submission]:
    """只保留 [start, end] 时间窗内的提交.

    兼容两种 submission 格式:
      - 有 contest_time_seconds (QOJ 列表页格式) -> 假定相对 contest start
      - 有 submitted_at (绝对时间) -> 直接比较
    """
    if not start and not end:
        return subs  # 无时间信息, 全部算

    s_dt = iso_to_dt(start)
    e_dt = iso_to_dt(end)
    duration_secs = None
    if s_dt and e_dt:
        duration_secs = (e_dt - s_dt).total_seconds()

    out = []
    for s in subs:
        # 路径 1: 有 contest_time_seconds (相对时间)
        if s.contest_time_seconds is not None and duration_secs is not None:
            if 0 <= s.contest_time_seconds <= duration_secs:
                out.append(s)
            continue
        # 路径 2: 有绝对时间
        s_dt_sub = iso_to_dt(s.submitted_at)
        if s_dt_sub:
            if s_dt and s_dt_sub < s_dt:
                continue
            if e_dt and s_dt_sub > e_dt:
                continue
            out.append(s)
            continue
        # 都无, 跳过 (无法判断)
    return out


def _map_standings_to_problems(
    standings: dict[str, StandingsEntry], problem_count: int,
) -> tuple[list[dict], dict]:
    """把 standings 折叠成 problems 数组 (A B C ...).

    输入: {letter: StandingsEntry} — 只含提交过的题.
    输出: 每题 status ∈ {O, !, .}, 附带 contest_time + tries.
    """
    letters = [chr(ord("A") + i) for i in range(problem_count)]
    problems = []
    summary = {"O": 0, "Ø": 0, "!": 0, ".": 0}
    for letter in letters:
        e = standings.get(letter)
        if e is None:
            problems.append({"letter": letter, "status": ".", "verdict": None,
                            "tries": 0, "no_submission": True})
            summary["."] += 1
            continue
        if e.score == 100 and e.verdict == "AC":
            tries = 1 + e.failed_attempts
            problems.append({
                "letter": letter, "status": "O", "verdict": "AC",
                "contest_time": _secs_to_str(e.contest_time_seconds),
                "tries": tries,
                "submission_id": e.submission_id,
            })
            summary["O"] += 1
        else:
            problems.append({
                "letter": letter, "status": "!", "verdict": e.verdict or "WA",
                "contest_time": _secs_to_str(e.contest_time_seconds),
                "tries": 1 + e.failed_attempts,
                "submission_id": e.submission_id,
            })
            summary["!"] += 1
    return problems, summary


def _map_submissions_to_problems(
    subs: list[Submission], problem_count: int,
) -> tuple[list[dict], dict]:
    """把 submissions 折叠成 problems 数组 (A B C ...).

    每个 problem 取**最晚**一次提交 (同一题可能多次提交).
    备用方案, upsolve 流程用得到 (per-submission 数据).
    """
    letters = [chr(ord("A") + i) for i in range(problem_count)]
    # 按 problem 分组, 找最晚的 (用 contest_time_seconds, 没就 None)
    by_problem: dict[str, Submission] = {}
    for s in subs:
        cur = by_problem.get(s.problem)
        if cur is None:
            by_problem[s.problem] = s
            continue
        # 比较: 有 contest_time 取大, 没时间的排最后
        cur_t = cur.contest_time_seconds if cur.contest_time_seconds is not None else -1
        new_t = s.contest_time_seconds if s.contest_time_seconds is not None else -1
        if new_t >= cur_t:
            by_problem[s.problem] = s

    problems = []
    summary = {"O": 0, "Ø": 0, "!": 0, ".": 0}
    for letter in letters:
        s = by_problem.get(letter)
        if s is None:
            problems.append({"letter": letter, "status": ".", "verdict": None,
                            "tries": 0, "no_submission": True})
            summary["."] += 1
            continue
        if s.verdict == "AC":
            problems.append({
                "letter": letter, "status": "O", "verdict": "AC",
                "contest_time": _secs_to_str(s.contest_time_seconds),
                "tries": s.tries,
            })
            summary["O"] += 1
        else:
            problems.append({
                "letter": letter, "status": "!", "verdict": s.verdict,
                "contest_time": _secs_to_str(s.contest_time_seconds),
                "tries": s.tries,
            })
            summary["!"] += 1
    return problems, summary


def _secs_to_str(secs: int | None) -> str | None:
    """秒数 -> 'H:MM:SS' 或 'M:SS' 字符串."""
    if secs is None:
        return None
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _slug_from_meta(meta, slug_override: str | None = None) -> str:
    """从 contest meta 生成 slug.

    规则:
      - title 以 4 位年份开头 -> slugify 后直接用 (已含年份)
      - 否则拼 year + slug
    """
    if slug_override:
        return slug_override

    title = meta.title or ""
    # 检查 title 是否以 4 位年份开头
    m = re.match(r"^(\d{4})\b", title)
    if m:
        # title 自带年份, 直接 slugify
        return slugify(title)

    # 没有年份, 拼上
    if meta.start_time:
        year = meta.start_time[:4]
    else:
        year = str(datetime.now().year)
    return f"{year}-{slugify(title)}"


def build_update_preview(
    platform: str, contest_id: str, user: str,
    csv_store: CsvStore, config_dir: Path,
    slug_override: str | None = None,
    fetch_fn=None,   # 测试用
) -> UpdatePreview:
    """构造 update-preview. 同步执行 QOJ 抓取."""
    import time
    t0 = time.time()

    client = make_client(platform, config_dir)
    if fetch_fn is not None:
        # 测试时注入 fetch_fn
        client._fetch_fn = fetch_fn

    if not client.cookies_valid():
        raise ValueError(f"cookie_missing_for_platform: {platform} "
                        f"(expected cookies/<platform>.txt)")

    meta = client.get_contest_meta(contest_id)
    # update 用 standings (结构化 JS 数据, 比 submissions HTML 稳)
    standings = client.get_user_standings(contest_id, user)
    problems, summary = _map_standings_to_problems(standings, meta.problem_count)

    slug = _slug_from_meta(meta, slug_override)
    slug_exists = csv_store.exists(slug)

    preview = UpdatePreview(
        platform=platform,
        contest_id=contest_id,
        username=user,
        record_state="update_existing" if slug_exists else "create_new",
        slug=slug,
        slug_exists=slug_exists,
        contest={
            "platform": meta.platform,
            "contest_id": meta.contest_id,
            "title": meta.title,
            "problem_count": meta.problem_count,
            "start_time": meta.start_time,
            "end_time": meta.end_time,
            "url": meta.url,
        },
        problems=problems,
        summary=summary,
        suggested={
            "slug": slug,
            "name": meta.title,
            "date": normalize_date_for_csv(meta.start_time),
            "link": meta.url,
        },
        fetch_seconds=round(time.time() - t0, 2),
    )
    return preview


# === Apply ===

@dataclass
class ApplyResult:
    """apply 操作的统一结果."""
    ok: bool
    slug: str
    record_state: str
    csv_written: bool
    body_written: str | None
    committed: bool
    commit_sha: str
    pushed: bool
    problems_before: list[str] | None = None
    problems_after: list[str] | None = None

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok,
            "slug": self.slug,
            "record_state": self.record_state,
            "csv_written": self.csv_written,
            "body_written": self.body_written,
            "committed": self.committed,
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
        }
        if self.problems_before is not None:
            d["problems_before"] = self.problems_before
            d["problems_after"] = self.problems_after
        return d


def apply_update(
    preview: UpdatePreview,
    csv_store: CsvStore,
    md_store: MdStore,
    git_ops: GitOps,
    *,
    overrides: dict | None = None,
    create_body: bool = True,
    run_sync: bool = True,
    push: bool = True,
) -> ApplyResult:
    """把 update-preview 应用到 csv + md + git."""
    overrides = overrides or {}

    # 合并 overrides 到 preview
    slug = overrides.get("slug", preview.slug)
    name = overrides.get("name", preview.suggested.get("name", ""))
    date = overrides.get("date", preview.suggested.get("date", ""))
    link = overrides.get("link", preview.suggested.get("link", ""))
    tags_list = overrides.get("tags", [])
    tags = " ".join(tags_list) if tags_list else ""

    problems = [p["status"] for p in preview.problems]
    total = len(problems)

    record_state = "update_existing" if csv_store.exists(slug) else "create_new"
    problems_before = None
    if record_state == "update_existing":
        existing = csv_store.get(slug)
        problems_before = list(existing.problems)

    # 构造 contest
    contest = Contest(
        slug=slug, name=name, date=date, solved=0, total=total,
        problems=problems, link=link, tags=tags,
    )

    if record_state == "create_new":
        csv_store.add(contest)
    else:
        csv_store.update(slug, name=name, date=date, total=total,
                         problems=problems, link=link, tags=tags)
    csv_store.save()

    # md
    body_written = None
    if create_body:
        if md_store.exists(slug):
            # 已有 md 不动 (默认)
            body_written = f"docs/contests/{slug}.md"
        else:
            md_store.write(slug, md_store.placeholder(contest))
            body_written = f"docs/contests/{slug}.md"

    # sync (调 sync.py 重建 index.md + data/contests.json)
    if run_sync:
        _run_sync(git_ops.repo_path)

    # commit + push
    msg = overrides.get("commit_message") or f"add({slug}): via qoj import"
    paths = ["contests.csv"]
    if body_written:
        paths.append(body_written)
    paths += ["docs/index.md", "docs/data/contests.json"]
    sha, pushed = git_ops.commit_and_push(msg, paths)

    return ApplyResult(
        ok=True,
        slug=slug,
        record_state=record_state,
        csv_written=True,
        body_written=body_written,
        committed=bool(sha),
        commit_sha=sha[:8] if sha else "",
        pushed=pushed and push,
        problems_before=problems_before,
        problems_after=problems,
    )


def _run_sync(repo_path: Path) -> None:
    """调 tools/sync.py 重建 index.md / data/contests.json."""
    import subprocess
    sync_py = repo_path / "tools" / "sync.py"
    if not sync_py.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(sync_py)],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # sync 失败不阻塞主流程, 但警告
        print(f"  ⚠ tools/sync.py 失败, index.md / data.json 可能未更新", file=sys.stderr)


# === Upsolve ===

def build_upsolve_preview(
    platform: str, contest_id: str | None, slug: str | None, user: str,
    csv_store: CsvStore, config_dir: Path,
    since_override: str | None = None,
    fetch_fn=None,
) -> UpsolvePreview:
    """构造 upsolve-preview.

    slug 解析优先级:
      1. 参数 slug
      2. contest_id -> 抓 meta -> 推 slug -> CSV 查找
    """
    if not slug and not contest_id:
        raise ValueError("slug 或 contest_id 必填一个")

    client = make_client(platform, config_dir)
    if fetch_fn is not None:
        client._fetch_fn = fetch_fn

    # 抓 contest meta (无论 slug 是否已知, 都需要 end_time)
    meta = None
    if contest_id:
        try:
            meta = client.get_contest_meta(contest_id)
        except Exception:
            meta = None

    # 解析 slug
    if not slug and meta:
        slug = _slug_from_meta(meta)

    if not slug:
        raise ValueError("无法解析 slug, 请直接提供 slug")

    contest = csv_store.get(slug)
    if contest is None:
        raise ValueError(f"slug_not_found_in_csv: {slug}")

    since = since_override
    if not since:
        if meta and meta.end_time:
            since = meta.end_time
        else:
            # fallback: contest.date 后 1 天
            try:
                y, m, d = contest.date.split(".")
                dt = datetime(int(y), int(m), int(d))
                since = dt.isoformat()
            except Exception:
                since = ""

    real_contest_id = (meta.contest_id if meta else contest_id) or "0"
    subs = client.get_user_submissions(real_contest_id, user)
    # 过滤: submitted_at > since
    since_dt = iso_to_dt(since)
    post_subs = []
    for s in subs:
        s_dt = iso_to_dt(s.submitted_at)
        if since_dt and s_dt and s_dt > since_dt:
            post_subs.append(s)
        elif s_dt is None and s.contest_time_seconds is None:
            # 没时间信息但 contest_time 也无 -> 当作赛后 (默认抓的是全量)
            post_subs.append(s)

    # 折叠: 每个 (user, problem) 取最早 AC
    earliest_ac: dict[str, Submission] = {}
    for s in post_subs:
        if s.verdict != "AC":
            continue
        key = s.problem
        cur = earliest_ac.get(key)
        if cur is None:
            earliest_ac[key] = s
            continue
        cur_t = cur.contest_time_seconds if cur.contest_time_seconds is not None else -1
        new_t = s.contest_time_seconds if s.contest_time_seconds is not None else -1
        if new_t < cur_t or (new_t == cur_t and s.submitted_at < cur.submitted_at):
            earliest_ac[key] = s

    # 找变化
    current = contest.problems
    letters = [chr(ord("A") + i) for i in range(contest.total)]
    changes = []
    upsolved = 0
    upsolved_from_bang = 0
    no_change_attempts = 0

    for i, letter in enumerate(letters):
        if i >= len(current):
            break
        cur_status = current[i]
        s = earliest_ac.get(letter)
        if s is None:
            # 赛后没有 AC 提交, 不变
            continue
        if cur_status in (".", "!"):
            new_status = "Ø"
            if cur_status == "!":
                upsolved_from_bang += 1
            else:
                upsolved += 1
            reason = "post_contest_ac_from_untouched" if cur_status == "." else \
                     "post_contest_ac_from_bang"
            changes.append({
                "letter": letter,
                "before": cur_status,
                "after": new_status,
                "verdict": s.verdict,
                "submitted_at": s.submitted_at,
                "tries": s.tries,
                "submission_id": s.submission_id,
                "reason": reason,
            })
        # O / Ø 不变 (不撤销已有)

    # 赛后尝试但未过的 (cur=., WA)
    for i, letter in enumerate(letters):
        if i >= len(current):
            break
        cur_status = current[i]
        if cur_status != ".":
            continue
        # 找是否有非 AC 提交
        wa_subs = [s for s in post_subs if s.problem == letter and s.verdict != "AC"]
        if wa_subs:
            no_change_attempts += 1

    return UpsolvePreview(
        platform=platform,
        slug=slug,
        contest_id=meta.contest_id if meta else None,
        username=user,
        since=since,
        current_problems=list(current),
        changes=changes,
        summary={
            "upsolved": upsolved,
            "upsolved_from_bang": upsolved_from_bang,
            "no_change_attempts": no_change_attempts,
            "skipped_already_o": 0,
        },
    )


def apply_upsolve(
    preview: UpsolvePreview,
    csv_store: CsvStore,
    md_store: MdStore,
    git_ops: GitOps,
    *,
    commit_message: str | None = None,
    push: bool = True,
) -> ApplyResult:
    """应用 upsolve-preview 到 csv."""
    contest = csv_store.get(preview.slug)
    if contest is None:
        raise ValueError(f"slug_not_found: {preview.slug}")

    problems = list(contest.problems)
    letters = [chr(ord("A") + i) for i in range(contest.total)]
    problems_before = list(problems)

    for ch in preview.changes:
        idx = letters.index(ch["letter"]) if ch["letter"] in letters else -1
        if idx < 0:
            continue
        problems[idx] = ch["after"]

    csv_store.update(preview.slug, problems=problems)
    csv_store.save()

    _run_sync(git_ops.repo_path)

    msg = commit_message or f"upsolve({preview.slug}): via qoj"
    paths = ["contests.csv", "docs/index.md", "docs/data/contests.json"]
    sha, pushed = git_ops.commit_and_push(msg, paths)

    return ApplyResult(
        ok=True,
        slug=preview.slug,
        record_state="update_existing",
        csv_written=True,
        body_written=None,
        committed=bool(sha),
        commit_sha=sha[:8] if sha else "",
        pushed=pushed and push,
        problems_before=problems_before,
        problems_after=problems,
    )


# === Misc ===

def contest_meta_to_dict(meta) -> dict:
    return {
        "platform": meta.platform,
        "contest_id": meta.contest_id,
        "title": meta.title,
        "problem_count": meta.problem_count,
        "start_time": meta.start_time,
        "end_time": meta.end_time,
        "url": meta.url,
    }