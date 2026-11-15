# 学习通自动化（Chaoxing Automation）

学习通《形势与政策》等课程**视频任务点自动化**工具：本地 Web 控制台 + 浏览器执行引擎（CDP）+ 本地大模型答题管线（Qwen3-VL-4B，llama.cpp CPU 推理）。

> 版本：**M2.3-20261115**
> 定位：个人学习/研究用途的桌面自动化工具；仅在本机运行，不依赖云端服务。

---

## 功能

- **本地 Web 控制台**（FastAPI，`127.0.0.1:8770`）：导入任务计划、查看进度/完成截图/成绩，面向非技术用户的浅色界面。
- **执行引擎**：独立 Chrome/Edge 实例 + CDP（端口 9222），按章节执行视频任务点、章节测验回填、提交与校验；带心跳安全停止、队列守护、逐节证据留档。
- **本地模型答题**：Qwen3-VL-4B-Instruct（Q4_K_M，CPU 推理）视觉识别题目 → 文本作答 → 置信度闸门；可选「只做视频、跳过测验」开关。
- **便携交付包**（单独分发，不入库）：内置便携 Python 3.13.9 与模型，解压即用。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `console/` | 本地 Web 控制台（FastAPI + 静态前端） |
| `engine/` | 执行引擎：浏览器/CDP、视频、测验、提交、队列守护、预检 |
| `pipeline/` | 本地模型答题管线（A 视觉识别 → C 文本作答 → 闸门） |
| `configs/` | 配置模板（engine / pipeline / launcher）与跳过答题说明 |
| `docs/` | 项目文档：目标书、决策记录、环境说明、里程碑、规划、交付复查指南 |
| `launcher.py` | 启动器：起控制台服务 + 队列守护 + 打开控制台（不抢前台） |
| `selfcheck.py` | 环境/依赖/模型/端口只读自检 |
| `toggle_skip_quiz.py` | 「跳过章节测验」开关切换 |
| `*.cmd` | Windows 双击入口（启动浏览器 / 启动自动化 / 停止 / 自检 / 切换跳过答题） |
| `version.json` | 版本、运行时与模型清单（含 sha256） |
| `requirements.txt` | 运行依赖版本记录（便携包已内置，无需安装） |

## 快速开始

### 方式 A：便携包（推荐给普通用户）

便携包另发（约 4.2GB，含 `runtime/python` 与 `runtime/models`），本仓库不含运行时与模型。使用：

1. 双击 `启动自动化浏览器.cmd` → 在打开的浏览器里**扫码登录学习通**（登录态存于 `runtime/browser-profile`）。
2. 双击 `启动学习通自动化.cmd` → 自动启动控制台并打开 `http://127.0.0.1:8770/`。
3. 在控制台确认在线后点「开始」。
4. 停止：双击 `停止.cmd`（安全停止，不会半截提交）。

### 方式 B：从源码运行

前置：Windows 10/11、Python 3.13、Chrome（或 Edge），并在 `runtime/models/` 放置：

- `Qwen3-VL-4B-Instruct-Q4_K_M.gguf`（约 2.33GB）
- `mmproj-Qwen3-VL-4B-Instruct-F16.gguf`（约 0.78GB）

模型 sha256 见 `version.json`。依赖见 `requirements.txt`（playwright、websocket-client、fastapi、uvicorn、llama_cpp_python、pillow、numpy、python-docx、pypdf）。

```powershell
# 自检（只读，不启动服务）
runtime\python\python.exe selfcheck.py --json
# 只读启动检查
runtime\python\python.exe launcher.py --check
```

## 配置要点

- `configs/launcher.json`：端口 `8770`、自动打开浏览器、队列守护开关、心跳宽限时间。
- `engine/config.json`：CDP 端口/页面、视频比例阈值、提交按钮选择器、答题开关 `answer.skip_quiz`。
- `pipeline/config.json`：模型路径（`runtime/models/...`）、线程数、上下文长度、置信度阈值。

## 文档

从 [docs/README.md](docs/README.md) 开始：项目目标书 → 决策记录 → 环境与运行说明 → 里程碑与待办 → 交付与复查指南。

## 已知限制

- 仅支持 Windows；不内置浏览器，使用系统 Chrome（首选）/ Edge（回退）。
- 首次需用户**扫码登录**；登录态失效需重新扫码。
- 本地模型为 CPU 推理，单节测验耗时较长（约 2–4 分钟/5 题，视机器而定）。
- 不含预构建 exe，通过 `.cmd` + 便携 `pythonw.exe` 启动。
- 本仓库**不含** `runtime/`（便携 Python）与模型文件：见 `.gitignore`。

## 说明与免责

- 本项目仅供个人学习与技术研究使用。
- 使用自动化操作第三方学习平台可能违反其用户协议，**账号与成绩风险由使用者自行承担**。
- 请遵守所在学校/平台的规定，勿用于作弊等违规用途。
