import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Risk:
    file: str
    line: int
    severity: str          # HIGH / MEDIUM / LOW
    message: str
    rule_id: str
    confidence: str = "N/A (rule)"  # 规则固定值或 LLM 浮点数
    source: str = "rule"            # "rule" 或 "llm"
    fix: str = ""           # 修复建议
    code_snippet: str = ""  # 问题代码片段（从 diff 中提取）


# -- 规则定义 --

_RULE_SPECS: list[tuple[str, str, str]] = [
    # (rule_id, pattern, severity) — 仅保留零误报规则
    (
        "R001",
        r"\b(?:eval|exec)\s*\(",
        "HIGH",
    ),
    (
        "R002",
        r"\bshell\s*=\s*True",
        "HIGH",
    ),
    (
        "R003",
        r"except\s*:\s*pass",
        "MEDIUM",
    ),
    (
        "R004",
        r"(?:api_key|secret|password|token|auth_token|api_secret)\s*=\s*['\"][a-zA-Z0-9_\-]{8,}['\"]",
        "HIGH",
    ),
    (
        "R005",
        r"def\s+\w+\s*\([^)]*=\s*(?:\[\]|\{\})",
        "MEDIUM",
    ),
    (
        "R006",
        r"^\s*(?:print|console\.log|var_dump|dd)\s*\(",
        "LOW",
    ),
]

_RULE_MESSAGES = {
    "R001": "使用了 eval/exec，存在代码注入风险",
    "R002": "subprocess 中使用 shell=True，存在命令注入风险",
    "R003": "裸 except: pass 吞掉所有异常，影响调试和稳定性",
    "R004": "疑似硬编码密钥/密码，应使用环境变量或密钥管理服务",
    "R005": "使用了可变默认参数（=[] 或 ={}），会导致状态污染和数据串链",
    "R006": "生产代码中残留调试打印，应替换为 logging 模块",
}

# 预编译所有规则的正则表达式
_RULES: list[tuple[str, re.Pattern, str, str]] = [
    (rid, re.compile(pat, re.MULTILINE | re.DOTALL | re.IGNORECASE), sev, _RULE_MESSAGES.get(rid, rid))
    for rid, pat, sev in _RULE_SPECS
]

# -- .lumidiff.toml 自定义规则 --

_custom_rules: list[tuple[str, re.Pattern, str, str]] | None = None  # (id, pattern, severity, message)


def _load_custom_rules() -> list[tuple[str, re.Pattern, str, str]]:
    """从 .lumidiff.toml 加载自定义规则。"""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return []

    config_path = Path(".lumidiff.toml")
    if not config_path.is_file():
        return []

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception:
        return []

    rules = []
    for item in config.get("rule", {}).get("custom", []):
        rid = item.get("id", "CUSTOM")
        pattern = item.get("pattern", "")
        severity = item.get("severity", "MEDIUM").upper()
        message = item.get("message", f"自定义规则 {rid} 命中")
        if pattern:
            try:
                compiled = re.compile(pattern, re.MULTILINE | re.DOTALL)
                rules.append((rid, compiled, severity, message))
            except re.error:
                pass
    return rules


def get_rules() -> list[tuple[str, re.Pattern, str, str]]:
    """获取所有规则（内置 + 自定义）。"""
    global _custom_rules
    if _custom_rules is None:
        _custom_rules = _load_custom_rules()

    all_rules = list(_RULES)
    all_rules.extend(_custom_rules)
    return all_rules


# -- 公开接口 --

def scan(filepath: str, patch: str) -> list[Risk]:
    """扫描单个文件的 patch，检测规则违规。

    仅检查新增行（hunk 中以 '+' 开头的行）。
    """
    results: list[Risk] = []

    # 提取新增行的行号和内容
    added_lines = _extract_added_lines(patch)
    if not added_lines:
        return results

    for rule_id, pattern, severity, message in get_rules():
        for lineno, content in added_lines:
            if pattern.search(content):
                results.append(Risk(
                    file=filepath,
                    line=lineno,
                    severity=severity,
                    message=message,
                    rule_id=rule_id,
                    code_snippet=content.strip(),
                ))

    return results


def scan_all(files: list["FileDiff"]) -> list[Risk]:
    """扫描一组 FileDiff 对象。"""
    from lumidiff.diff_source import FileDiff
    all_risks: list[Risk] = []
    for fd in files:
        if fd.patch:
            all_risks.extend(scan(fd.path, fd.patch))
    # 按严重度排序：HIGH > MEDIUM > LOW，同严重度按文件名排序
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    all_risks.sort(key=lambda r: (order.get(r.severity, 9), r.file, r.line))
    return all_risks


# -- 内部函数 --

def _extract_added_lines(patch: str) -> list[tuple[int, str]]:
    """从 unified diff 中提取新增行的 (行号, 内容)。

    通过 @@ -old,oldlen +new,newlen @@ 标头定位 hunk。
    """
    result: list[tuple[int, str]] = []
    new_line = 0
    in_hunk = False

    for line in patch.split("\n"):
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            new_line = int(m.group(1))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith("+"):
            content = line[1:]  # 去掉行首的 '+'
            result.append((new_line, content))
            new_line += 1
        elif line.startswith("-"):
            # 删除行 — 不推进 new_line 计数器
            pass
        else:
            # 上下文行或标头 — 同时推进新旧行号
            new_line += 1

    return result
