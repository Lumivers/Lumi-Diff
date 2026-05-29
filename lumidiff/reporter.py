import json

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
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
    header = Panel(
        f"Files: {file_count}  [+{added} -{deleted}]   Risks: {risk_count}   {llm_info}",
        title="LumiDiff Report",
        border_style="bold cyan",
    )
    console.print(header)

    # -- summary --
    if llm and llm.summary and not llm.parse_error:
        console.print(Markdown(llm.summary))
        console.print()

    # -- risks table --
    if risks:
        console.print(f"[bold]Risks[/bold]  {risk_count} found\n")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Sev", width=4)
        table.add_column("Location", width=30)
        table.add_column("Description")
        table.add_column("Confidence", width=14)

        for r in risks:
            sev_color, sev_label = _severity_style(r.severity)

            loc = Text(f"{r.file}:{r.line}")
            loc.stylize("underline")

            conf_text = r.confidence if r.confidence else "-"
            if r.source == "rule":
                conf_text = "N/A (rule)"

            # dim low-confidence LLM suggestions
            is_low_conf = False
            if r.source == "llm":
                try:
                    if float(r.confidence) < 0.8:
                        is_low_conf = True
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

    elif not risks:
        console.print("[dim]No risks detected.[/dim]")

    # -- commit message hint --
    if llm and llm.commit_message:
        console.print()
        console.print("[bold]Suggested commit message:[/bold]")
        console.print(f"  {llm.commit_message}")

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


def _severity_style(severity: str) -> tuple[str, str]:
    s = severity.upper()
    if s == "HIGH":
        return "bold red", "HIGH"
    elif s == "MEDIUM":
        return "yellow", "MED"
    return "dim", "LOW"
