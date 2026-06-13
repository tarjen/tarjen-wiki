#!/usr/bin/env python3
"""
tools/server.py — Wiki Backend FastAPI Server

监听 127.0.0.1:8001 (默认). 提供:
  - GET  /healthz
  - GET  /contests              列出 (支持筛选)
  - GET  /contests/{slug}       单条详情
  - POST /contests              新增
  - PUT  /contests/{slug}       改字段
  - PATCH /contests/{slug}/body  改 md 详情页
  - DELETE /contests/{slug}     删

后续 (Phase 3.2-3.4):
  - /import/* (update/upsolve preview+apply)
  - /codes/* (fetch + list + show + delete)
  - /watchlist / /stats / /repo/* / /import/cookies/*

错误格式统一:
  {"ok": false, "error": {"code": "...", "message": "...", "details": {...}}}
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# 允许 tools/ 直接 import (因为我们是 tools/server.py, 这不是必须, 但保险起见)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from csv_store import Contest, CsvStore, CsvValidationError  # noqa: E402
from md_store import MdStore  # noqa: E402
from git_ops import GitConflictError, GitOps, GitPushError  # noqa: E402
from watchlist import Watchlist  # noqa: E402
from codes_store import CodesStore, ensure_gitignore  # noqa: E402
from import_logic import (  # noqa: E402
    ApplyResult, UpdatePreview, UpsolvePreview,
    apply_update, apply_upsolve, build_update_preview, build_upsolve_preview,
    contest_meta_to_dict, make_client,
)


# === 配置 ===

def repo_path_from_env() -> Path:
    """从 REPO_PATH 环境变量读仓库路径, 默认 cwd."""
    p = os.environ.get("REPO_PATH", "").strip()
    return Path(p).expanduser() if p else Path.cwd()


def config_dir_from_env() -> Path:
    """从 CONFIG_DIR 环境变量读配置目录, 默认 ~/.config/wiki."""
    p = os.environ.get("CONFIG_DIR", "").strip()
    if p:
        return Path(p).expanduser()
    return Path.home() / ".config" / "wiki"


def codes_dir_from_env() -> Path:
    """从 CODES_DIR 环境变量读代码缓存目录, 默认 ~/.local/share/wiki/codes."""
    p = os.environ.get("CODES_DIR", "").strip()
    if p:
        return Path(p).expanduser()
    return Path.home() / ".local" / "share" / "codes" / "wiki"


# === App state ===

class AppState:
    """启动时初始化, lifespan 内持有."""

    def __init__(self):
        self.repo_path: Path = repo_path_from_env()
        self.config_dir: Path = config_dir_from_env()
        self.codes_dir: Path = codes_dir_from_env()

        self.csv_store: CsvStore | None = None
        self.md_store: MdStore | None = None
        self.git_ops: GitOps | None = None
        self.watchlist: Watchlist | None = None
        self.codes_store: CodesStore | None = None
        self.platforms: dict = {}  # name -> PlatformClient (Phase 3.2)

        self.started_at: float = 0.0

    def init(self) -> None:
        """从磁盘加载所有 store."""
        self.csv_store = CsvStore(self.repo_path / "contests.csv")
        self.csv_store.load()

        self.md_store = MdStore(self.repo_path / "docs" / "contests")

        self.git_ops = GitOps(self.repo_path)

        self.watchlist = Watchlist(self.config_dir / "watchlist.txt")
        self.watchlist.load()

        self.codes_store = CodesStore(self.codes_dir)
        ensure_gitignore(self.codes_dir)

        self.started_at = time.time()


state = AppState()


# === FastAPI app ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化, 关闭时清理."""
    state.init()
    yield


app = FastAPI(
    title="Wiki Backend",
    version="1.0.0",
    description="Personal contest tracker backend (CSV + Git + QOJ import)",
    lifespan=lifespan,
)


# === 错误处理 ===

@app.exception_handler(CsvValidationError)
async def csv_validation_handler(request: Request, exc: CsvValidationError):
    code_map = {
        "slug_already_exists": 409,
        "slug_invalid": 400,
        "date_invalid": 400,
        "problems_length_mismatch": 400,
        "problems_invalid_char": 400,
    }
    # 匹配错误信息前缀找 code
    code = "validation_error"
    status = 400
    msg = str(exc)
    for prefix, c in [
        ("slug 已存在", ("slug_exists", 409)),
        ("slug 不存在", ("slug_not_found", 404)),
        ("slug 非法", ("slug_invalid", 400)),
        ("date 格式", ("date_invalid", 400)),
        ("problems 长度", ("problems_length_mismatch", 400)),
        ("字符非法", ("problems_invalid_char", 400)),
        ("未知字段", ("unknown_field", 400)),
        ("name 不能为空", ("name_empty", 400)),
    ]:
        if msg.startswith(prefix):
            code, status = c
            break

    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {
            "code": code,
            "message": msg,
            "details": {"row_num": exc.row_num, "slug": exc.slug},
        }},
    )


@app.exception_handler(GitConflictError)
async def git_conflict_handler(request: Request, exc: GitConflictError):
    return JSONResponse(
        status_code=409,
        content={"ok": False, "error": {
            "code": "repo_dirty",
            "message": str(exc),
        }},
    )


@app.exception_handler(GitPushError)
async def git_push_handler(request: Request, exc: GitPushError):
    return JSONResponse(
        status_code=502,
        content={"ok": False, "error": {
            "code": "gh_push_failed",
            "message": str(exc),
        }},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """统一 HTTPException → {"ok": false, "error": {...}} 格式."""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        # 已经是 {"code": ..., "message": ...} 格式
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "error": detail},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": {
            "code": "http_error",
            "message": str(detail) if detail else "HTTP error",
        }},
    )


# === Health ===

@app.get("/healthz")
def healthz():
    s = state
    if s.csv_store is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": {
            "code": "not_initialized", "message": "server starting up"}})
    repo_status = s.git_ops.status() if s.git_ops else None
    return {
        "ok": True,
        "config": {
            "repo_path": str(s.repo_path),
            "config_dir": str(s.config_dir),
            "codes_dir": str(s.codes_dir),
        },
        "csv": {
            "contests": len(s.csv_store),
        },
        "repo": {
            "branch": repo_status.branch if repo_status else None,
            "clean": repo_status.clean if repo_status else None,
            "ahead": repo_status.ahead if repo_status else 0,
            "behind": repo_status.behind if repo_status else 0,
            "last_commit": (
                {"sha": repo_status.last_commit.sha[:8],
                 "message": repo_status.last_commit.message,
                 "time": repo_status.last_commit.time}
                if repo_status and repo_status.last_commit else None
            ),
        } if repo_status else None,
        "watchlist_count": len(s.watchlist) if s.watchlist else 0,
        "uptime_seconds": int(time.time() - s.started_at),
    }


# === Contest schemas ===

class ContestIn(BaseModel):
    """POST /contests body."""
    slug: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=200)
    date: str
    total: int = Field(..., ge=1, le=26)
    problems: list[str] = Field(..., min_length=1, max_length=26)
    tags: list[str] = Field(default_factory=list)
    link: str = ""
    body: str | None = None          # 可选, 同时创建 md 详情页
    commit_message: str | None = None


class ContestPatch(BaseModel):
    """PUT /contests/{slug} body (部分更新)."""
    name: str | None = None
    date: str | None = None
    total: int | None = Field(default=None, ge=1, le=26)
    problems: list[str] | None = None
    tags: list[str] | None = None
    link: str | None = None
    slug: str | None = None          # 改 slug (rename)
    commit_message: str | None = None


class BodyPatch(BaseModel):
    content: str


# === Helpers ===

def contest_to_dict(c: Contest) -> dict:
    return {
        "slug": c.slug,
        "name": c.name,
        "date": c.date,
        "solved": c.solved,
        "in_contest": c.in_contest_solved,
        "upsolved": c.upsolved,
        "total": c.total,
        "problems": c.problems,
        "tags": c.tags_list,
        "tags_raw": c.tags,
        "link": c.link,
    }


def filter_contests(
    since: str | None = None,
    until: str | None = None,
    tag: list[str] | None = None,
    solved_min: int | None = None,
    sort: str = "date",
    order: str = "desc",
    limit: int | None = None,
) -> list[Contest]:
    """根据查询参数过滤 contests."""
    contests = list(state.csv_store.all())

    if since:
        contests = [c for c in contests if c.iso_date >= since]
    if until:
        contests = [c for c in contests if c.iso_date <= until]
    if tag:
        # tag 参数支持带 # 或不带, 规范化后比较
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
    else:
        raise HTTPException(400, detail={"code": "invalid_sort",
                                          "message": f"unknown sort: {sort}"})

    contests.sort(key=key, reverse=(order == "desc"))
    if limit:
        contests = contests[:limit]
    return contests


# === Contest endpoints ===

@app.get("/contests")
def list_contests(
    since: str | None = Query(None, description="YYYY-MM-DD"),
    until: str | None = Query(None, description="YYYY-MM-DD"),
    tag: list[str] | None = Query(None),
    solved_min: int | None = Query(None, ge=0),
    sort: str = Query("date", pattern="^(date|solved|rate|total)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int | None = Query(None, ge=1, le=1000),
):
    contests = filter_contests(
        since=since, until=until, tag=tag, solved_min=solved_min,
        sort=sort, order=order, limit=limit,
    )
    return {
        "count": len(contests),
        "total": len(state.csv_store),
        "contests": [
            {**contest_to_dict(c), "body_exists": state.md_store.exists(c.slug)}
            for c in contests
        ],
    }


@app.get("/contests/{slug}")
def get_contest(slug: str):
    c = state.csv_store.get(slug)
    if c is None:
        raise HTTPException(404, detail={"code": "slug_not_found",
                                          "message": f"slug not found: {slug}"})
    body = None
    body_path = None
    if state.md_store.exists(slug):
        body = state.md_store.read(slug)
        body_path = f"docs/contests/{slug}.md"

    return {
        **contest_to_dict(c),
        "body_exists": body is not None,
        "body_path": body_path,
        "body": body,
    }


@app.post("/contests", status_code=201)
def create_contest(body: ContestIn):
    # 检查 slug 存在
    if state.csv_store.exists(body.slug):
        raise HTTPException(409, detail={"code": "slug_exists",
                                          "message": f"slug already exists: {body.slug}"})

    # 构造 Contest
    contest = Contest(
        slug=body.slug,
        name=body.name,
        date=body.date,
        solved=0,
        total=body.total,
        problems=body.problems,
        link=body.link,
        tags=" ".join(body.tags),
    )

    # 加 (会重算 solved)
    state.csv_store.add(contest)
    state.csv_store.save()

    # 创建 md 详情页
    body_written = None
    if body.body:
        state.md_store.write(body.slug, body.body)
        body_written = f"docs/contests/{body.slug}.md"
    elif state.md_store.exists(body.slug) is False:
        # 用默认模板
        state.md_store.write(body.slug, state.md_store.placeholder(contest))
        body_written = f"docs/contests/{body.slug}.md"

    # git commit + push
    msg = body.commit_message or f"add({body.slug}): via API"
    paths = ["contests.csv"]
    if body_written:
        paths.append(body_written)
    sha, pushed = state.git_ops.commit_and_push(msg, paths)

    return {
        "ok": True,
        "slug": body.slug,
        "csv_written": True,
        "body_written": body_written,
        "committed": bool(sha),
        "commit_sha": sha[:8] if sha else "",
        "pushed": pushed,
    }


@app.put("/contests/{slug}")
def update_contest(slug: str, body: ContestPatch):
    fields = {k: v for k, v in body.model_dump().items()
              if v is not None and k not in ("commit_message",)}
    # tags: list -> string
    if "tags" in fields and isinstance(fields["tags"], list):
        fields["tags"] = " ".join(fields["tags"])

    if "slug" in fields and fields["slug"] != slug:
        # rename: 新 slug
        new_slug = fields.pop("slug")
        old = state.csv_store.get(slug)
        if old is None:
            raise HTTPException(404, detail={"code": "slug_not_found",
                                              "message": f"slug not found: {slug}"})
        # 删旧, 加新
        state.csv_store.delete(slug)
        old.slug = new_slug
        state.csv_store.add(old)
        slug = new_slug
        # 重命名 md 文件
        if state.md_store.exists(f"{fields.get('_old_slug', '')}".strip() or new_slug):
            pass
    elif fields:
        state.csv_store.update(slug, **fields)

    state.csv_store.save()

    msg = body.commit_message or f"update({slug}): via API"
    paths = ["contests.csv"]
    sha, pushed = state.git_ops.commit_and_push(msg, paths)

    return {
        "ok": True,
        "slug": slug,
        "committed": bool(sha),
        "commit_sha": sha[:8] if sha else "",
        "pushed": pushed,
    }


@app.patch("/contests/{slug}/body")
def update_body(slug: str, body: BodyPatch):
    if state.csv_store.get(slug) is None:
        raise HTTPException(404, detail={"code": "slug_not_found",
                                          "message": f"slug not found: {slug}"})
    state.md_store.write(slug, body.content)
    sha, pushed = state.git_ops.commit_and_push(
        f"update({slug}): body", [f"docs/contests/{slug}.md"],
    )
    return {
        "ok": True,
        "slug": slug,
        "body_written": f"docs/contests/{slug}.md",
        "committed": bool(sha),
        "commit_sha": sha[:8] if sha else "",
        "pushed": pushed,
    }


@app.delete("/contests/{slug}")
def delete_contest(
    slug: str,
    keep_body: bool = Query(False),
    commit_message: str | None = Query(None),
):
    c = state.csv_store.get(slug)
    if c is None:
        raise HTTPException(404, detail={"code": "slug_not_found",
                                          "message": f"slug not found: {slug}"})

    state.csv_store.delete(slug)
    state.csv_store.save()

    body_removed = False
    if not keep_body:
        body_removed = state.md_store.delete(slug)

    msg = commit_message or f"remove({slug}): via API"
    paths = ["contests.csv"]
    if body_removed:
        paths.append(f"docs/contests/{slug}.md")
    sha, pushed = state.git_ops.commit_and_push(msg, paths)

    return {
        "ok": True,
        "slug": slug,
        "csv_removed": True,
        "body_removed": body_removed,
        "committed": bool(sha),
        "commit_sha": sha[:8] if sha else "",
        "pushed": pushed,
    }


# === 入口 (供 `python -m tools.server` 调用) ===

# === Import endpoints (Phase 3.2) ===

from platforms.base import (  # noqa: E402
    CFBlockedError, CookieExpiredError, NotFoundError, ParseError,
    PlatformError, get_client_class,
)


class UpdatePreviewRequest(BaseModel):
    platform: str = "qoj"
    contest_id: str
    user: str | None = None      # None 时用 config.default_user
    slug: str | None = None       # 覆盖 slug 生成


class UpdateApplyRequest(BaseModel):
    platform: str = "qoj"
    preview: dict                  # 完整 preview (与 preview 响应一致)
    overrides: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)


class UpsolvePreviewRequest(BaseModel):
    platform: str = "qoj"
    contest_id: str | None = None
    slug: str | None = None
    user: str | None = None
    since: str | None = None      # ISO, 不传则自动推断


class UpsolveApplyRequest(BaseModel):
    platform: str = "qoj"
    preview: dict
    options: dict = Field(default_factory=dict)


def _get_user(req_user: str | None) -> str:
    """从 request 或 config 读默认 user."""
    if req_user:
        return req_user
    cfg = state.config_dir / "config.json"
    if cfg.exists():
        import json
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            users = data.get("default_user", {})
            if isinstance(users, dict) and "qoj" in users:
                return users["qoj"]
        except (json.JSONDecodeError, KeyError):
            pass
    raise HTTPException(400, detail={
        "code": "no_user_specified",
        "message": "未指定 user 且 config 里没默认 user",
    })


def _platform_error_to_http(e: PlatformError) -> HTTPException:
    """把 PlatformError 转成对应 HTTPException."""
    if isinstance(e, CookieExpiredError):
        return HTTPException(401, detail={"code": "qoj_cookie_expired",
                                          "message": str(e)})
    if isinstance(e, CFBlockedError):
        return HTTPException(502, detail={"code": "qoj_cf_blocked",
                                          "message": str(e)})
    if isinstance(e, NotFoundError):
        return HTTPException(404, detail={"code": "qoj_contest_not_found",
                                          "message": str(e)})
    if isinstance(e, ParseError):
        return HTTPException(422, detail={"code": "qoj_parse_failed",
                                          "message": str(e)})
    return HTTPException(500, detail={"code": "platform_error",
                                       "message": str(e)})


@app.post("/import/update-preview")
def import_update_preview(body: UpdatePreviewRequest):
    user = _get_user(body.user)
    try:
        preview = build_update_preview(
            platform=body.platform,
            contest_id=body.contest_id,
            user=user,
            csv_store=state.csv_store,
            config_dir=state.config_dir,
            slug_override=body.slug,
        )
    except ValueError as e:
        msg = str(e)
        if "cookie_missing_for_platform" in msg:
            raise HTTPException(401, detail={
                "code": "cookies_missing_for_platform",
                "message": f"cookie 文件不存在: {state.config_dir}/cookies/{body.platform}.txt",
            })
        raise HTTPException(400, detail={"code": "invalid_request",
                                          "message": msg})
    except PlatformError as e:
        raise _platform_error_to_http(e)

    return preview.to_dict()


@app.post("/import/update-apply")
def import_update_apply(body: UpdateApplyRequest):
    preview_dict = body.preview
    platform = preview_dict.get("platform", body.platform)

    # 重建 preview 对象 (避免序列化/反序列化失真)
    contest_nested = preview_dict.get("contest", {})
    preview = UpdatePreview(
        platform=platform,
        contest_id=preview_dict.get("contest_id") or contest_nested.get("contest_id", ""),
        username=preview_dict.get("username", ""),
        record_state=preview_dict.get("record_state", "create_new"),
        slug=preview_dict.get("slug", ""),
        slug_exists=preview_dict.get("slug_exists", False),
        contest=contest_nested,
        problems=preview_dict.get("problems", []),
        summary=preview_dict.get("summary", {}),
        suggested=preview_dict.get("suggested", {}),
    )

    options = body.options
    try:
        result = apply_update(
            preview=preview,
            csv_store=state.csv_store,
            md_store=state.md_store,
            git_ops=state.git_ops,
            overrides=body.overrides,
            create_body=options.get("create_body", True),
            run_sync=options.get("run_sync", True),
            push=options.get("push", True),
        )
    except CsvValidationError:
        raise  # 让全局 handler 处理
    except PlatformError as e:
        raise _platform_error_to_http(e)

    return result.to_dict()


@app.post("/import/upsolve-preview")
def import_upsolve_preview(body: UpsolvePreviewRequest):
    user = _get_user(body.user)
    try:
        preview = build_upsolve_preview(
            platform=body.platform,
            contest_id=body.contest_id,
            slug=body.slug,
            user=user,
            csv_store=state.csv_store,
            config_dir=state.config_dir,
            since_override=body.since,
        )
    except ValueError as e:
        msg = str(e)
        if "slug_not_found_in_csv" in msg:
            raise HTTPException(404, detail={
                "code": "slug_not_found",
                "message": msg,
            })
        raise HTTPException(400, detail={"code": "invalid_request",
                                          "message": msg})
    except PlatformError as e:
        raise _platform_error_to_http(e)

    return preview.to_dict()


@app.post("/import/upsolve-apply")
def import_upsolve_apply(body: UpsolveApplyRequest):
    preview_dict = body.preview
    preview = UpsolvePreview(
        platform=preview_dict["platform"],
        slug=preview_dict["slug"],
        contest_id=preview_dict.get("contest_id"),
        username=preview_dict.get("username", ""),
        since=preview_dict.get("since", ""),
        current_problems=preview_dict.get("current_problems", []),
        changes=preview_dict.get("changes", []),
        summary=preview_dict.get("summary", {}),
    )

    options = body.options
    try:
        result = apply_upsolve(
            preview=preview,
            csv_store=state.csv_store,
            md_store=state.md_store,
            git_ops=state.git_ops,
            commit_message=options.get("commit_message"),
            push=options.get("push", True),
        )
    except CsvValidationError:
        raise
    except PlatformError as e:
        raise _platform_error_to_http(e)

    return result.to_dict()


@app.get("/import/contest/{cid}")
def import_contest_meta(cid: str, platform: str = Query("qoj")):
    """只取 contest meta, 不抓 submissions."""
    try:
        client = make_client(platform, state.config_dir)
        meta = client.get_contest_meta(cid)
    except ValueError as e:
        if "cookie_missing" in str(e):
            raise HTTPException(401, detail={
                "code": "cookies_missing_for_platform",
                "message": str(e),
            })
        raise HTTPException(400, detail={"code": "invalid_request",
                                          "message": str(e)})
    except PlatformError as e:
        raise _platform_error_to_http(e)
    return contest_meta_to_dict(meta)


# === 入口 (供 `python -m tools.server` 调用) ===

if __name__ == "__main__":
    import uvicorn
    bind = os.environ.get("BIND", "127.0.0.1")
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run(app, host=bind, port=port, log_level="info")