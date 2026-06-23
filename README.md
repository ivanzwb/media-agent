# Media Agent

自媒体运营自动助理：抓取 RSS/网页文章 → 去重归档 → 主题分类 → 保真改写为爆款 Markdown 草稿。

## 安装

```bash
python -m venv .venv
source .venv/Scripts/activate   # git bash on Windows
pip install -r requirements.txt
```

## 配置

编辑 `feeds.yaml` 配置主题与来源。设置环境变量：

- `MEDIA_AGENT_DATA_DIR`（默认 `data`）
- `MEDIA_AGENT_LLM_PROVIDER`（`mock` | `openai`）
- `MEDIA_AGENT_LLM_API_KEY`
- `MEDIA_AGENT_LLM_MODEL`

## 使用

```bash
python -m app.cli init
python -m app.cli run --feeds feeds.yaml
```

归档原文写入 `data/archive/`，草稿写入 `data/drafts/`（Markdown，带 front-matter）。

不配置任何真实模型时，默认使用 `mock` provider，可在无 API key 的情况下跑通整条流水线（改写内容为占位）。

## 测试

```bash
python -m pytest -v
```

## 路线图

- Plan 1（已实现）：抓取 → 去重 → 归档 → 分类 → 保真改写 → md 草稿（CLI）
- Plan 2：配图（原图下载 + AI 封面）+ 本地 Web 界面（归档浏览 / 草稿 md 编辑审核）
- Plan 3：定时调度 + 运行记录 + 平台适配改写
