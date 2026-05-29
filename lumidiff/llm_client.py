import json
import os
import time
from dataclasses import dataclass, field

import requests
from pydantic import BaseModel, ValidationError
from rich.progress import Progress, SpinnerColumn, TextColumn


# -- Pydantic models for LLM response --

class _Suggestion(BaseModel):
    file: str
    line: int
    severity: str           # HIGH / MEDIUM / LOW
    message: str
    confidence: float


class _LLMResponse(BaseModel):
    summary: str
    suggestions: list[_Suggestion] = field(default_factory=list)
    commit_message: str | None = None


# -- result dataclass --

@dataclass
class LLMResult:
    summary: str = ""
    suggestions: list = field(default_factory=list)
    commit_message: str | None = None
    elapsed_seconds: float = 0.0
    model_used: str = ""
    raw_text: str = ""      # fallback when JSON parse fails
    parse_error: str | None = None


# -- config --

_MODEL_BASES = {
    "deepseek-chat": "https://api.deepseek.com/v1",
    "deepseek-reasoner": "https://api.deepseek.com/v1",
    "moonshot-v1-8k": "https://api.moonshot.cn/v1",
    "moonshot-v1-32k": "https://api.moonshot.cn/v1",
    "moonshot-v1-128k": "https://api.moonshot.cn/v1",
}


def _build_system_prompt(is_local_mode: bool) -> str:
    commit_line = '"commit_message": "feat: xxx（建议的 commit message）"' if is_local_mode else ""
    return f"""你是资深代码审查专家。以下是一次代码变更的 unified diff。请仔细分析后，以严格 JSON 格式输出审查结果。

输出 JSON 格式：
{{
  "summary": "变更摘要（中文，2-4句，概括这次改了什么、影响范围）",
  "suggestions": [
    {{
      "file": "文件路径",
      "line": 行号,
      "severity": "HIGH|MEDIUM|LOW",
      "message": "具体、可执行的问题描述",
      "confidence": 0.0 到 1.0 之间的浮点数
    }}
  ]{',' + commit_line if commit_line else ''}
}}

重要约束：
- confidence 必须诚实评估。不确定时宁低勿高，不要虚标 0.9+。
- 仅报告 diff 中实际可见的代码，不要臆测上下文或推测文件其它部分。
- 关注跨行代码模式（如跨行的 SQL 拼接、换行写的 except 等）。
- 如果 diff 中没有值得报告的问题，suggestions 数组可以为空。
- 输出必须是纯 JSON，不要包裹在 ```json``` 或任何其他格式中。"""


# -- API call --

def analyze(
    diff_text: str,
    model: str = "deepseek-chat",
    is_local: bool = False,
) -> LLMResult:
    """Send diff to LLM and return structured result."""
    api_key = os.environ.get("LUMIDIFF_API_KEY", "")
    if not api_key:
        return LLMResult(
            summary="(未设置 LUMIDIFF_API_KEY，跳过 LLM 分析)",
            parse_error="missing API key",
        )

    api_base = os.environ.get(
        "LUMIDIFF_API_BASE",
        _MODEL_BASES.get(model, "https://api.deepseek.com/v1"),
    )

    # truncate diff to avoid blowing context
    max_chars = 8000
    if len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars] + "\n... (diff truncated)"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt(is_local)},
            {"role": "user", "content": diff_text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": False,
    }

    t0 = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task("AI analyzing...", total=None)

        try:
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return LLMResult(
                summary="(LLM 调用失败)",
                parse_error=f"network: {e}",
                elapsed_seconds=time.perf_counter() - t0,
                model_used=model,
            )

    elapsed = time.perf_counter() - t0
    body = resp.json()
    content = body["choices"][0]["message"]["content"]

    # parse JSON — retry once on failure
    for attempt in range(2):
        try:
            parsed = json.loads(content)
            validated = _LLMResponse.model_validate(parsed)
            return _llm_to_result(validated, model, elapsed)
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == 0:
                content = content.strip().removeprefix("```json").removesuffix("```").strip()
                continue
            return LLMResult(
                summary="(LLM 返回格式异常，以下为原始输出)",
                parse_error=str(e),
                raw_text=content,
                elapsed_seconds=elapsed,
                model_used=model,
            )

    # unreachable
    return LLMResult(parse_error="unknown", elapsed_seconds=elapsed, model_used=model)


def _llm_to_result(validated: _LLMResponse, model: str, elapsed: float) -> LLMResult:
    from lumidiff.rule_engine import Risk

    suggestions = [
        Risk(
            file=s.file,
            line=s.line,
            severity=s.severity.upper(),
            message=s.message,
            rule_id=f"llm-{i}",
            confidence=str(s.confidence),
            source="llm",
        )
        for i, s in enumerate(validated.suggestions)
    ]
    return LLMResult(
        summary=validated.summary,
        suggestions=suggestions,
        commit_message=validated.commit_message,
        elapsed_seconds=elapsed,
        model_used=model,
    )
