# RUNBOOK —— 学习通自动化引擎（M2）操作手册

更新时间：2026-11-08 00:35 (+08)
当前形态：默认 **Edge**（独立 profile `work/edge-profile`） + CDP 9222；引擎 daemon 常驻；控制台同窗口第二标签。

---

## 1. 一键自检（只读）

~~~powershell
# 浏览器/CDP/页面状态
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --status --pids

# 守护状态（state.json）
Get-Content -Raw -Encoding UTF8 .\work\stage-d\console\state.json

# 守护日志（UTF-8 BOM，可直接用记事本/VS Code 打开）
Get-Content -Encoding UTF8 .\work\stage-d\engine\daemon.log -Tail 20

# 队列与账本
Get-Content -Encoding UTF8 .\work\stage-d\console\plans\queue.jsonl
Get-Content -Encoding UTF8 .\work\stage-d\engine\queue_state.jsonl -Tail 20
~~~

正常空闲态（浏览器未登录时）：

~~~json
{"schema_version":"engine-state/1","state":"idle","current":null,"last_result":null,
 "queue":[],"paused":false,"daemon_pid":22296,
 "detail":"学习通浏览器未登录，请在浏览器里扫码登录；队列为空","updated_at":"..."}
~~~

正常空闲态（已登录、无计划）：`detail="队列为空"`。

---

## 2. 日常启动顺序

1. **浏览器**（若未开）：按配置启动 Edge（同窗口双标签：学习通 + 控制台）
   ~~~powershell
   .\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --print-cmd   # 打印由 config 生成的启动参数
   .\work\.venv\Scripts\python.exe .\work\stage-d\engine\browser.py --launch       # 或直接启动（无窗口时）
   ~~~
   - 人工等价命令（Edge 首次会弹「同步确认」，关掉即可）：
     `msedge.exe --user-data-dir=work\edge-profile --remote-debugging-port=9222 --no-first-run --no-default-browser-check https://i.chaoxing.com/base http://127.0.0.1:8770/`
2. **用户在 Edge 里扫码登录学习通**（`https://i.chaoxing.com/base` 标签）。
3. **daemon**（单 PID 推荐用基础解释器，daemon 只用标准库）：
   ~~~powershell
   C:\Users\<user>\AppData\Local\Programs\Python\Python313\python.exe -X utf8 .\work\stage-d\engine\queue_daemon.py
   ~~~
   或用 `work/.venv/Scripts/python.exe` 启动（会显示「launcher + 真实解释器」两个 PID，同一进程树，非双实例）。
4. **控制台**：`http://127.0.0.1:8770/`（与学习通同一 Edge 窗口的第二标签）。
5. 用户在控制台点「开始」→ 控制台写 `queue.jsonl` → daemon 取计划 → 逐节 real。

---

## 2.1 开始前进度核对（preflight，只读）

控制台「刷新进度」/ 引擎计划开始前统一调用：

~~~powershell
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\preflight.py --json
.\work\.venv\Scripts\python.exe .\work\stage-d\engine\preflight.py --json --sections 2.4,3.1
~~~
- stdout 只输出一行 JSON：成功 `{"ok":true,"logged_in":true,"n":12,"total":59,"text":"已完成任务点: 12/59","at":"ISO","source":"course_page","sections":[...]}`；
  失败 `{"ok":false,"code":"BROWSER_OFFLINE|BROWSER_LOGIN_REQUIRED|...","message":"中文提示","retryable":true}`。
- 只读：不写 state/queue、不播放/不答题/不提交；默认读完恢复原页面。
- daemon 计划开始前自动执行，规则：页面进度优先并写 `state.progress{n,total,source:"page"}`；
  任务点=0 的节 skip（`last_result.skipped=true`）；计划内全为 0 → `last_result.all_done=true` + 「没有需要做的小节」；
  证据在 `runs/_preflight/<plan_id>-<ts>/preflight.json`。

## 3. 错误态与处置

state=error 时同时有 `error` 与 `last_error`（code/message/retryable/at/detail）：

| code | 含义 | retryable | 引擎行为 | 处置 |
|---|---|---|---|---|
| `BROWSER_OFFLINE` | CDP 9222 不可达 | true | 不消费计划，继续守候 | 打开 Edge（见 §2），无需重启 daemon |
| `BROWSER_LOGIN_REQUIRED` | 检测到登录页/未登录 | true | 不消费计划，继续守候 | 用户在 Edge 扫码登录后自动继续 |
| `SECTION_FAILED` | 某一节执行失败（闸门/回填/视频/页面异常） | false | 失败即停；该计划记 plan_failed；**保留 error 不回 idle** | 看 `run_dir` 证据；修好后在控制台重新点开始（生成新计划） |
| `QUIZ_ONLY_UNSUPPORTED` | 计划是只做测验 | false | 记 plan_failed，停 | 控制台改选「完整流程」 |

示例（浏览器未登录 + 有待执行计划）：

~~~json
{"state":"error","current":null,"queue":[{"section":"2.4","plan_id":"...","status":"queued"}],
 "error":{"code":"BROWSER_LOGIN_REQUIRED","message":"学习通浏览器未登录，请在浏览器里扫码登录",
          "retryable":true,"at":"...","detail":"https://passport2.chaoxing.com/login..."},
 "last_error":{"code":"BROWSER_LOGIN_REQUIRED","message":"同上","retryable":true}}
~~~

暂停/继续：控制台写 `console/state.cmd.json`（`{"action":"pause|resume","ts":<epoch>}`）。
引擎只读不删，**节间生效**（正在跑的那一节会跑完）。

---

## 4. 失败证据在哪里

- 单节证据：`work/stage-d/engine/runs/<section>/<YYYYmmdd-HHMMSS>/`
  - `run.json`（最终状态/阶段/耗时/score/progress_total）
  - `run.log`、`child-console.log`（完整日志）
  - `progress.json`（最后进度）
  - `pipeline/`（A→C 产物）、`gate.json`、`fill.json`、`submit.json`、`verify.json`
  - `evidence/*.png`（题图/回填/成绩截图）、`evidence/video-samples.jsonl`
- 队列账本：`engine/queue_state.jsonl`（plan_started/section_*/plan_*）
- 队列备份：`console/plans/queue.jsonl.bak`（历史测试计划）、`queue.jsonl.bak-incident-20261108-002137`（1.3 失败计划）
- 经典失败案例：`runs/1.3/20261108-002138/`（CDP 断连，0.8s 失败，未提交任何题目）

---

## 5. 换浏览器（只改 config）

`engine/config.json` → `browser`：

~~~json
"browser": {
  "kind": "edge",
  "exe_path": "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "fallback": {"kind": "chrome", "exe_path": "C:/Program Files/Google/Chrome/Application/chrome.exe"},
  "user_data_dir": "work/edge-profile",
  "port": 9222,
  "login_url": "https://i.chaoxing.com/base",
  "console_url": "http://127.0.0.1:8770/"
}
~~~

- primary 不存在时自动用 fallback（Edge→Chrome）；也可直接改 `kind/exe_path`。
- **必须独立 profile**：只有非默认 profile + `--remote-debugging-port` 才能开 CDP；不要用用户日常 profile。
- 版本/路径变了只改这里，引擎代码不写死浏览器路径。
- 登录后若停在「学习空间」：引擎按 `config.nav.course_name`（默认「形势与政策」）自动点进课程，再进目标小节；
  若课程名不符，改 config.nav.course_name 即可。

---

## 6. 单端登录与旧浏览器清理

- 同一学习通账号在多个浏览器登录会互相踢下线；建议只保留 **automation Edge** 登录。
- **Edge CDP 可用且已登录后**，daemon 首次就绪时会调用
  `browser.py --close-legacy`：只关闭「占用同一 CDP 端口、但 profile 不是 `work/edge-profile`」的旧自动化浏览器
  （按 `--type=` 过滤，只杀顶层进程；不碰用户日常浏览器与其它项目如 9223 headless）。
- 手动检查：`browser.py --close-legacy --dry-run`。

---

## 7. 单实例与 PID 说明

- daemon：`engine/.locks/daemon.lock`；第二实例启动会提示并退出码 5。
- 单节：`engine/.locks/section.lock`；同一时间只允许一节在跑。
- `work/.venv/Scripts/python.exe` 是 **Python Launcher**，会再拉起真实解释器 → 系统看到两个 PID 属正常；
  用基础解释器跑 daemon 则只有 1 个 PID（daemon 只用标准库）。
- 日志编码：daemon.log 为 UTF-8 BOM（Windows 工具可直接识别）；若用旧文件，引擎启动时会自动补 BOM。

---

## 8. 纪律红线（不可破）

1. 禁止 SetForegroundWindow / ShowWindow / bring_to_front / activateTarget —— 不抢前台。
2. 禁止硬编码带 `enc` 的 URL；导航只点页面目录/页面内链接。
3. 提交按钮/确认按钮各 **1 次**；任何异常不得重复提交；成绩 <100 不重试。
4. 闸门（min_conf≥0.8、100% 覆盖、无 unknown）不过 → 不回填、不提交。
5. 同一时间只允许一个 daemon + 一节执行（文件锁）。
6. 引擎只操作学习通标签（URL allow=chaoxing.com；deny=127.0.0.1:8770 等），控制台标签永不操作。
7. 失败即停：写 state=error + 证据，不静默回 idle、不自动重试同一计划。

## 9. 日志/证据/报告（复查入口）

- 单节证据：`engine/runs/<节>/<时间戳>/`；全局索引：`engine/logs/index.jsonl`；
  汇总报告：`engine/logs/report.md`（`tools/collect_report.py` 生成）。
- 环境自检：`tools/selfcheck.py --json`（python/依赖/浏览器/CDP/登录/目录/磁盘/控制台）。
- 日志 UTF-8、自动脱敏（enc/cpi/token -> ***）、>20MB 轮转、daemon.log 与 console 日志分开。
- 打包：`tools/package.py --keep-days 7`（排除 venv/profile/大截图；产物 `dist/chaoxing-engine-*.zip`）。

## 10. 桌面启动器 / 心跳 / 自动关闭

- 配置：`configs/launcher.json`（launcher/launcher.json 为 dev 默认）。
- 心跳：页面每 5s POST /api/heartbeat -> `console/last_heartbeat.json`；关闭 sendBeacon /api/bye。
- 心跳失联（> heartbeat_grace_s，默认 20s）或控制台判定 stopping -> 启动器写 `console/stop.json`，
  引擎安全停止（视频暂停/答题不提交/提交中等完成），随后停 uvicorn/daemon、退出并写
  `dist/logs/launcher.log` / `console/idle-close.log`。
- 闲置关闭：`idle_timeout_s`（默认 600s）无请求且引擎 idle 且队列空 -> 优雅退出。
- 构建 exe：`launcher/build_exe.py`（需 PyInstaller）；无 PyInstaller 时用 `dist/启动学习通自动化.vbs`
  （pythonw，无 CMD 窗口）。


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
