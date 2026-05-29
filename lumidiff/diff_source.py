import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests


# -- built-in file extension blacklist --
IGNORED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".mp3", ".webm", ".ogg", ".wav",
    ".zip", ".tar.gz", ".rar", ".7z", ".gz", ".bz2",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".pyc", ".class", ".o", ".so", ".dll", ".exe", ".bin", ".wasm",
    ".min.js", ".min.css", ".map", ".bundle.js",
    ".csv", ".tsv", ".jsonl", ".parquet",
    ".pb.go", ".proto",
}

IGNORED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "Gemfile.lock", "poetry.lock", "composer.lock",
    "pipfile.lock", "mix.lock",
}

IGNORED_PATTERNS = [
    re.compile(r".*\.sum$"),
    re.compile(r".*_generated\..*"),
    re.compile(r"\.terraform\.lock\..*"),
]


def should_ignore(filepath: str) -> bool:
    name = Path(filepath).name
    ext = Path(filepath).suffix
    if ext in IGNORED_EXTENSIONS:
        return True
    if name in IGNORED_FILENAMES:
        return True
    for pat in IGNORED_PATTERNS:
        if pat.match(name):
            return True
    return False


# -- dataclasses --

@dataclass
class FileDiff:
    path: str
    patch: str | None = None   # None when GitHub truncates the diff
    additions: int = 0
    deletions: int = 0
    truncated: bool = False    # patch was null from GitHub API


@dataclass
class DiffResult:
    files: list[FileDiff] = field(default_factory=list)
    raw_diff: str = ""
    context_diff: str = ""
    skipped_files: list[str] = field(default_factory=list)
    truncated_files: list[str] = field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0


# -- GitHub PR URL parsing --

_PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    m = _PR_URL_RE.search(url)
    if not m:
        return None
    return m.group("owner"), m.group("repo"), int(m.group("number"))


# -- diff sources --

def get_staged_diff(context_lines: int = 10) -> DiffResult:
    """Run git diff --staged and return DiffResult."""
    cmd = ["git", "diff", "--staged", f"-U{context_lines}"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    raw = result.stdout or ""

    files = _parse_diff_files(raw)
    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)

    # filter non-code
    code_files = [f for f in files if not should_ignore(f.path)]
    skipped = [f.path for f in files if should_ignore(f.path)]

    return DiffResult(
        files=code_files,
        raw_diff=raw,
        context_diff=raw,
        skipped_files=skipped,
        total_additions=additions,
        total_deletions=deletions,
    )


def get_pr_diff(
    pr_url: str,
    github_token: str | None = None,
) -> DiffResult:
    """Fetch PR diff via GitHub API. Returns DiffResult."""
    parsed = parse_pr_url(pr_url)
    if not parsed:
        raise ValueError(f"无法解析 PR URL: {pr_url}")
    owner, repo, pr_number = parsed

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "lumidiff",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    params = {"per_page": 100}

    all_files: list[FileDiff] = []
    truncated: list[str] = []

    while url:
        resp = requests.get(url, headers=headers, params=params if "?" not in url else {})
        resp.raise_for_status()
        data = resp.json()

        for item in data:
            if isinstance(item, dict):
                filename = item.get("filename", "")
                if should_ignore(filename):
                    continue
                patch = item.get("patch")
                fd = FileDiff(
                    path=filename,
                    patch=patch,
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                    truncated=patch is None,
                )
                all_files.append(fd)
                if patch is None:
                    truncated.append(filename)

        # handle pagination
        url = _next_page(resp.headers)

    raw_diff = _render_unified_diff(all_files)
    total_add = sum(f.additions for f in all_files)
    total_del = sum(f.deletions for f in all_files)

    return DiffResult(
        files=all_files,
        raw_diff=raw_diff,
        context_diff=raw_diff,
        total_additions=total_add,
        total_deletions=total_del,
        truncated_files=truncated,
    )


# -- helpers --

def _parse_diff_files(raw: str) -> list[FileDiff]:
    """Parse files from unified diff output."""
    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    current_patch: list[str] = []
    additions = 0
    deletions = 0

    for line in raw.split("\n"):
        if line.startswith("diff --git"):
            if current_file and current_patch:
                current_file.patch = "\n".join(current_patch)
                current_file.additions = additions
                current_file.deletions = deletions
                files.append(current_file)
            # start new file
            current_patch = [line]
            additions = 0
            deletions = 0
            current_file = FileDiff(path="")
        elif line.startswith("---"):
            if current_file:
                current_patch.append(line)
        elif line.startswith("+++"):
            if current_file:
                current_patch.append(line)
                # extract filename from +++ b/path
                m = re.match(r"\+\+\+ [ab]/(.*)", line)
                if m:
                    current_file.path = m.group(1)
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
            current_patch.append(line)
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
            current_patch.append(line)
        else:
            current_patch.append(line)

    if current_file and current_patch:
        current_file.patch = "\n".join(current_patch)
        current_file.additions = additions
        current_file.deletions = deletions
        files.append(current_file)

    return [f for f in files if f.path]


def _render_unified_diff(files: list[FileDiff]) -> str:
    """Build a single unified diff string from FileDiff list."""
    parts = []
    for f in files:
        if f.patch:
            parts.append(f.patch)
        else:
            parts.append(f"diff --git a/{f.path} b/{f.path}\n")
            parts.append(f"--- a/{f.path}\n")
            parts.append(f"+++ b/{f.path}\n")
            parts.append(
                f"@@ [patch unavailable] @@\n"
                f"  +{f.additions} additions, -{f.deletions} deletions\n"
            )
    return "\n".join(parts)


def _next_page(headers) -> str | None:
    """Extract next page URL from Link header (requests.response.headers)."""
    link = headers.get("Link", "")
    for part in link.split(","):
        if 'rel="next"' in part:
            start = part.find("<") + 1
            end = part.find(">")
            if start > 0 and end > start:
                return part[start:end]
    return None
