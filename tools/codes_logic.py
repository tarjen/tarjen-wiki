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
from platforms.base import PlatformClient, Submission
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

    # 1. 抓所有 AC 提交 (翻页由 client 自己处理)
    client = platform_client_factory(req.platform)
    try:
        all_subs = client.get_user_submissions(req.cid, req.username)
    except Exception as e:
        result.errors += 1
        result.duration_seconds = time.time() - start
        raise

    # 2. 过滤: 限定题目
    if req.problems:
        all_subs = [s for s in all_subs if s.problem in req.problems]

    # 3. 分桶
    mine_subs = []
    watchlist_subs = []
    others_subs = []
    for s in all_subs:
        if req.fetch_self and s.user == req.username:
            mine_subs.append(s)
        elif req.fetch_watchlist and s.user in wl_users:
            watchlist_subs.append(s)
        else:
            others_subs.append(s)

    # 4. 自己: 取所有
    my_files = []
    for s in mine_subs:
        if cancel_check and cancel_check():
            break
        my_files.append(s)
    # watchlist: 取所有 AC
    wl_files = []
    for s in watchlist_subs:
        if cancel_check and cancel_check():
            break
        if s.verdict == "AC":
            wl_files.append(s)
    # others: 折叠 + top N
    others_files = _pick_others(others_subs, req.fetch_others, req.others_n)

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
        if req.skip_existing and codes_store.exists(req.cid, s.user, s.problem):
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
        elif s.user in wl_users:
            source = "watchlist"
        else:
            source = "sample"

        path = codes_store.save(
            req.cid, s.user, s.problem, code, lang,
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


def _pick_others(subs: list[Submission], mode: str, n: int) -> list[Submission]:
    """按 mode 从 others 选."""
    if mode == "none" or not subs:
        return []

    # 按 (user, problem) 折叠: 取最早的 (contest_time 最小)
    by_key: dict[tuple[str, str], Submission] = {}
    for s in subs:
        key = (s.user, s.problem)
        cur = by_key.get(key)
        if cur is None:
            by_key[key] = s
            continue
        cur_t = cur.contest_time_seconds if cur.contest_time_seconds is not None else 9999999
        new_t = s.contest_time_seconds if s.contest_time_seconds is not None else 9999999
        if new_t < cur_t:
            by_key[key] = s

    # 只留 AC
    acs = [s for s in by_key.values() if s.verdict == "AC"]
    if not acs:
        return []

    # 按 problem 分组, 每题选 N
    by_problem: dict[str, list[Submission]] = {}
    for s in acs:
        by_problem.setdefault(s.problem, []).append(s)

    out = []
    for prob, lst in by_problem.items():
        if mode == "top_n_fastest":
            lst.sort(key=lambda x: x.contest_time_seconds if x.contest_time_seconds is not None else 9999999)
        elif mode == "top_n_shortest":
            lst.sort(key=lambda x: x.code_length if x.code_length is not None else 9999999)
        elif mode == "random_n":
            random.shuffle(lst)
        out.extend(lst[:n])
    return out


def _secs_to_str(secs: int | None) -> str | None:
    if secs is None:
        return None
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"