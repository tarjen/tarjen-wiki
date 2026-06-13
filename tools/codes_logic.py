#!/usr/bin/env python3
"""
tools/codes_logic.py — 代码抓取的业务逻辑

被 server.py 调用. 不依赖 FastAPI.

策略 (watchlist + sample):
  - 自己的提交: 全抓 (含 WA/TLE, 用于复盘)
  - watchlist 用户: 所有 AC
  - 其他用户: 每题最早 AC 的前 N 个 (默认 N=1)

后端 store: ~/.local/share/wiki/codes/<cid>/<user>/<prob>.<ext>
索引: ~/.local/share/wiki/codes/<cid>/index.json
gitignored.

长任务: 通过 task_id 暴露状态, 不阻塞 HTTP.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from csv_store import CsvStore
from codes_store import CodesStore
from platforms import get_client_class
from platforms.base import FastestACEntry, PlatformClient, Submission
from watchlist import Watchlist


Source = Literal["mine", "watchlist", "sample", "other"]


# === Task state (in-memory, single-process) ===

TASKS: dict[str, "FetchTask"] = {}


@dataclass
class FetchTask:
    task_id: str
    cid: str
    status: Literal["started", "running", "done", "error"] = "started"
    progress: dict = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None
    cancel_requested: bool = False


def get_task(task_id: str) -> FetchTask | None:
    return TASKS.get(task_id)


def list_tasks() -> list[FetchTask]:
    return list(TASKS.values())


def create_task(cid: str) -> FetchTask:
    tid = f"fetch_{cid}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    task = FetchTask(task_id=tid, cid=cid, started_at=time.time())
    TASKS[tid] = task
    return task


# === Fetch strategy ===

@dataclass
class FetchRequest:
    platform: str = "qoj"
    cid: str = ""
    username: str = ""
    fetch_self: bool = True
    fetch_watchlist: bool = True
    fetch_others: str = "top_n_fastest"   # "top_n_fastest" | "top_n_shortest" | "random_n" | "none"
    others_n: int = 1
    problems: list[str] | None = None
    skip_existing: bool = True
    timeout_seconds: int = 600
    request_interval: float = 1.5


@dataclass
class FetchResult:
    fetched: int = 0
    skipped_existing: int = 0
    skipped_non_ac: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    files: list[dict] = field(default_factory=list)
    error_details: list[dict] = field(default_factory=list)  # [{sid, user, prob, msg}]

    def to_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "skipped_existing": self.skipped_existing,
            "skipped_non_ac": self.skipped_non_ac,
            "errors": self.errors,
            "duration_seconds": self.duration_seconds,
            "files": self.files,
            "error_details": self.error_details,
        }


def fetch_codes(
    req: FetchRequest,
    platform_client_factory,
    codes_store: CodesStore,
    watchlist_obj: Watchlist,
    progress_callback=None,
    cancel_check=None,
) -> FetchResult:
    """执行抓取.

    Args:
        req: 抓取请求
        platform_client_factory: 接受 (platform, cookies) -> PlatformClient. 用于测试时注入.
        codes_store: 存储后端
        watchlist_obj: watchlist 实例
        progress_callback: 可选, FetchTask 设进 progress 字段
        cancel_check: 可选, 返回 True 时中断
    """
    import random
    from datetime import datetime

    start = time.time()
    result = FetchResult()
    wl_users = set(watchlist_obj.users())

    # 1. 拿 client
    client = platform_client_factory(req.platform)
    platform = req.platform

    # 2. 拿所有 AC (含自己/watchlist/others), 用 standings 数据 (结构化, 免 HTML 分页)
    #    - mine: 用 get_user_standings (含 WAs/fails), 转成 "submission-like" 列表
    #    - others: 用 get_all_user_standings 拿所有用户 AC, 按时间排
    try:
        # 自己: 拿所有 StandingsEntry (含 WAs)
        my_standings = client.get_user_standings(req.cid, req.username) if req.fetch_self else {}
        # 所有用户的 AC (排除自己), 按时间排
        others_per_problem = client.get_all_user_standings(
            req.cid, exclude_users={req.username}
        ) if req.fetch_others != "none" else {}
    except Exception as e:
        result.errors += 1
        result.duration_seconds = time.time() - start
        raise

    # 3. 自己: 转 submission 列表 (含 WAs, 用于复盘)
    mine_subs = _standings_to_subs(my_standings, req.username, req.problems)
    # watchlist: 需要按用户名拿 — 复用 get_user_standings (一个个)
    watchlist_subs = []
    if req.fetch_watchlist:
        for u in wl_users:
            if u == req.username:
                continue  # 已在 mine
            try:
                u_standings = client.get_user_standings(req.cid, u)
            except Exception as e:
                result.errors += 1
                result.error_details.append({
                    "submission_id": "?", "user": u, "problem": "?",
                    "error": f"get_user_standings: {type(e).__name__}: {e}",
                })
                continue
            for entry in u_standings.values():
                if req.problems and entry.letter not in req.problems:
                    continue
                if entry.verdict == "AC":  # watchlist 只看 AC
                    watchlist_subs.append(_entry_to_sub(entry, u))

    # 4. others: 从 standings 选 top N
    #    - 自己永远排除
    #    - watchlist 用户只在 fetch_watchlist=True 时排除
    #    (watchlist=False 时, watchlist 用户降级到 others — 跟旧行为一致)
    others_exclude = {req.username}
    if req.fetch_watchlist:
        others_exclude |= wl_users
    others_files = _pick_others_from_standings(
        others_per_problem, req.fetch_others, req.others_n,
        req.problems, exclude_users=others_exclude,
    )

    # 5. 合并去重 (按 user+prob)
    my_files = mine_subs  # 自己所有
    wl_files = watchlist_subs  # watchlist 的 AC

    # 5. 合并去重 (按 user+prob)
    seen: set[tuple[str, str]] = set()
    final = []
    for s in my_files + wl_files + others_files:
        key = (s.user, s.problem)
        if key in seen:
            continue
        seen.add(key)
        final.append(s)

    # 6. 抓代码 (串行)
    for s in final:
        if cancel_check and cancel_check():
            break
        # 检查 skip_existing
        if req.skip_existing and codes_store.exists(platform, req.cid, s.problem, s.user):
            result.skipped_existing += 1
            continue

        try:
            code, lang = client.get_submission_code(s.submission_id)
        except Exception as e:
            result.errors += 1
            result.error_details.append({
                "submission_id": s.submission_id,
                "user": s.user,
                "problem": s.problem,
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        # 决定 source
        if s.user == req.username:
            source = "mine"
        elif s.user in wl_users and req.fetch_watchlist:
            source = "watchlist"
        else:
            source = "sample"

        path = codes_store.save(
            platform, req.cid, s.problem, s.user, code, lang,
            verdict=s.verdict, submission_id=s.submission_id,
            source=source, contest_time=_secs_to_str(s.contest_time_seconds),
        )
        result.fetched += 1
        result.files.append({
            "user": s.user, "problem": s.problem, "verdict": s.verdict,
            "lang": lang, "size": path.stat().st_size,
            "source": source, "submission_id": s.submission_id,
        })

        if progress_callback:
            progress_callback({
                "fetched": result.fetched,
                "total": len(final),
                "current": f"{s.user}/{s.problem}",
            })

        # 速率控制
        time.sleep(req.request_interval)

    result.duration_seconds = round(time.time() - start, 2)
    return result


def _standings_to_subs(standings: dict, user: str,
                      problems: list[str] | None) -> list[Submission]:
    """StandingsEntry dict → Submission 列表 (含 WAs)."""
    subs = []
    for letter, entry in standings.items():
        if problems and letter not in problems:
            continue
        subs.append(_entry_to_sub(entry, user))
    return subs


def _entry_to_sub(entry, user: str) -> Submission:
    """单个 StandingsEntry → Submission (含 fake sub_id)."""
    return Submission(
        platform="qoj",
        submission_id=entry.submission_id or "",
        user=user,
        problem=entry.letter,
        verdict=entry.verdict or "WA",
        submitted_at="",
        contest_time_seconds=entry.contest_time_seconds,
        language=None,
        code_length=None,
    )


def _pick_others_from_standings(
    per_problem: dict[str, list[FastestACEntry]],
    mode: str, n: int,
    problems: list[str] | None,
    exclude_users: set[str],
) -> list[Submission]:
    """从 standings 拿的 per_problem 选 others.

    per_problem[letter] 已经是按时间排好序的 (最快在前).
    """
    if mode == "none":
        return []
    out: list[Submission] = []
    for letter, entries in per_problem.items():
        if problems and letter not in problems:
            continue
        # 排除 self+watchlist
        candidates = [e for e in entries if e.user not in exclude_users and e.submission_id]
        if not candidates:
            continue
        if mode == "top_n_fastest":
            pass  # 已经是按时间排, 直接取前 n
        elif mode == "top_n_shortest":
            # 不知道 code_length, 用 random 退化
            random.shuffle(candidates)
        elif mode == "random_n":
            random.shuffle(candidates)
        for e in candidates[:n]:
            out.append(Submission(
                platform="qoj",
                submission_id=e.submission_id,
                user=e.user,
                problem=letter,
                verdict="AC",
                submitted_at="",
                contest_time_seconds=e.time_seconds,
                language=None,
                code_length=None,
            ))
    return out


def _secs_to_str(secs: int | None) -> str | None:
    if secs is None:
        return None
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"