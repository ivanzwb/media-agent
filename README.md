# Media Agent · 自媒体运营自动助理

围绕你设定的主题（如 AI、物理 AI），自动从 RSS / 网页抓取前沿公司与机构的最新新闻和技术文章，去重归档、主题分类，并以自媒体格式（图文并茂、通俗易懂、吸引眼球、以爆款为目标）**保真改写**——只基于原文事实、不瞎编，改写后还会做一次事实校验，存疑处高亮提醒人工核对。配套本地 Web 界面用于浏览归档、审核与 Markdown 编辑草稿、配置来源、触发与定时运行。

> 半自动定位：抓取 + 归档 + 生成草稿全自动，**发布前由人工审核**。

## 核心特性

### 采集与归档
- **多来源抓取**：RSS 订阅 + 网页爬取（列表页自动发现链接 / 单页抓取）。列表页支持**分页跟随**（`max_pages`，跟随 `?page=N` / `/page/N`），href 提取兼容单/双/无引号；对 SPA/SSR 站点可选 **JS 渲染**（`render_js`，需 Playwright，未装则回退静态）。
- **去重归档**：URL 规范化 + 标题指纹去重，跨轮次持久化；原文以 Markdown（带 front-matter）落盘，按主题分目录。
- **选题热度打分**：`compute_hotness()` 以 LLM 给主题打基线分 + 联网新鲜度加成，辅助排序选题。
- **智能推荐**（Web）：输入主题 →（多选）子主题 → 关键词，一键加为主题；并可「按主题发现前沿公司/机构来源」自动加入。

### 媒体
- **媒体提取**：图片支持 `src`/`data-src`/`srcset`/`<picture>`/CSS background-image，视频支持 `<video>/<source>`、平台 iframe、**JS 动态填充**（扫描 `<script>`/JSON 中的媒体 URL），并支持 **HLS（`.m3u8`，边播拉 `.ts`）**。
- **媒体本地化**：抓取归档时把文章里的图片（httpx）与视频（yt-dlp，直链/HLS 均可）**下载到本地** `data/media/<hash>/`，front-matter 与正文引用改为本地路径；后续转写/合成直接用本地文件，避免防盗链/失效。归档页每篇可单独「重新抓取 / 本地化媒体」。
- **配图**：原文配图下载 + AI 生成封面（provider 可切换，离线用占位图）。

### 改写与合规
- **主题分类**：关键词优先命中，模糊时 LLM 零样本分类。
- **保真改写**：原文为唯一事实来源，禁编造数据/引用/结论；产出候选标题 + 钩子 + 正文 + 来源标注；改写后事实校验、存疑处标红。
- **敏感词过滤**：文章/视频脚本生成后自动移除平台敏感/违禁词（广告法绝对化用语、引流、医疗/金融夸大等），内置词库**按等级**（`off`/`basic`/`standard`/`strict`），可自定义追加；静默处理、记录并在草稿页提示。
- **平台同步**：一键从主稿生成公众号 / 小红书 / 头条 / 知乎风格的新草稿；平台架构可插拔，新增平台只需加一个文件。文章编辑和视频讲解两个 Tab 均支持同步到平台（复制内容到剪贴板 + 打开平台编辑器）。

### 讲解视频
- **脚本 + 配音**：从草稿自动生成口播分镜脚本（LLM）→ 逐段 TTS 配音；分镜可在草稿页**编辑**（旁白/画面/重选背景素材、增删/排序），并**只对改动的分镜重配**。
- **合成**：ffmpeg 合成「图片轮播 / 原视频片段 + 中文字幕 + 配音」的 mp4；中文字幕用 PIL 生成画面帧/叠层（规避 ffmpeg 中文字体问题）；画面适配可选 `fit`（完整+黑边）/`crop`/`blur`。同一视频被连续分镜引用时**续播**、放完**冻结最后一帧**。
- **TTS**：默认内置 `kitten`（中文走 edge-tts，免 key）；可选 **本地声音复刻 `cosyvoice`**（CosyVoice2-0.5B，上传干声本地克隆音色，最佳中文 TTS 自然度，需 Python < 3.13 + NVIDIA GPU）；也支持 Fish Audio 云端克隆、OpenAI 兼容 / 外部 Kitten 服务。
- 合成需本地 **ffmpeg**；原视频/HLS 下载需 **yt-dlp**。

### 运行与平台
- **运行控制**：按时间窗（`max_age_days`）与每源条数（`max_per_source`）限制抓取量。
- **并发加速**：多来源抓取、图片/视频下载用线程池并发，数量可配（`MEDIA_AGENT_DOWNLOAD_WORKERS`，默认 CPU 核数）。
- **定时调度**：APScheduler cron 定时自动运行；**实时运行进度/日志面板**。
- **本地 Web 界面**：仪表盘、归档浏览（按日期分组、查看原文）、草稿编辑（文章/讲解视频双 Tab）、来源配置、设置、立即运行。
- **可切换大模型**：业务代码不绑定厂商；内置 `mock`（离线跑通全链路）与 `openai`，易扩展。
- **配置导出/导入**：把来源 + 配置导出为 JSON 分享/备份，导入可恢复；**导出不含 API Key**。

> 版权提示：把第三方视频片段剪入自己的成片并发布可能涉及版权，请自行注明出处或取得授权；AI 生成图建议标注「AI 生成」。

## 安装

需要 Python 3.11+（已在 3.12 验证）。

**可选外部依赖（按需安装）：**
- **ffmpeg**（讲解视频合成、HLS 合并）：装好并加入 PATH（<https://ffmpeg.org>）。中文字幕字体可用 `MEDIA_AGENT_FONT` 指定（默认自动探测微软雅黑/黑体/Noto CJK）。
- **yt-dlp**（平台视频 / HLS 下载）：已在 `requirements.txt`；CLI 不在 PATH 时自动回退 `python -m yt_dlp`。
- **Playwright**（SPA/SSR 页面 JS 渲染抓取）：`pip install playwright && playwright install chromium`。
- **TTS / 声音复刻**（按需选装）：

| 方案 | 类型 | Python | 显存 | 中文 | 声音复刻 | 安装 |
|---|---|---|---|---|---|---|
| **CosyVoice2-0.5B** ⬅️内置 | 本地 | <3.13 | 4GB+ | ⭐⭐⭐ 最佳 | ✅ zero-shot(3-10s) | `pip install cosyvoice` |
| **GPT-SoVITS** | 本地 | 3.9-3.11 | 4GB+ | ⭐⭐⭐ 极好 | ✅ 1分钟样本 | 单独项目(RVC-Boss) |
| **Fish Speech** | 本地 | >=3.10 | 4GB+ | ⭐⭐ 好 | ✅ 小样本 | `pip install fish-speech` |
| **Fish Audio SDK** | ☁️ 云端 | **任意** | **无需** | ⭐⭐⭐ 好 | ✅ 即时复刻 | `pip install fish-audio-sdk` |
| **edge-tts (kitten)** | 云端 | **任意** | **无需** | ⭐⭐ 中上 | ❌ 无 | 内置 |

```bash
python -m venv .venv
# Windows (git bash): source .venv/Scripts/activate
# Windows (PowerShell): .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

> 国内网络可用镜像加速：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

### 打包版（免 Python 环境）

不想装 Python？从 [Releases](https://github.com/ivanzwb/media-agent/releases) 下载对应平台的 zip，解压后直接运行：

```
Windows:  media-agent\media-agent.exe serve
macOS:    media-agent/media-agent serve
```

打包版已内置所有 Python 依赖（含 fish-audio-sdk、playwright 库）。解压后运行 `setup-optional.bat`（Win）或 `setup-optional.sh`（Mac）可自动检测并引导安装可选组件：

| 组件 | 打包版内置？ | 说明 |
|---|---|---|
| fish-audio-sdk | ✅ 已内置 | 云端声音克隆 |
| playwright 库 | ✅ 已内置 | 需额外下载 Chromium（脚本一键完成） |
| ffmpeg | ❌ 需单独装 | 视频合成必需，[下载](https://ffmpeg.org)后加入 PATH |
| CosyVoice | ✅ 嵌入式 Python 侧边部署 | 脚本自动部署嵌入式 Python + 安装 PyTorch + 下载模型，一键完成 |

### 本地构建

```bash
# Windows：双击 build-win.bat → dist\media-agent-win64.zip
# macOS：   bash build-mac.sh  → dist/media-agent/
```

CI 自动构建：推送带 `v*` 的 tag 即触发 GitHub Actions，同时输出 Win + Mac 两版到 Release。

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
| `MEDIA_AGENT_TTS_PROVIDER` | `kitten`（内置，中文走 edge-tts）\| `cosyvoice`（本地声音复刻/克隆，CosyVoice2-0.5B，需 Python\<3.13 + GPU）\| `fishaudio`（云端声音克隆，纯 HTTP 任意 Python）\| `mock` \| `kitten_http` \| `openai`（兼容） | `kitten` |
| `MEDIA_AGENT_TTS_API_BASE` | 外部 TTS 服务地址（仅 `kitten_http`/`openai` 需要） | 无 |
| `MEDIA_AGENT_TTS_API_KEY` | TTS API Key（内置/本地服务可留空） | 无 |
| `MEDIA_AGENT_TTS_MODEL` | TTS 模型名（openai 兼容用） | `tts-1` |
| `MEDIA_AGENT_TTS_VOICE` | 音色/风格：`female`/`child`/`male`… 或具体音色名 | `assistant` |
| `MEDIA_AGENT_MAX_AGE_DAYS` | 只保留 N 天内的文章（空=不限） | 不限 |
| `MEDIA_AGENT_MAX_PER_SOURCE` | 每个来源最多抓取条数（空=不限） | 不限 |
| `MEDIA_AGENT_DOWNLOAD_WORKERS` | 来源抓取 / 图片视频下载的并发线程数（空=CPU 核数） | CPU 核数 |
| `MEDIA_AGENT_VIDEO_FIT_MODE` | 讲解视频画面适配：`fit`（完整+黑边）\| `crop`（铺满裁剪）\| `blur`（完整+模糊背景） | `fit` |
| `MEDIA_AGENT_SENSITIVE_LEVEL` | 敏感词过滤等级：`off` \| `basic` \| `standard` \| `strict` | `standard` |
| `MEDIA_AGENT_SENSITIVE_WORDS` | 自定义敏感词（逗号/换行分隔，追加到内置词库） | 无 |

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

- **仪表盘**：归档/草稿/运行计数、最近运行、最新草稿、右上角「立即运行」（带实时进度/日志面板）。
- **归档**：按日期分组浏览、主题筛选；每篇可「查看原文」（本地媒体内联渲染）、「重新抓取」、「本地化媒体」、「转写/重写」；抓取/本地化日志在右下角浮动面板（不遮挡列表）。
- **草稿**：列表 + 双 Tab 编辑器——
  - 「文章内容」：候选标题、正文 Markdown（分屏实时预览）、状态流转（drafted→reviewing→approved→published）、封面预览、**同步到平台**（公众号/小红书/头条/知乎）；**事实校验存疑 / 敏感词过滤**会高亮提示。
  - 「讲解视频」：生成分镜脚本+配音、分镜编辑（旁白/画面/重选素材、增删/排序、局部重配）、合成 mp4、播放/下载、**同步到平台**。
- **来源**：查看/编辑主题与来源；「智能推荐主题」（主题→子主题→关键词）、「按主题发现前沿来源」、「从网址/关键词发现」自动添加（写回 `feeds.yaml`）。
- **设置**：LLM / 图片 / **TTS（含声音库录入）** provider、并发数、视频画面适配、**敏感词等级与自定义词**、运行控制、定时 cron、配置导出/导入。

> 定时设置在 `serve` 启动时生效，修改后请重启服务。

## 产出物

- `data/archive/<主题>/<日期>-<slug>.md`：抓取的原文（含 front-matter：标题/来源/主题/images/videos 等）。
- `data/drafts/<主题>/<id>-<平台>-<slug>.md`：改写后的草稿，front-matter 含候选标题、来源、封面、平台、`flagged_claims`（存疑项）、`sensitive_hits`（被过滤的敏感词）、状态。
- `data/media/<hash>/`：本地化的原文图片/视频。
- `data/videos/draft-<id>/`：讲解视频产物（`script.json` 分镜脚本、逐段配音、`video.mp4`）。
- `data/voices/`：声音复刻样本与注册表（`voices.json`）。
- `data/images/`：生成的封面。
- `data/media.db`：SQLite 元数据库（articles / drafts / runs / sources / settings）。

## 项目结构

```
app/
├─ config.py            # 配置（env + DB 覆盖）
├─ db.py / store.py     # SQLite + 归档/草稿文件读写与查询
├─ models.py            # Article / Draft 数据模型
├─ feeds.py             # feeds.yaml 读写（含 max_pages / render_js）
├─ cli.py               # Typer CLI（init / run / serve）
├─ scheduler.py         # APScheduler 定时调度
├─ discovery.py         # 来源自动发现（RSS 探测 + 关键词搜索）
├─ sources/             # rss / scraper（分页/JS 渲染）/ extractor（媒体提取）/ dedup
├─ media/               # 下载策略链（downloader / strategies：httpx 直链 / URL 变换 / yt-dlp）
├─ llm/                 # 大模型抽象层 + providers（mock / openai）
├─ images/              # 图片抽象层 + providers（mock / openai）
├─ tts/                 # TTS 抽象层 + providers（kitten / cosyvoice / fishaudio / openai…）+ 声音库
├─ video/              # builder：PIL 字幕帧 + ffmpeg 合成
├─ platforms/           # 平台同步（wechat / xiaohongshu / toutiao / zhihu）— 可插拔架构
├─ pipeline/            # classifier / rewriter / sanitizer / localize / recommender(+热度)
│                       #   / script / narration / images / adapter / video / orchestrator
└─ web/                 # FastAPI 服务 + 模板 + 静态资源
tests/                  # pytest（160 个测试）
feeds.yaml  requirements.txt
build-win.bat build-mac.sh  # 本地打包脚本
```

### `tools/` — 许可与加固

| 文件 | 用途 |
|---|---|
| `tools/licctl.py` | Vendor 端离线许可工具：生成密钥对、签发激活码 |
| `tools/build_hardened.py` | 许可核心加固（Cython 编译 / PyArmor 混淆），可选 |
| `tools/license_private_key.b64` | 私钥（**.gitignored**，切不可提交） |

```bash
# 生成密钥对（仅一次）
python -m tools.licctl keygen
# → tools/license_private_key.b64（密不外泄）
# → app/licensing/public_key.b64（提交到仓库）

# 签发许可（按机器绑定 / 按天数 / 永久浮动）
python -m tools.licctl issue --key MA-PRO-0001 --days 365
python -m tools.licctl issue --key MA-PRO-0002 --machine 1A2B-3C4D-5E6F-7A8B
```

## 测试

```bash
python -m pytest -v
```

## 内容真实性

改写严格基于原文事实，禁止编造数据、引用、结论或时间；改写后会对照原文做事实校验，把「原文未提及/可能失真」的说法写入草稿的 `flagged_claims` 并在编辑界面高亮，需人工核对通过后再发布。原文配图注明出处，AI 生成图建议标注「AI 生成」。

## 路线图（后续可扩展）

- 平台 API 直发（公众号素材管理 API、头条号开放平台等）。
- 多平台一键同步发布。
- 更多来源（X/Twitter 等社交平台）。
- 多模型对比改写、A/B 标题。
- 归档双格式（原始 HTML + Markdown）以便提取逻辑升级后回溯重提取（见 issue #9）。
- 背景音乐 / 转场 / 数字人口播 / 竖屏 9:16 适配。
```
