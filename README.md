# LumiDiff — AI 代码审查 CLI 工具
demo演示视频：https://www.bilibili.com/video/BV1W8VJ6hEzN
> 输入 GitHub PR 链接或本地变更，输出结构化审查报告：变更摘要 / 风险识别 / Review 建议。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/lumidiff.svg)](https://pypi.org/project/lumidiff/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---
> 在线体验：https://web-production-3a59c.up.railway.app/
> （因 API 额度和 Railway 免费 tier 限制，此链接将在 2026.06.07 后失效。若出现打不开等问题请使用科学上网）
## 效果展示

```
╭─ LumiDiff Report ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ Files: 3  [+143 -35]   Risks: 4   Review: ~12-18min   LLM: 14.6s (mimo-v2.5-pro)                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯

┌─ Files Changed ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ File                         │    +  │    -  │
│ src/auth.py                  │  +87  │  -12  │  ← 改动量大，标红
│ src/config.py                │  +12  │   -3  │
│ tests/test_auth.py           │  +45  │   -0  │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

本次变更为认证模块新增了 OAuth2 登录支持，同时修复了配置解析的边界问题。

Risks  Rules: 1  |  LLM: 3

┌──────┬──────────────────────┬──────────────────────────────────┬──────────────────────────────────┬────────────────┐
│ Sev  │ Location             │ Code                             │ Description                      │ Confidence     │
├──────┼──────────────────────┼──────────────────────────────────┼──────────────────────────────────┼────────────────┤
│ HIGH │ src/auth.py:42       │ result = eval(user_input)        │ 使用了 eval/exec，存在代码注入风险   │ N/A (rule)     │
│ MED  │ src/config.py:15     │ port = config["port"]            │ 配置项缺少默认值，未设置时可能抛异常   │ 0.85           │
│ LOW  │ src/auth.py:88       │ # TODO: refactor this            │ TODO 标记残留                      │ 0.7 (uncertain)│
└──────┴──────────────────────┴──────────────────────────────────┴──────────────────────────────────┴────────────────┘

Suggested commit message:
  feat(auth): add OAuth2 third-party login support

复制执行：
  git commit -m "feat(auth): add OAuth2 third-party login support"
```

---

## 快速开始

### 1. 安装

```bash
pip install lumidiff
```

### 2. 配置 API Key

```bash
# Windows
set MIMO_API_KEY=sk-your-key-here

# Mac/Linux
export MIMO_API_KEY=sk-your-key-here
```

### 3. 使用

```bash
# 分析暂存区变更（git add 之后）
lumidiff local

# 分析 GitHub PR
lumidiff pr https://github.com/owner/repo/pull/123

# 分析某个 commit
lumidiff commit HEAD~1
lumidiff commit https://github.com/owner/repo/commit/abc123
```

### Web 端

无需安装，浏览器直接访问：

```bash
# 本地启动
pip install fastapi uvicorn jinja2 python-multipart
uvicorn web.app:app --reload --port 8000
# 打开 http://localhost:8000
```

---

## 命令一览

| 命令 | 说明 |
|------|------|
| `lumidiff` | 显示帮助 |
| `lumidiff local` | 分析暂存区变更 |
| `lumidiff local --stage` | 自动 `git add -A` 后分析 |
| `lumidiff commit <ref>` | 分析本地 commit（如 `HEAD~1`） |
| `lumidiff commit <url>` | 分析 GitHub commit |
| `lumidiff pr <url>` | 分析 GitHub PR |
| `lumidiff model` | 查看当前模型配置 |
| `lumidiff model <name>` | 切换模型 |

### 通用参数

| 参数 | 说明 |
|------|------|
| `--model <name>` | 指定 LLM 模型 |
| `--json` | 输出 JSON 格式（供脚本/插件消费） |
| `--no-llm` | 仅运行规则引擎，不调 LLM |
| `--ci` | CI 模式：纯文本输出，HIGH 风险时退出码 1 |
| `--show-all` | 显示所有建议（包括低置信度） |
| `--context-lines <N>` | hunk 上下文行数（默认 10） |

---

## 配置

### 环境变量

| 变量 | 说明 |
|------|------|
| `MIMO_API_KEY` | MiMo API Key |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `LUMIDIFF_API_BASE` | 自定义 API Base URL |
| `LUMIDIFF_MODEL` | 默认模型名 |
| `GITHUB_TOKEN` | GitHub Token（可选，提升 API 速率限制） |
| `LUMIDIFF_TIMEOUT` | LLM 请求超时秒数（默认 120） |

### 模型切换

```bash
# 使用 MiMo（默认）
lumidiff model mimo-v2.5-pro

# 使用 DeepSeek
lumidiff model deepseek-v4-pro

# 使用自定义 OpenAI 兼容模型
lumidiff model gpt-4o --base-url https://api.openai.com/v1
```

### .lumignore 文件

在项目根目录创建 `.lumignore`，语法兼容 `.gitignore`，指定不参与分析的文件：

```
*.md
tests/fixtures/
.changeset/
```

### .lumidiff.toml 自定义规则

在项目根目录创建 `.lumidiff.toml`，添加自定义正则规则：

```toml
[[rule.custom]]
id = "no-console-log"
pattern = "console\\.log\\s*\\("
severity = "LOW"
message = "避免在提交中包含 console.log"

[[rule.custom]]
id = "no-internal-import"
pattern = "from\\s+internal\\."
severity = "HIGH"
message = "禁止引用 internal 包"
```

---

## 设计思路

### 双轨分析架构

LumiDiff 采用**规则引擎 + LLM 双轨分析**：

- **规则引擎**（确定性）：6 条零误报规则，毫秒级完成，作为首层快速扫描
- **LLM 引擎**（概率性）：五维度综合分析（安全审计、代码规范、逻辑健壮性、变更影响、测试覆盖），作为二道防线查漏补缺

两轨结果合并后去重，按严重度排序输出。

规则引擎内置 6 条零误报规则，覆盖安全、健壮性、性能三个维度：

| ID | 规则 | 严重度 | 维度 |
|----|------|--------|------|
| R001 | eval/exec 使用 → 代码注入风险 | HIGH | 安全 |
| R002 | shell=True → 命令注入风险 | HIGH | 安全 |
| R003 | except: pass → 异常吞没 | MEDIUM | 健壮性 |
| R004 | 硬编码密钥/密码 → 凭证泄露 | HIGH | 安全 |
| R005 | 可变默认参数（=[]/= {}）→ 状态污染 | MEDIUM | 健壮性 |
| R006 | 生产代码残留 print/console.log | LOW | 性能 |

用户还可通过 `.lumidiff.toml` 添加自定义正则规则，与内置规则同流程处理。

### 模型选择

| 模型 | 选择理由 |
|------|----------|
| **MiMo v2.5 Pro**（默认） | 中文理解能力强，OpenAI 兼容协议，百万级上下文窗口，token 单价极低 |
| **DeepSeek V4 Pro** | 国产高性能模型，JSON 输出稳定，token 单价极低 |

选择标准：
1. **成本可控** — 两款模型的 token 单价均处于行业低位，单次 PR 审查成本可控在 ¥0.10 以内，适合高频调用
2. **性能足够** — 在代码理解、结构化输出等任务上表现稳定，能够满足代码审查的准确性要求
3. **中文能力** — 代码审查报告面向中文开发者，需要流畅的中文表达
4. **上下文窗口** — 大 PR 的 diff 可能达到数万 token，需要足够的上下文
5. **JSON 输出稳定性** — 结构化输出是核心需求，模型必须稳定返回合法 JSON
6. **API 兼容性** — 统一使用 OpenAI 兼容协议，方便切换模型

### 上下文获取策略

| 模式 | 策略 |
|------|------|
| 本地模式 | `git diff --staged -U10`，直接获取 10 行上下文 |
| PR 模式 | GitHub API `/pulls/{n}/files`，使用 patch 字段 |
| Commit 模式 | `git show -U10` 或 GitHub API `/commits/{sha}` |

当前版本使用 **patch 级上下文**（hunk 前后 N 行），未来计划引入文件级扩展（拉取完整文件补充上下文）。

### 误报与漏报控制

| 层级 | 机制 | 说明 |
|------|------|------|
| 规则引擎 | 6 条零误报规则 | 宁可漏报，不可误报 |
| LLM Prompt | 五维度分析 + 约束指令 | 明确要求忽略测试 mock、changeset 文件 |
| 置信度过滤 | confidence ≤ 0.6 默认折叠 | 低置信度建议不干扰判断 |
| 输出标记 | `(uncertain)` 标记 | 0.6-0.8 区间的建议标注不确定 |

### Inline Code Context

每个 risk 都附带问题代码片段（`code_snippet`），让 reviewer 无需跳转即可理解问题上下文：

- **规则引擎**：扫描时从 diff 的 added lines 中直接提取匹配行内容
- **LLM 引擎**：在 prompt 中要求模型从 diff 中原样摘录问题代码行
- **CLI 展示**：风险表格新增 Code 列（截断显示），Fix Suggestions 区域展示完整代码 + 修复建议的对比
- **Web 展示**：风险表格新增 Code 列，Fix Suggestions 区域分 Current / Fix 两块代码展示

这一设计大幅降低了 reviewer 的上下文切换成本——从"知道哪行有问题"升级为"直接看到问题代码"。

---

## 未来扩展方向

### GitHub Bot 自动评论
将 LumiDiff 部署为 GitHub App，通过 Webhook 监听 PR 事件，自动触发审查并将结果以 Review Comment 的形式发回 PR。支持行级 inline comment，将 risks 精确定位到代码行。

技术方案：
- GitHub App + Webhook 监听 `pull_request.opened` / `pull_request.synchronize` 事件
- 使用 [Create a review comment API](https://docs.github.com/en/rest/pulls/comments#create-a-review-comment) 将 risks 定位到具体代码行
- 部署为常驻服务（Railway / Fly.io），OAuth 接入实现一键安装

### Web 端 Diff 预览
当前 Web 端只展示文件名和变更行数，计划引入 syntax-highlighted diff 视图（side-by-side / inline 两种模式），并将 risks 行高亮关联到代码上，提升可读性。

技术方案：
- 引入 `diff2html` 或自研 unified diff parser
- 支持 side-by-side 和 inline 两种视图切换
- risks 行自动高亮，点击 risk 跳转到对应代码位置

### SSE 流式输出
当前 Web 端同步等待 LLM 返回，大 PR 可能需要 30-60 秒。计划引入 Server-Sent Events 实现流式输出，用户可实时看到分析进度和中间结果。

技术方案：
- `llm_client.py` 改为 streaming 模式（`stream: true`）
- FastAPI 使用 `StreamingResponse` 逐步推送结果
- 前端使用 `EventSource` 接收并逐步渲染

### Breaking Change 检测
检测公开 API 的签名变更、删除、重命名，这对 reviewer 来说是高价值信号。计划引入轻量级 AST 解析（tree-sitter），分析函数签名和类定义的变更。

### MCP Server
将 `lumidiff.review_pr` / `lumidiff.review_staged` 暴露为 MCP 工具，供 Claude、Copilot 等 AI 助手直接调用。

### IDE 插件
VSCode Extension，消费 `--json` 输出，映射到 Diagnostics API 实现编辑器内红色波浪线标注。

### 多平台支持
`diff_source.py` 按 provider 策略模式扩展，支持 GitLab API、Gitee API。

### 仓库级分析
引入 tree-sitter 构建跨文件符号索引，理解变更的 blast radius（影响范围）。

---

## 项目结构

```
LumiDiff/
├── lumidiff/                # 核心包
│   ├── __init__.py          # 版本号
│   ├── __main__.py          # python -m lumidiff 入口
│   ├── cli.py               # 命令行参数 + 主流程编排
│   ├── diff_source.py       # diff 获取（本地 git / GitHub API）+ 文件过滤
│   ├── rule_engine.py       # 确定性规则扫描 + 自定义规则加载
│   ├── llm_client.py        # LLM API 调用 + Pydantic 校验
│   └── reporter.py          # rich 终端渲染 + JSON 输出
├── web/                     # Web 端
│   ├── app.py               # FastAPI 后端
│   └── templates/
│       └── index.html       # 前端页面（深色/浅色自适应）
├── pyproject.toml
├── requirements.txt
├── README.md
└── LICENSE
```

---

## License

MIT
