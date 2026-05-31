import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from functools import lru_cache

import requests


# -- 内置文件扩展名黑名单 --
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

# -- .lumignore 支持 --

def _load_lumignore() -> list[str]:
    """从 .lumignore 文件加载过滤规则（当前目录或 git 根目录）。"""
    patterns: list[str] = []
    for candidate in [Path(".lumignore"), Path(_git_root()) / ".lumignore"]:
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
            break
    return patterns


def _git_root() -> str:
    """获取 git 仓库根目录。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return "."


def _match_lumignore(filepath: str, patterns: list[str]) -> bool:
    """检查文件路径是否匹配 .lumignore 规则（兼容 gitignore 语法）。"""
    from fnmatch import fnmatch
    path = filepath.replace("\\", "/")
    for pat in patterns:
        # directory pattern: "docs/" matches "docs/anything"
        if pat.endswith("/"):
            if path.startswith(pat) or fnmatch(path, pat + "*"):
                return True
        # file pattern
        elif fnmatch(path, pat) or fnmatch(Path(path).name, pat):
            return True
    return False


_lumignore_patterns: list[str] | None = None


def should_ignore(filepath: str) -> bool:
    global _lumignore_patterns
    if _lumignore_patterns is None:
        _lumignore_patterns = _load_lumignore()

    name = Path(filepath).name
    ext = Path(filepath).suffix
    if ext in IGNORED_EXTENSIONS:
        return True
    if name in IGNORED_FILENAMES:
        return True
    for pat in IGNORED_PATTERNS:
        if pat.match(name):
            return True
    if _lumignore_patterns and _match_lumignore(filepath, _lumignore_patterns):
        return True
    return False


# -- 数据结构 --

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


# -- GitHub URL 解析 --

_PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)

_COMMIT_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commit/(?P<sha>[0-9a-f]{7,40})"
)


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    m = _PR_URL_RE.search(url)
    if not m:
        return None
    return m.group("owner"), m.group("repo"), int(m.group("number"))


def parse_commit_url(url: str) -> tuple[str, str, str] | None:
    m = _COMMIT_URL_RE.search(url)
    if not m:
        return None
    return m.group("owner"), m.group("repo"), m.group("sha")


# -- diff 获取 --

def get_staged_diff(context_lines: int = 10) -> DiffResult:
    """执行 git diff --staged 获取暂存区变更。"""
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


def get_commit_diff(
    commit_ref: str,
    github_token: str | None = None,
    context_lines: int = 10,
) -> DiffResult:
    """获取 commit 的 diff。支持 GitHub URL 或本地 git SHA/ref。"""
    parsed = parse_commit_url(commit_ref)
    if parsed:
        owner, repo, sha = parsed
        return _get_github_commit_diff(owner, repo, sha, github_token)
    # treat as local git ref
    return _get_local_commit_diff(commit_ref, context_lines)


def _get_local_commit_diff(ref: str, context_lines: int = 10) -> DiffResult:
    """通过 git show 获取本地 commit 的 diff。"""
    cmd = ["git", "show", ref, f"-U{context_lines}"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise ValueError(f"git show 失败: {result.stderr.strip()}")
    raw = result.stdout or ""
    files = _parse_diff_files(raw)
    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)
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


def _get_github_commit_diff(
    owner: str, repo: str, sha: str, github_token: str | None,
) -> DiffResult:
    """通过 GitHub API 获取单个 commit 的 diff。"""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "lumidiff",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    files_data = data.get("files", [])
    return _build_diff_result_from_github_files(files_data)


def get_pr_diff(
    pr_url: str,
    github_token: str | None = None,
) -> DiffResult:
    """通过 GitHub API 获取 PR diff。"""
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

    headers_tuple = tuple(headers.items())
    all_files_data = _fetch_pr_files(owner, repo, pr_number, headers_tuple)
    return _build_diff_result_from_github_files(all_files_data)


@lru_cache(maxsize=16)
def _fetch_pr_files(owner: str, repo: str, pr_number: int, headers: tuple[tuple[str, str], ...]) -> list[dict]:
    """Fetch PR files from GitHub API with caching."""
    headers_dict = dict(headers)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    params = {"per_page": 100}

    all_files: list[dict] = []
    while url:
        resp = requests.get(url, headers=headers_dict, params=params if "?" not in url else {})
        resp.raise_for_status()
        data = resp.json()
        all_files.extend(item for item in data if isinstance(item, dict))
        url = _next_page(resp.headers)
    return all_files


# -- 辅助函数 --

def _build_diff_result_from_github_files(files_data: list[dict]) -> DiffResult:
    """将 GitHub API 返回的 files 数组转换为 DiffResult。"""
    all_files: list[FileDiff] = []
    truncated: list[str] = []

    for item in files_data:
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


def _parse_diff_files(raw: str) -> list[FileDiff]:
    """从 unified diff 输出中解析文件列表。"""
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
    """将 FileDiff 列表拼接为 unified diff 字符串。"""
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
    """从 Link 响应头中提取下一页 URL。"""
    link = headers.get("Link", "")
    for part in link.split(","):
        if 'rel="next"' in part:
            start = part.find("<") + 1
            end = part.find(">")
            if start > 0 and end > start:
                return part[start:end]
    return None
