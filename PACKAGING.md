# 打包与加固 (Packaging & Hardening)

本项目支持 Python wheel、本地 PyInstaller 构建，以及由 GitHub Actions 自动生成的 Windows / macOS 发布包。当前项目版本以 `pyproject.toml` 为准。

## 1. Python wheel / pip 安装

`pyproject.toml` 已配置 `media-agent` 控制台入口，并把 `app/web/static` 与许可公钥打入 wheel。Vendor 工具和签发私钥不会进入 Python 包。

```bash
python -m pip install build
python -m build

# 使用实际生成的版本号
python -m pip install dist/media_agent-<version>-py3-none-any.whl

media-agent init
media-agent run --feeds feeds.yaml
media-agent serve
```

如需发布到 PyPI：

```bash
python -m pip install twine
python -m twine upload dist/*
```

> 当前 wheel **不包含**顶层 `frontend/dist`。在源码目录安装时可先执行 `cd frontend && npm run build` 后启动 Web UI；脱离源码目录分发时应使用下面的 PyInstaller onedir Release 包。

## 2. Release 包（PyInstaller onedir）

`.github/workflows/build.yml` 是正式发布流程。推送 `v*` tag 后会：

1. 使用 Node.js 构建 React SPA；
2. 安装 Python 依赖与 PyInstaller；
3. 生成 onedir 应用；
4. 对冻结后的 `app.web.server` 执行导入冒烟测试；
5. 分别输出 Windows、macOS Intel (`x86_64`) 与 macOS Apple Silicon (`arm64`) zip；
6. 上传到 `ivanzwb/release` 对应 GitHub Release。

发布包会包含：

- `frontend/dist` 与 `app/web/static`
- `app/licensing/public_key.b64`
- Playwright Python 库（Chromium 仍按需安装）
- CosyVoice worker
- `setup-optional.bat` 或 `setup-optional.sh`

运行：

```text
Windows: media-agent\media-agent.exe
macOS:   media-agent/media-agent
```

应用由 `packaging/run_app.py` 启动本地服务并打开浏览器，数据默认写入应用同级 `data/`。

### 本地构建

```text
Windows: build-win.bat
macOS:   bash build-mac.sh
```

本地脚本与 CI 都使用 onedir 思路。构建后应至少执行冻结模块冒烟测试，并在目标系统打开 Web UI 验证静态资源和数据库写入。

## 3. 实验性单文件脚手架

`packaging/media-agent.spec` 保留单文件 / 自定义 PyInstaller 的实验脚手架：

```bash
python -m pip install pyinstaller
pyinstaller packaging/media-agent.spec
```

该 spec **不是当前 Release CI 的正式产物**。FastAPI、uvicorn、trafilatura、lxml、Playwright 等包含动态导入；修改依赖后需要同步检查 `hiddenimports`、`datas` 和冻结环境冒烟测试。

## 4. 可选本地 AI Runtime

CosyVoice 与 SadTalker 不直接塞入主应用包，而是使用独立托管 Runtime，避免 torch 等大型依赖污染主程序。

- CI：`.github/workflows/build-cosyvoice-runtime.yml`、`.github/workflows/build-sadtalker-runtime.yml`
- 打包器：`packaging/pack_cosyvoice_runtime.py`、`packaging/pack_sadtalker_runtime.py`
- 开发准备：`prepare-cosyvoice-dev.*`、`prepare-sadtalker-dev.*`
- Windows 使用 CUDA Runtime；macOS Intel / Apple Silicon 使用 CPU Runtime
- Runtime 与模型支持分片、manifest、断点续传和真实推理验证

## 5. 许可与加固

客户端已内置：

- License 离线 **Ed25519 验签**（私钥不在客户端）
- 授权文件加密与机器指纹绑定
- 时间回拨检测
- 公钥完整性自校验
- Free / Pro 功能门控与每日免费转写额度

Pro 功能目录位于 `app/licensing/features.py`，包括无限转写、搜索创作、知识系列创作、风格学习、AI 来源发现、讲解视频、平台同步、声音复刻和定时调度。

进一步加固：

```bash
# PyArmor 示例
pyarmor gen -O dist_obf app/licensing app/web/server.py

# Cython 编译关键模块
python tools/build_hardened.py cythonize
```

建议顺序：生成密钥并固定公钥哈希 → 运行测试 → 加固 / 编译 → 构建前端 → PyInstaller 打包 → 冻结模块与实际启动验证。

## 6. 发证流程（卖家）

```bash
# 首次生成密钥对
python -m tools.licctl keygen

# 一年授权
python -m tools.licctl issue --key MA-PRO-0001 --days 365

# 绑定机器
python -m tools.licctl issue \
  --key MA-PRO-0002 \
  --machine 1A2B-3C4D-5E6F-7A8B
```

`tools/license_private_key.b64` 是签发私钥，**务必保密、切勿提交或复制到 CI / 客户端产物**。客户端只携带 `app/licensing/public_key.b64` 与完整性校验信息。

本地开发可设置 `MEDIA_AGENT_LICENSE_DEV=1` 跳过 Pro 门控；正式发行和验收时必须移除该变量。
