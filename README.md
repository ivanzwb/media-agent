# Media Agent · 自媒体运营自动助理

围绕你设定的主题（如 AI、物理 AI），自动从 RSS / 网页抓取前沿公司与机构的最新新闻和技术文章，去重归档、主题分类，并以自媒体格式（图文并茂、通俗易懂、吸引眼球、以爆款为目标）**保真改写**——只基于原文事实、不瞎编，改写后还会做一次事实校验，存疑处高亮提醒人工核对。配套本地 Web 界面用于浏览归档、审核与 Markdown 编辑草稿、配置来源、触发与定时运行。

> 半自动定位：抓取 + 归档 + 生成草稿全自动，**发布前由人工审核**。

## 核心特性

- **多来源抓取**：RSS 订阅 + 网页爬取（列表页自动发现链接 / 单页抓取），统一为标准文章结构。
- **去重归档**：URL 规范化 + 标题指纹去重，跨轮次持久化；原文以 Markdown（带 front-matter）落盘，按主题分目录。
- **主题分类**：关键词优先命中，模糊时用 LLM 零样本分类到既定主题集合。
- **保真改写**：原文为唯一事实来源，强约束不编造数据/引用/结论；产出候选标题 + 钩子 + 正文 + 来源标注；改写后做事实校验并标红存疑内容。
- **配图**：原文配图下载 + AI 生成封面（provider 可切换，离线用占位图）。
- **平台适配**：一键从主稿生成公众号 / 小红书 / 知乎风格的新草稿。
- **运行控制**：按时间窗（`max_age_days`）与每源条数（`max_per_source`）限制抓取量。
- **定时调度**：APScheduler cron 定时自动运行。
- **本地 Web 界面**：仪表盘、归档浏览、草稿 Markdown 编辑审核、来源配置、设置、立即运行。
- **可切换大模型**：业务代码不绑定厂商；内置 `mock`（离线跑通全链路）与 `openai`，易扩展 DeepSpeed/Claude/本地模型等。

## 安装

需要 Python 3.11+（已在 3.12 验证）。

```bash
python -m venv .venv
# Windows (git bash): source .venv/Scripts/activate
# Windows (PowerShell): .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

> 国内网络可用镜像加速：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

## 配置

### 来源与主题（`feeds.yaml`）

```yaml
topics:
  - name: AI
    keywords: [AI, LLM, AGI, foundation model, GPT, model]
  - name: 物理AI
    keywords: [robotics, embodied, world model, physical AI, manipulation]

sources:
  - name: OpenAI Blog
    type: rss
    url: https://openai.com/blog/rss.xml
    topics: [AI]
  - name: Google DeepMind Blog
    type: scrape
    mode: list                 # list（列表页发现链接）| single（直接抓单页）
    url: https://deepmind.google/discover/blog/
    include_pattern: /discover/blog/
    topics: [AI, 物理AI]
```

也可以在 Web 界面的「来源」页查看并新增来源（会写回 `feeds.yaml`）。除手动填写外，还支持**自动发现**：

- **从网址发现**：给一个网站/栏目 URL，自动解析其 RSS 订阅链接并加为 rss 源；找不到 RSS 则加为网页爬取源。
- **按关键词发现**：输入关键词，联网搜索（DuckDuckGo，无需 API key）相关站点并自动发现其来源。

发现到的来源去重后写回 `feeds.yaml`。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `MEDIA_AGENT_DATA_DIR` | 数据目录（db + 归档 + 草稿 + 图片） | `data` |
| `MEDIA_AGENT_LLM_PROVIDER` | `mock` \| `openai` | `mock` |
| `MEDIA_AGENT_LLM_API_KEY` | 大模型 API Key | 无 |
| `MEDIA_AGENT_LLM_MODEL` | 模型名（如 `gpt-4o-mini`） | provider 默认 |
| `MEDIA_AGENT_IMAGE_PROVIDER` | `mock` \| `openai` | `mock` |
| `MEDIA_AGENT_MAX_AGE_DAYS` | 只保留 N 天内的文章（空=不限） | 不限 |
| `MEDIA_AGENT_MAX_PER_SOURCE` | 每个来源最多抓取条数（空=不限） | 不限 |

> 不配置任何真实模型时默认走 `mock`，可在无 API Key 的情况下跑通整条流水线（改写内容为占位文本），便于先验证流程。

## 使用

### 命令行

```bash
# 初始化数据目录与数据库
python -m app.cli init

# 运行一轮流水线（抓取→去重→归档→分类→改写→草稿）
python -m app.cli run --feeds feeds.yaml

# 带封面图、并限制抓取范围
python -m app.cli run --feeds feeds.yaml --with-images --max-age-days 14 --max-per-source 20

# 启动本地 Web 界面（默认 http://127.0.0.1:8000）
python -m app.cli serve
```

接真实模型示例：

```bash
export MEDIA_AGENT_LLM_PROVIDER=openai
export MEDIA_AGENT_LLM_API_KEY=sk-...
export MEDIA_AGENT_IMAGE_PROVIDER=openai
python -m app.cli run --feeds feeds.yaml --with-images
```

### Web 界面

`python -m app.cli serve` 后访问 `http://127.0.0.1:8000`：

- **仪表盘**：归档/草稿/运行计数、最近运行、最新草稿、右上角「立即运行」。
- **归档**：按主题筛选浏览已抓取原文，链接到来源。
- **草稿**：列表 + Markdown 编辑器（实时预览）；候选标题、状态流转（drafted→reviewing→approved→published）、封面预览；**事实校验存疑会高亮警告**；一键生成公众号/小红书/知乎适配版本。
- **来源**：查看主题与来源；手动新增，或「从网址发现」/「按关键词发现」自动添加来源（写回 `feeds.yaml`）。
- **设置**：查看模型/图片 provider/运行控制；配置定时运行的 cron 表达式（如 `0 8 * * *`，每天 8 点）。

> 定时设置在 `serve` 启动时生效，修改后请重启服务。

## 产出物

- `data/archive/<主题>/<日期>-<slug>.md`：抓取的原文（含 front-matter 元数据）。
- `data/drafts/<主题>/<id>-<平台>-<slug>.md`：改写后的草稿，front-matter 含候选标题、来源、封面、平台、`flagged_claims`（存疑项）、状态。
- `data/images/`：下载的原图与生成的封面。
- `data/media.db`：SQLite 元数据库（articles / drafts / runs / sources / settings）。

## 项目结构

```
app/
├─ config.py            # 配置（env）
├─ db.py / store.py     # SQLite + 归档/草稿文件读写与查询
├─ models.py            # Article / Draft 数据模型
├─ feeds.py             # feeds.yaml 读写
├─ cli.py               # Typer CLI（init / run / serve）
├─ scheduler.py         # APScheduler 定时调度
├─ discovery.py         # 来源自动发现（RSS 探测 + 关键词搜索）
├─ sources/             # rss / scraper / extractor / dedup
├─ llm/                 # 大模型抽象层 + providers（mock / openai）
├─ images/              # 图片抽象层 + providers（mock / openai）
├─ pipeline/            # classifier / rewriter / images / adapter / orchestrator
└─ web/                 # FastAPI 服务 + 模板 + 静态资源
docs/superpowers/       # 设计文档与三个实现计划
tests/                  # pytest（83 个测试）
feeds.yaml  requirements.txt
```

## 测试

```bash
python -m pytest -v
```

## 内容真实性

改写严格基于原文事实，禁止编造数据、引用、结论或时间；改写后会对照原文做事实校验，把「原文未提及/可能失真」的说法写入草稿的 `flagged_claims` 并在编辑界面高亮，需人工核对通过后再发布。原文配图注明出处，AI 生成图建议标注「AI 生成」。

## 路线图（后续可扩展）

- 真实平台发布 API 对接（公众号、知乎等）。
- 选题热度/趋势推荐。
- 更多来源（X/Twitter 等社交平台）。
- 多模型对比改写、A/B 标题。
```
