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
    confidence: str = "N/A (rule)"  # rule-literal or LLM float
    source: str = "rule"            # "rule" or "llm"
    fix: str = ""           # 修复建议


# -- rule definitions --

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
]

_RULE_MESSAGES = {
    "R001": "使用了 eval/exec，存在代码注入风险",
    "R002": "subprocess 中使用 shell=True，存在命令注入风险",
    "R003": "裸 except: pass 吞掉所有异常，影响调试和稳定性",
}

# patterns are pre-compiled per spec
_RULES: list[tuple[str, re.Pattern, str, str]] = [
    (rid, re.compile(pat, re.MULTILINE | re.DOTALL), sev, _RULE_MESSAGES.get(rid, rid))
    for rid, pat, sev in _RULE_SPECS
]

# -- custom rules from .lumidiff.toml --

_custom_rules: list[tuple[str, re.Pattern, str, str]] | None = None  # (id, pattern, severity, message)


def _load_custom_rules() -> list[tuple[str, re.Pattern, str, str]]:
    """Load custom rules from .lumidiff.toml."""
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
    """Get all rules (built-in + custom)."""
    global _custom_rules
    if _custom_rules is None:
        _custom_rules = _load_custom_rules()

    all_rules = list(_RULES)
    all_rules.extend(_custom_rules)
    return all_rules


# -- public API --

def scan(filepath: str, patch: str) -> list[Risk]:
    """Scan a single file's patch for rule violations.

    Only checks added lines (lines starting with '+' in the hunk body).
    """
    results: list[Risk] = []

    # extract line numbers for added lines
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
                ))

    return results


def scan_all(files: list["FileDiff"]) -> list[Risk]:
    """Scan a collection of FileDiff objects."""
    from lumidiff.diff_source import FileDiff
    all_risks: list[Risk] = []
    for fd in files:
        if fd.patch:
            all_risks.extend(scan(fd.path, fd.patch))
    # sort: HIGH first, then MEDIUM, then LOW; within same severity by file
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    all_risks.sort(key=lambda r: (order.get(r.severity, 9), r.file, r.line))
    return all_risks


# -- internal --

def _extract_added_lines(patch: str) -> list[tuple[int, str]]:
    """Parse unified diff to extract (new_line_number, content) for added lines.

    Navigates hunks using the @@ -old,oldlen +new,newlen @@ headers.
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
            content = line[1:]  # strip the leading '+'
            result.append((new_line, content))
            new_line += 1
        elif line.startswith("-"):
            # deleted line — does not advance new_line counter
            pass
        else:
            # context line or header — advances both old and new
            new_line += 1

    return result
