# 自媒体运营自动助理 — 设计文档

- 日期：2026-06-23
- 状态：设计评审中
- 方案：方案 A（单体 FastAPI + SQLite + APScheduler）

## 1. 目标与范围

做一个本地运行的自媒体运营自动助理，围绕给定主题（如 AI、物理 AI 等），自动完成：

1. 从网络抓取最新新闻/文章，尤其是前沿公司、机构发布的最新动态与技术文章。
2. 自动分类、归档。
3. 以自媒体格式（图文并茂、通俗易懂、吸引眼球、以爆款为目标）重写，**内容真实、不瞎编**。
4. 通过本地 Web 界面进行配置、定时/手动运行、查看归档、审核与编辑草稿（Markdown）。

### 关键决策（已确认）

| 维度 | 决策 |
| --- | --- |
| 发布平台 | 多平台通用：先产出一份主稿，再适配各平台 |
| 自动化程度 | 半自动：抓取+归档+生成草稿，人工审核后再发 |
| 技术栈 | Python |
| 大模型 | 可切换 provider（OpenAI / Claude / DeepSeek / 本地 等），暂不锁定 |
| 数据来源 | RSS 订阅 + 网页爬取 |
| 配图 | 原图优先；缺图则 AI 生成；封面用 AI 生成 |
| 运行方式 | 本地 Web 界面，可在界面配置定时运行与手动运行 |
| 文章格式 | 全部 Markdown，编辑也基于 Markdown |

### 非目标（YAGNI）

- 不做多用户/权限系统（单机个人/小团队使用）。
- 不做平台自动发布 API 对接（先导出 Markdown，人工发布；后续可扩展）。
- 不做分布式/高并发抓取架构。
- 不引入重型 Agent 框架（保持确定性流水线，利于内容保真与调试）。

## 2. 技术栈

- Python 3.11+
- FastAPI（后端 API + 前端挂载）
- SQLite（元数据库）
- APScheduler（定时任务）
- feedparser（RSS 解析）
- httpx（HTTP 抓取）
- trafilatura / readability-lxml（网页正文与配图抽取）
- 前端：轻量页面（HTML + Alpine.js 或简单 React，以最小可用为主），集成 Markdown 编辑器（如 EasyMDE / Toast UI Editor）
- LLM SDK：按 provider 动态加载（openai、anthropic 等）

## 3. 整体架构与目录

```
media-agent/
├─ app/
│  ├─ main.py              # FastAPI 入口，挂载路由 + 前端
│  ├─ config.py            # 全局配置（模型 key、路径、阈值）
│  ├─ db.py                # SQLite 连接与初始化
│  ├─ models.py            # 数据模型
│  ├─ sources/             # 数据来源层（统一产出标准化文章对象）
│  │   ├─ base.py          #   统一 Source 接口 + 标准 Article 结构
│  │   ├─ rss.py           #   RSS 拉取
│  │   ├─ scraper.py       #   网页爬取（列表页发现链接 → 正文抽取）
│  │   ├─ extractor.py     #   正文/标题/配图抽取
│  │   └─ dedup.py         #   去重（URL/标题指纹）
│  ├─ pipeline/            # 流水线核心
│  │   ├─ classifier.py    #   主题分类/归档
│  │   ├─ rewriter.py      #   LLM 保真改写
│  │   ├─ images.py        #   原图提取 + AI 配图/封面
│  │   └─ orchestrator.py  #   串起一整轮
│  ├─ llm/                 # 大模型抽象层（可切换 provider）
│  │   ├─ base.py
│  │   └─ providers/       #   openai.py / claude.py / deepseek.py / ollama.py
│  ├─ scheduler.py         # APScheduler 定时任务
│  ├─ routes/              # API 路由（配置、归档、草稿、运行）
│  └─ web/                 # 前端静态页面
├─ data/
│  ├─ media.db             # SQLite
│  ├─ archive/             # 抓取的原始文章（md + 元数据）
│  ├─ drafts/              # 改写后的草稿（md）
│  └─ images/              # 下载的原图 / AI 生成图
├─ feeds.yaml              # 主题与来源配置（也可在界面改）
├─ requirements.txt
└─ README.md
```

### 核心数据流

```
feeds.yaml / 界面配置
   → sources（rss / scraper）拉取
   → extractor 抽取标准 Article
   → dedup 去重
   → 落 archive/*.md + 入库
   → classifier 打主题标签
   → rewriter 生成爆款草稿（drafts/*.md）
   → images 配图（原图优先 + AI 封面/补图）
   → 界面审核 / 编辑 md
   → 标记“已发布” / 导出
```

## 4. 数据来源层

### 4.1 统一接口

所有来源实现统一 `Source` 接口，输出标准 `Article` 结构，下游不区分来源：

```python
@dataclass
class Article:
    title: str
    content_md: str          # 正文（Markdown）
    url: str
    source_name: str         # 来源名（如 "OpenAI Blog"）
    source_type: str         # "rss" | "scrape"
    published_at: datetime | None
    images: list[str]        # 原文配图 URL 列表
    raw_summary: str | None  # 原文摘要（若有）
    fetched_at: datetime
```

### 4.2 RSS（`rss.py`）

- 用 feedparser 解析订阅源（如 OpenAI / DeepMind / Anthropic 官方博客、arXiv、机器之心等）。
- 提取标题、链接、摘要、发布时间；正文若 RSS 不全，回退到 extractor 抓全文。

### 4.3 网页爬取（`scraper.py`）

- **两种模式**：
  - `list`：给栏目/博客首页 URL，自动发现文章链接，再逐篇抓正文。
  - `single`：直接给文章 URL 抓正文。
- **正文抽取**（`extractor.py`）：用 trafilatura 抽取标题、正文、主图、发布时间，自动去广告/导航；失败回退 readability。
- **礼貌爬取**：遵守 `robots.txt`、自定义 User-Agent、请求限速、失败重试与超时控制。

### 4.4 去重（`dedup.py`）

- 一级：URL 规范化后精确去重。
- 二级：标题 + 正文指纹（如 SimHash / 归一化标题哈希）做近似去重，避免同一新闻多源重复。
- 已抓取记录入库，跨轮次持久去重。

### 4.5 来源配置（`feeds.yaml`）

```yaml
topics:
  - name: AI
    keywords: [AI, LLM, AGI, foundation model]
  - name: 物理AI
    keywords: [robotics, embodied AI, world model, physical AI]

sources:
  - name: OpenAI Blog
    type: rss
    url: https://openai.com/blog/rss.xml
    topics: [AI]
  - name: DeepMind Blog
    type: scrape
    mode: list
    url: https://deepmind.google/discover/blog/
    topics: [AI, 物理AI]
    selector: null        # 可选，缺省走自动抽取
```

## 5. 流水线

### 5.1 编排（`orchestrator.py`）

一轮运行（run）= 一次完整流水线，可手动触发或定时触发。每一步产出可观测的状态，便于界面展示进度与失败重试。

```
run():
  articles = collect_from_sources()      # rss + scrape
  articles = dedup(articles)
  archive(articles)                      # 落 md + 入库，status=archived
  for a in articles:
      a.topic = classify(a)              # status=classified
  for a in selected(articles):           # 可按主题/时间/数量筛选
      draft = rewrite(a)                 # status=drafted
      draft = attach_images(draft, a)
      save_draft(draft)
```

### 5.2 分类归档（`classifier.py`）

- 先用关键词规则快速命中主题；命中模糊时用 LLM 做主题判定（零样本分类到既定主题集合）。
- 归档：原文 Markdown 落 `data/archive/{topic}/{date}-{slug}.md`，元数据入库。

### 5.3 保真改写（`rewriter.py`）— 核心

目标：**爆款化但不瞎编**。策略：

1. **基于原文事实改写**：将原文正文作为唯一事实来源传给 LLM，提示词强约束“只能基于提供的原文，不得编造数据、引用、结论；不确定就不写”。
2. **结构化产出**：让模型输出
   - 多个候选标题（吸睛但不做标题党虚假承诺）
   - 开头钩子
   - 正文（通俗易懂、分点、配 emoji/小标题，适合自媒体阅读）
   - 关键信息要点
   - **来源标注**（原文链接、机构名）保证可追溯
3. **事实校验环节（faithfulness check）**：改写后再过一遍校验提示，让模型对照原文标记“原文未提及/可能失真”的句子，自动修正或标红提示人工确认。
4. **平台适配**：先产出一份“主稿”（Markdown），再提供按平台（公众号长文 / 小红书短图文 / 知乎深度）的轻量改写适配按钮。
5. **草稿格式**：Markdown，带 front-matter 元数据（标题候选、主题、来源、配图占位、平台）。

草稿示例结构：

```markdown
---
title_candidates:
  - "标题A"
  - "标题B"
topic: AI
source_url: https://...
source_name: OpenAI Blog
platform: master
cover_image: images/xxx-cover.png
status: drafted
---

![封面](images/xxx-cover.png)

> 开头钩子……

## 小标题
正文……

---
**信息来源**：[OpenAI Blog](https://...)
```

### 5.4 配图（`images.py`）

- **原图优先**：从原文 `images` 下载可用配图到 `data/images/`，正文按位置插入（注明出处）。
- **AI 兜底/补图**：原文无图或图不够时，按正文要点生成示意图（接 AI 文生图 provider，纳入 LLM 抽象层的图像能力或单独图像 provider）。
- **封面**：统一用 AI 生成吸睛封面图。
- **版权**：原图注明出处；AI 图标注“AI 生成”。

## 6. 大模型抽象层（`llm/`）

- `base.py` 定义统一接口：`chat(messages, **opts) -> str`、`generate_image(prompt) -> path`（可选）。
- `providers/` 下各实现 OpenAI / Claude / DeepSeek / Ollama 等。
- provider 与 key 在 `config.py` / 界面配置中选择，运行时动态加载。
- 改写、分类、校验、图像生成都通过该层调用，**业务代码不绑定具体厂商**。

## 7. 存储与数据模型

正文存文件（Markdown），数据库存元数据与状态。

### SQLite 表

- `sources`：来源配置（name, type, url, mode, topics, enabled）。
- `articles`：抓取记录（id, title, url, source_id, topic, published_at, fetched_at, archive_path, fingerprint, status）。
- `drafts`：草稿（id, article_id, platform, draft_path, cover_image, status[drafted/reviewing/approved/published], updated_at）。
- `runs`：运行记录（id, started_at, finished_at, stats_json, status）。
- `settings`：全局配置（LLM provider/key、定时计划、阈值等）。

文章状态流转：`archived → classified → drafted → reviewing → approved → published`。

## 8. Web 界面

最小可用，覆盖以下页面：

1. **仪表盘**：最近运行、抓取/归档/草稿数量、快速“手动运行”按钮。
2. **来源与主题配置**：增删改 RSS / 爬取源、主题与关键词（等价于编辑 `feeds.yaml`）。
3. **定时设置**：配置定时计划（cron 表达式或简单频率），开关定时任务。
4. **归档浏览**：按主题/时间/来源筛选查看已归档原文。
5. **草稿审核与编辑**：Markdown 编辑器，左改右预览；显示来源、保真校验提示（标红存疑句）；候选标题切换；平台适配按钮；状态流转（通过/导出）。
6. **设置**：LLM provider 与 key、图像生成开关、爬取限速等。

API 路由（`routes/`）按资源划分：`/api/sources`、`/api/runs`、`/api/articles`、`/api/drafts`、`/api/settings`。

## 9. 定时与运行

- APScheduler 在服务内运行，从 `settings` 读取计划。
- 支持：手动触发一轮、定时触发一轮、单独触发某来源。
- 运行写 `runs` 表，界面可看进度与失败原因，支持失败步骤重试。

## 10. 错误处理与稳健性

- 抓取：超时/重试/限速；单来源失败不影响整轮，记录到 run 日志。
- 抽取失败：回退策略（trafilatura → readability → 仅用 RSS 摘要并标注“正文抓取失败”）。
- LLM 调用：超时、重试、token 超限降级（截断/分段改写）。
- 保真：校验环节存疑内容标红，不静默发布；半自动模式下必须人工通过才能标记 approved。
- 数据：SQLite 单文件，定期可备份；md 文件与 db 路径解耦。

## 11. 测试策略

- 来源层：用本地保存的 RSS/HTML 样本做解析与抽取的单元测试（不依赖网络）。
- 去重：构造重复/近似样本验证。
- 分类：关键词规则单测；LLM 分类用 mock。
- 改写/校验：LLM 调用 mock，校验提示词与产出结构、front-matter 解析。
- 流水线：用假 Source + 假 LLM provider 跑端到端 orchestrator。
- API：FastAPI TestClient 覆盖主要路由。

## 12. 里程碑（建议实现顺序）

1. 项目骨架 + 配置 + SQLite + 数据模型。
2. LLM 抽象层（先接 1 个 provider + mock）。
3. 来源层：RSS + 网页爬取 + 抽取 + 去重 + 归档。
4. 分类归档。
5. 保真改写 + 校验 + Markdown 草稿产出。
6. 配图（原图下载 + AI 封面）。
7. Web 界面（归档浏览 + 草稿编辑审核优先）。
8. 定时调度 + 运行记录。
9. 平台适配改写。
10. 测试与文档完善。

## 13. 开放问题 / 后续可扩展

- 平台自动发布 API 对接（公众号、知乎等）。
- 选题智能推荐（基于热度/趋势）。
- 多模型对比改写、A/B 标题。
- 抓取热门社交平台（X 等）作为补充来源。
