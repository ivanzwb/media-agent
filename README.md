# Media Agent · 自媒体运营自动助理

Media Agent 是一套本地运行的自媒体内容生产系统，提供三条创作工作流：

1. **抓取、转写创作**：持续从 RSS / 网页抓取新内容，去重归档、主题分类并批量保真转写。
2. **搜索创作（Pro）**：围绕临时选题，从多个搜索引擎查找和筛选资料，综合生成带引用的草稿。
3. **系列创作（Pro）**：围绕一个知识主题，先摸清该领域公开资料的样子，再拆成有递进关系的章节，逐章检索资料写成一组草稿。

三条工作流都会进入统一的事实校验、敏感词处理、Markdown 编辑、媒体处理、内容归档和平台发布流程。系统只根据来源材料组织内容，存疑事实会在草稿中提示人工核对。

> 半自动定位：资料采集、整理与草稿生成可以自动完成，**最终内容与发布操作仍应由人工审核**。

## 核心特性

### 采集与归档
- **多来源抓取**：RSS 订阅 + 网页爬取（列表页自动发现链接 / 单页抓取）。列表页支持**分页跟随**（`max_pages`，跟随 `?page=N` / `/page/N`），href 提取兼容单/双/无引号；对 SPA/SSR 站点可选 **JS 渲染**（`render_js`，需 Playwright，未装则回退静态）。
- **去重归档**：URL 规范化 + 标题指纹去重，跨轮次持久化；原文以 Markdown（带 front-matter）落盘，按主题分目录。
- **选题热度打分**：`compute_hotness()` 以 LLM 给主题打基线分 + 联网新鲜度加成，辅助排序选题。
- **来源发现**：免费版可从网址 / 关键词手动发现和批量添加来源；Pro 版可用 AI 推荐主题、子主题和关键词，并按主题一键 / 批量发现前沿来源。
- **归档视频标记**：归档列表自动识别文章中的视频并显示「🎬 视频」，地图、广告等普通 iframe 不会被误标。

### 三条创作工作流
- **抓取、转写创作**：通过 CLI、定时任务或 Web 顶栏「立即运行」执行 `抓取 → 去重 → 归档 → 分类 → 相关性过滤 → 转写 → 草稿`。可限制时间范围、每来源条数和每轮最多转写篇数。
- **搜索创作（Pro）**：执行 `理解选题 → 搜索资料 → 抓取页面 → 筛选排序 → 综合写作 → 事实校验 → 保存草稿`。选题框里填一句口语化的要求、一个问题甚至一段网址都行：模型先读懂你到底想要一篇什么文章（要讲的主题、你想解决什么、写给谁、点明的重点），再按这个理解写检索词、按理解出来的主题给资料排序，理解结果连同检索词一并存进草稿的「搜索参数」里可查。理解这步失败只会退回按原词检索，不会让整轮跑不下去。支持中文 / 英文 / 双语、7/30/90 天或不限时间、5/10/20 个参考来源，以及 Bing / 百度 / DuckDuckGo / Google / Brave 多选；百度中文资料最全但不支持时间筛选，近期资料不足时会自动扩大时间范围。默认沿用「设置 → 内容与风格」中的全局风格、标签、推广和敏感词配置，也可为单次任务指定风格。
- **系列创作（Pro）**：执行 `理解主题 → 摸底检索 → 梳理知识架构 → 生成提纲 → 逐章资料预检 → 逐章检索与写作`。系列主题同样先由模型读懂——「想做个系列把 RAG 讲清楚，读者是后端工程师」会被理解成领域、目标和读者，摸底检索按理解出来的领域名去搜，提纲也照这份理解来切，不会跑去讲你没问的东西。先用真实的公开资料校准术语，再由模型画出该领域的知识架构图（Mermaid）并据此拆章。章节数默认由提纲自己决定——主题窄就少写几章，分支多才多写，深度定位同时决定拆分粒度（3–10 章），也可以指定固定篇数；提纲生成后**先交给你审阅**，可改标题、范围、检索词与章节顺序，确认后才进入耗时的逐章写作。预检会提前标出公开资料偏少、可能写不成的章节。深度定位不只用来切提纲，还会一路带到写作：决定检索词找什么样的资料、正文的篇幅下限，以及必须写到机制、参数与取舍——术语第一次出现要先解释再使用，不允许靠罗列名词凑覆盖面；选「深入」时每篇参考资料读得更长，以便写到开头介绍之外的内容。第一章固定是整个系列的开篇总览：写作时会拿到完整的章节安排与知识架构图，交代主题全貌、各部分的关系和后面每章解决什么问题。写作按章隔离：某一章资料不足不会拖垮整个系列，可单独重跑；系列内跨章去重，同一来源不会在多章重复引用。章与章之间会写出钩子：每篇接住上一篇的结尾往下写，结尾点名下一篇并交代它要回答的问题，最后一篇改为收束整个系列——钩子取自真实的章节安排，不会预告不存在的内容。每章开头还会自动生成系列导航（本篇位置、上下篇、前置章节），整个系列可导出为单个 Markdown 或可打印为 PDF 的自包含网页。

### 媒体
- **媒体提取**：图片支持 `src`/`data-src`/`srcset`/`<picture>`/CSS background-image，视频支持 `<video>/<source>`、平台 iframe、**JS 动态填充**（扫描 `<script>`/JSON 中的媒体 URL），并支持 **HLS（`.m3u8`，边播拉 `.ts`）**。
- **媒体本地化**：抓取归档时可把文章里的图片（httpx）与视频（yt-dlp，直链/HLS 均可）**下载到本地** `data/media/<hash>/`，front-matter 与正文引用改为本地路径；草稿编辑器也可对正文媒体执行本地化。归档页提供「重新抓取」。
- **配图**：原文配图下载 + AI 生成封面（provider 可切换，离线用占位图）。

### 改写与合规
- **主题分类**：关键词优先命中，模糊时 LLM 零样本分类。
- **保真改写**：原文为唯一事实来源，禁编造数据/引用/结论；产出候选标题 + 钩子 + 正文 + 来源标注；改写后事实校验、存疑处标红。
- **转写风格**：内置多种写作风格。Pro 可从 3–10 篇 URL 或粘贴正文中学习语气、句式、开篇、段落、修辞、标题与结尾模式；学习结果可编辑、复用并通过 JSON 导入 / 导出。转写时最多注入 2 个表达示例，只学习写法，不带入样例事实。
- **敏感词过滤**：文章/视频脚本生成后自动移除平台敏感/违禁词（广告法绝对化用语、引流、医疗/金融夸大等），内置词库**按等级**（`off`/`basic`/`standard`/`strict`），可自定义追加；静默处理、记录并在草稿页提示。
- **草稿安全清理**：内部封面生成提示块不会写入、读取或发布到文章正文；旧草稿中的 `:::image-prompt` 块也会在访问时自动移除。
- **平台同步（Pro）**：公众号支持通过 API 推送草稿箱或直接发布；头条支持复制美化 HTML，小红书 / 知乎支持平台适配与复制；视频支持打开平台创作页及视频号半自动发布。平台架构可插拔。

### 讲解视频
- **脚本 + 配音**：从草稿自动生成口播分镜脚本（LLM）→ 逐段 TTS 配音；分镜可在草稿页**编辑**（旁白/画面/重选背景素材、增删/排序），并**只对改动的分镜重配**。
- **合成**：ffmpeg 合成「图片轮播 / 原视频片段 + 中文字幕 + 配音」的 mp4；中文字幕用 PIL 生成画面帧/叠层（规避 ffmpeg 中文字体问题）；画面适配可选 `fit`（完整+黑边）/`crop`/`blur`。同一视频被连续分镜引用时**续播**、放完**冻结最后一帧**。
- **TTS**：默认内置 `kitten`（中文走 edge-tts，免 key）；可选 **本地声音复刻 `cosyvoice`**（CosyVoice2-0.5B，设置内一键安装托管 Runtime 与模型；Windows 使用 CUDA，macOS Intel/Apple Silicon 使用 CPU）；也支持 OpenAI 兼容 TTS。
- **更像人的语气**：edge-tts 可调 **语速 / 音调**（`tts_rate` / `tts_pitch`）；CosyVoice 可给 **语气/情感指令**（`tts_instruct`，如「用亲切自然的语气」）。多分镜配音**并行**合成，速度更快。
- **数字人主播（可选）**：在讲解视频中叠加一个口播主播——**画中画（角落）**或**全屏主播**，可按分镜切换。口型同步使用独立的 **SadTalker Runtime**；Windows 使用 CUDA，macOS Intel/Apple Silicon 使用 CPU。不可用时自动回退为**静态头像**叠加。
- 合成需本地 **ffmpeg**；原视频/HLS 下载需 **yt-dlp**；数字人口型同步需 **SadTalker**（可选）。

### 运行与平台
- **运行控制**：按时间窗（`max_age_days`）与每源条数（`max_per_source`）限制抓取量。
- **并发加速**：多来源抓取、图片/视频下载用线程池并发，数量可配（`MEDIA_AGENT_DOWNLOAD_WORKERS`，默认 CPU 核数）。
- **定时调度**：APScheduler cron 定时自动运行；**实时运行进度/日志面板**。
- **本地 Web 界面**：仪表盘、归档、草稿、系列、来源、设置，以及 Pro 搜索创作与系列创作；支持实时运行进度、日志、暂停和停止。
- **可切换大模型**：业务代码不绑定厂商；内置 `mock`、OpenAI 兼容接口，也可使用 opencode / codex / copilot CLI Agent 执行转写。
- **配置导出/导入**：把来源 + 配置导出为 JSON 分享/备份，导入可恢复；**导出不含 API Key**。

### 免费版与 Pro 版

| 功能 | 免费版 | Pro 版 |
|---|:---:|:---:|
| RSS / 网页抓取、去重归档、主题分类 | ✅ | ✅ |
| 手动添加 / 发现来源、编辑器组件与模板 | ✅ | ✅ |
| 媒体本地化、kitten 配音 | ✅ | ✅ |
| LLM 转写 | 每日 1 篇 | 不限 |
| 搜索创作 | — | ✅ |
| 知识系列创作 | — | ✅ |
| AI 学习、管理、导入导出转写风格 | — | ✅ |
| AI 推荐主题与一键 / 批量发现来源 | — | ✅ |
| 讲解视频与数字人主播 | — | ✅ |
| 平台同步 / 公众号 API 发布 | — | ✅ |
| CosyVoice 声音复刻 | — | ✅ |
| 定时调度 | — | ✅ |

未激活许可证时自动使用免费版。Pro 使用离线、可绑定机器的激活码；本地开发可设置 `MEDIA_AGENT_LICENSE_DEV=1` 临时解锁功能，**不要用于正式发行包**。

> 版权提示：把第三方视频片段剪入自己的成片并发布可能涉及版权，请自行注明出处或取得授权；AI 生成图建议标注「AI 生成」。

## 一键启动

确保已安装 [Node.js](https://nodejs.org) 和 Python 3.11+ 后：

```bash
# Windows：双击 start.bat
start.bat

# macOS / Linux：
bash start.sh
```

脚本会自动完成：创建 Python 虚拟环境 → 安装依赖 → 构建前端 SPA → 启动 Web 服务。首次运行需下载 npm 包，耗时约 1-2 分钟。

启动后访问 **http://127.0.0.1:8000** 即可打开 Web 界面（基于 React SPA）。

## 安装

需要 Python 3.10+（推荐 3.11；已在 3.12 验证）。部分本地语音运行时对 Python 版本有独立要求，打包版会使用托管 Runtime 隔离依赖。

**可选外部依赖（按需安装）：**
- **ffmpeg**（讲解视频合成、HLS 合并）：装好并加入 PATH（<https://ffmpeg.org>）。中文字幕字体可用 `MEDIA_AGENT_FONT` 指定（默认自动探测微软雅黑/黑体/Noto CJK）。
- **yt-dlp**（平台视频 / HLS 下载）：已在 `requirements.txt`；CLI 不在 PATH 时自动回退 `python -m yt_dlp`。
- **Playwright**（SPA/SSR 页面 JS 渲染抓取）：`pip install playwright && playwright install chromium`。
- **TTS / 声音复刻**（按需选装）：

| 方案 | 类型 | 资源要求 | 中文 | 声音复刻 | 说明 |
|---|---|---|---|---|---|
| **kitten（edge-tts）** | 云端 | 无需 GPU | 良好 | ❌ | 默认内置，免 API Key |
| **CosyVoice2-0.5B** | 本地托管 Runtime | Windows CUDA 4GB+；macOS CPU | 最佳 | ✅ 3–10 秒样本 | 设置页一键安装 |
| **OpenAI 兼容 TTS** | 外部服务 | 由服务端决定 | 由模型决定 | 由服务端决定 | 配置 API Base / Key / Model |
| **mock** | 本地测试 | 无 | 占位输出 | ❌ | 用于离线验证流程 |

```bash
python -m venv .venv
# Windows (git bash): source .venv/Scripts/activate
# Windows (PowerShell): .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

> 国内网络可用镜像加速：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

### 打包版（免 Python 环境）

不想装 Python？从 [Releases](https://github.com/ivanzwb/release/releases) 下载对应平台的 zip，解压后直接运行：

> 国内下载慢？用镜像加速：把 `https://github.com` 替换为 `https://ghproxy.net/https://github.com`，例如：
> `https://ghproxy.net/https://github.com/ivanzwb/release/releases/download/v0.2.34/media-agent-v0.2.34-win64.zip`

```
Windows:  media-agent\media-agent.exe
macOS:    media-agent/media-agent
```

打包版已内置所有 Python 依赖（playwright 库）。解压后运行 `setup-optional.bat`（Win）或 `setup-optional.sh`（Mac）可自动检测并引导安装可选组件：

| 组件 | 打包版内置？ | 说明 |
|---|---|---|
| playwright 库 | ✅ 已内置 | 需额外下载 Chromium（脚本一键完成） |
| ffmpeg | ❌ 需单独装 | 视频合成必需，[下载](https://ffmpeg.org)后加入 PATH |
| CosyVoice | ✅ 设置内一键安装（Windows/macOS） | Windows CUDA；macOS Intel/Apple Silicon CPU（速度较慢）；自动断点续传 |
| SadTalker | ✅ 设置内一键安装（Windows/macOS，可选） | Windows CUDA；macOS Intel/Apple Silicon CPU（速度较慢）；无需源码或 Python 配置 |

### 本地构建

```bash
# Windows：双击 build-win.bat → dist\media-agent-win64.zip
# macOS：   bash build-mac.sh  → dist/media-agent/
```

开发时也可用 `start.bat` / `start.sh` 一键构建前端并启动服务（见「一键启动」）。

CI 自动构建：推送带 `v*` 的 tag 即触发 GitHub Actions，构建并冒烟验证 Windows、macOS Intel 与 macOS Apple Silicon 三个 onedir 包，然后上传 zip 到 Release。

Windows 本地开发需要 SadTalker 时，运行：

```powershell
.\prepare-sadtalker-dev.bat
```

脚本与 CI 共用同一依赖清单和打包器，预先构建 CUDA Runtime、生成发布版相同的分片/manifest，并安装到 `data/runtimes/sadtalker/`；模型下载到 `data/models/sadtalker/`。可用 `-Force` 重建，或通过 `-Proxy http://127.0.0.1:7890` 指定下载代理。

Windows 本地开发需要 CosyVoice 时，运行：

```powershell
.\prepare-cosyvoice-dev.bat
```

它与 CosyVoice Runtime CI 共用固定依赖清单、源码提交和打包器，生成发布兼容的分片/manifest，安装到 `data/runtimes/cosyvoice/`，并把模型预下载到 `data/models/cosyvoice/CosyVoice2-0.5B/`。

macOS 本地开发（Intel 与 Apple Silicon）分别运行：

```bash
bash ./prepare-sadtalker-dev.sh
bash ./prepare-cosyvoice-dev.sh
```

脚本会构建与 Release 相同架构的 CPU Runtime、预下载模型并执行真实推理验证。CPU 生成速度显著慢于 Windows CUDA。

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
| `MEDIA_AGENT_LLM_API_BASE` | OpenAI 兼容接口地址 | provider 默认 |
| `MEDIA_AGENT_LLM_TIMEOUT` | LLM 请求超时秒数 | `120` |
| `MEDIA_AGENT_IMAGE_PROVIDER` | `mock` \| `openai` | `mock` |
| `MEDIA_AGENT_IMAGE_API_KEY` | 图片模型 API Key | 无 |
| `MEDIA_AGENT_IMAGE_API_BASE` | 图片模型兼容接口地址 | 跟随 LLM API Base |
| `MEDIA_AGENT_IMAGE_MODEL` | 图片模型名 | provider 默认 |
| `MEDIA_AGENT_TTS_PROVIDER` | `kitten`（内置）\| `cosyvoice`（托管 Runtime；Windows CUDA/macOS CPU）\| `mock` \| `openai`（兼容） | `kitten` |
| `MEDIA_AGENT_TTS_API_BASE` | 外部 TTS 服务地址（仅 `openai` 需要） | 无 |
| `MEDIA_AGENT_TTS_API_KEY` | TTS API Key（内置/本地服务可留空） | 无 |
| `MEDIA_AGENT_TTS_MODEL` | TTS 模型名（openai 兼容用） | `tts-1` |
| `MEDIA_AGENT_TTS_VOICE` | 音色/风格：`female`/`child`/`male`… 或具体音色名 | `assistant` |
| `MEDIA_AGENT_TTS_RATE` | edge-tts 语速，如 `+10%` / `-10%`（空=正常） | 无 |
| `MEDIA_AGENT_TTS_PITCH` | edge-tts 音调，如 `+15Hz` / `-10Hz`（调高更活泼） | 无 |
| `MEDIA_AGENT_TTS_INSTRUCT` | CosyVoice 语气/情感指令，如「用亲切自然的语气」（仅 `cosyvoice`） | 无 |
| `MEDIA_AGENT_MAX_AGE_DAYS` | 只保留 N 天内的文章（空=不限） | 不限 |
| `MEDIA_AGENT_MAX_PER_SOURCE` | 每个来源最多抓取条数（空=不限） | 不限 |
| `MEDIA_AGENT_MAX_DRAFTS` | 每轮最多转写 / 改写篇数 | `10` |
| `MEDIA_AGENT_DOWNLOAD_WORKERS` | 来源抓取 / 图片视频下载的并发线程数（空=CPU 核数） | CPU 核数 |
| `MEDIA_AGENT_DOWNLOAD_IMAGES` | 抓取时本地化图片（`0` 关闭） | `1` |
| `MEDIA_AGENT_DOWNLOAD_VIDEOS` | 抓取时本地化视频（`0` 关闭） | `1` |
| `MEDIA_AGENT_RELEVANCE_FILTER` | LLM 过滤非新闻 / 非研究内容（`0` 关闭） | `1` |
| `MEDIA_AGENT_FETCH_PROXY` | RSS、网页抓取与媒体下载 HTTP/HTTPS 代理 | 无 |
| `MEDIA_AGENT_REWRITE_STYLE` | 全局默认转写风格 ID | 内置默认 |
| `MEDIA_AGENT_REWRITE_PRIORITY` | `agent` 优先或普通 LLM 转写 | `agent` |
| `MEDIA_AGENT_CLI_TOOL` | CLI Agent：`none` \| `opencode` \| `codex` \| `copilot` | `none` |
| `MEDIA_AGENT_CLI_TIMEOUT` | CLI Agent 超时秒数 | `500` |
| `MEDIA_AGENT_PROMOTION_FOOTER` | 自动附加到文章末尾的推广文案 | 无 |
| `MEDIA_AGENT_SEO_TAGS_ENABLED` | 自动生成 SEO 标签（`0` 关闭） | `1` |
| `MEDIA_AGENT_VIDEO_FIT_MODE` | 讲解视频画面适配：`fit`（完整+黑边）\| `crop`（铺满裁剪）\| `blur`（完整+模糊背景） | `fit` |
| `MEDIA_AGENT_VIDEO_BRAND_NAME` | 视频品牌名称 | `Media Agent` |
| `MEDIA_AGENT_FONT` | 视频中文字幕字体文件 | 自动探测 |
| `MEDIA_AGENT_AVATAR_ENABLED` | 讲解视频叠加数字人主播（`1` 开启） | `0` |
| `MEDIA_AGENT_AVATAR_IMAGE` | 主播头像文件名（放在 `data/avatar/`；建议在「设置」页上传） | 无 |
| `MEDIA_AGENT_AVATAR_POSITION` | 默认位置：`pip`（画中画/角落）\| `full`（全屏主播）；可按分镜覆盖 | `pip` |
| `MEDIA_AGENT_AVATAR_PROVIDER` | `sadtalker`（Windows CUDA / macOS CPU）\| `still`（静态头像） | `sadtalker` |
| `MEDIA_AGENT_SADTALKER_DIR` | 自定义 SadTalker Runtime 目录 | 托管目录 |
| `MEDIA_AGENT_SADTALKER_PYTHON` | 自定义 SadTalker Python 路径 | 托管 Runtime |
| `MEDIA_AGENT_SENSITIVE_LEVEL` | 敏感词过滤等级：`off` \| `basic` \| `standard` \| `strict` | `standard` |
| `MEDIA_AGENT_SENSITIVE_WORDS` | 自定义敏感词（逗号/换行分隔，追加到内置词库） | 无 |
| `MEDIA_AGENT_WECHAT_APPID` | 公众号 AppID | 无 |
| `MEDIA_AGENT_WECHAT_APPSECRET` | 公众号 AppSecret | 无 |
| `MEDIA_AGENT_WECHAT_AUTHOR` | 公众号默认作者 | 无 |
| `MEDIA_AGENT_GITHUB_MIRROR` | GitHub 下载镜像 | 无 |
| `MEDIA_AGENT_HUGGINGFACE_MIRROR` | Hugging Face 下载镜像 | 无 |
| `MEDIA_AGENT_LICENSE_DEV` | 本地开发绕过 Pro 许可（禁止发行包使用） | `0` |

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

`python -m app.cli serve` 后访问 `http://127.0.0.1:8000`（React SPA）。顶栏包含 **仪表盘 / 归档 / 草稿 / 系列 / 来源 / 设置**，以及 **搜索创作（Pro）**、**系列创作（Pro）** 和 **立即运行**。

- **仪表盘**：归档 / 草稿 / 运行计数，主题与来源热度排名、最近运行、最新草稿及数据清理。
- **归档**：按日期分组和主题筛选；每次只取一屏（60 篇），往下用「加载更多」续取，未展开的日期不渲染，归档再多打开也不变慢；视频文章显示「🎬 视频」；每篇可查看原文、重新抓取、选择风格转写 / 重写；支持批量删除和右下角转写进度面板。
- **草稿列表**：状态筛选、评分、分页、来源类型标记和批量删除。
- **草稿编辑 · 文章内容**：标题与候选标题、Markdown 分屏预览、排版组件 / 模板 / 素材库、状态流转、封面抓取 / AI 生成、事实校验、敏感词提示、媒体本地化、Agent 编辑，以及 Pro 平台适配 / 发布。搜索创作稿会显示参考来源与引用。
- **草稿编辑 · 讲解视频（Pro）**：生成分镜和配音，编辑旁白 / 画面 / 素材 / 主播位置，局部重配，合成与下载 mp4，并打开平台视频页或执行视频号半自动发布。
- **搜索创作（Pro）**：输入主题并配置语言、时间范围、参考来源数、写作风格和搜索引擎；页面实时显示七阶段进度、日志，支持取消和完成后打开草稿。
- **系列创作（Pro）**：输入知识主题并配置深度定位、语言、每章参考来源数、风格和搜索引擎，章节数默认自动（也可固定）；先出提纲供审阅和编辑（含知识架构图与逐章资料预检），确认后逐章写作，页面实时显示阶段进度与日志，支持取消和单章重跑。
- **系列（Pro）**：已规划或写成的系列都在这里，可查看知识架构图与逐章状态、打开对应草稿、补写未完成的章节，以及把整个系列导出为 Markdown 或可打印网页。
- **来源**：主题 / 来源增删改、启用禁用、可达性检测和批量操作；免费版可从网址 / 关键词手动发现，Pro 可 AI 推荐主题并一键 / 按主题发现来源。
- **设置**：8 个 Tab——**授权｜模型｜语音与视频｜数字人主播｜内容与风格｜采集与定时｜公众号｜其它**。自定义风格的学习、管理和导入导出位于「内容与风格」。

> 定时调度仅在 Pro 许可有效时启动。修改 cron 后请重启服务。

## 产出物

- `data/archive/<主题>/<日期>-<slug>.md`：抓取的原文（含 front-matter：标题/来源/主题/images/videos 等）。
- `data/drafts/<主题>/<article_id>-<slug>.md`：改写后的草稿，front-matter 含候选标题、来源、封面、平台、`flagged_claims`（存疑项）、`sensitive_hits`（被过滤的敏感词）、状态。平台适配通常返回复制 / 发布内容，不另外创建平台命名文件。
- `data/media/<hash>/`：本地化的原文图片/视频。
- `data/videos/draft-<id>/`：讲解视频产物（`script.json` 分镜脚本、逐段配音、`video.mp4`）。
- `data/voices/`：声音复刻样本与注册表（`voices.json`）。
- `data/avatar/`：数字人主播头像（口播叠加用）。
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
├─ licensing/           # Free / Pro 功能门控、离线许可与完整性校验
├─ discovery.py         # 来源自动发现（RSS 探测 + 关键词搜索）
├─ sources/             # rss / scraper（分页/JS 渲染）/ extractor（媒体提取）/ dedup
├─ media/               # 下载策略链（downloader / strategies：httpx 直链 / URL 变换 / yt-dlp）
├─ llm/                 # 大模型抽象层 + providers（mock / openai）
├─ images/              # 图片抽象层 + providers（mock / openai）
├─ tts/                 # TTS 抽象层 + providers（kitten / cosyvoice / openai…）+ 声音库
├─ video/               # builder：PIL 字幕帧 + ffmpeg 合成
├─ wechat/              # 公众号 HTML、媒体上传、草稿箱与直接发布
├─ platforms/           # 平台同步（wechat / xiaohongshu / toutiao / zhihu）— 可插拔架构
├─ pipeline/            # 抓取转写、搜索创作、风格学习、事实校验、媒体与视频流水线
├─ web/                 # FastAPI 服务 + 静态资源 + React SPA 挂载
frontend/               # React SPA 源码（Vite + Ant Design + React Router）
tests/                  # pytest 测试套件
packaging/              # PyInstaller、CosyVoice / SadTalker Runtime 打包工具
feeds.yaml  requirements.txt
build-win.bat build-mac.sh  # 本地打包脚本
start.bat start.sh      # 一键启动脚本（构建前端 + 启动服务）
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

- 扩展更多平台的官方 API 直发（公众号草稿箱 / 直接发布已支持）。
- 更完整的多平台统一发布状态与失败重试。
- 更多来源（X/Twitter 等社交平台）。
- 多模型对比改写、A/B 标题。
- 归档双格式（原始 HTML + Markdown）以便提取逻辑升级后回溯重提取（见 issue #9）。
- 背景音乐 / 转场 / 竖屏 9:16 适配。（数字人口播已支持，见「讲解视频」）
