# 学习通自动化控制台（AI 对话优先 · M2）

浅色界面、面向没有电脑基础的使用者：默认打开「AI 对话」，直接说想做到哪里，助手给出计划卡片；右侧常驻小面板显示课程进度、当前项、下一项和系统状态。总览、任务板、题目与文档、设置作为辅助页面。

## 启动

    cd work\stage-d\console
    ..\..\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8770

访问 http://127.0.0.1:8770 （仅绑定 127.0.0.1）。也可以双击 start-console.cmd。

## 页面

- **AI 对话（默认首页）**：消息气泡、快捷按钮「继续下一节 / 做完这一章 / 全部做完」、输入框；助手用通俗中文回复，可展示计划卡片（要做什么 / 共几节 / 预计几分钟 / 范围，以及 [开始] [改范围] [取消]）。
- **右侧常驻小面板**：课程进度（默认 12/59，以课程页真实读数为准）、当前项（正在进行 + 环节文案 + 进度百分比 / 已完成 + 成绩）、下一项（待开始）、系统状态（运行正常 / 已暂停）＋「暂停 / 继续」按钮＋「刷新进度」按钮；窄屏自动收起为「进度」抽屉。
- **总览 / 任务板 / 题目与文档 / 设置**：辅助页；技术信息只出现在「设置 → 高级设置」。

## 接口与消费契约（前端与后台解耦）

界面分开取数、互不依赖，任一接口失败只影响对应区域：

| 路径 | 消费方 | 说明 |
|---|---|---|
| GET /api/engine | 右侧面板、对话消息（每 2 秒） | 轻量：只读 state.json + state.cmd.json，不扫证据；返回 ok / engine / system |
| GET /api/status | 进度数字、总览（每 5 秒） | 引擎字段 + 只读证据摘要；引擎读取失败或证据采集失败都不整体报错 |
| GET /api/tasks | 任务板、总览「最近完成」、面板兜底（每 5 秒） | 只读证据；每行补 title/status_text/score_text；失败返回空列表 |
| GET /api/section/<节> | 章节详情抽屉（点开才取） | 截图、题目、成绩，独立接口 |
| POST /api/state/command | 面板「暂停 / 继续」 | body 里 action 取 pause 或 resume，写 state.cmd.json，回「已暂停 / 已继续」 |
| GET /api/preflight | 面板刷新按钮的缓存 | 返回最近一次预检结果（不触发读页面） |
| POST /api/preflight | 面板「刷新进度」、每次「开始」前 | 调用引擎 preflight.py 读课程页真实 N/59；失败返回中文提示，不阻塞其它接口 |
| GET /api/friendly | 兼容旧调用 | 聚合接口（界面已不依赖） |

对话/计划接口（M1）：

| 路径 | 说明 |
|---|---|
| POST /api/chat | 规则解析一句话 → 结构化计划 JSON（不调用浏览器、不执行） |
| POST /api/plan/start | 先自动预检一次课程页真实进度，再把计划写入 plans/ 并回显「已加入队列」；浏览器未登录/离线时返回中文提示+retryable，不阻塞其它功能 |
| GET /api/plans | 最近加入队列的计划 |
| POST /api/import-answers / POST /api/export/questions | 答案导入 / 题目 Word 导出（辅助页） |

### /api/engine 的 engine 稳定字段

    schema_version  原样透传（当前 engine-state/1）
    state           running | idle | paused | error
    current         运行中：section / title / stage / progress / detail
                    stage 取 video | quiz | submit；progress 统一成 0-100 整数（引擎可发 0.35）
    last_result     section / title / status（completed 或 failed）/ score / at
                    / progress_total（完成后的任务点数，进度数字优先取它）
                    / next_section（下一节，如 1.4）/ skipped（该节是否因已完成被跳过）
    queue           [{section, title}, ...]；面板「下一项」取第一项
    page_progress   引擎预检写入的页面真实进度 {n, total, source}
    last_skipped    最近一次跳过事件 {section, at}（用于对话提示「你之前已经完成了，已跳过」）
    updated_at      可选，展示用

缺失字段、文件不存在、JSON 半截都走兜底文案，不报错。界面只消费以上字段，不解析其它内容。

### 执行引擎状态文件（引擎维护，控制台只读）

work/stage-d/console/state.json（示例）：

    {
      "schema_version": "engine-state/1",
      "state": "running",
      "current": {"section":"2.4","title":"…","stage":"video","progress":0.35,"detail":"正在播放视频 3:12/9:00"},
      "last_result": {"section":"2.3","title":"…","status":"completed","score":60,"at":"…"},
      "queue": [{"section":"2.4","title":"…","status":"queued"}],
      "updated_at": "…"
    }

- 引擎 state 只会是 idle / running / paused / error；idle、paused、error 时 current 为 null（面板改显示 last_result，没有则「当前无进行中任务」）。
- 控制台不写 state.json。

### 暂停 / 继续命令文件

state.cmd.json（控制台写，引擎只读；控制台只写不删）：

    {"action":"pause","at":"2026-11-08T00:12:14","ts":1794067934.69,"source":"chat-console"}

- M2 先只改状态：点「暂停」立即显示「已暂停」并追加对话消息，不真正中断引擎；真正暂停由引擎的 queue_daemon 在「节与节之间」消费（按 ts 顺序、旧命令不回溯）。
- 命令文件优先于 state.json 的 paused 状态：用户最后一次「暂停 / 继续」决定面板显示。

### 预检（刷新进度）契约

控制台调用引擎只读脚本（stdout 最后一行一个 JSON，日志走 stderr）：

    work\.venv\Scripts\python.exe work/stage-d/engine/preflight.py --json

    成功：{"ok":true,"logged_in":true,"n":12,"total":59,"text":"已完成任务点 12/59","source":"course_page","at":"ISO"}
    失败：{"ok":false,"code":"BROWSER_LOGIN_REQUIRED|BROWSER_OFFLINE|…","message":"中文提示","retryable":true}

- 只读：只导航到课程页/目录读文字，不播放、不答题、不提交；控制台以 UTF-8 解析子进程输出。
- 预检失败（浏览器未登录/未连接/超时）时面板与对话显示中文提示 +「重试」，其它页面与接口照常工作。
- 进度数字优先级：课程页预检 N（或引擎写入的 page_progress）> last_result.progress_total > 证据统计；取最大值避免回退。

### 页面心跳 / 关闭即停止

- 前端每 5 秒 POST /api/heartbeat（页面隐藏/后台也发）；页面关闭或刷新时用 navigator.sendBeacon('/api/bye', JSON Blob) 告别。心跳不刷新界面、不触发对话消息，也不写 last_activity（避免日志噪音）。
- 后端心跳写 console/last_heartbeat.json（供引擎启动器判失联）；bye 写 bye.jsonl 并进入 2 秒宽限期（刷新/重连的心跳会取消）。
- 心跳超过 15 秒未到（或 bye 宽限期结束且页面没回来）：记录 console/stop-request.json（action=stop / reason / ts / at）并写 state.cmd.json action="stop"（引擎按 ts 消费，idle 立即退出，running 安全收尾后退出），同时 /api/engine 顶层带 stopping=true + stop_request。
- 界面收到 stopping / 引擎 state=stopped 时，对话提示「浏览器已关闭/即将停止，本任务已结束」，系统状态显示「已停止」；页面首页与设置页有一行小字「关闭这个页面会自动结束正在运行的任务并关闭程序」。
- 测试用环境变量：CONSOLE_HEARTBEAT_TIMEOUT_S / CONSOLE_HEARTBEAT_GRACE_S / CONSOLE_STOP_FILE / CONSOLE_LAST_HEARTBEAT_FILE / CONSOLE_STATE_CMD_FILE（生产不使用）。

## 前端行为

- 分开轮询：/api/engine 2 秒；/api/status、/api/tasks 5 秒；章节详情点开才取。
- 刷新进度：面板「刷新进度」按钮随时可点；每次「开始」前自动预检一次；近 30 秒无操作且页面在前台时每 10 秒检查一次（仅引擎空闲时）自动轻量刷新。
- 对话消息只在引擎状态变化时追加（state / current.section / current.stage / last_result / 暂停状态 / 最近跳过事件），不轮询全量证据。例：
  - 「开始做 2.4：正在播放视频…」
  - 「2.4：正在答题…」
  - 「2.3 已完成，成绩 100 分，进度 11/59」
  - 「已暂停。需要的时候点「继续」。」/「继续了，接着做。」
  - 「1.4 你之前已经完成了，已跳过」（引擎 last_result.skipped 或 last_skipped 事件）
  - 预检失败：「学习通还没登录，请在浏览器里扫码登录后重试」+「重试」
- 文案映射集中在 static/app.js 顶部 TEXT 表（stage 到中文、state 到中文状态、兜底文案），改措辞只改这一处。
- 章节数据全部来自接口，前端不写死任何章节。

## 计划文件

    plans/plan-YYYYmmdd-HHMMSS-xxxxxx.json   计划快照
    plans/queue.jsonl                        加入队列流水

M1/M2 只记录计划，不真正驱动自动执行（执行引擎另有 queue_daemon）。

## 数据来源（只读）

- 进度数字：扫描 work/stage-d/**/*.json 里的「已完成任务点 N/59」，取最大值
- 任务状态：各节目录的 submit / video / status / verify JSON
- 引擎状态：state.json（引擎维护）；暂停命令：state.cmd.json（控制台维护）
- 页面真实进度：引擎 preflight.py 只读课程页读到的 N/59，缓存到 console/preflight.json 供界面优先显示

## 边界

- 只读数据：不触发自动化、不写课堂状态、不碰浏览器窗口
- 无窗口/焦点 API；服务只监听 127.0.0.1
- 主界面不展示文件、路径、日志、JSON 等字样
- 联调/测试用环境变量（生产不使用）：CONSOLE_STATE_FILE（模拟 state.json）、CONSOLE_PLANS_DIR（隔离计划目录）、CONSOLE_PREFLIGHT_FILE（隔离预检缓存）
