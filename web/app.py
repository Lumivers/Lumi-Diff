"""
LumiDiff Web 后端 — FastAPI 应用
本地开发：uvicorn web.app:app --reload
Cloudflare 部署：通过 Pages Functions 或外部托管
"""

import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# 确保能导入 lumidiff 包
sys.path.insert(0, str(Path(__file__).parent.parent))

from lumidiff.diff_source import get_pr_diff, get_commit_diff, parse_pr_url, parse_commit_url
from lumidiff.rule_engine import scan_all
from lumidiff.llm_client import analyze, DEFAULT_MODEL

app = FastAPI(title="LumiDiff", description="AI 代码审查工具")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# -- token 限额（简单实现，生产环境用 KV/Redis） --

_daily_usage: dict[str, int] = {}
DAILY_LIMIT = int(os.environ.get("DAILY_TOKEN_LIMIT", "5000000"))


def _today_key() -> str:
    from datetime import date
    return date.today().isoformat()


def _check_usage() -> tuple[bool, int]:
    """检查今日用量，返回 (是否可用, 已用 token 数)."""
    key = _today_key()
    used = _daily_usage.get(key, 0)
    return used < DAILY_LIMIT, used


def _add_usage(tokens: int) -> None:
    """累加今日用量."""
    key = _today_key()
    _daily_usage[key] = _daily_usage.get(key, 0) + tokens


# -- 路由 --

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"result": None, "error": None},
    )


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_form(request: Request, url: str = Form(...)):
    # 检查 token 限额
    ok, used = _check_usage()
    if not ok:
        return templates.TemplateResponse(
            request=request, name="index.html",
            context={"result": None, "error": f"今日额度已用完（{used:,} / {DAILY_LIMIT:,} token），请明天再试。"},
        )

    # 判断 URL 类型
    url = url.strip()
    if not url:
        return templates.TemplateResponse(
            request=request, name="index.html",
            context={"result": None, "error": "请输入 GitHub PR 或 Commit URL。"},
        )

    try:
        t0 = time.perf_counter()

        if "/pull/" in url:
            diff = get_pr_diff(url, github_token=os.environ.get("GITHUB_TOKEN") or None)
            is_local = False
        elif "/commit/" in url or parse_commit_url(url):
            diff = get_commit_diff(url, github_token=os.environ.get("GITHUB_TOKEN") or None)
            is_local = False
        else:
            return templates.TemplateResponse(
                request=request, name="index.html",
                context={"result": None, "error": f"无法识别 URL 格式：{url}"},
            )

        # 规则引擎
        risks = scan_all(diff.files)

        # LLM 分析
        llm_result = None
        if diff.files:
            llm_result = analyze(diff.raw_diff, is_local=is_local)

        # 合并 LLM suggestions
        if llm_result and llm_result.suggestions:
            rule_keys = {(r.file, r.line, r.message) for r in risks}
            for s in llm_result.suggestions:
                key = (s.file, s.line, s.message)
                if key not in rule_keys:
                    risks.append(s)
            risks.sort(key=lambda r: (
                {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(r.severity, 9),
                r.file, r.line,
            ))

        elapsed = time.perf_counter() - t0

        # 统计 token 用量（估算：prompt + response）
        if llm_result:
            est_tokens = len(diff.raw_diff) // 4 + len(llm_result.summary) * 2
            _add_usage(est_tokens)

        return templates.TemplateResponse(
            request=request, name="index.html",
            context={
                "result": {
                    "diff": diff,
                    "risks": risks,
                    "llm": llm_result,
                    "elapsed": round(elapsed, 1),
                },
                "error": None,
            },
        )

    except Exception as e:
        return templates.TemplateResponse(
            request=request, name="index.html",
            context={"result": None, "error": f"分析失败：{e}"},
        )


@app.get("/api/analyze")
async def api_analyze(url: str):
    """JSON API，和 CLI --json 输出格式一致."""
    ok, used = _check_usage()
    if not ok:
        return JSONResponse(
            {"error": f"今日额度已用完（{used:,} / {DAILY_LIMIT:,} token）"},
            status_code=429,
        )

    url = url.strip()
    if not url:
        return JSONResponse({"error": "缺少 url 参数"}, status_code=400)

    try:
        if "/pull/" in url:
            diff = get_pr_diff(url, github_token=os.environ.get("GITHUB_TOKEN") or None)
            is_local = False
        else:
            diff = get_commit_diff(url, github_token=os.environ.get("GITHUB_TOKEN") or None)
            is_local = False

        risks = scan_all(diff.files)
        llm_result = analyze(diff.raw_diff, is_local=is_local) if diff.files else None

        if llm_result and llm_result.suggestions:
            rule_keys = {(r.file, r.line, r.message) for r in risks}
            for s in llm_result.suggestions:
                if (s.file, s.line, s.message) not in rule_keys:
                    risks.append(s)

        if llm_result:
            _add_usage(len(diff.raw_diff) // 4 + len(llm_result.summary) * 2)

        return JSONResponse({
            "files": len(diff.files),
            "total_additions": diff.total_additions,
            "total_deletions": diff.total_deletions,
            "risks": [
                {
                    "file": r.file,
                    "line": r.line,
                    "severity": r.severity,
                    "message": r.message,
                    "code_snippet": r.code_snippet,
                    "fix": r.fix,
                    "confidence": r.confidence,
                    "source": r.source,
                }
                for r in risks
            ],
            "summary": llm_result.summary if llm_result else "",
            "commit_message": llm_result.commit_message if llm_result else None,
            "llm_model": llm_result.model_used if llm_result else None,
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
