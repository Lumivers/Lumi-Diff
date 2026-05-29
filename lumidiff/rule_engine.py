import re
from dataclasses import dataclass


@dataclass
class Risk:
    file: str
    line: int
    severity: str          # HIGH / MEDIUM / LOW
    message: str
    rule_id: str
    confidence: str = "N/A (rule)"  # rule-literal or LLM float
    source: str = "rule"            # "rule" or "llm"


# -- rule definitions --

_RULE_SPECS: list[tuple[str, str, str]] = [
    # (rule_id, pattern, severity)
    (
        "R001",
        r"(?:password|secret|api_key|api_secret|token|AUTH_TOKEN)\s*=\s*['\"][^'\"]{8,}['\"]",
        "HIGH",
    ),
    (
        "R002",
        r"\b(?:eval|exec)\s*\(",
        "HIGH",
    ),
    (
        "R003",
        r"\bshell\s*=\s*True",
        "HIGH",
    ),
    (
        "R004",
        r"except\s*:\s*",
        "MEDIUM",
    ),
    (
        "R005",
        r"#\s*(?:TODO|FIXME|HACK)",
        "LOW",
    ),
]

_RULE_MESSAGES = {
    "R001": "疑似硬编码密钥",
    "R002": "使用了 eval/exec，存在代码注入风险",
    "R003": "subprocess 中使用 shell=True，存在命令注入风险",
    "R004": "裸 except: 可能吞掉关键异常，影响调试和稳定性",
    "R005": "TODO/FIXME/HACK 标记残留",
}

# patterns are pre-compiled per spec
_RULES: list[tuple[str, re.Pattern, str]] = [
    (rid, re.compile(pat, re.MULTILINE | re.DOTALL), sev)
    for rid, pat, sev in _RULE_SPECS
]


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

    for rule_id, pattern, severity in _RULES:
        for lineno, content in added_lines:
            if pattern.search(content):
                results.append(Risk(
                    file=filepath,
                    line=lineno,
                    severity=severity,
                    message=_RULE_MESSAGES[rule_id],
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
