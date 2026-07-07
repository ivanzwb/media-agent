# 打包与加固 (Packaging & Hardening)

商业化发版的三种产物 + 防破解。除 PyPI 外，exe 与混淆需在你的构建环境运行
（本仓库未预构建、未在 CI 验证）。

## 1. PyPI / pip install（已配置，可直接用）

`pyproject.toml` + `MANIFEST.in` 已就绪（含模板/静态/公钥打包、控制台入口
`media-agent`）。

```bash
pip install build
python -m build                 # 产出 dist/*.whl 和 *.tar.gz
pip install dist/media_agent-0.1.0-py3-none-any.whl
media-agent serve               # 启动本地 Web UI
media-agent init | run | serve  # 其它命令同 `python -m app.cli`
```

发布到 PyPI：`pip install twine && twine upload dist/*`。

## 2. 单文件 exe（PyInstaller · 未测试脚手架）

```bash
pip install pyinstaller
pyinstaller packaging/media-agent.spec
# -> dist/media-agent(.exe)；双击启动并自动打开浏览器（packaging/run_app.py）
```

注意：FastAPI/uvicorn/trafilatura/lxml 等有动态导入，首次构建大概率要按报错补
`hiddenimports`/`datas`。数据目录默认落在 exe 同级 `data/`。

## 3. 防破解 / 加固

已内置（代码级、可测）：
- License 离线 **Ed25519 验签**（私钥不在客户端，无法伪造激活码）
- 授权文件 **AES 加密 + 机器指纹绑定**（拷贝到其他机器失效）
- **时间回拨检测**（防改系统时间绕过过期）
- **公钥防调包自校验**（`app/licensing/integrity.py`，运行 `licctl keygen` 后启用）

进一步加固（`tools/build_hardened.py`，需你的构建环境）：
- **PyArmor**：`pyarmor gen -O dist_obf app/licensing app/web/server.py`
- **Cython** 编译关键模块为原生 `.pyd/.so`：`python tools/build_hardened.py cythonize`

顺序建议：`licctl keygen`（生成密钥并 pin 公钥哈希）→ 加固/编译 → 打包。

## 发证流程（卖家）

```bash
python -m tools.licctl keygen                          # 一次性：生成密钥对
python -m tools.licctl issue --key MA-PRO-0001 --days 365
python -m tools.licctl issue --key MA-PRO-0002 --machine 1A2B-3C4D-5E6F-7A8B
```

`tools/license_private_key.b64` 是签发私钥，**务必保密、切勿提交**（已 gitignore）。
`app/licensing/public_key.b64` 与 `integrity.py` 的 pin 需随包发布。
