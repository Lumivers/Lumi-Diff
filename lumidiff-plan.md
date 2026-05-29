# LumiDiff Implementation Plan

## Context
从零搭建一个 CLI 工具，输入 GitHub PR 链接或本地 staged diff，输出结构化 AI Review 报告。

**架构原则**：Core / Presentation 严格分离。所有分析模块只返回 dataclass，`reporter.py` 是唯一消费 `rich` 的模块。`--json` 作为 API 边界，为后续 IDE 插件 / 独立 App 留扩展点。第一期 32h 只交付 CLI，但架构不封死后续可能。

---

## 优先级标签说明

| 标签 | 含义 | 执行策略 |
|------|------|----------|
| 🔴 MUST | 核心链路，没有它产品不成立 | Day 1-2 必须完成 |
| 🟡 SHOULD | 显著加分，但不阻塞核心链路 | Day 2-3 视进度穿插 |
| ⚪ NICE | 锦上添花 | Day 3 仍有时间才碰 |
| ❌ CUT | 32h 内明确不做 | 文档里提一句作为未来方向即可 |

### 核心链路（🔴 MUST 做完后就能跑通的 MVP）

1. 本地 staged diff → 规则引擎扫 5 条 → LLM 调 DeepSeek → rich 输出 Summary + Risks
2. PR URL → GitHub API 拿 patch → 同上流程 → rich 输出
3. `--json` 输出 + `--model` 切换

**做完这条链路 = 可以演示 = 作业交得上去。** 其他所有 🟡 ⚪ 是增量。

---

---

## Project Structure

```
LumiDiff/
├── lumidiff/
│   ├── __init__.py          # 版本号
│   ├── __main__.py          # python -m lumidiff entry
│   ├── cli.py               # argparse + main orchestration
│   ├── config.py            # .lumidiff.toml / .lumignore 加载
│   ├── diff_source.py       # 获取 diff（本地 git staged 或 GitHub PR）
│   ├── lumignore.py         # 文件过滤，排除非代码杂碎
│   ├── rule_engine.py       # 确定性规则扫描 + 自定义规则
│   ├── llm_client.py        # LLM API 调用（DeepSeek/Kimi，并行+缓存）
│   └── reporter.py          # rich 终端输出（含文件统计表、耗时预估）
├── pyproject.toml
├── README.md
└── .gitignore
```

---

## 🔴 Step 1: pyproject.toml + package skeleton

- `pyproject.toml`: 项目元数据 + 依赖 (`rich`, `requests`, `pydantic`, `tomli` for Python <3.11) + console_scripts entry point (`lumidiff`)
- `.gitignore`: Python 标准忽略项
- `lumidiff/__init__.py`: `__version__ = "0.1.0"`
- `lumidiff/__main__.py`: `from lumidiff.cli import main; main()`

---

## 🟡 Step 2: Config Layer (`config.py`)

> 🟡 先做最简版本：只加载 `.lumidiff.toml` 里 `[llm]` 段（model / api_base），其余用硬编码。自定义规则、ignore 合并等 ⚪ 阶段再补。

负责加载所有配置文件，提供统一配置对象。

### .lumidiff.toml（项目级，可选）
```toml
# 规则开关 / 降级
[rule.hardcoded_secret]
enabled = true
severity = "HIGH"      # 可覆盖为 "MEDIUM" 或 "LOW"

[rule.eval_exec]
enabled = false         # 关闭某规则

[rule.debug_print]
enabled = true
severity = "LOW"

# 自定义规则
[[rule.custom]]
id = "no-console-log"
pattern = "console\\.log\\s*\\("
severity = "LOW"
message = "避免在提交中包含 console.log"

[[rule.custom]]
id = "internal-import"
pattern = "from\\s+internal\\."
severity = "HIGH"
message = "禁止引用 internal 包"

# LLM 配置
[llm]
model = "deepseek-chat"
max_context_chars = 8000       # 单次 LLM 调用的最大 diff 字符数
context_lines = 10             # 每个 hunk 前后扩展行数
concurrency = 3                # 并行 LLM 调用数

# 忽略文件（与 .lumignore 合并）
[[ignore]]
patterns = ["docs/*", "*.md", "examples/*"]
```

### 加载优先级
1. 内置默认值 → 2. `.lumidiff.toml`（仓库根目录）覆盖 → 3. 命令行参数覆盖
- 使用 `tomllib`（Python 3.11+）或 `tomli`（兼容 3.10）
- `Config` dataclass 统一承载所有配置项

---

## Step 3: CLI Layer (`cli.py`)

### 🔴 MUST 参数
| 参数 | 说明 |
|------|------|
| （默认） | 本地 staged diff 模式 |
| `--pr <url>` | GitHub PR 模式 |
| `--model <name>` | 覆盖 LLM 模型（默认 deepseek-chat） |
| `--json` | 输出原始 JSON，替代 rich 渲染 |

### 🟡 SHOULD 参数
| 参数 | 说明 |
|------|------|
| `--no-llm` | 仅运行规则引擎，跳过 LLM |
| `--ci` | CI 模式：HIGH 风险时退出码 1，否则 0。输出精简文本 |
| `--context-lines <N>` | hunk 上下文扩展行数（默认 10） |

### ⚪ NICE 参数
| 参数 | 说明 |
|------|------|
| `--quiet` / `-q` | 仅输出 risk 列表 |

### 🟡 CI 模式行为
- 无 rich 渲染，输出纯文本（适合 CI log）
- 发现 HIGH 风险 → exit code = 1
- 无 HIGH 风险 → exit code = 0

### 主流程编排
1. 加载配置（`config.py`）→ 2. 获取 diff → 3. lumignore 过滤 → 4. 规则引擎扫描 → 5. LLM 分析（可选）→ 6. 渲染报告

---

## 🔴 Step 4: Diff Source + 上下文获取策略 (`diff_source.py`)

### 🔴 上下文获取策略（赛题明确要求的设计说明）

**问题**：Unified diff 的 hunk header `@@ -a,b +c,d @@` 默认只带 3 行上下文。对大函数、大类，LLM 无法看到完整结构，分析质量下降。

**策略 — 两级上下文**：
1. **Hunk 级**（🔴 默认）：本地 `git diff --staged -U{context_lines}` 直接拿扩展上下文；PR 模式直接用 GitHub API 返回的 `patch` 字段（自带 3 行上下文）
2. **文件级**（❌ V2）：拉完整文件内容，需二次 API + base64 解码 + token 控制，32h 内不碰

**实施**：
- 本地模式：`git diff --staged -U{context_lines}` 直接拿扩展上下文
- PR 模式：直接使用 `/pulls/{n}/files` 返回的 `patch` 字段，**不二次拉取**。文档里写明"V1 使用 patch 级上下文，V2 引入文件级扩展"

### 🟡 GitHub API patch 为 null 的边缘处理

**问题**：GitHub `GET /repos/{owner}/{repo}/pulls/{number}/files` 返回的 JSON 中，如果文件过大或 diff 过长，`patch` 字段会直接为 `null`（不返回 diff 内容以避免撑爆响应体）。这不是二进制文件，而是一个需要特殊处理的边缘情况。

**保底逻辑**（遍历 files 时对每个 file 执行）：
```python
if file.get("patch") is None:
    if lumignore.should_ignore(file["filename"]):
        continue  # 确实是非代码文件，跳过
    # 代码文件但 patch 为空 → 拉全量内容作为降级
    content = fetch_file_content(owner, repo, file["filename"], ref=base_sha)
    diff_result.skipped_for_size.append(file["filename"])  # 标记为"超大 diff 降级"
    # 用全量文件内容替代 patch，标记为 "full file (patch unavailable)"
```
- 在 Reporter 中单独列出 "Files with truncated diff (patch unavailable): N"，说明这些文件的分析基于全量内容而非 diff，精度可能下降
- 这展示了**边缘情况处理能力**，是评委眼中的加分项
- 大 PR 切片策略：⚪ 按文件边界切片

### 🔴 DiffResult 结构
```python
@dataclass
class DiffResult:
    files: list[FileDiff]           # 每个文件的变更信息
    raw_diff: str                   # 完整 unified diff
    context_diff: str               # 扩展上下文后的 diff（送给 LLM）
    skipped_files: list[str]        # 被 lumignore 过滤掉的文件
    total_additions: int
    total_deletions: int
    estimated_review_minutes: tuple[int, int]  # (min, max)

@dataclass
class FileDiff:
    path: str
    patch: str                      # 该文件的 unified diff
    additions: int
    deletions: int
    language: str                   # 从扩展名推断
```

### ⚪ Review 耗时预估
> ⚪ 纯启发式，不影响核心链路。公式：
> `est = (total_additions * lang_coefficient) + (high_risk_count * 3min)`，返回 `(est*0.7, est*1.3)` 区间

---

## 🔴 Step 5: 文件过滤 (`diff_source.py` 内置)

> 🔴 极简实现：内置扩展名黑名单 set，30 行代码。不要独立 `lumignore.py` 模块。

**🔴 内置黑名单**：
- 二进制/图片: `*.png`, `*.jpg`, `*.gif`, `*.ico`, `*.svg`, `*.woff`, `*.woff2`, `*.ttf`, `*.eot`, `*.mp4`, `*.mp3`, `*.webm`
- 锁文件: `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `Gemfile.lock`, `poetry.lock`, `composer.lock`, `*.sum`
- 编译产物: `*.pyc`, `*.class`, `*.o`, `*.so`, `*.dll`, `*.exe`, `*.bin`, `*.wasm`
- 压缩/文档: `*.zip`, `*.tar.gz`, `*.rar`, `*.7z`, `*.pdf`, `*.docx`, `*.xlsx`
- 生成/压缩代码: `*.min.js`, `*.min.css`, `*.map`, `*.bundle.js`
- 数据文件: `*.csv`, `*.tsv`, `*.jsonl`, `*.parquet`
- 自动生成: `*.pb.go`, `*.proto`, `*_generated.*`

**🟡 项目级 `.lumignore`**（可选）：
> 🟡 支持读取仓库根目录 `.lumignore` 文件，语法兼容 `.gitignore`

---

## 🔴 Step 6: Rule Engine (`rule_engine.py`)

### 🔴 内置规则（5 条核心）

| # | 规则 | 正则 | 严重度 |
|---|------|------|--------|
| 1 | 硬编码密钥 | `(password\|secret\|api_key\|token\|AUTH_TOKEN)\s*=\s*['"][^'"]{8,}['"]` | HIGH |
| 2 | eval/exec 使用 | `\beval\s*\(` / `\bexec\s*\(` | HIGH |
| 3 | shell=True | `shell\s*=\s*True` | HIGH |
| 4 | 裸 except: | `except\s*:` 且下一行无具体异常类型或为 `pass` | MEDIUM |
| 5 | TODO/FIXME/HACK | `#\s*(TODO\|FIXME\|HACK)` | LOW |

### 🟡 扩展规则（+2 条）

| 6 | SQL 注入风险 | f-string / `.format()` 在 cursor.execute / session.execute 中 | HIGH |
| 7 | 调试打印残留 | `\b(print\|console\.log\|var_dump\|dd)\s*\(` 排除测试文件 | LOW |

### ⚪ 自定义规则 + 误报控制
- 解析 `[[rule.custom]]` 段，生成与内置规则同结构的 `Rule` 对象
- 统一走同一个 `scan()` 函数，内置与自定义无区别对待

### 误报控制
1. **仅扫描变更行**：从 hunk header 提取新增/修改行号范围，跳过未变更代码
2. **行级抑制**：`# lumidiff:ignore` 或 `// lumidiff:ignore` — 该行跳过所有规则
3. **规则级抑制**：`# lumidiff:ignore(E001)` — 跳过指定规则
4. **文件级抑制**：`.lumidiff.toml` 中按文件路径 glob 关闭规则

### 🔴 扫描原则

- 按文件逐行扫描，仅对新增/修改行检查
- 启用 `re.MULTILINE | re.DOTALL`，跨行模式用 `[\s\S]*?` 非贪婪
- 规则引擎定位为**首层快速扫描**（零误报优先），LLM 作为**二道防线**查漏

### Risk dataclass
```python
@dataclass
class Risk:
    file: str
    line: int
    severity: str        # HIGH / MEDIUM / LOW
    message: str
    rule_id: str
    confidence: str      # "N/A (rule)" 或 LLM 的 0-1 值
    source: str          # "rule" 或 "llm"
```

---

## 🔴 Step 7: LLM Client (`llm_client.py`)

### 🔴 模型支持
- API Key: `LUMIDIFF_API_KEY` 环境变量
- API Base: `LUMIDIFF_API_BASE`，按 `--model` 推断默认值
  - `deepseek-chat` → `https://api.deepseek.com/v1`
  - `kimi` / `moonshot-v1` → `https://api.moonshot.cn/v1`
- OpenAI 兼容 `/chat/completions` 端点（`requests` 库）

### 🔴 API 调用细节

**强制 JSON 输出**：
```python
payload = {
    "model": model,
    "messages": [...],
    "response_format": {"type": "json_object"},
    "temperature": 0.3,
}
```

**🔴 Pydantic 校验 + 降级**：
```python
try:
    parsed = json.loads(raw)
    result = LLMResponse.model_validate(parsed)
except (json.JSONDecodeError, ValidationError):
    if retry_count < 1:
        retry()
    else:
        degrade_to_raw(raw)  # 界面不崩
```

### 🔴 System Prompt
- 角色：资深代码审查专家
- 输入：扩展上下文后的 unified diff
- 输出要求：严格 JSON
  ```json
  {
    "summary": "变更摘要（中文，2-4句）",
    "suggestions": [
      {
        "file": "src/auth.py",
        "line": 42,
        "severity": "HIGH",
        "message": "具体、可执行的问题描述",
        "confidence": 0.92
      }
    ],
    "commit_message": "feat: xxx（仅本地模式生成）"
  }
  ```
- 约束：confidence 诚实，不确定时宁低勿高；仅报告 diff 中实际可见的代码

### 🟡 响应速度优化
> 🟡 Day 2 末有空再加

1. **PR 缓存**：`functools.lru_cache` 装饰获取函数，同一 URL 复用
2. **超时 + 重试**：单次 30s 超时，失败重试一次
3. **Progress 显示**：rich Progress bar "AI analyzing..."
4. **⚪ 多文件并行**：`ThreadPoolExecutor`

### 🔴 LLMResult
```python
@dataclass
class LLMResult:
    summary: str
    suggestions: list[Risk]
    commit_message: str | None
    elapsed_seconds: float
    model_used: str
```

---

## 🔴 Step 8: Reporter (`reporter.py`)

终端渲染结构（自上而下）：

### 🔴 1. Header Panel
```
┌─ LumiDiff Report ────────────────────┐
│ Files: 5 (1 skipped) │ Risks: 3      │
│ LLM: 3.2s (deepseek-chat)            │
└──────────────────────────────────────┘
```

### 🔴 2. Summary（来自 LLM）
Rich Markdown 渲染，中文变更摘要

### 🟡 3. 文件变更统计表
> 🟡 rich Table，+Δ/-Δ 列，>50 行标红

### 🔴 4. Risks Table
- 列：Severity | Location | Description | Confidence
- Severity 圆点：🔴 HIGH red，🟡 MEDIUM yellow，⚪ LOW dim
- `File:Line` 格式支持终端 clickable link
- confidence < 0.8 的行整体 dim/gray
- LLM Suggestions 过滤掉与 rule 重复的项

### ❌ 5. 交互模式 — CUT
> TUI 需要 curses/rich live，32h 内不碰

### 🔴 输出模式

| 模式 | 输出 |
|------|------|
| 默认 | rich Panel + Table + Markdown |
| `--json` | `print(json.dumps(report_dict))` |
| 🟡 `--ci` | 纯文本 `[SEVERITY] file:line` |
| ⚪ `--quiet` | 仅 Risks Table |

---

## 🔴 Step 9: README.md （赛题要求的完整设计说明）

> 🔴 先写骨架（5 个核心章节），🟡 补充 CI 说明。

1. **简介** + 安装
2. **快速开始**：本地 / PR 模式
3. **🔴 模型选择说明**：DeepSeek-V3 推荐理由（性价比、中文、兼容 OpenAI）、Kimi 替代
4. **🔴 上下文获取策略**：V1 patch 级（`git -U10`），V2 文件级扩展
5. **🔴 规则引擎设计**：5 条核心规则 + 双轨分析原则
6. **🔴 未来扩展**：MCP Server、IDE 插件（VSCode Diagnostics API）、GitLab 支持、tree-sitter 仓库级分析
7. **🟡 CI 集成**：`--ci` 模式 + GitHub Actions 示例

---

## 🔴 Step 10: Verification

### 🔴 本地模式
- `git add` 后 `lumidiff`，确认 Summary + Risks
- 构造含 5 条规则各类风险的 demo diff

### 🔴 PR 模式
- 小 PR（1-2 文件）、中 PR（5-10 文件）

### 🟡 CI 模式
- HIGH 风险 → exit 1，无 HIGH → exit 0

### 参数
- `--json` / `--no-llm` / `--model`

---

## Implementation Order（按优先级）

### Day 1 — 先让核心链路跑通（🔴 MUST）
1. `pyproject.toml` + `.gitignore` + package skeleton
2. `diff_source.py` — git staged + GitHub API + 内置扩展名黑名单
3. `rule_engine.py` — 5 条规则 + `re.MULTILINE`
4. `llm_client.py` — 单线程 DeepSeek 调用 + response_format + Pydantic 校验
5. `reporter.py` — rich Panel + Summary + Risks Table + dim
6. `cli.py` — argparse（默认 / --pr / --json / --model）+ 主流程编排
7. 端到端测试（本地 + 至少 1 个 PR）

### Day 2 — 补齐加分项（🟡 SHOULD）
8. `config.py` — 最简 `.lumidiff.toml`（仅 [llm] 段）
9. `--no-llm` / `--ci` / `--context-lines` 参数
10. 扩展规则 +2 条（SQL 注入、调试打印）
11. `lru_cache` PR 缓存
12. 文件统计表
13. 🟡 `.lumignore` 文件读取

### Day 3 — 打磨（⚪ NICE）
14. `README.md` — 完整文档（模型选择 / 上下文策略 / 扩展方向）
15. 3 个真实 PR 端到端验证
16. ⚪ 自定义规则 / 行级抑制 / Review 耗时预估（能做一个算一个）
