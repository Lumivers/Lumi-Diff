import argparse
import os
import sys

from lumidiff.diff_source import get_staged_diff, get_pr_diff, get_commit_diff
from lumidiff.rule_engine import scan_all
from lumidiff.llm_client import analyze, DEFAULT_MODEL, DEFAULT_API_BASE
from lumidiff.reporter import render


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lumidiff",
        description="AI-PR-Review CLI — analyze staged diff or GitHub PR",
    )
    subparsers = parser.add_subparsers(dest="command")

    # lumidiff model
    model_parser = subparsers.add_parser("model", help="查看或切换当前 LLM 模型配置")
    model_parser.add_argument(
        "name", nargs="?", default=None,
        help="要切换到的模型名称（留空则查看当前配置）",
    )
    model_parser.add_argument(
        "--base-url", type=str, default=None,
        help="API Base URL",
    )

    parser.add_argument(
        "--pr", type=str, default=None,
        help="GitHub PR URL to analyze",
    )
    parser.add_argument(
        "--commit", type=str, default=None,
        help="GitHub commit URL to analyze",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"LLM model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of rich rendering",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Skip LLM analysis, only run rule engine",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: plain text output, exit 1 on HIGH risks",
    )
    parser.add_argument(
        "--context-lines", type=int, default=10,
        help="Lines of context around hunks (default: 10)",
    )
    args = parser.parse_args()

    # handle `lumidiff model` subcommand
    if args.command == "model":
        _show_or_switch_model(args)
        return

    # 1. get diff
    if args.pr and args.commit:
        print("错误: --pr 和 --commit 不能同时使用")
        sys.exit(1)

    token = os.environ.get("GITHUB_TOKEN", "")
    if args.pr:
        diff = get_pr_diff(args.pr, github_token=token or None)
    elif args.commit:
        diff = get_commit_diff(args.commit, github_token=token or None)
    else:
        diff = get_staged_diff(context_lines=args.context_lines)

    if not diff.files:
        if args.json:
            print('{"error": "no code files to analyze"}')
        else:
            print("No code files to analyze.")
        return

    # 2. rule engine
    risks = scan_all(diff.files)

    # 3. LLM
    llm_result = None
    if not args.no_llm:
        diff_text = diff.context_diff or diff.raw_diff
        llm_result = analyze(
            diff_text,
            model=args.model,
            is_local=(args.pr is None and args.commit is None),
        )

    # 4. merge LLM suggestions into risks (de-duplicate)
    if llm_result and llm_result.suggestions:
        rule_keys = {(r.file, r.line, r.message) for r in risks}
        for s in llm_result.suggestions:
            key = (s.file, s.line, s.message)
            if key not in rule_keys:
                risks.append(s)
        risks.sort(key=lambda r: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(r.severity, 9), r.file))

    # 5. render
    if args.ci:
        _ci_output(risks)
    else:
        render(diff, risks, llm_result, args)


def _show_or_switch_model(args) -> None:
    from rich.console import Console
    from rich.table import Table
    from lumidiff.llm_client import _MODEL_REGISTRY

    console = Console()
    current_model = os.environ.get("LUMIDIFF_MODEL", DEFAULT_MODEL)

    if args.name:
        os.environ["LUMIDIFF_MODEL"] = args.name
        if args.base_url:
            os.environ["LUMIDIFF_API_BASE"] = args.base_url
        console.print(f"[green]已切换模型:[/green] {args.name}")
        if args.base_url:
            console.print(f"[green]API Base:[/green] {args.base_url}")
    else:
        base, env_key = _MODEL_REGISTRY.get(current_model, (DEFAULT_API_BASE, "LUMIDIFF_API_KEY"))
        table = Table(title="当前 LLM 配置", show_header=False, border_style="cyan")
        table.add_column("Key", style="bold")
        table.add_column("Value")
        table.add_row("模型", current_model)
        table.add_row("API Base", base)
        table.add_row("API Key", f"{env_key}: ***已设置***" if os.environ.get(env_key) else f"{env_key}: [red]未设置[/red]")
        console.print(table)
        console.print()
        console.print("[dim]切换模型: lumidiff model <name> --base-url <url>[/dim]")


def _ci_output(risks: list) -> None:
    high_count = 0
    for r in risks:
        tag = r.severity.upper()[:4]
        print(f"[{tag}] {r.file}:{r.line}  {r.message}")
        if r.severity.upper() == "HIGH":
            high_count += 1
    if high_count > 0:
        print(f"\n{high_count} HIGH risk(s) found.")
        sys.exit(1)
    else:
        print("\nNo HIGH risks.")
        sys.exit(0)
