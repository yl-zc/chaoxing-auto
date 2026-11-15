# A→C 两段式流水线（可复用入口）

> 目的：把 1.2 验证过的「视觉抽取 + 文本推理」固化为独立进程入口，供后续章节批量复用。
> 纪律：**不碰浏览器、不提交**；本目录只做「图片 → 答案 JSON + 闸门判定」。

## 1. 为什么两段式

| 方案 | 单题耗时 | 准确率（1.2 实测） | 说明 |
|---|---|---|---|
| 一次视觉作答 | 28–32 s | 5/5 | 1.2 的 A 策略 |
| 多选逐项判断 | 98–109 s/题 | **3/5** | **否决**：全选倾向 + 自检幻觉 |
| **两段式 A→C** | 视觉 ~25 s + 文本 **3–8 s** | **5/5** | 视觉只转写，推理用纯文本 |

两段式的额外好处：A 阶段结束即退出，释放约 4 GB 内存；C 阶段不加载视觉投影器，更快更省。

## 2. 目录

    pipeline/
    ├── common.py            # schema 校验 / 闸门 / JSON IO（唯一事实源）
    ├── A_extract.py         # 阶段A：读图 -> 纯文本题目 JSON（进程退出）
    ├── C_answer.py          # 阶段C：纯文本推理 -> 答案 JSON（进程退出）
    ├── run_pipeline.py      # 编排：A -> 校验 -> C -> 校验 -> 闸门
    ├── make_manifest.py     # 由 q*.png 目录生成 manifest
    ├── selftest.py          # 离线 schema 自测（12 项，不加载模型）
    ├── validate_real_1_2.py # 用 1.2 真实数据端到端校验
    └── fixtures/            # 校验用固定样本

## 3. 用法

    $py = "D:\pyai\pych\.venv\Scripts\python.exe"

    # 1) 生成 manifest（题图 q1..qN.png）
    & $py make_manifest.py --dir <evidence目录> --out manifest.json --session 2.3 --meta quiz.json

    # 2) 跑两段式（自动校验 + 闸门；不提交）
    & $py run_pipeline.py --manifest manifest.json --outdir out --threshold 0.8

退出码：**0** 通过闸门（可回填）｜**3** schema 校验失败（不可用）｜**4** 闸门未过（缺题 / unknown / 低置信度 → 不提交）

产物（outdir 下）：extracted.json（A）、answers.json（C）、pipeline-result.json（耗时/校验/闸门）

## 4. schema

**extracted.json**

    {"schema":"chaoxing-quiz-extract/1","source":{...},"engine":{...},
     "questions":[{"index":1,"type":"单选题","stem":"...","options":[{"key":"A","text":"..."}]}],
     "validation":{"pass":true,"errors":[]}}

**answers.json**

    {"schema":"chaoxing-answer/1","engine":{...},
     "answers":[{"index":1,"type":"单选题","value":["A"],"confidence":0.9,"reason":"..."}],
     "unknown":[], "validation":{"pass":true,"errors":[]}}

## 5. 校验规则（common.py）

**抽取（extract）**：schema 版本；questions 非空；index 唯一且 ≥1；type 属于五类；stem 非空；单选/多选 ≥2 选项；判断题恰好 2 选项；选项 key 非空且不重复。

**答案（answer）**：schema 版本；answers 为列表；value 为非空字符串数组；confidence 在 [0,1]；index 不重复；**覆盖性**：expected 中每题必须出现在 answers 或 unknown，且不得有超范围 index。

**选项一致性**：答案字母必须落在该题选项 key 集合内；判断题允许 true/false/A/B/对/错。

**闸门（gate）**：无 unknown 且最小 confidence ≥ 阈值（默认 0.8）；任一不满足 → NOT-READY，**不得提交**。

## 6. 自测结果（2026-11-07 22:4x，离线执行）

    SELFTEST ALL PASS (12 checks)
    REAL-DATA VALIDATION: PASS   （1.2 真实 5 题，gate min_conf=0.9）

覆盖用例：正常样本、判断题 3 选项、缺题、字母越界、低置信度、unknown、置信度 >1、重复 index、schema 版本错误。

## 7. 与浏览器侧的分工

| 阶段 | 由谁做 | 是否碰浏览器 |
|---|---|---|
| 截图 / 裁剪 | 浏览器侧脚本（整视口快照 + PIL） | 是（CDP 只读） |
| **A 视觉抽取** | 本目录 A_extract.py | 否 |
| **C 文本推理** | 本目录 C_answer.py | 否 |
| 回填 / 提交 | 浏览器侧脚本（aria-checked 三对照 + 顶层 #popok） | 是（**唯一写操作**） |

> 本目录任何脚本都**不加载浏览器、不点提交**；提交仍由浏览器侧脚本在闸门 PASS 后执行，且只执行一次。
