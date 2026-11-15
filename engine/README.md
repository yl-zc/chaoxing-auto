# engine —— 单节统一运行器 + 队列守护（M2）

把控制台「开始」变成真实执行：控制台只写队列/计划文件，引擎读文件、逐节串行执行、写回状态。
**引擎与控制台不互相 import，只通过文件契约交互。**

## 1. 模块职责（单一职责，可单独替换）

| 文件 | 职责 | 只依赖 |
|---|---|---|
| `common.py` | 配置加载、原子 JSON、文件锁、日志、进度上报、路径解析 | 标准库 |
| `browser.py` | 浏览器层：Edge 默认/Chrome 回退、双标签启动、CDP 探测、旧实例清理、引擎选页规则 | 标准库（websocket 可选） |
| `nav.py` | 小节定位/进入（点页面目录）、任务点数、目录顺序、返回课程、下一节 | common |
| `video.py` | 1x 静音真实播放到任务点计入（≥90% 或 marker）、只读探测、暂停 | common |
| `capture.py` | 进章节测验、读题型、整视口 CDP 截图、调 pipeline 裁剪、生成 manifest | common + pipeline CLI |
| `answer.py` | 子进程调 `pipeline/run_pipeline.py`（A 视觉→C 作答→校验→闸门），只读 JSON 产物 | common + pipeline CLI |
| `fill.py` | DOM 回填 + 三对照（显示字母 / 提交码 / 隐藏域） | common |
| `submit.py` | 提交按钮 1 次 + 确认 1 次 + 结果页轮询读成绩（失败绝不重复提交） | common |
| `verify.py` | 跳下一节并暂停、读下一节任务点、可选回课程页读进度 | common/nav/video |
| `run_section.py` | **编排层**：dry/real 流程、阶段进度、证据落盘、退出码；不实现页面细节 | 以上全部 |
| `queue_daemon.py` | **队列守护**：轮询 queue.jsonl、取 plan、逐节串行调 run_section、写 state.json、失败即停 | common + 子进程 |

`auto-2.3/run_2_3.py` 是参考实现；本目录把它按职责拆成上面的模块，没有整段复制。
pipeline 的 A/C **只通过 CLI 子进程调用**，引擎内不复制其内部逻辑（校验/闸门在 pipeline 里）。
所有可变参数集中在 `engine/config.json`（CDP 端口、路径、解释器、线程/阈值、各阶段超时、比例阈值、开关）。

## 2. 用法

解释器：`work/.venv/Scripts/python.exe`（已装 playwright/websocket-client；**不要装新依赖**）。
pipeline 解释器：`D:/pyai/pych/.venv/Scripts/python.exe`（llama_cpp + Pillow），在 config.python.pipeline 配置。
浏览器：默认 **Edge**（独立 profile `work/edge-profile`，缺失自动回退 Chrome），全部由 config.browser 决定。

~~~powershell
# 浏览器层（配置驱动；--print-cmd 可打印启动参数供人工执行）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --status
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --launch
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --print-cmd
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --close-legacy --dry-run

# dry 只读检查（不播放/不答题/不提交；会打开该节的测验标签页读题型，然后切回视频标签）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\run_section.py --section 2.4 --mode dry

# real 完整流程（必须先放行，见 §5）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\run_section.py --section 2.4 --mode real

# 队列守护：常驻轮询 queue.jsonl，逐节串行执行（real 未放行时只写 idle 不执行）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\queue_daemon.py

# 自测（不真跑课程）：取第一个待执行计划的第一节跑 dry，然后退出
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\queue_daemon.py --self-test

# 只处理一个计划后退出 / 只处理指定计划（调试用）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\queue_daemon.py --once
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\queue_daemon.py --plan plan-20261107-235743-5edae5
~~~

退出码（run_section）：0 成功（含 skipped/dry_ok）；2 业务中止；3 异常；4 real 未放行；5 已有实例在跑。

## 3. 文件契约（字段只增不改；JSON 全部原子写）

### 3.0 浏览器（Edge 默认 / Chrome 回退）

`engine/config.json` 的 browser 段（换浏览器只改这里）：

~~~json
{
  "kind": "edge",
  "exe_path": "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "fallback": {"kind": "chrome", "exe_path": "C:/Program Files/Google/Chrome/Application/chrome.exe"},
  "user_data_dir": "work/edge-profile",
  "port": 9222,
  "login_url": "https://i.chaoxing.com/base",
  "console_url": "http://127.0.0.1:8770/",
  "launch_args": ["--no-first-run", "--no-default-browser-check"],
  "page_url_allow": ["chaoxing.com"],
  "page_url_deny": ["127.0.0.1:8770", "localhost:8770", "chrome://", "edge://", "about:", "devtools://"],
  "course_page_hints": ["studentstudy", "mycourse", "knowledge", "mooc"]
}
~~~

- **独立 profile 才能开 CDP 调试口**（不能用系统默认 profile，避免与用户日常浏览器冲突）。
- 启动时同一窗口开两个标签：**学习通登录页 + 控制台**（控制台最后打开、用户可见）；只补缺失标签，绝不关闭任何标签。
- 引擎选页规则（`browser.pick_page_index`）：URL 命中 `page_url_allow` 且不含 `page_url_deny`；
  优先 `course_page_hints`（studentstudy/mycourse/knowledge/mooc）。**控制台标签 127.0.0.1:8770 永远排除**。
- 切换页面只用 CDP 指定 target，不调用 activate / bring_to_front，不抢前台。
- 登录后若停在「学习空间/课程列表」而不是课程目录页：引擎按 `config.nav.course_name`（默认「形势与政策」）
  自动点进课程（课程页在同标签或新标签都能识别），再点目录进入目标小节。
- 单端登录纪律：同一学习通账号建议只保留一个已登录浏览器；Edge 就绪且登录后，
  `browser.py --close-legacy` 会关闭占用同一 CDP 端口的旧自动化浏览器（不同 profile 才关，不碰用户日常浏览器）。

### 3.1 输入：console/plans/queue.jsonl（控制台写，引擎只读）
每行一个计划引用：`{"id":"plan-...","at":"...","status":"queued","label":"...","count":4}`
引擎按行序取第一个未完成计划，再读 `console/plans/<id>.json` 的 `plan.sections[]`。
> 现状：console M1 写的 plan JSON **没有 schema_version**（视为 legacy v0，引擎兼容）；
> 建议控制台后续加 `schema_version: "plan/1"`，字段只增不改。

### 3.2 输出：console/state.json（queue_daemon 是**唯一写者**，原子写）
~~~json
{
  "schema_version": "engine-state/1",
  "state": "running",
  "current": {"section":"2.4","title":"…","stage":"video","progress":0.35,
              "detail":"正在播放视频 3:12/9:00","plan_id":"plan-…","started_at":"ISO"},
  "last_result": {"section":"2.3","title":"…","status":"completed","score":60,
                  "progress_total":12,"skipped":false,"next_section":"2.4",
                  "finished_at":"ISO","at":"ISO","run_dir":"…"},
  "progress": {"n":12,"total":59,"source":"page","at":"ISO"},
  "progress_source": "page",
  "queue": [{"section":"2.4","title":"…","plan_id":"plan-…","status":"queued"}],
  "paused": false,
  "daemon_pid": 12345,
  "updated_at": "ISO"
}
~~~
- `state` 仅四值：`idle|running|paused|error`；`current=null` 时控制台回退显示 `last_result`。
- `stage` 仅 `video|quiz|submit`（准备/导航阶段归入 video，验证/跳转阶段归入 submit，以 `detail` 文案为准）。
- `current.progress` 为 0~1 小数；顶层 `progress` 是**页面真实进度** {n,total,source:"page"}（控制台可优先展示）。
- `last_result` 额外字段：`skipped`（true=判定已完成而跳过）、`next_section`（平台下一节）、`all_done`（计划内全部已完成）。
- `last_result.status` 只发 `completed|failed`（dry 自测不写 last_result）。
- state=error 时同时写 `error` 与 `last_error`（字段只增）：
  `{"code":"BROWSER_OFFLINE|BROWSER_LOGIN_REQUIRED|SECTION_FAILED|QUIZ_ONLY_UNSUPPORTED","message":"中文提示","retryable":true,"at":"ISO","detail":"..."}`。
  - `BROWSER_OFFLINE` / `BROWSER_LOGIN_REQUIRED`：retryable=true，**不消费计划**，浏览器打开/登录后自动继续；
  - `SECTION_FAILED`：retryable=false，失败即停并保留 error（不会静默回 idle），直到用户在控制台发起新计划；
  - `QUIZ_ONLY_UNSUPPORTED`：M2 只支持完整流程，需在控制台改选。
- 完整 error 示例见 [RUNBOOK.md](RUNBOOK.md) §3。

### 3.3 输入：console/state.cmd.json（控制台写，引擎**只读、绝不删除**）
`{"action":"pause|resume","at":"ISO","ts":<epoch>,"source":"chat-console"}`
引擎按 ts 顺序消费、旧命令不回溯；**暂停在「节与节之间」生效**（正在跑的那一节会跑完）。
M2 的 pause/resume 只影响队列调度，不影响已开始的单节。

### 3.4 引擎内部
- `engine/queue_state.jsonl`：引擎账本（schema_version=engine-queue/1），事件：
  `plan_started / section_started / section_completed / section_skipped / section_failed / plan_failed / plan_completed / dry_test`
  （`dry_test` 不参与完成判定，real 会重新执行。）
- `engine/runs/<section>/<YYYYmmdd-HHMMSS>/`：单节证据（`run.log`、`run.json`、`progress.json`、
  `capture-raw.json`、`manifest.json`、`quiz-meta.json`、`pipeline/`（extracted/answers/pipeline-result）、
  `gate.json`、`fill.json`、`submit.json`、`verify.json`、`evidence/*.png`、`evidence/video-samples.jsonl`）。
- `engine/.locks/daemon.lock`、`.locks/section.lock`：单实例文件锁（msvcrt 字节锁，进程退出自动释放）。
- `engine/daemon.log`：守护循环日志。

### 3.5 开始前进度核对（preflight，只读）

**目的**：每次开始前以「页面」为准核对学习内容是否已更新（用户可能自己手动做过），避免重复做。

- 控制台「刷新进度」/ 引擎计划开始前都会调用：
  ~~~powershell
  .\work\.venv\Scripts\python.exe .\work\stage-d\engine\preflight.py --json [--sections 2.4,3.1] [--no-restore]
  ~~~
  stdout 只输出一行 JSON（日志走 stderr）：
  `{"ok":true,"logged_in":true,"n":12,"total":59,"text":"已完成任务点: 12/59","at":"ISO","source":"course_page","sections":[...]}`
  失败：`{"ok":false,"code":"BROWSER_OFFLINE|BROWSER_LOGIN_REQUIRED|BROWSER_NO_PAGE|PREFLIGHT_FAILED","message":"中文提示","retryable":true|false}`
  该命令只读：不写 state/queue、不播放、不答题、不提交；读完默认恢复原页面（`--no-restore` 可关闭）。
- 引擎自身也支持 `run_section.py --preflight-only --sections 2.4,3.1`（同样只读，写 `runs/_preflight/.../preflight.json`）。
- daemon 处理计划时：**先 preflight** 读页面进度与计划各节任务点 →
  1) 页面进度与本地 `last_result.progress_total` 不一致时，以页面为准，写 `state.progress={n,total,source:"page"}` 并记日志；
  2) 任务点=0 的小节记为 `already_done` 跳过（不改课程、不提交），`last_result.skipped=true`，状态继续推进；
  3) 计划内所有小节都已完成为 0 → 不执行任何节，`last_result.all_done=true`，提示「没有需要做的小节」；
  4) 证据：`runs/_preflight/<plan_id>-<ts>/preflight.json`（页面进度文本、各节任务点数、时间、URL）+ 账本 `plan_preflight` 事件。
- 节内二次核对：run_section 进入某节前会先读页面目录任务点数；若为 0 直接 `status=already_done` 返回；若视频点已 0 只有测验点 → 只做测验并回显「视频任务点已计入，本次只做测验」。

## 4. 流程与硬纪律

### dry（只读）
连 CDP → 点页面目录进入目标节 → 读任务点数 → 只读探测视频（不 play/不 load）→
点「章节测验」读题型与题量 → CDP 截一张证据图 → 切回视频标签 → 暂停任何自动播放 → 写 run.json。
**不播放、不答题、不提交。**

### real（完整）
0. **页面核对**：读「已完成任务点 N/59」+ 目录各节任务点；计划内全部为 0 → 直接结束（all_done，不执行任何节）
1. 选页：按 URL 规则选**学习通标签**（排除控制台）；再导航（点目录；不拼 URL、无 enc）；本节任务点=0 → already_done 跳过
2. 基线任务点数；=0 视为已完成，幂等跳过
3. 视频（需要时才做）：静音 1x 播放；≥90% 先暂停查任务点，未计入继续；marker 且 ≥85% 或 ≥98% 兜底停止；等任务点计入
4. 章节测验：进标签页 → 读题型 → 逐题整视口截图 + pipeline 裁剪（1.6x）→ manifest
5. `run_pipeline.py` 子进程：A 视觉转写 → C 文本作答 → schema 校验 → **闸门（min_conf≥0.8、覆盖 100%、无 unknown）**
6. 闸门不过 → **立即停止，不回填、不提交**
7. DOM 回填 + 三对照（显示字母/提交码/隐藏域），不一致 → **立即停止，不提交**
8. 提交：按钮 **1 次** + 顶层确认 `#popok` **1 次**；确认弹窗异常 → 停止且**不重复点击**
9. 轮询结果页读「本次成绩」；已提交但读不到成绩 → 记 error 留证据（不重试）
10. 点「下一节」并暂停下一节视频 → 可选回课程页读「已完成任务点 N/59」再返回下一节
11. 写 run.json / submit.json / verify.json / 各阶段截图

**全局纪律**：不调用任何窗口/焦点 API（SetForegroundWindow / ShowWindow / bring_to_front 一律没有）、
不硬编码带 enc 的 URL（导航只用页面 onclick / 页面里的链接）、同时间只允许一个实例（文件锁）、
每一节提交不可逆（成绩 <100 不重试）。

## 5. real 放行（默认关闭）

`engine/config.json` → `real: {"enabled": false}`。放行步骤：
1. 确认浏览器已登录、CDP 9222 在线、控制台未在跑其他任务；
2. 把 `real.enabled` 改为 `true`；
3. 单节：`run_section.py --section 2.4 --mode real`；或队列：启动 `queue_daemon.py`（守护会**热加载**该开关）；
4. 收回放行：改回 `false`（正在跑的那一节不会被打断）。

## 6. 已知限制 / 风险

- **提交不可逆**：real 会真实提交测验；成绩 <100 也不重试（纪律）。
- 视频播放约等于课程时长（8 分钟左右/节），real 单节通常 3~10 分钟。
- 暂停（state.cmd.json）只在节间生效，不支持中断当前节。
- `quiz-only` 计划（plan.mode=quiz）暂不支持 → daemon 写 error 并停止，需在控制台改选「完整流程」。
- Edge 首次用新 profile 会弹「同步确认」对话框，属正常，用户关掉即可（不影响 CDP）。
- 学习通登录态失效时 daemon 写 `BROWSER_LOGIN_REQUIRED`（retryable），登录后自动继续，不会误提交。
- 系统可能只显示「python.exe 启动器 + 真实解释器」两个 PID：这是 venv launcher 行为；
  用**基础解释器**跑 daemon 可得单 PID（daemon 只用标准库），run_section 子进程仍用 `work/.venv`。
- dry 会把浏览器留在目标节（只读，不改课程数据；不改页面上的答案/提交）。
- 平台侧栏任务点读数可能滞后，real 以「提交结果 + 成绩 + 课程进度」为准。
- plan.json 的 schema_version 需 console 侧后续补（引擎已兼容 legacy）。
- 引擎不做模型下载/换模型推理参数：模型路径、mmproj、max_tokens 在 `pipeline/config.json`。

## 7. 扩展点（改哪里）

| 需求 | 改哪里 |
|---|---|
| 换站点/页面结构（目录 onclick 变体） | `nav.py` 的 `COLLECT_JS/CLICK_JS` 策略；标签文案、下一节文案在 config.nav |
| 换题型/题目渲染方式 | `capture.py`（读题/截图）、`fill.py`（点选与三对照）；题干文字识别仍在 pipeline A 段 |
| 换模型/提示词 | 只改 `pipeline/config.json` 与 `pipeline/A_extract.py / C_answer.py` 的 PROMPT（引擎无需改） |
| 调线程/阈值/超时 | `engine/config.json`（pipeline.n_threads / min_conf / 各 timeout） |
| 换浏览器（Edge/Chrome/其它 Chromium） | 只改 `engine/config.json` 的 browser 段（kind/exe_path/fallback/profile/port/login_url/console_url） |
| 换控制台 | 只改文件契约（§3）；引擎不 import 控制台 |
| 改单节步骤顺序 | `run_section.py` 的 `real_flow()`（纯编排）；单步实现留在各自模块 |

## 8. 自测与激活记录（2026-11-08）

- `run_section.py --section 3.1 --mode dry` / `--section 2.4 --mode dry`：导航、任务点、题型、CDP 截图均通过。
- `queue_daemon.py --self-test`：dry 链路通过（state/queue/progress/账本契约）。
- **preflight 实测（12/59）**：
  - `preflight.py --json --sections 2.4,3.1,2.3` → `{"ok":true,"n":12,"total":59,"text":"已完成任务点: 12/59","sections":[{2.4:2},{3.1:2},{2.3:0}]}`
  - `run_section.py --preflight-only --sections 2.4,3.1,2.3` → 写 `runs/_preflight/20261108-005005/preflight.json`，退出码 0。
  - daemon 计划级验证：注入测试计划（2.2+2.3，均已完成）→ preflight 判定全部为 0 → **不执行任何节**，
    `last_result.all_done=true`、detail=「没有需要做的小节」，账本记 `plan_preflight + plan_completed(all_done)`。
  - 已完成节 skip：daemon 混合计划中 2.3（任务点 0）被跳过，`last_result.skipped=true`，未创建该节 run 目录、未改课程。
- **真实运行失败复盘（00:21:37 1.3）**：CDP 断连 0.8s 失败 rc=3；证据 `runs/1.3/20261108-002138/`；队列已备份并清空。
- **架构**：默认 Edge + 独立 profile；同窗口学习通 + 控制台；引擎选页只认学习通（控制台 127.0.0.1:8770 排除）。
- 单实例锁、UTF-8 BOM 日志、pause/resume 均已实测。
- 操作手册见 [RUNBOOK.md](RUNBOOK.md)。

## 9. 日志与证据（打包给用户独立复查）

### 9.1 看哪里
- **单节证据**：`engine/runs/<节>/<时间戳>/`（自包含）
  - `run.log`（人读，结构化：时间戳 级别 [节/模式] 内容）
  - `run.json`（机读：状态/阶段/成绩/进度/耗时/错误）
  - `progress.json`（最后进度）、`evidence/*.png`（题图/回填/成绩）、`evidence/video-samples.jsonl`
  - `pipeline/`（extracted/answers/pipeline-result）、`gate.json`、`fill.json`、`submit.json`、`verify.json`
- **全局索引**：`engine/logs/index.jsonl`（每次运行一行：时间/模式/节/状态/成绩/进度/耗时/run_dir/错误）
- **汇总报告**：`engine/logs/report.md` + `report.json`（每节最新状态/成绩/进度/耗时/证据链接 + 失败明细）
- **守护日志**：`engine/daemon.log`；**控制台日志**：`console/server.log`（两者分开）
- **启动器日志**：`dist/logs/launcher.log`（自动关闭/心跳失联原因）、`console/idle-close.log`

### 9.2 规范
- 全部 **UTF-8**；daemon.log 带 BOM（Windows 记事本可直接识别）。
- 日志行：`2026-11-08T01:13:48+0800 INFO [daemon plan=plan-... sec=2.4] ...`
- **脱敏**：URL 的 `enc/cpi/token/session` 等参数值自动打码为 `***`（日志与 JSON 证据落盘前统一处理）。
- **轮转**：日志超过 20MB 自动切分 `.1/.2/.3`（config.logging）。
- 不记录 cookie/token/密码。

### 9.3 一键命令
~~~powershell
# 环境自检（python/依赖/浏览器/CDP/登录/目录可写/磁盘/控制台/版本）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\selfcheck.py
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\selfcheck.py --json

# 汇总报告（生成 logs/report.md + report.json）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\collect_report.py

# 打包（排除 venv/profile/大截图，保留最近 N 天）
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\package.py --keep-days 7
~~~

### 9.4 打包清单
- 包含：`engine/`、`pipeline/`、`console/`（代码/静态/配置）、`dist/configs/`、`dist/docs/`（README/RUNBOOK/version.json）、最近 N 天证据。
- **排除**：`work/.venv`、`work/edge-profile`、`work/chrome-profile`、`__pycache__`、`*.pyc`、`.locks`、`console/exports`、早于 keep-days 的截图。
- 版本：`engine/version.json`（版本 + 各 schema_version）；依赖：`engine/requirements.txt`（仅记录，禁止联网安装）。
- 模型文件不入包（路径见 `pipeline/config.json`）。

## 10. 桌面启动器（.exe）/ 心跳 / 自动关闭

源码：`launcher/launcher.py`（tkinter，无 CMD）；自检：`launcher/selfcheck_gui.py`；
打包脚本：`launcher/build_exe.py`；配置：`configs/launcher.json`（dev 为 `launcher/launcher.json`）。

~~~json
{"port":8770,"idle_timeout_s":600,"auto_open_browser":true,
 "keep_daemon_after_close":false,"activity_poll_s":5,
 "heartbeat_grace_s":20,"heartbeat_interval_s":5,
 "on_browser_close":"stop_and_exit"}
~~~

- 双击（exe）：8770 无服务 -> 后台起 uvicorn（无 CMD）-> 就绪自动开默认浏览器；
  已有服务 -> 不重复启动，直接开浏览器并提示「已在运行」；单实例锁防重复双击。
- 页面每 5s `POST /api/heartbeat`（app.py 写 `console/last_heartbeat.json`），关闭时 `sendBeacon('/api/bye')`。
- 心跳超过 `heartbeat_grace_s`（默认 20s）无更新（或控制台返回 stopping）-> 启动器写
  `console/stop.json` 安全停止请求 -> 引擎按检查点收尾 -> 启动器停 uvicorn/daemon 并退出，原因写 `dist/logs/launcher.log`。
- **安全停止语义**（引擎 `run_section` + `queue_daemon`）：
  视频阶段→暂停并结束本节（不提交/不计完成）；题目捕获或作答阶段→到检查点停止，绝不回填/提交；
  回填后提交前→停止不提交；**已点提交→等本次成绩出来再停**；state 写 `stopped`，证据保留，队列计划标 cancelled。
- 任务运行中不会因「闲置」退出；但浏览器关闭是明确信号，仍走上述安全停止。
- 窗口显示状态/倒计时（「闲置 xx 秒后自动关闭」）+ 按钮「保持运行」重置计时。
- 构建（需 PyInstaller）：
~~~powershell
.\work\.venv\Scripts\python.exe -m pip install pyinstaller     # 需用户授权
.\work\.venv\Scripts\python.exe .\work\stage-d\launcher\build_exe.py
# 产物：dist/学习通自动化.exe、dist/自检.exe + dist/console|engine|configs|docs
~~~
  当前会话中 `pip install pyinstaller` 被自动审查拒绝，故暂以 **pythonw + VBS**
  （`dist/启动学习通自动化.vbs`、`运行自检.vbs`）实现无 CMD 启动；exe 构建命令已就绪。
- **exe 与 bat/vbs 并存时默认用 exe**；VBS 仅作为未装 PyInstaller 的过渡入口。


## 11. 跳过答题（skip_quiz）

- 配置：engine/config.json → answer（enabled=true 表示答题模块启用；skip_quiz 默认 false）。
  ~~~json
  "answer": {"enabled": true, "skip_quiz": false}
  ~~~
- 三种开启方式：
  1) 改 engine/config.json 的 answer.skip_quiz=true；
  2) run_section 命令行加 --skip-quiz；
  3) daemon 启动读取 config.answer.skip_quiz（热加载，下个计划生效）。
- 行为：视频照常播放/已计入则跳过；随后**不做章节测验、不回填、不提交**，该节 run.json 记
  status=skipped_quiz、skipped_quiz=true，daemon 账本记 section_skipped_quiz，状态继续下一节。
- 默认 false（保持原行为）。关闭：skip_quiz=false 或去掉 --skip-quiz。


## 12. 2026-11-15 行为变更（重要）

### 12.1 取消「预检/课程目录数字跳过」，改为节内实时检测
- **不再**用课程目录/预检数字判断 already_done；preflight 仅用于控制台进度显示，**不得用于跳过**。
- 每节流程：先导航进入该节，再在该节页面读取实时任务点数（视频/测验未完成数）：
  - 读到明确数字 → 正常处理；
  - 读到 **0** → 本节已完成，记 already_done（仅限节内实时明确 0，且不改课程/不提交）；
  - 读不到（None） → run.json status=**unknown**，不跳过、不默认 0；daemon 记录 section_unknown；
    连续 **2 节 unknown** 触发安全阀：daemon 写 state=error（code=TASK_COUNT_UNKNOWN）并停止。
- 视频阶段：5s 轮询播放进度/marker/任务点数（采样含 side）；完成后实时确认 2→1（读不到 → unknown，不跳过）。
- 测验阶段：提交后实时确认任务点 1→0（读不到记 warning，以成绩+课程进度佐证）；成绩/attempt/三对照仍按原闸门。
- 控制台「刷新进度」继续只影响显示，不影响跳过判定。

### 12.2 pipeline 模型后端可切换 local / api
- pipeline/config.json：
  - backend = "local"（默认，不发起任何网络请求）｜"api"
  - api.base_url / api.api_key_env（默认 MAAS_API_KEY）/ api.credentials_file /
    api.model_vision（默认 qwen3-vl-plus）/ api.model_text（默认 qwen3.8-flash）/
    api.timeout_s / api.fallback_local（默认 true，单题失败回退本地 llama_cpp）
- 密钥来源：优先环境变量 api_key_env；否则读 credentials_file 的 refs.<KEY>（如 E:/DSH/data/home/.credentials.yaml）；
  **密钥不写入代码/日志/证据**（api_meta 只记录 key_source=env|credentials_file）。
- A 段（识图）用 model_vision；C 段（文本）用 model_text。backend=api 时输出 engine.backend=openai_compatible_api（或 mixed_api_local）。
- 离线 1.4 对比：API 5/5 与本地 100 分答案一致；A 平均 2.40s/题、C 4.08s/题；
  报告见 engine/logs/api-vs-local-1.4.md / .json。
