import json
import os
import re
import time
from dataclasses import dataclass, field

import requests
from pydantic import BaseModel, ValidationError
from rich.progress import Progress, SpinnerColumn, TextColumn


# -- LLM 响应的 Pydantic 模型 --

class _Suggestion(BaseModel):
    file: str
    line: int
    severity: str           # HIGH / MEDIUM / LOW
    message: str
    confidence: float
    fix: str = ""           # 修复建议
    code_snippet: str = ""  # 问题代码片段（原样摘录 diff 中的代码行）


class _LLMResponse(BaseModel):
    summary: str
    suggestions: list[_Suggestion] = field(default_factory=list)
    commit_message: str | None = None


# -- 结果数据结构 --

@dataclass
class LLMResult:
    summary: str = ""
    suggestions: list = field(default_factory=list)
    commit_message: str | None = None
    elapsed_seconds: float = 0.0
    model_used: str = ""
    raw_text: str = ""      # JSON 解析失败时的原始文本
    parse_error: str | None = None


# -- 配置 --

DEFAULT_MODEL = "mimo-v2.5-pro"
DEFAULT_API_BASE = "https://api.xiaomimimo.com/v1"

# 模型 -> (api_base, 环境变量名)
_MODEL_REGISTRY = {
    "deepseek-v4-pro": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "mimo-v2.5-pro": ("https://api.xiaomimimo.com/v1", "MIMO_API_KEY"),
}


def _resolve_api_key(model: str) -> str:
    """读取模型对应的 API Key 环境变量。"""
    if model in _MODEL_REGISTRY:
        return os.environ.get(_MODEL_REGISTRY[model][1], "")
    return ""


def _resolve_api_base(model: str) -> str:
    """解析 API Base URL：环境变量 > 模型注册表 > 默认值。"""
    env = os.environ.get("LUMIDIFF_API_BASE")
    if env:
        return env
    if model in _MODEL_REGISTRY:
        return _MODEL_REGISTRY[model][0]
    return DEFAULT_API_BASE


def _build_system_prompt(is_local_mode: bool) -> str:
    commit_line = '"commit_message": "feat: xxx（建议的 commit message）"' if is_local_mode else ""
    return f"""你是资深代码审查专家，同时精通安全审计、代码规范和变更管理。以下是一次代码变更的 unified diff。请从以下 5 个维度综合分析：

## 分析维度

### 1. 安全审计（必做）
- 注入漏洞：SQL/NoSQL/OS Command/XSS/路径穿越/SSRF
- 硬编码敏感信息：API Key / Token / Password（⚠️ 测试文件中的 mock key 不算）
- 不安全 API：弱加密（MD5/SHA-1）、禁用 TLS、不安全反序列化
- 认证授权缺陷：缺少鉴权、IDOR、Token 泄露
- 注意：仅报告真实风险，不要对测试文件中的 fake key、fixture 数据报安全问题

### 2. 代码规范
- 未使用变量/导入、拼写错误、命名不规范
- 框架最佳实践违背（如 Python 中的 bare except、TypeScript 中的 any 滥用）
- 与项目现有代码风格的一致性

### 3. 逻辑与健壮性
- 边界条件未处理、空指针风险、异常吞没
- 并发/竞态问题、资源泄露（文件句柄、连接未关闭）
- 跨行代码模式（如跨行 SQL 拼接、换行写的 except）

### 4. 变更影响评估
- Breaking Change 检测：公开 API 移除/重命名/签名变更
- 依赖变更风险：新增依赖是否有已知漏洞
- 配置变更影响

### 5. 测试覆盖
- 新增功能是否配套测试
- 关键路径（公开 API / 异常分支）是否有测试

## 输出 JSON 格式
{{
  "summary": "变更摘要（中文，2-4句，概括这次改了什么、影响范围）",
  "suggestions": [
    {{
      "file": "文件路径",
      "line": 行号,
      "severity": "HIGH|MEDIUM|LOW",
      "message": "问题描述",
      "code_snippet": "问题代码行（从 diff 中原样摘录，不含 +/- 前缀）",
      "fix": "修复建议代码或操作步骤",
      "confidence": 0.0 到 1.0
    }}
  ]{',' + commit_line if commit_line else ''}
}}

## 约束
- confidence 诚实评估，不确定时宁低勿高
- 仅报告 diff 中实际可见的代码，不要臆测
- 测试文件（tests/、__tests__/、*_test.*）中的 mock/fixture 数据不算安全问题
- .changeset/、*.md 等非代码文件的变更不需要报告代码问题
- 如果没有值得报告的问题，suggestions 可以为空
- 输出必须是纯 JSON，不要包裹在 ```json``` 中

## 误报控制（重要）
- 仅报告真实风险，不要把"代码风格偏好"或"可以更好"当作问题
- severity 为 HIGH 必须有明确的安全漏洞或崩溃风险，"不方便维护"、"不够优雅"最多标 LOW
- 依赖框架默认安全机制的代码（如模板引擎自动转义、ORM 参数化查询）不要报告注入类漏洞
- 已被注释、TODO 标记或文档说明的已知限制，不要重复报告
- 配置/脚本/工具代码中的宽松写法（如通用异常兜底、硬编码本地地址），不要套用生产服务标准
- 如果不确定某个问题是否真实存在，confidence 应低于 0.6，让置信度过滤机制处理"""


def _sanitize_json(text: str) -> str:
    """修复 LLM JSON 输出的常见问题：markdown 包裹、非法转义。"""
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    # 修复非法 JSON 转义：\s, \p, \c 等不在 JSON 规范中的转义
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
    return text


# -- API 调用 --

def analyze(
    diff_text: str,
    model: str | None = None,
    is_local: bool = False,
) -> LLMResult:
    """将 diff 发送给 LLM 并返回结构化结果。"""
    if model is None:
        model = os.environ.get("LUMIDIFF_MODEL", DEFAULT_MODEL)
    api_key = _resolve_api_key(model)
    env_key = _MODEL_REGISTRY.get(model, ("", "LUMIDIFF_API_KEY"))[1]
    if not api_key:
        return LLMResult(
            summary=f"(未设置 {env_key}，跳过 LLM 分析)",
            parse_error=f"missing {env_key}",
        )

    api_base = _resolve_api_base(model)

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
                timeout=int(os.environ.get("LUMIDIFF_TIMEOUT", "120")),
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

    # 解析 JSON — 失败时重试一次
    for attempt in range(2):
        try:
            parsed = json.loads(content)
            validated = _LLMResponse.model_validate(parsed)
            return _llm_to_result(validated, model, elapsed)
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == 0:
                content = _sanitize_json(content)
                continue
            return LLMResult(
                summary="(LLM 返回格式异常，以下为原始输出)",
                parse_error=str(e),
                raw_text=content,
                elapsed_seconds=elapsed,
                model_used=model,
            )

    # 不可达
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
            fix=s.fix,
            code_snippet=s.code_snippet,
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
