import argparse
import os
import sys

from lumidiff.diff_source import get_staged_diff, get_pr_diff
from lumidiff.rule_engine import scan_all
from lumidiff.llm_client import analyze
from lumidiff.reporter import render


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lumidiff",
        description="AI-PR-Review CLI — analyze staged diff or GitHub PR",
    )
    parser.add_argument(
        "--pr", type=str, default=None,
        help="GitHub PR URL to analyze",
    )
    parser.add_argument(
        "--model", type=str, default="deepseek-chat",
        help="LLM model name (default: deepseek-chat)",
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

    # 1. get diff
    if args.pr:
        token = os.environ.get("GITHUB_TOKEN", "")
        diff = get_pr_diff(args.pr, github_token=token or None)
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
            is_local=(args.pr is None),
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
