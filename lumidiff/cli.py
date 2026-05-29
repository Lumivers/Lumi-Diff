import argparse
import json
import os
import sys

from lumidiff.diff_source import get_staged_diff, get_pr_diff, get_commit_diff
from lumidiff.rule_engine import scan_all
from lumidiff.llm_client import analyze, DEFAULT_MODEL, DEFAULT_API_BASE
from lumidiff.reporter import render


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add shared flags to a subcommand parser."""
    parser.add_argument(
        "--model", type=str, default=None,
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
    parser.add_argument(
        "--show-all", action="store_true",
        help="显示所有建议，包括低置信度（默认隐藏 confidence <= 0.6 的建议）",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lumidiff",
        description="AI-PR-Review CLI — AI 辅助代码审查工具",
        epilog=(
            "示例:\n"
            "  lumidiff local              分析暂存区变更（git add 之后）\n"
            "  lumidiff commit HEAD~1      分析上一次 commit\n"
            "  lumidiff commit <github-url> 分析 GitHub commit\n"
            "  lumidiff pr <url>           分析 GitHub Pull Request\n"
            "  lumidiff model              查看/切换 LLM 模型\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # lumidiff local
    local_parser = subparsers.add_parser(
        "local",
        help="分析暂存区变更（git add 之后、commit 之前）",
    )
    local_parser.add_argument(
        "--stage", action="store_true",
        help="自动执行 git add -A 后再分析",
    )
    _add_common_args(local_parser)

    # lumidiff commit
    commit_parser = subparsers.add_parser(
        "commit",
        help="分析一个 git commit（本地 hash 或 GitHub URL）",
    )
    commit_parser.add_argument(
        "ref", type=str,
        help="commit hash / ref（如 HEAD~1, abc123）或 GitHub commit URL",
    )
    _add_common_args(commit_parser)

    # lumidiff pr
    pr_parser = subparsers.add_parser(
        "pr",
        help="分析一个 GitHub Pull Request",
    )
    pr_parser.add_argument(
        "url", type=str,
        help="GitHub PR URL",
    )
    _add_common_args(pr_parser)

    # lumidiff model
    model_parser = subparsers.add_parser(
        "model",
        help="查看或切换当前 LLM 模型配置",
    )
    model_parser.add_argument(
        "name", nargs="?", default=None,
        help="要切换到的模型名称（留空则查看当前配置）",
    )
    model_parser.add_argument(
        "--base-url", type=str, default=None,
        help="API Base URL",
    )

    args = parser.parse_args()

    # no subcommand → show help
    if args.command is None:
        parser.print_help()
        return

    # handle subcommands
    if args.command == "model":
        _show_or_switch_model(args)
        return

    # the rest: local / commit / pr
    _run_analysis(args)


def _run_analysis(args) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")

    # 1. get diff
    if args.command == "pr":
        diff = get_pr_diff(args.url, github_token=token or None)
    elif args.command == "commit":
        diff = get_commit_diff(
            args.ref, github_token=token or None,
            context_lines=args.context_lines,
        )
    else:  # local
        diff = get_staged_diff(context_lines=args.context_lines)

        # auto-stage: --stage flag or interactive prompt
        if not diff.files and not diff.skipped_files:
            if args.stage:
                _do_git_add()
                diff = get_staged_diff(context_lines=args.context_lines)
            elif _has_unstaged_changes():
                try:
                    answer = input("发现未暂存的变更，是否先 git add -A？[y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "n"
                if answer in ("y", "yes"):
                    _do_git_add()
                    diff = get_staged_diff(context_lines=args.context_lines)

    if not diff.files:
        if args.json:
            print(json.dumps({
                "error": "no code files to analyze",
                "skipped_files": diff.skipped_files,
            }, ensure_ascii=False))
        else:
            if diff.skipped_files:
                print(f"所有 {len(diff.skipped_files)} 个文件均为非代码文件，已跳过：")
                for f in diff.skipped_files[:10]:
                    print(f"  - {f}")
                if len(diff.skipped_files) > 10:
                    print(f"  ...等 {len(diff.skipped_files) - 10} 个")
            else:
                print("没有找到可分析的代码变更。")
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
            is_local=(args.command == "local"),
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


def _do_git_add() -> None:
    import subprocess
    subprocess.run(["git", "add", "-A"], check=True)
    print("已执行 git add -A")


def _has_unstaged_changes() -> bool:
    import subprocess
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return bool(result.stdout.strip())


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
        table.add_row(
            "API Key",
            f"{env_key}: ***已设置***" if os.environ.get(env_key)
            else f"{env_key}: [red]未设置[/red]",
        )
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
