# 学习通自动化 便携版 · 使用说明

版本：**M2.3-20261115**（含便携 Python 运行时 + 内置本地模型，解压即用，无需安装 Python / 依赖 / 模型）

---

## 1. 包内容一览

| 目录/文件 | 作用 |
| --- | --- |
| `runtime/python/` | 便携 Python 3.13.9（含 site-packages，已内置全部依赖） |
| `runtime/models/` | 本地模型 Qwen3-VL-4B-Instruct Q4_K_M + mmproj F16（约 3.18GB） |
| `runtime/browser-profile/` | 首次启动自动化浏览器后自动生成（保存学习通登录态） |
| `console/` | 本地 Web 控制台（FastAPI，127.0.0.1:8770） |
| `engine/` | 执行引擎（浏览器/CDP、视频、测验、提交、队列守护） |
| `pipeline/` | 本地模型答题管线（A 视觉识别 → C 文本作答 → 闸门） |
| `configs/` | 配置模板（engine-config.json / pipeline-config.json / launcher.json）与跳过答题说明 |
| `docs/` | 项目文档（交付与复查指南、环境说明、决策记录等） |
| `launcher.py` | 包内启动器（起服务 + 起队列守护 + 打开控制台；不抢前台） |
| `selfcheck.py` / `toggle_skip_quiz.py` | 自检 / 跳过答题开关 |
| `manifest.json` | 全包文件清单（相对路径 + 大小 + SHA256），用于复查完整性 |

**双击文件（包根目录）**

| 文件 | 用途 |
| --- | --- |
| `启动自动化浏览器.cmd` | 启动独立浏览器（Chrome/Edge，独立 profile，CDP 9222），打开学习通登录页 |
| `启动学习通自动化.cmd` | 启动控制台服务 + 队列守护，并自动用系统默认浏览器打开控制台 |
| `停止.cmd` | 安全停止（先让引擎停在安全点，再收服务；不会半截提交） |
| `自检.cmd` | 检查运行时依赖 / 模型 / 配置 / 端口（不启动服务） |
| `切换跳过答题.cmd` | 切换「只做视频、不做章节测验」开关 |

---

## 2. 首次使用（3 步）

1. **登录**：双击 `启动自动化浏览器.cmd`，在弹出的浏览器里**扫码登录学习通**（以后登录态保存在 `runtime/browser-profile`，失效再扫）。
2. **启动**：双击 `启动学习通自动化.cmd`。启动器会在后台起控制台（http://127.0.0.1:8770/）和队列守护，并自动用系统默认浏览器打开控制台页面。
   - 若浏览器没有自动弹出：手动访问 `http://127.0.0.1:8770/`。
3. **开始**：在控制台里确认工具/浏览器状态为在线，然后点「开始」执行计划；任务由队列守护在后台逐节执行。

> 运行期间**不要关闭控制台页面**（页面心跳是安全信号）；确需关闭时请双击 `停止.cmd`。关闭页面后约 20 秒启动器会自动安全停止（可在 `configs/launcher.json` 改 `on_browser_close`）。

---

## 3. 跳过答题（skip_quiz）

- 双击 `切换跳过答题.cmd`：ON/OFF 一键切换，当前状态会打印在窗口里。
- 开关实际位置：`engine/config.json` → `answer.skip_quiz`（同时同步 `configs/engine-config.json`）。
- **OFF（默认 false）**：正常流程 = 课程视频 + 章节测验 + 回填 + 提交。
- **ON（true）**：只做课程视频；不做章节测验、不回填、不提交（该节记 skipped_quiz=true，自动继续下一节）。
- 生效：新计划 / 重启启动器后生效。说明见 `configs/跳过答题说明.md`。

---

## 4. 自检

双击 `自检.cmd`（或 `runtimepythonpython.exe selfcheck.py --json`），检查：

- 运行时依赖 import：fastapi / uvicorn / llama_cpp / numpy / PIL / docx / pypdf
- 包内文件：server/app.py、engine、pipeline、模型、配置、启动器
- 配置中的模型路径与 python 路径能否解析
- 端口 8770（控制台）、9222（浏览器 CDP）状态（只探测，不启动）
- 结果同时追加到 `logs/selfcheck.log`

---

## 5. 日志与证据位置（复查用）

| 内容 | 位置 |
| --- | --- |
| 启动器日志 | `logs/launcher.log` |
| 自检记录 | `logs/selfcheck.log` |
| 控制台服务日志 | `console/server.log` |
| 队列守护日志 | `engine/daemon.log`、`engine/daemon.stdout.log` |
| 单节运行证据 | `engine/runs/<节>/<YYYYmmdd-HHMMSS>/`（run.log / run.json / 截图 evidence/） |
| 全局索引/报告 | `engine/logs/index.jsonl`、`engine/logs/report.md` |
| 引擎账本 | `engine/queue_state.jsonl` |
| 控制台状态/计划 | `console/state.json`、`console/plans/` |

---

## 6. 交付复查

1. `manifest.json`：全包文件清单（相对路径 + size + sha256）；抽查 `runtime/models/*.gguf` 的 sha256 应与版本 JSON 中记录一致。
2. `version.json`：版本、运行时、模型 sha256、入口说明。
3. 自检：双击 `自检.cmd`，全部 OK（WARN 项是端口/浏览器未启动，属正常）。
4. 只读启动检查（不起服务）：`runtimepythonpython.exe launcher.py --check`。
5. 该 zip 内文件与 `manifest.json` 一一对应；zip 生成方式 `tar -a -c -f`。

---

## 7. 已知限制

- 需 Windows 10/11；浏览器用系统 Chrome（首选）/ Edge（回退），不内置浏览器。
- 首次启动会创建 `runtime/browser-profile`，需要用户扫码登录学习通；登录态失效需重新扫码。
- 本地模型推理为 CPU 推理，单节测验耗时较长（约 2–4 分钟/5 题，视机器而定），请耐心等待。
- 模型后端默认 `backend=local`（完全本地推理，不发任何网络请求）；`pipeline/config.json` 保留 `api` 段作预留但不启用，包内不含任何密钥。
- 本包不含预构建 exe；通过 `.cmd` + 便携 `pythonw.exe` 启动（无黑色 CMD 窗口）。
- 首次启动稍慢（便携运行库 + 模型加载）；若安全软件拦截，请允许 `runtime/python` 与 `runtime/browser-profile`。

---

## 8. 常见问题

| 现象 | 处理 |
| --- | --- |
| 双击启动没反应 | 打开 `logs/launcher.log` 看原因；确认 `runtime/python/pythonw.exe` 存在 |
| 控制台打不开 / 端口占用 | 双击 `停止.cmd` 后再启动；或换端口：`runtimepythonpython.exe launcher.py --port 8780` |
| 控制台显示浏览器离线/未登录 | 双击 `启动自动化浏览器.cmd`，在打开的浏览器里扫码登录 |
| 点「开始」没执行 | 确认 `configs/launcher.json` 的 `start_daemon` 为 true（默认），并看 `engine/daemon.log` |
| 模型加载失败 | 双击 `自检.cmd`；确认 `runtime/models/` 两个 gguf 完整（见 manifest/version.json 的 sha256） |
| 想不弹控制台页面 | 用 `runtimepythonpython.exe launcher.py --no-browser`，再手动开 `http://127.0.0.1:8770/` |
