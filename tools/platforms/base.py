#!/usr/bin/env python3
"""
tools/platforms/base.py — 比赛平台抽象基类

定义各 OJ (QOJ, Codeforces, AtCoder, ...) 都必须实现的接口。
让 server.py / cli_main.py 跟具体 OJ 解耦——加新 OJ 只需写一个 client 类并注册。

抽象方法 (各 OJ 必须实现):
  - cookies_valid()             检查 cookie 是否还有效
  - get_contest_meta(cid)       比赛标题 / 题数 / 起止时间
  - get_user_submissions(cid, user)  某用户在该比赛的所有提交
  - get_submission_code(sid)    取单份提交代码 + 语言
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass
class ContestMeta:
    """比赛元信息."""
    platform: str
    contest_id: str
    title: str
    problem_count: int
    start_time: str | None = None     # ISO 8601
    end_time: str | None = None       # ISO 8601
    url: str = ""


@dataclass
class Submission:
    """一条提交记录."""
    platform: str
    submission_id: str
    user: str
    problem: str                       # "A", "B", ... 或平台原样
    verdict: str                       # "AC" / "WA" / "TLE" / "RE" / "MLE" / ...
    submitted_at: str                 # ISO 8601
    contest_time_seconds: int | None   # 赛中相对秒数; None = 赛后
    tries: int = 1                     # 该 (user, problem) 的累计提交次数
    language: str | None = None
    code_length: int | None = None


@dataclass
class StandingsEntry:
    """一道题在 standings 里的状态 (含 upsolve).

    QOJ standings JS 数据格式:
      score={"<user>": {"<pid>": [score, time_sec, sub_id, failed_before, ...]}}
    """
    platform: str
    problem_id: str                   # 0-indexed, 字符串 ("0"-"10")
    letter: str                       # 映射后: "A", "B", ...
    score: int                        # 0-100
    contest_time_seconds: int         # 提交时间 (秒). 0 = 未提交
    submission_id: str | None
    failed_attempts: int              # AC 前的失败次数 (0 表示一次就过)
    verdict: str                      # "AC" / "WA" / "" (not submitted)


@dataclass
class FastestACEntry:
    """一道题所有 AC 之一 (用于"每题最快"采样).

    来自 standings score 数据:
      score={"<user>": {"<pid>": [score=100, time_sec, sub_id, ...]}}
    """
    user: str
    time_seconds: int                 # 提交时间 (秒). 0 = 未提交/异常
    submission_id: str                # 空 = 数据缺失


class PlatformError(RuntimeError):
    """平台特定错误基类."""


class CookieExpiredError(PlatformError):
    """Cookie 失效 (401/403 或登录页 HTML)."""


class CFBlockedError(PlatformError):
    """被 Cloudflare 拦截."""


class ParseError(PlatformError):
    """HTML 结构变化, 解析失败."""


class TimeoutError_(PlatformError):  # 避免跟 stdlib TimeoutError 冲突
    """抓取超时."""


class NotFoundError(PlatformError):
    """比赛/提交不存在 (404)."""


class PlatformClient(ABC):
    """所有 OJ 客户端的抽象基类."""

    # 子类必须设置 (例如 "qoj", "codeforces", "atcoder")
    name: ClassVar[str] = ""

    @abstractmethod
    def cookies_valid(self) -> bool:
        """本地 cookie 是否还存在 (粗略检查, 不一定真有效)."""

    @abstractmethod
    def get_contest_meta(self, contest_id: str) -> ContestMeta:
        """获取比赛元信息. 404 抛 NotFoundError."""

    @abstractmethod
    def get_user_submissions(
        self, contest_id: str, user: str,
    ) -> list[Submission]:
        """获取某用户在该比赛的所有提交 (赛中 + 赛后, 调用方按时间筛)."""

    @abstractmethod
    def get_user_standings(
        self, contest_id: str, user: str,
    ) -> dict[str, StandingsEntry]:
        """从 standings (结构化数据) 拿 user 的每题结果.

        返回: {letter: StandingsEntry} — 只包含提交过的题 (没提交的不在 dict 里).
        用于 wiki update (比 submission HTML 更可靠, 不会被 HTML 微调搞坏).
        """

    @abstractmethod
    def get_all_user_standings(
        self, contest_id: str, exclude_users: set[str] | None = None,
    ) -> dict[str, list[FastestACEntry]]:
        """从 standings 拿所有用户的 AC, 按时间排序. 用于"每题最快"采样.

        返回: {letter: [FastestACEntry, ...]}  —  时间从小到大
        exclude_users: 跳过这些用户名 (一般传 {self_username} 排除自己)
        """

    @abstractmethod
    def get_submission_code(self, submission_id: str) -> tuple[str, str]:
        """获取单份提交的代码 + 语言. 返回 (code, language)."""


# === 注册表 ===

_REGISTRY: dict[str, type[PlatformClient]] = {}


def register(cls: type[PlatformClient]) -> type[PlatformClient]:
    """装饰器: 注册一个平台 client. 同时验证 name 不为空."""
    if not cls.name:
        raise ValueError(f"{cls.__name__}.name 不能为空")
    if cls.name in _REGISTRY:
        raise ValueError(f"platform '{cls.name}' 重复注册")
    _REGISTRY[cls.name] = cls
    return cls


def get_registry() -> dict[str, type[PlatformClient]]:
    """返回当前已注册的所有平台 (供 /healthz / docs 用)."""
    return dict(_REGISTRY)


def get_client_class(name: str) -> type[PlatformClient]:
    if name not in _REGISTRY:
        raise ValueError(
            f"不支持的平台: {name!r}. "
            f"已注册: {list(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]