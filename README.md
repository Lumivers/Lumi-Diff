# LumiDiff — AI 代码审查 CLI 工具

> 输入 GitHub PR 链接或本地变更，输出结构化审查报告：变更摘要 / 风险识别 / Review 建议。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

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

┌──────┬──────────────────────┬────────────────────────────────────────────┬────────────────┐
│ Sev  │ Location             │ Description                                │ Confidence     │
├──────┼──────────────────────┼────────────────────────────────────────────┼────────────────┤
│ HIGH │ src/auth.py:42       │ 使用了 eval/exec，存在代码注入风险           │ N/A (rule)     │
│ MED  │ src/config.py:15     │ 配置项缺少默认值，未设置时可能抛异常          │ 0.85           │
│ LOW  │ src/auth.py:88       │ TODO 标记残留                               │ 0.7 (uncertain)│
└──────┴──────────────────────┴────────────────────────────────────────────┴────────────────┘

Suggested commit message:
  feat(auth): add OAuth2 third-party login support

复制执行：
  git commit -m "feat(auth): add OAuth2 third-party login support"
```

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/your-username/lumidiff.git
cd lumidiff
pip install -e .
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

- **规则引擎**（确定性）：3 条零误报规则（eval/exec、shell=True、except:pass），毫秒级完成，作为首层快速扫描
- **LLM 引擎**（概率性）：五维度综合分析（安全审计、代码规范、逻辑健壮性、变更影响、测试覆盖），作为二道防线查漏补缺

两轨结果合并后去重，按严重度排序输出。

### 模型选择

| 模型 | 选择理由 |
|------|----------|
| **MiMo v2.5 Pro**（默认） | 中文理解能力强，OpenAI 兼容协议，百万级上下文窗口 |
| **DeepSeek V4 Pro** | 国产高性能模型，性价比高，JSON 输出稳定 |

选择标准：
1. **中文能力** — 代码审查报告面向中文开发者，需要流畅的中文表达
2. **上下文窗口** — 大 PR 的 diff 可能达到数万 token，需要足够的上下文
3. **JSON 输出稳定性** — 结构化输出是核心需求，模型必须稳定返回合法 JSON
4. **API 兼容性** — 统一使用 OpenAI 兼容协议，方便切换模型

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
| 规则引擎 | 仅 3 条零误报规则 | 宁可漏报，不可误报 |
| LLM Prompt | 五维度分析 + 约束指令 | 明确要求忽略测试 mock、changeset 文件 |
| 置信度过滤 | confidence ≤ 0.6 默认折叠 | 低置信度建议不干扰判断 |
| 输出标记 | `(uncertain)` 标记 | 0.6-0.8 区间的建议标注不确定 |

---

## 未来扩展方向

### MCP Server
将 `lumidiff.review_pr` / `lumidiff.review_staged` 暴露为 MCP 工具，供 Claude、Copilot 等 AI 助手直接调用。

### IDE 插件
VSCode Extension，消费 `--json` 输出，映射到 Diagnostics API 实现编辑器内红色波浪线标注。

### Web 端
Flask/FastAPI 后端 + 前端页面，评委直接打开浏览器输入 PR URL 即可体验，无需安装。

### 多平台支持
`diff_source.py` 按 provider 策略模式扩展，支持 GitLab API、Gitee API。

### 仓库级分析
引入 tree-sitter 构建跨文件符号索引，理解变更的 blast radius（影响范围）。

---

## 项目结构

```
lumidiff/
├── __init__.py          # 版本号
├── __main__.py          # python -m lumidiff 入口
├── cli.py               # 命令行参数 + 主流程编排
├── diff_source.py       # diff 获取（本地 git / GitHub API）+ 文件过滤
├── rule_engine.py       # 确定性规则扫描 + 自定义规则加载
├── llm_client.py        # LLM API 调用 + Pydantic 校验
└── reporter.py          # rich 终端渲染 + JSON 输出
```

---

## License

MIT
