# LumiDiff Web UI 实现计划

## Context
为 LumiDiff CLI 工具添加 Web 界面，评委无需安装即可直接体验。后端复用现有核心模块，前端单页面展示报告。

---

## 架构

```
浏览器 → FastAPI 后端 → lumidiff 核心逻辑 → LLM API
              ↓
        HTML 渲染报告（Jinja2 模板）
```

---

## 文件结构

```
web/
├── worker.py           # Cloudflare Worker 入口（Python）
├── templates/
│   └── index.html      # 前端页面（单文件，内嵌 CSS/JS）
├── wrangler.toml       # Cloudflare Workers 配置
└── requirements.txt    # web 额外依赖（本地开发用）
```

---

## Step 1: requirements.txt + 依赖

```
fastapi
uvicorn
jinja2
```

安装：`pip install -r web/requirements.txt`

---

## Step 2: FastAPI 后端 (`app.py`)

### 路由设计

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 首页，展示输入框 |
| `/analyze` | POST | 接收 PR/Commit URL，返回分析结果页面 |
| `/api/analyze` | GET | JSON API，供前端 fetch 调用 |

### 核心逻辑

```python
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# 复用现有核心模块
from lumidiff.diff_source import get_pr_diff, get_commit_diff
from lumidiff.rule_engine import scan_all
from lumidiff.llm_client import analyze

app = FastAPI()
templates = Jinja2Templates(directory="web/templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "result": None})

@app.post("/analyze", response_class=HTMLResponse)
async def analyze_form(request: Request, url: str = Form(...)):
    # 判断是 PR 还是 Commit
    if "/pull/" in url:
        diff = get_pr_diff(url)
    else:
        diff = get_commit_diff(url)
    
    risks = scan_all(diff.files)
    llm_result = analyze(diff.raw_diff, is_local=False)
    
    # 合并 LLM suggestions
    if llm_result and llm_result.suggestions:
        risks.extend(llm_result.suggestions)
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": {
            "diff": diff,
            "risks": risks,
            "llm": llm_result,
        }
    })

@app.get("/api/analyze")
async def api_analyze(url: str):
    # JSON API，和 CLI --json 输出格式一致
    ...
```

### 关键点

- API Key 从环境变量读取，部署时配置在服务器
- 大 PR 可能需要 30-60s，前端显示 loading
- 超时处理：设置 120s 超时
- 错误处理：URL 格式错误、API 调用失败等

---

## Step 3: 前端页面 (`index.html`)

### 布局设计

```
┌─────────────────────────────────────────────────────────────┐
│  LumiDiff — AI 代码审查                                       │
│  ┌─────────────────────────────────────────┐  ┌──────────┐  │
│  │ 输入 GitHub PR 或 Commit URL            │  │  分析     │  │
│  └─────────────────────────────────────────┘  └──────────┘  │
├─────────────────────────────────────────────────────────────┤
│  [Loading 动画]                                              │
├─────────────────────────────────────────────────────────────┤
│  ┌─ LumiDiff Report ─────────────────────────────────────┐  │
│  │ Files: 5  [+143 -35]   Risks: 4   LLM: 14.6s         │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ Files Changed ──────────────────────────────────────┐   │
│  │ File              │  +  │  -  │                       │   │
│  │ src/auth.py       │ +87 │ -12 │ 🔴                    │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  [Summary]                                                  │
│  本次变更主要为...                                            │
│                                                             │
│  [Risks]  Rules: 1 | LLM: 3                                │
│  ┌──────┬──────────────┬────────────────────┬────────────┐  │
│  │ Sev  │ Location     │ Description        │ Confidence │  │
│  │ HIGH │ auth.py:42   │ eval/exec 风险     │ N/A (rule) │  │
│  └──────┴──────────────┴────────────────────┴────────────┘  │
│                                                             │
│  [Fix Suggestions]                                          │
│  ...                                                        │
└─────────────────────────────────────────────────────────────┘
```

### 技术选型

- **纯 HTML + CSS + JS**，不引入前端框架
- CSS 参考 CLI 的 rich 输出风格（深色背景、彩色文字）
- JS fetch 调用 `/api/analyze` 获取数据，DOM 操作渲染
- Loading 状态：提交后显示 spinner，结果返回后渲染

### 响应式设计

- 桌面端：表格横向展示
- 移动端：表格纵向堆叠（卡片式）

---

## Step 4: 部署到 Cloudflare Workers

- 整个应用部署为 Worker
- Cloudflare Workers 已支持 Python runtime
- API Key 存储在 Workers 环境变量（Secrets）
- 前端 HTML 直接内嵌在 Worker 响应中（无需单独部署前端）

### Token 限额

- **单日上限 500 万 token**，防止被薅羊毛
- 使用 Cloudflare KV 存储每日计数（key: `usage:YYYY-MM-DD`，value: 累计 token 数）
- 每次 LLM 调用后累加 token 用量
- 超限时返回 429 错误，提示"今日额度已用完"
- 每日自动重置（KV 的 TTL 设为 24h）

### 部署步骤

1. 安装 Wrangler CLI：`npm install -g wrangler`
2. `wrangler login` 登录 Cloudflare
3. 配置 `wrangler.toml`
4. 设置 Secrets：`wrangler secret put MIMO_API_KEY`
5. 创建 KV namespace：`wrangler kv namespace create USAGE_KV`
6. 部署：`wrangler deploy`

---

## Step 5: 验证

1. 本地启动：`uvicorn web.app:app --reload`
2. 浏览器打开 `http://localhost:8000`
3. 输入 PR URL，点击分析
4. 验证：Loading → 报告展示 → 样式正确
5. 验证 JSON API：`curl http://localhost:8000/api/analyze?url=xxx`

---

## 实施顺序

1. `web/worker.py` — Cloudflare Worker 入口，复用核心模块
2. `web/templates/index.html` — 前端页面
3. `web/wrangler.toml` — Workers 配置 + KV 绑定
4. Token 限额逻辑（KV 读写）
5. 本地测试（`wrangler dev`）
6. 部署：`wrangler deploy`

---

## 优先级

| 标签 | 内容 |
|------|------|
| 🔴 MUST | 输入 URL → 显示报告 |
| 🔴 MUST | 深色主题 + 彩色 severity |
| 🟡 SHOULD | Loading 动画 |
| 🟡 SHOULD | JSON API 端点 |
| ⚪ NICE | 响应式设计（移动端适配） |
| ⚪ NICE | 文件统计表格 |
| ❌ CUT | 用户登录 / 历史记录 |
