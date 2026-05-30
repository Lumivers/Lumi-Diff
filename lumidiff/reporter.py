import json
import re
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from lumidiff.diff_source import DiffResult
from lumidiff.llm_client import LLMResult
from lumidiff.rule_engine import Risk


def render(
    diff: DiffResult,
    risks: list[Risk],
    llm: LLMResult | None,
    args,
) -> None:
    """Render the full LumiDiff report."""
    console = Console()

    # -- json mode --
    if args.json:
        _render_json(diff, risks, llm)
        return

    # -- header panel --
    file_count = len(diff.files)
    risk_count = len(risks)
    llm_info = ""
    if llm and llm.parse_error and "missing" in llm.parse_error:
        llm_info = "LLM: skipped"
    elif llm:
        llm_info = f"LLM: {llm.elapsed_seconds:.1f}s ({llm.model_used})"

    added = diff.total_additions
    deleted = diff.total_deletions
    est_min, est_max = _estimate_review_minutes(diff, risks)
    header = Panel(
        f"Files: {file_count}  [+{added} -{deleted}]   Risks: {risk_count}   "
        f"Review: ~{est_min}-{est_max}min   {llm_info}",
        title="LumiDiff Report",
        border_style="bold cyan",
    )
    console.print(header)

    # -- file stats table --
    if diff.files:
        ftable = Table(
            show_header=True, header_style="bold",
            width=120, expand=False,
            title="Files Changed",
        )
        ftable.add_column("File", no_wrap=False, ratio=1)
        ftable.add_column("+", width=6, justify="right")
        ftable.add_column("-", width=6, justify="right")

        for f in sorted(diff.files, key=lambda x: x.additions + x.deletions, reverse=True):
            total = f.additions + f.deletions
            if total > 50:
                style = "bold red"
            elif total > 20:
                style = "yellow"
            else:
                style = ""
            ftable.add_row(
                f.path,
                f"+{f.additions}",
                f"-{f.deletions}",
                style=style,
            )

        console.print(ftable)
        console.print()

    # -- summary --
    if llm and llm.summary and not llm.parse_error:
        console.print(Markdown(llm.summary))
        console.print()

    # -- risks table --
    if risks:
        # split: visible (confidence > 0.6 or rule-based) vs hidden
        show_all = getattr(args, "show_all", False)
        visible, hidden = _split_by_confidence(risks, show_all)

        rule_count = sum(1 for r in risks if r.source == "rule")
        llm_count = risk_count - rule_count
        parts = []
        if rule_count:
            parts.append(f"[bold red]Rules: {rule_count}[/bold red]")
        if llm_count:
            parts.append(f"[bold yellow]LLM: {llm_count}[/bold yellow]")
        console.print(f"[bold]Risks[/bold]  {'  |  '.join(parts)}\n")
        table = Table(
            show_header=True, header_style="bold",
            width=120, expand=False,
        )
        table.add_column("Sev", width=6, no_wrap=True)
        table.add_column("Location", no_wrap=True)
        table.add_column("Description", no_wrap=False, ratio=1)
        table.add_column("Confidence", width=16, no_wrap=True)

        for r in visible:
            sev_color, sev_label = _severity_style(r.severity)

            loc = Text(f"{r.file}:{r.line}")
            loc.stylize("underline")

            conf_text = r.confidence if r.confidence else "-"
            if r.source == "rule":
                conf_text = "N/A (rule)"

            # mark low-confidence as uncertain
            is_low_conf = False
            if r.source == "llm":
                try:
                    if float(r.confidence) < 0.8:
                        is_low_conf = True
                        conf_text = f"{r.confidence} (uncertain)"
                except (ValueError, TypeError):
                    pass

            row_style = "dim" if is_low_conf else ""
            table.add_row(
                Text(sev_label, style=sev_color),
                loc,
                r.message,
                conf_text,
                style=row_style,
            )

        console.print(table)

        if hidden:
            console.print(
                f"[dim]... {len(hidden)} 个低置信度建议已隐藏，使用 --show-all 查看全部[/dim]"
            )

        # -- fix suggestions (only for visible risks) --
        fixes = [r for r in visible if r.fix]
        if fixes:
            console.print()
            console.print("[bold]Fix Suggestions[/bold]\n")
            for r in fixes:
                sev_color, sev_label = _severity_style(r.severity)
                console.print(
                    f"  {Text(sev_label, style=sev_color)} "
                    f"[underline]{r.file}:{r.line}[/underline]"
                )
                console.print(f"  {r.message}")
                # strip markdown fences and render with syntax highlighting
                fix_text = _strip_code_fences(r.fix)
                if _looks_like_code(fix_text):
                    lang = _detect_lang(r.file)
                    console.print(Syntax(fix_text, lang, theme="monokai", padding=(0, 2)))
                else:
                    console.print(f"  [green]Fix:[/green] {fix_text}")
                console.print()

    elif not risks:
        console.print("[dim]No risks detected.[/dim]")

    # -- commit message hint --
    if llm and llm.commit_message:
        high_risks = [r for r in risks if r.severity.upper() == "HIGH"]
        if high_risks:
            console.print()
            console.print("[bold red]存在 HIGH 风险，建议修复后再提交。[/bold red]")

        console.print()
        console.print("[bold]Suggested commit message:[/bold]")
        console.print(f"  {llm.commit_message}")
        # show ready-to-copy git commit command
        escaped_msg = llm.commit_message.replace('"', '\\"')
        console.print()
        console.print("[dim]复制执行：[/dim]")
        console.print(f"  [cyan]git commit -m \"{escaped_msg}\"[/cyan]")

    # -- skipped / truncated --
    if diff.skipped_files:
        console.print()
        console.print(
            f"[dim]Skipped {len(diff.skipped_files)} non-code files: "
            + ", ".join(diff.skipped_files[:5])
            + ("..." if len(diff.skipped_files) > 5 else "")
            + "[/dim]"
        )
    if diff.truncated_files:
        console.print()
        console.print(
            f"[yellow]Files with patch unavailable (diff too large, "
            f"analysis degraded): {len(diff.truncated_files)}[/yellow]"
        )

    # -- parse warning --
    if llm and llm.parse_error and "missing" not in llm.parse_error:
        console.print()
        console.print(f"[yellow]LLM parse warning: {llm.parse_error}[/yellow]")
        if llm.raw_text:
            console.print("[dim]" + llm.raw_text[:500] + "[/dim]")


def _render_json(diff: DiffResult, risks: list[Risk], llm: LLMResult | None) -> None:
    output = {
        "files": len(diff.files),
        "total_additions": diff.total_additions,
        "total_deletions": diff.total_deletions,
        "skipped_files": diff.skipped_files,
        "truncated_files": diff.truncated_files,
        "risks": [
            {
                "file": r.file,
                "line": r.line,
                "severity": r.severity,
                "fix": r.fix,
                "message": r.message,
                "rule_id": r.rule_id,
                "confidence": r.confidence,
                "source": r.source,
            }
            for r in risks
        ],
    }
    if llm:
        output["summary"] = llm.summary
        output["commit_message"] = llm.commit_message
        output["llm_model"] = llm.model_used
        output["llm_elapsed_s"] = round(llm.elapsed_seconds, 2)
        if llm.parse_error:
            output["llm_error"] = llm.parse_error

    print(json.dumps(output, ensure_ascii=False, indent=2))


def _estimate_review_minutes(
    diff: DiffResult, risks: list[Risk],
) -> tuple[int, int]:
    """Estimate review time in minutes based on diff size and risk count."""
    lang_coeff = {
        ".py": 0.5, ".js": 0.4, ".ts": 0.4, ".jsx": 0.4, ".tsx": 0.4,
        ".go": 0.3, ".rs": 0.4, ".java": 0.5, ".kt": 0.5,
    }
    minutes = 0.0
    for f in diff.files:
        coeff = lang_coeff.get(Path(f.path).suffix, 0.5)
        minutes += f.additions * coeff
    high_count = sum(1 for r in risks if r.severity.upper() == "HIGH")
    minutes += high_count * 3
    lo = max(1, int(minutes * 0.7))
    hi = max(1, int(minutes * 1.3))
    return lo, hi


def _severity_style(severity: str) -> tuple[str, str]:
    s = severity.upper()
    if s == "HIGH":
        return "bold red", "HIGH"
    elif s == "MEDIUM":
        return "yellow", "MED"
    return "dim", "LOW"


def _split_by_confidence(
    risks: list[Risk], show_all: bool,
) -> tuple[list[Risk], list[Risk]]:
    """Split risks into visible and hidden based on confidence threshold."""
    if show_all:
        return risks, []
    visible: list[Risk] = []
    hidden: list[Risk] = []
    for r in risks:
        # rule-based risks are always visible
        if r.source == "rule":
            visible.append(r)
            continue
        try:
            conf = float(r.confidence)
            if conf <= 0.6:
                hidden.append(r)
            else:
                visible.append(r)
        except (ValueError, TypeError):
            visible.append(r)
    return visible, hidden


def _strip_code_fences(text: str) -> str:
    """Remove ```lang ... ``` markdown fences."""
    text = text.strip()
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _looks_like_code(text: str) -> bool:
    """Heuristic: if it has multiple lines or indentation, treat as code."""
    lines = text.split("\n")
    if len(lines) >= 2:
        return True
    if text.startswith("    ") or text.startswith("\t"):
        return True
    return False


def _detect_lang(filepath: str) -> str:
    """Guess language from file extension."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "jsx", ".tsx": "tsx", ".go": "go", ".rs": "rust",
        ".java": "java", ".kt": "kotlin", ".rb": "ruby",
        ".sh": "bash", ".yml": "yaml", ".yaml": "yaml",
        ".json": "json", ".html": "html", ".css": "css",
        ".sql": "sql", ".c": "c", ".cpp": "cpp", ".h": "c",
    }
    for ext, lang in ext_map.items():
        if filepath.endswith(ext):
            return lang
    return "text"
