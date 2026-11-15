#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习通自动化控制台 M1 —— FastAPI + uvicorn 后端
复用 server.py 的只读数据聚合；提供 SSE、控制指令、答案导入、题目 Word 导出。
启动: uvicorn app:app --host 127.0.0.1 --port 8770
"""
import asyncio
import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as core

from fastapi import FastAPI, Body, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
STAGE_D = os.path.dirname(HERE)
CONSOLE = HERE
WORK = os.path.dirname(STAGE_D)
PROJECT_ROOT = os.path.dirname(WORK)
# 便携包：包根/<console> + 包根/runtime/python 存在时，PROJECT_ROOT/WORK 都以包根为准
if os.path.isdir(os.path.join(STAGE_D, "runtime", "python")):
    PROJECT_ROOT = STAGE_D
    WORK = STAGE_D
ENGINE_DIR = os.path.join(STAGE_D, "engine")
ENGINE_LEDGER = os.path.join(ENGINE_DIR, "queue_state.jsonl")
PREFLIGHT_FILE = os.environ.get("CONSOLE_PREFLIGHT_FILE") or os.path.join(CONSOLE, "preflight.json")
STOP_FILE = os.environ.get("CONSOLE_STOP_FILE") or os.path.join(CONSOLE, "stop-request.json")
HEARTBEAT_TIMEOUT = float(os.environ.get("CONSOLE_HEARTBEAT_TIMEOUT_S") or 15)
HEARTBEAT_GRACE = float(os.environ.get("CONSOLE_HEARTBEAT_GRACE_S") or 2.0)
VENV_PY = os.path.join(WORK, ".venv", "Scripts", "python.exe")
# 便携包：优先用包内 runtime/python/python.exe（preflight 等子进程）
if os.path.isfile(os.path.join(STAGE_D, "runtime", "python", "python.exe")):
    VENV_PY = os.path.join(STAGE_D, "runtime", "python", "python.exe")
CONTROL_LOG = os.path.join(CONSOLE, "control.jsonl")
EXPORT_DIR = os.path.join(CONSOLE, "exports")
# 执行引擎维护（控制台只读）；联调时可用环境变量 CONSOLE_STATE_FILE 指向模拟文件（不写正式状态）
STATE_FILE = os.environ.get("CONSOLE_STATE_FILE") or os.path.join(CONSOLE, "state.json")
STATE_CMD_FILE = os.environ.get("CONSOLE_STATE_CMD_FILE") or os.path.join(CONSOLE, "state.cmd.json")  # 控制台写：暂停/继续/停止命令
LAST_HEARTBEAT_FILE = os.environ.get("CONSOLE_LAST_HEARTBEAT_FILE") or os.path.join(CONSOLE, "last_heartbeat.json")
PLANS_DIR = os.environ.get("CONSOLE_PLANS_DIR") or os.path.join(CONSOLE, "plans")
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(PLANS_DIR, exist_ok=True)

# CPU 采样线程（复用 core 的 Win32 采样）
def _cpu_sampler():
    try:
        core.cpu_sample()
    except Exception:
        pass
    while True:
        time.sleep(1.0)
        try:
            core.cpu_sample()
        except Exception:
            pass

threading.Thread(target=_cpu_sampler, daemon=True).start()

app = FastAPI(title="学习通自动化控制台", version="1.0-M1")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ---------------- 活动监控（供桌面启动器判断闲置） ----------------
LAST_ACTIVITY = os.path.join(HERE, "last_activity.json")


@app.middleware("http")
async def _record_activity(request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path
        if not path.startswith("/static") and path not in ("/api/heartbeat", "/api/bye"):
            rec = {"ts": time.time(),
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "path": path[:200],
                   "ua": (request.headers.get("user-agent") or "")[:200]}
            tmp = LAST_ACTIVITY + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, LAST_ACTIVITY)
    except Exception:
        pass
    return response

HEARTBEAT_FILE = LAST_HEARTBEAT_FILE
BYE_LOG = os.path.join(HERE, "bye.jsonl")
STOP_REQUEST = STOP_FILE


@app.post("/api/heartbeat")
def api_heartbeat(body: dict = Body(default={})):
    """页面每 5s 心跳（页面隐藏/后台也继续）。只更新时间戳，不刷新界面、不写日志。"""
    now = time.time()
    with HB_LOCK:
        HB_STATE["last"] = now
        if HB_STATE["pending"] and not HB_STATE["stopping"]:
            HB_STATE["pending"] = None       # 刷新/重连：取消待停止
        if HB_STATE["stopping"] and now - HB_STATE["stopping_at"] < 5.0:
            HB_STATE["stopping"] = False      # 页面很快回来，撤销尚未执行的停止
            HB_STATE["stopping_at"] = 0.0
            HB_STATE["stop_request"] = None
        stopping = HB_STATE["stopping"]
    _write_heartbeat_file(stopping=stopping)
    return {"ok": True, "stopping": stopping}


@app.post("/api/bye")
async def api_bye(request: Request):
    """页面关闭/刷新：sendBeacon 告别。给短宽限期；页面没回来才记录停止请求。"""
    reason, ua = "bye", ""
    try:
        raw = await request.body()
        if raw:
            try:
                j = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(j, dict):
                    reason = str(j.get("reason") or "bye")[:80]
                    ua = str(j.get("ua") or "")[:200]
            except Exception:
                reason = raw.decode("utf-8", "replace")[:80] or "bye"
    except Exception:
        pass
    try:
        with open(BYE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "reason": reason, "ua": ua}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    with HB_LOCK:
        HB_STATE["pending"] = {"at": time.time() + HEARTBEAT_GRACE, "reason": "page_closed"}
    return {"ok": True, "grace_s": HEARTBEAT_GRACE}


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())

# ---------------- 只读 API ----------------
@app.get("/api/status")
def api_status():
    """引擎状态 + 只读证据摘要。引擎字段独立读取；证据采集失败只影响对应字段，不整体报错。"""
    engine = _engine_snapshot()
    system = _system_state(engine)
    try:
        st = core.collect_status()
    except Exception:
        st = {}
    st["engine"] = engine
    st["system"] = system
    st["progress"] = _effective_progress(st, engine)
    hb = _hb_snapshot()
    st["stopping"] = hb["stopping"]
    st["stop_request"] = hb["stop_request"]
    return st

@app.get("/api/engine")
def api_engine():
    """轻量引擎状态：只读 state.json + state.cmd.json，不扫证据，供界面每 2 秒轮询。
    state_source=env 表示联调时用了 CONSOLE_STATE_FILE；生产应为 state.json。"""
    engine = _engine_snapshot()
    hb = _hb_snapshot()
    return {"ok": True, "engine": engine, "system": _system_state(engine),
            "state_source": "env" if os.environ.get("CONSOLE_STATE_FILE") else "state.json",
            "stopping": hb["stopping"], "stop_request": hb["stop_request"],
            "last_heartbeat": hb["last_heartbeat"]}

@app.get("/api/preflight")
def api_preflight_get():
    """最近一次预检缓存（不触发读取课程页）。"""
    pf = _read_preflight()
    return {"ok": bool(pf and pf.get("ok")), "preflight": pf}

@app.post("/api/preflight")
def api_preflight_post():
    """手动刷新进度：调用引擎 preflight-only 读课程页真实 N/59。"""
    res = _run_preflight()
    return {"ok": bool(res.get("ok")), "preflight": res}

@app.get("/api/tasks")
def api_tasks():
    """任务板数据（只读证据），与引擎状态互不依赖；读取失败返回空列表而不是报错。"""
    try:
        res = core.collect_tasks()
    except Exception:
        return {"sections": [], "summary": {}, "error": "任务信息暂时读不到"}
    # 引擎刚完成的节（证据目录可能还没落地）：用 state.json 的 last_result 覆盖任务板状态
    engine = _engine_snapshot()
    lr = (engine or {}).get("last_result") or {}
    sections = res.get("sections") or []
    if lr.get("section") and lr.get("status") == "completed":
        for r in sections:
            if r.get("section") == lr["section"]:
                r["status"] = "completed"
                r["score"] = lr.get("score")
                r["submitted"] = "引擎已完成"
                r["updated_at"] = lr.get("at") or r.get("updated_at")
                break
    for r in sections:
        sec = r.get("section")
        r["title"] = section_title(sec or "")
        r["status_text"] = STATUS_TEXT.get(r.get("status"), "待开始")
        sc = r.get("score")
        r["score_text"] = ("%s 分" % sc) if sc not in (None, "") else ""
    res["summary"] = {
        "total": len(sections),
        "completed": len([r for r in sections if r.get("status") == "completed"]),
        "video_done": len([r for r in sections if r.get("status") == "video_done"]),
        "failed": len([r for r in sections if r.get("status") == "failed"]),
        "pending": len([r for r in sections if r.get("status") == "pending"]),
    }
    return res

@app.get("/api/evidence")
def api_evidence(section: str = ""):
    return core.collect_evidence(section)

@app.get("/api/log")
def api_log(tail: int = 200):
    return core.collect_log(max(10, min(tail, 2000)))

@app.get("/api/health")
def api_health():
    return {"ok": True, "framework": "fastapi", "version": app.version,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S")}

# ---------------- SSE ----------------
@app.get("/api/stream")
async def api_stream():
    async def gen():
        try:
            while True:
                try:
                    payload = json.dumps(core.collect_status(), ensure_ascii=False)
                except Exception as e:
                    payload = json.dumps({"error": str(e)[:200]}, ensure_ascii=False)
                yield "event: status\ndata: " + payload + "\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ---------------- 控制指令（只记录） ----------------
ACTIONS = {"pause", "resume", "stop", "start_section", "next_section", "note"}

@app.post("/api/control")
def api_control(body: dict = Body(...)):
    action = str(body.get("action") or "").strip()
    if action not in ACTIONS:
        return JSONResponse({"ok": False, "error": "不支持的指令"}, status_code=400)
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action,
           "section": body.get("section"), "params": body.get("params"),
           "note": (body.get("note") or "")[:300], "executed": False}
    with open(CONTROL_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if action in ("pause", "stop", "resume"):
        try:
            _write_state_command("resume" if action == "resume" else "pause", "control")
        except Exception:
            pass
    return {"ok": True, "queued": rec, "note": "已记录，当前版本不自动执行"}

@app.post("/api/state/command")
def api_state_command(body: dict = Body(...)):
    """暂停 / 继续：写 state.cmd.json（M2 只改状态，不真正暂停引擎）"""
    action = str(body.get("action") or "").strip().lower()
    if action not in ("pause", "resume"):
        return JSONResponse({"ok": False, "error": "不支持的指令"}, status_code=400)
    try:
        rec = _write_state_command(action, "chat-console")
    except Exception:
        return JSONResponse({"ok": False, "error": "暂时没能保存命令"}, status_code=500)
    return {"ok": True, "action": action, "at": rec.get("at"),
            "message": "已暂停" if action == "pause" else "已继续"}

# ---------------- 答案导入 ----------------
TEXT_PATTERNS = [
    re.compile(r"^\s*(?:Q|q|题)?\s*(\d{1,3})\s*[\.\、\:：\)]\s*([A-Da-d](?:\s*[,，、/]\s*[A-Da-d])*)\s*$"),
    re.compile(r"^\s*(?:Q|q|题)?\s*(\d{1,3})\s+([A-Da-d](?:\s*[,，、/]\s*[A-Da-d])*)\s*$"),
    re.compile(r"^\s*(\d{1,3})\s*[\.\、\:：\)]?\s*([对错正确错误]{1,2})\s*$"),
]

def parse_answers_text(text):
    answers, unparsed = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        hit = None
        for pat in TEXT_PATTERNS:
            m = pat.match(line)
            if m:
                hit = m
                break
        if not hit:
            unparsed.append(line[:80])
            continue
        idx = int(hit.group(1))
        raw_val = hit.group(2)
        if re.search(r"[对错]", raw_val):
            val = ["A"] if raw_val[0] in "对正" else ["B"]
        else:
            val = [x.upper() for x in re.split(r"\s*[,，、/]\s*", raw_val) if x]
        answers.append({"index": idx, "value": sorted(set(val)), "confidence": 1.0})
    return answers, unparsed

def docx_to_text(data):
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" ".join(c.text for c in row.cells))
    return "\n".join(parts)

def pdf_to_text(data):
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)

@app.post("/api/import-answers")
def api_import(body: dict = Body(...)):
    section = str(body.get("section") or "")
    filename = str(body.get("filename") or "")
    text = body.get("text")
    b64 = body.get("content_base64")
    source = "text"
    raw = ""
    if b64:
        try:
            data = base64.b64decode(b64)
        except Exception:
            return JSONResponse({"ok": False, "error": "文件内容无法识别"}, status_code=400)
        low = filename.lower()
        try:
            if low.endswith(".docx"):
                raw = docx_to_text(data); source = "docx"
            elif low.endswith(".pdf"):
                raw = pdf_to_text(data); source = "pdf"
            elif low.endswith(".json"):
                raw = data.decode("utf-8", errors="replace"); source = "json"
            else:
                raw = data.decode("utf-8", errors="replace"); source = "text-file"
        except Exception:
            return JSONResponse({"ok": False, "error": "文件解析失败"}, status_code=400)
    elif isinstance(text, str):
        raw = text
    else:
        return JSONResponse({"ok": False, "error": "缺少内容"}, status_code=400)

    if source == "json":
        try:
            j = json.loads(raw)
            arr = j.get("answers") if isinstance(j, dict) else j
            answers = []
            for it in (arr or []):
                if isinstance(it, dict) and "index" in it:
                    v = it.get("value") or it.get("answers") or it.get("answer")
                    if isinstance(v, str):
                        v = [x.strip().upper() for x in re.split(r"[,，、/]", v) if x.strip()]
                    answers.append({"index": int(it["index"]), "value": sorted(set(v or [])),
                                    "confidence": it.get("confidence", 1.0)})
            unparsed = []
        except Exception:
            return JSONResponse({"ok": False, "error": "内容格式无法识别"}, status_code=400)
    else:
        answers, unparsed = parse_answers_text(raw)

    answers.sort(key=lambda x: x["index"])
    return {"ok": True, "section": section, "source": source, "count": len(answers),
            "answers": answers, "unparsed": unparsed[:20]}

# ---------------- 题目 Word 导出 ----------------
def load_question_sets(sections):
    sets = []
    if sections:
        for sec in sections:
            d = core.SECTION_DIRS.get(sec)
            if not d:
                continue
            p = os.path.join(d, "extracted.json")
            if os.path.exists(p):
                j = core.read_json(p) or {}
                for q in (j.get("questions") or []):
                    sets.append({"section": sec, "qno": q.get("index"), "q": q})
        return sets
    man_p = os.path.join(STAGE_D, "eval", "manifest-20.json")
    ext_p = os.path.join(STAGE_D, "eval", "extracted.json")
    if os.path.exists(man_p) and os.path.exists(ext_p):
        man = core.read_json(man_p) or {}
        ext = core.read_json(ext_p) or {}
        by = {q["index"]: q for q in (ext.get("questions") or [])}
        for it in (man.get("questions") or []):
            q = by.get(it["index"])
            if q:
                sets.append({"section": it.get("section"), "qno": it.get("qno"), "q": q})
    return sets

@app.post("/api/export/questions")
def api_export(body: dict = Body(default={})):
    try:
        import docx
    except Exception:
        return JSONResponse({"ok": False, "error": "导出组件不可用"}, status_code=500)
    sections = body.get("sections") or ([body["section"]] if body.get("section") else [])
    sets = load_question_sets(sections)
    if not sets:
        return JSONResponse({"ok": False, "error": "没有可导出的题目"}, status_code=400)
    d = docx.Document()
    d.add_heading("学习通章节测验题目", level=1)
    d.add_paragraph("导出时间：%s　题目数：%d" % (time.strftime("%Y-%m-%d %H:%M:%S"), len(sets)))
    cur_sec = None
    for it in sets:
        sec = it.get("section")
        if sec != cur_sec:
            cur_sec = sec
            d.add_heading("章节 %s" % sec, level=2)
        q = it["q"]
        d.add_paragraph("%s. 【%s】%s" % (it.get("qno") or q.get("index"), q.get("type") or "", q.get("stem") or ""))
        for o in (q.get("options") or []):
            d.add_paragraph("    %s. %s" % (o.get("key"), o.get("text")))
        d.add_paragraph("    我的答案：__________")
        d.add_paragraph("")
    fname = "questions-%s.docx" % time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(EXPORT_DIR, fname)
    d.save(out)
    return {"ok": True, "path": out, "filename": fname, "url": "/exports/" + fname,
            "count": len(sets), "size": os.path.getsize(out)}

@app.get("/exports/{name}")
def api_export_file(name: str):
    p = os.path.normpath(os.path.join(EXPORT_DIR, name))
    if not p.startswith(os.path.normpath(EXPORT_DIR)) or not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return FileResponse(p, filename=name)

@app.get("/evidence/{section}/{rest:path}")
def api_evidence_file(section: str, rest: str):
    base = core.SECTION_DIRS.get(section)
    if not base:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    target = os.path.normpath(os.path.join(base, rest))
    if not target.startswith(os.path.normpath(base)) or not os.path.isfile(target):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return FileResponse(target)

# ================= 友好数据（面向普通用户） =================
SECTION_TITLES = {
    "1.1": "大党之治：百年党史中最恢宏的篇章",
    "1.2": "习近平党建思想的核心要义",
    "1.3": "深刻认识习近平党建思想的重大意义",
    "1.4": "青年学子在兴党强党中的使命担当",
    "2.1": "制定和实施五年规划是中国共产党治国理政一条重要经验",
    "2.2": "“十五五”时期在基本实现现代化进程中具有承前启后的重要地位",
    "2.3": "深刻领会“十五五”时期经济社会发展的战略任务",
    "2.4": "青年要在推进中国式现代化的新征程上挺膺担当",
    "3.1": "习近平总书记关于发展实体经济的重要思想",
    "3.2": "第四次产业革命正在重塑产业体系和国际竞争格局",
    "3.3": "以新兴产业和未来产业为重",
    "3.4": "战略性新兴产业优先发展，引领和支持传统产业升级改造",
    "3.5": "着眼于现代化目标，调整优化新质生产力的空间布局",
    "3.6": "金融如何支持实体经济",
    "4.1": "习近平法治思想是新时代全面依法治国必须长期坚持的指导思想",
    "4.2": "习近平法治思想内涵丰富、论述深刻、逻辑严密、系统完备",
    "4.3": "全面依法治国的历史性成就充分彰显了习近平法治思想的真理力量和实践伟力",
    "4.4": "坚定不移走中国特色社会主义法治道路",
    "5.1": "数字中国建设的时代背景",
    "5.2": "数字中国建设行稳致远的中国实践",
    "5.3": "人工智能在数字中国建设中的重要作用",
    "5.4": "推动“十五五”时期数字中国建设迈上新台阶",
    "6.1": "台湾属于中国的史实和法理不容置疑",
    "6.2": "祖国统一的历史大势不可阻挡",
    "6.3": "团结台湾同胞共谋民族复兴和国家统一",
    "7.1": "大国关系事关全球战略稳定",
    "7.2": "维护大国关系总体稳定",
    "7.3": "中国特色大国外交不断开创新局面",
}
STATUS_TEXT = {"completed": "已完成", "video_done": "进行中", "pending": "待开始", "failed": "有问题"}
SHOT_PRIORITY = {
    "1.1": ["evidence/filled-02-verified.png", "evidence/submit-02-course.png"],
    "1.2": ["evidence/submit-1.2-result.png", "evidence/verify-1.2-course.png"],
    "2.2": ["evidence/submit-2.2-result.png", "evidence/verify-2.2-course.png", "evidence/video-01-done.png"],
    "2.3": ["evidence/p8-result.png", "evidence/p10-course-progress.png", "evidence/p10-final-2.4.png"],
}

def section_title(sec):
    return SECTION_TITLES.get(sec, "第 %s 节" % (sec or ""))

def _per_question_scores(sec):
    d = core.SECTION_DIRS.get(sec)
    if not d:
        return []
    p = os.path.join(d, "submit-record.json")
    if os.path.exists(p):
        r = (core.read_json(p) or {}).get("result") or {}
        if isinstance(r.get("per_question_scores"), list):
            return r["per_question_scores"]
    p = os.path.join(d, "answers-both.json")
    if os.path.exists(p):
        s = (core.read_json(p) or {}).get("session") or {}
        if isinstance(s.get("per_question"), list):
            return s["per_question"]
    p = os.path.join(d, "submit-2.2.json")
    if os.path.exists(p):
        s = (core.read_json(p) or {}).get("submit") or {}
        pq = s.get("per_question")
        if isinstance(pq, dict):
            return [pq.get(str(i)) for i in range(1, len(pq) + 1)]
        if isinstance(pq, list):
            return pq
    p = os.path.join(d, "submit.json")
    if os.path.exists(p):
        j = core.read_json(p) or {}
        if isinstance(j.get("per_question"), list):
            return j["per_question"]
    return []

def _questions_for(sec):
    d = core.SECTION_DIRS.get(sec)
    qs = []
    if d:
        p = os.path.join(d, "extracted.json")
        if os.path.exists(p):
            qs = (core.read_json(p) or {}).get("questions") or []
    if not qs and sec == "1.1":
        p = os.path.join(STAGE_D, "eval", "extracted.json")
        qs = [q for q in ((core.read_json(p) or {}).get("questions") or []) if q.get("index") in (1, 2, 3, 4, 5)]
    return qs

def _answers_for(sec):
    d = core.SECTION_DIRS.get(sec)
    if not d:
        return {}
    p = os.path.join(d, "answers.json")
    if os.path.exists(p):
        return {a["index"]: a for a in ((core.read_json(p) or {}).get("answers") or [])}
    p = os.path.join(d, "answers-both.json")
    if os.path.exists(p):
        fin = (core.read_json(p) or {}).get("final_answers") or {}
        return {int(k): {"value": v} for k, v in fin.items()}
    return {}

@app.get("/api/section/{name}")
def api_section_detail(name: str):
    st = core.section_state(name)
    scores = _per_question_scores(name)
    qs = _questions_for(name)
    ans = _answers_for(name)
    items = []
    for i, q in enumerate(qs):
        idx = q.get("index") or (i + 1)
        a = ans.get(idx) or {}
        sc = None
        if i < len(scores) and scores[i] is not None:
            try:
                sc = float(scores[i])
            except Exception:
                sc = None
        items.append({"no": idx, "type_text": q.get("type") or "", "stem": q.get("stem") or "",
                      "options": [{"key": o.get("key"), "text": o.get("text")} for o in (q.get("options") or [])],
                      "my_answer": "、".join(a.get("value") or []) if a.get("value") else "",
                      "score": sc})
    shots = []
    for rel in SHOT_PRIORITY.get(name, []):
        p = os.path.join(core.SECTION_DIRS.get(name, ""), rel)
        if os.path.exists(p):
            shots.append({"url": "/evidence/%s/%s" % (name, rel), "caption": "完成截图"})
    return {"section": name, "title": section_title(name),
            "status": st.get("status"), "status_text": STATUS_TEXT.get(st.get("status"), "待开始"),
            "score": st.get("score"),
            "score_text": ("%s 分 / 100 分" % st.get("score")) if st.get("score") not in (None, "") else "暂无成绩",
            "shots": shots, "questions": items, "question_count": len(items)}

# ---------------- 执行引擎状态（只读 state.json；缺失/损坏一律兜底，不抛错） ----------------
ENGINE_SCHEMA_VERSION = 1
ENGINE_STAGE_VALUES = ("video", "quiz", "submit")
ENGINE_RUNNING_VALUES = ("running", "working", "busy", "playing", "executing", "active")
ENGINE_PAUSED_VALUES = ("paused", "pause", "suspended")
ENGINE_STOPPED_VALUES = ("stopped", "stop", "cancelled", "canceled", "shutdown")
ENGINE_IDLE_VALUES = ("idle", "done", "finished", "ready", "waiting", "")

def _progress_percent(v):
    """把 0.35 / 35 / "35%" 统一成 0-100 的整数；无法识别返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        f = float(m.group(1))
    except Exception:
        return None
    if 0.0 <= f <= 1.0 and "%" not in s:
        f *= 100.0
    return max(0, min(100, int(round(f))))

def _normalize_last_result(last):
    """last_result 稳定字段：section/title/status(completed|failed|unknown)/score/at。"""
    if not isinstance(last, dict):
        return None
    sec = str(last.get("section") or "").strip()
    title = str(last.get("title") or "").strip() or section_title(sec)
    status_raw = str(last.get("status") or "").strip().lower()
    if status_raw in ("failed", "error", "fail", "失败", "有问题"):
        status = "failed"
    elif status_raw in ("completed", "complete", "done", "success", "ok", "已完成"):
        status = "completed"
    else:
        status = "completed" if sec else "unknown"
    score = last.get("score")
    if score is not None:
        try:
            score = int(float(score))
        except Exception:
            score = None
    progress_total = last.get("progress_total")
    if progress_total is not None:
        try:
            progress_total = int(float(progress_total))
        except Exception:
            progress_total = None
    return {"section": sec, "title": title, "status": status, "score": score,
            "at": str(last.get("at") or last.get("finished_at") or ""),
            "progress_total": progress_total,
            "next_section": str(last.get("next_section") or "").strip(),
            "plan_id": str(last.get("plan_id") or "").strip(),
            "skipped": bool(last.get("skipped"))}

ENGINE_ERROR_TEXT = {
    "BROWSER_OFFLINE": "学习通浏览器未连接，请先打开并登录后重试",
    "BROWSER_LOGIN_REQUIRED": "学习通还没登录，请在浏览器里扫码登录后重试",
    "SECTION_FAILED": "这一节没做成，请稍后点「重试」再试一次",
    "QUIZ_ONLY_UNSUPPORTED": "这一节暂时只能做测验，请稍后点「重试」再试一次",
}

def _engine_error_text(err, last_error=None, detail=""):
    """state=error 时给普通用户看的中文失败提示（不出现技术词）。
    优先用引擎的 last_error.code 映射；其次按消息关键词兜底。"""
    code = ""
    if isinstance(last_error, dict):
        code = str(last_error.get("code") or "").strip().upper()
    if code in ENGINE_ERROR_TEXT:
        return ENGINE_ERROR_TEXT[code]
    msg = ""
    for src in (last_error, err):
        if isinstance(src, dict) and src.get("message"):
            msg = str(src.get("message"))
            break
    if not msg:
        msg = str(detail or "")
    low = msg.lower()
    if "登录" in msg or "扫码" in msg or "login" in low:
        return "学习通还没登录，请在浏览器里扫码登录后重试"
    if any(k in low for k in ("cdp", "浏览器", "browser", "9222", "连接", "chrome", "edge", "webdriver")):
        return "学习通浏览器未连接，请先打开并登录后重试"
    return "刚才这一步没做成，请稍后点「重试」再试一次"

def _effective_progress(st, engine):
    """进度数字：优先用引擎 state.json 的 last_result.progress_total，缺失时才用证据统计。"""
    prog = dict((st or {}).get("progress") or {})
    try:
        total = int(prog.get("total") or 59)
    except Exception:
        total = 59
    try:
        n = int(prog.get("n") or 0)
    except Exception:
        n = 0
    lr = (engine or {}).get("last_result") or {}
    pt = lr.get("progress_total")
    if isinstance(pt, int) and pt > n:
        n = pt
        prog["source"] = "engine:last_result.progress_total"
    eng_pp = (engine or {}).get("page_progress") or {}
    try:
        epn = int(eng_pp.get("n"))
    except Exception:
        epn = None
    if epn is not None and epn > n:
        n = epn
        total = int(eng_pp.get("total") or total)
        prog["source"] = "engine:page_progress"
    pf = _read_preflight()
    if pf and pf.get("ok"):
        try:
            pn = int(pf.get("n"))
            ptot = int(pf.get("total") or total)
        except Exception:
            pn, ptot = None, None
        if pn is not None and pn > n:
            n = pn
            prog["source"] = "preflight:course_page"
            prog["preflight_at"] = pf.get("at")
        if ptot:
            total = ptot
    prog["n"] = n
    prog["total"] = total
    prog["percent"] = round(n * 100.0 / total, 1) if total else 0
    prog["text"] = "已完成 %d 个任务点 / 共 %d 个" % (n, total)
    return prog

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

# ---------------- 页面心跳 / 关闭即停止 ----------------
HB_LOCK = threading.Lock()
HB_STATE = {"last": None, "pending": None, "stopping": False, "stopping_at": 0.0, "stop_request": None}

def _hb_snapshot():
    with HB_LOCK:
        return {"last_heartbeat": HB_STATE["last"], "stopping": HB_STATE["stopping"],
                "stop_request": HB_STATE["stop_request"], "stopping_at": HB_STATE["stopping_at"]}

def _clear_stopping(reason=""):
    with HB_LOCK:
        HB_STATE["stopping"] = False
        HB_STATE["stopping_at"] = 0.0
        HB_STATE["stop_request"] = None
        HB_STATE["pending"] = None

def _record_stop(reason, force=False):
    """记录停止请求（页面关闭 / 心跳丢失）。真实停止引擎由引擎契约消费，控制台只记录+标记。"""
    with HB_LOCK:
        if HB_STATE["stopping"] and not force:
            return HB_STATE["stop_request"]
        now = time.time()
        last = HB_STATE["last"]
        req = {"action": "stop", "ts": now, "at": _now_iso(), "reason": reason,
               "source": "heartbeat",
               "heartbeat_age_s": round(now - last, 1) if last else None}
        HB_STATE["stopping"] = True
        HB_STATE["stopping_at"] = now
        HB_STATE["pending"] = None
    engine = _engine_snapshot()
    req["engine"] = {"state": (engine or {}).get("state")}
    with HB_LOCK:
        HB_STATE["stop_request"] = req
    try:
        tmp = STOP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(req, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STOP_FILE)
    except Exception:
        pass
    # 引擎停止契约：state.cmd.json action=stop（ts 晚于 daemon 启动才消费）
    try:
        _write_state_command("stop", "heartbeat", reason)
    except Exception:
        pass
    _write_heartbeat_file(stopping=True, reason=reason)
    try:
        with open(CONTROL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": req["at"], "action": "stop", "section": None,
                                 "params": None, "note": "页面关闭/心跳丢失：" + reason,
                                 "executed": False}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return req

def _heartbeat_watchdog():
    while True:
        time.sleep(1.0)
        now = time.time()
        with HB_LOCK:
            last = HB_STATE["last"]
            stopping = HB_STATE["stopping"]
            pending = HB_STATE["pending"]
        if stopping:
            continue
        if pending and now >= pending["at"]:
            _record_stop(pending["reason"], force=True)
            continue
        if last is not None and now - last > HEARTBEAT_TIMEOUT:
            _record_stop("heartbeat_lost")

threading.Thread(target=_heartbeat_watchdog, daemon=True).start()

def _preflight_python():
    if os.path.isfile(VENV_PY):
        return VENV_PY
    return sys.executable

def _read_preflight():
    """最近一次预检缓存（控制台自己写，界面启动时先用它显示）。"""
    try:
        if os.path.exists(PREFLIGHT_FILE):
            j = core.read_json(PREFLIGHT_FILE)
            if isinstance(j, dict):
                return j
    except Exception:
        pass
    return None

def _last_skipped_event():
    """只读引擎账本，取最近一次 section_skipped（引擎暂未在 last_result 暴露 skipped 时的兜底）。"""
    try:
        if not os.path.exists(ENGINE_LEDGER):
            return None
        with open(ENGINE_LEDGER, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-300:]
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("event") == "section_skipped":
                return {"section": str(j.get("section") or ""),
                        "at": str(j.get("at") or ""),
                        "plan_id": str(j.get("plan_id") or "")}
    except Exception:
        pass
    return None

def _run_preflight(timeout=None):
    """调用引擎 preflight-only 读课程页真实进度。不写引擎状态、不阻塞页面。
    返回 {ok, n, total, text, source, at} 或 {ok:false, code, message, retryable, at}。"""
    engine = _engine_snapshot()
    if engine and engine.get("state") in ("running", "paused"):
        return {"ok": False, "code": "ENGINE_RUNNING",
                "message": "正在执行任务，完成后再刷新进度", "retryable": True, "at": _now_iso()}
    cmd = None
    for path, extra in ((os.path.join(ENGINE_DIR, "preflight.py"), ["--json"]),
                        (os.path.join(ENGINE_DIR, "queue_daemon.py"), ["--preflight-only", "--json"])):
        if os.path.isfile(path):
            cmd = [_preflight_python(), path] + extra
            break
    if cmd is None:
        res = {"ok": False, "code": "PREFLIGHT_UNAVAILABLE",
               "message": "暂时读不到课程进度，请稍后再试", "retryable": True, "at": _now_iso()}
    else:
        try:
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=child_env,
                                  timeout=float(timeout or 45))
        except subprocess.TimeoutExpired:
            res = {"ok": False, "code": "PREFLIGHT_TIMEOUT",
                   "message": "读取课程进度超时，请稍后重试", "retryable": True, "at": _now_iso()}
        except Exception:
            res = {"ok": False, "code": "PREFLIGHT_FAILED",
                   "message": "暂时读不到课程进度，请稍后再试", "retryable": True, "at": _now_iso()}
        else:
            parsed = None
            for line in reversed((proc.stdout or "").strip().splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if isinstance(j, dict):
                    parsed = j
                    break
            if not isinstance(parsed, dict):
                res = {"ok": False, "code": "PREFLIGHT_BAD_OUTPUT",
                       "message": "暂时读不到课程进度，请稍后再试", "retryable": True,
                       "at": _now_iso(), "detail": (proc.stderr or "")[:200]}
            elif parsed.get("ok"):
                try:
                    n = int(parsed.get("n"))
                    total = int(parsed.get("total") or 59)
                except Exception:
                    n, total = None, 59
                if n is None:
                    res = {"ok": False, "code": "PREFLIGHT_BAD_OUTPUT",
                           "message": "暂时读不到课程进度，请稍后再试", "retryable": True, "at": _now_iso()}
                else:
                    res = {"ok": True, "logged_in": bool(parsed.get("logged_in", True)),
                           "n": n, "total": total,
                           "text": str(parsed.get("text") or ("已完成任务点 %d/%d" % (n, total))),
                           "source": str(parsed.get("source") or "course_page"),
                           "at": str(parsed.get("at") or _now_iso())}
            else:
                res = {"ok": False,
                       "code": str(parsed.get("code") or "PREFLIGHT_FAILED"),
                       "message": str(parsed.get("message") or "暂时读不到课程进度，请稍后再试"),
                       "retryable": bool(parsed.get("retryable", True)),
                       "at": str(parsed.get("at") or _now_iso())}
    res.setdefault("at", _now_iso())
    res["checked_at"] = _now_iso()
    try:
        tmp = PREFLIGHT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False)
        os.replace(tmp, PREFLIGHT_FILE)
    except Exception:
        pass
    return res

def _engine_hint_text(detail):
    """idle 时的友好提示（如等待扫码登录）；识别不了就返回空，不显示原始技术文案。"""
    s = str(detail or "")
    if not s:
        return ""
    if any(k in s for k in ("登录", "扫码", "login", "login_required")):
        return "学习通还没登录，请在浏览器里扫码登录"
    if any(k in s for k in ("浏览器", "CDP", "9222", "连接", "browser", "chrome", "edge")):
        return "学习通浏览器未连接，请先打开并登录"
    return ""

def _read_engine_state():
    """读 work/stage-d/console/state.json（执行引擎维护）。不存在/坏 JSON 返回 None。"""
    if not os.path.exists(STATE_FILE):
        return None
    raw = core.read_json(STATE_FILE)
    return raw if isinstance(raw, dict) else None

def _engine_snapshot():
    """state.json -> 界面稳定结构：
    {schema_version, state: running|idle|paused, current{section,title,stage,progress,detail},
     last_result{section,title,status,score,at}, queue[{section,title}], updated_at}
    文件不存在或内容异常时返回 None，界面自行显示兜底文案，不报错。"""
    raw = _read_engine_state()
    if raw is None:
        return None
    state_raw = str(raw.get("state") or "").strip().lower()
    cur_raw = raw.get("current") if isinstance(raw.get("current"), dict) else None
    current = None
    if cur_raw:
        sec = str(cur_raw.get("section") or "").strip()
        current = {
            "section": sec,
            "title": str(cur_raw.get("title") or "").strip() or section_title(sec),
            "stage": str(cur_raw.get("stage") or "").strip().lower(),
            "progress": _progress_percent(cur_raw.get("progress")),
            "detail": str(cur_raw.get("detail") or "").strip()[:80],
        }
    last = _normalize_last_result(raw.get("last_result"))
    page_progress = None
    pp = raw.get("progress") if isinstance(raw.get("progress"), dict) else None
    if pp:
        try:
            pn = int(pp.get("n"))
            pt = int(pp.get("total") or 59)
        except Exception:
            pn = None
        if pn is not None:
            page_progress = {"n": pn, "total": pt,
                             "source": str(raw.get("progress_source") or "page")}
    if state_raw in ENGINE_PAUSED_VALUES:
        state = "paused"
    elif state_raw in ENGINE_STOPPED_VALUES:
        state = "stopped"
    elif state_raw in ENGINE_RUNNING_VALUES or (cur_raw is not None and state_raw not in ENGINE_IDLE_VALUES):
        state = "running"
    elif state_raw == "error":
        state = "error"
    else:
        state = "idle"
    queue = []
    qraw = raw.get("queue")
    if isinstance(qraw, list):
        for it in qraw[:20]:
            if isinstance(it, dict):
                sec = str(it.get("section") or "").strip()
                if sec:
                    queue.append({"section": sec,
                                  "title": str(it.get("title") or "").strip() or section_title(sec)})
            elif isinstance(it, str) and it.strip():
                queue.append({"section": it.strip(), "title": section_title(it.strip())})
    sv = raw.get("schema_version")
    if sv is None:
        sv = ENGINE_SCHEMA_VERSION
    error_raw = raw.get("error") if isinstance(raw.get("error"), dict) else None
    last_error = raw.get("last_error") if isinstance(raw.get("last_error"), dict) else None
    error_text = ""
    error_retryable = True
    if state == "error":
        current = None          # 出错时绝不显示"正在播放/答题/提交"
        error_text = _engine_error_text(error_raw, last_error, raw.get("detail"))
        if isinstance(last_error, dict) and isinstance(last_error.get("retryable"), bool):
            error_retryable = last_error["retryable"]
    hint_text = _engine_hint_text(raw.get("detail")) if state == "idle" else ""
    last_skipped = raw.get("last_skipped") if isinstance(raw.get("last_skipped"), dict) else _last_skipped_event()
    return {"schema_version": sv,
            "state": state,
            "current": current,
            "last_result": last,
            "queue": queue,
            "queue_count": len(qraw) if isinstance(qraw, list) else 0,
            "updated_at": str(raw.get("updated_at") or raw.get("at") or ""),
            "error": error_raw,
            "last_error": last_error,
            "error_text": error_text,
            "error_retryable": error_retryable,
            "hint_text": hint_text,
            "last_skipped": last_skipped,
            "page_progress": page_progress}

def _write_state_command(action, source="console", reason=None):
    """命令写 state.cmd.json（原子写）：pause/resume/stop。引擎按 ts 顺序消费。"""
    rec = {"action": action,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "ts": time.time(), "source": source}
    if reason:
        rec["reason"] = str(reason)[:80]
    tmp = STATE_CMD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, STATE_CMD_FILE)
    return rec

def _write_heartbeat_file(stopping=False, reason=None):
    """页面心跳文件（供引擎启动器判失联；原子写、不写日志）。"""
    rec = {"at": _now_iso(), "ts": time.time(), "stopping": bool(stopping)}
    if reason:
        rec["reason"] = str(reason)[:80]
    try:
        tmp = LAST_HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, LAST_HEARTBEAT_FILE)
    except Exception:
        pass
    return rec

def _state_command():
    j = core.read_json(STATE_CMD_FILE) if os.path.exists(STATE_CMD_FILE) else None
    if not isinstance(j, dict):
        return None
    return {"action": str(j.get("action") or "").strip().lower(),
            "at": str(j.get("at") or ""), "ts": j.get("ts")}

def _system_state(engine=None):
    """系统状态：运行正常 / 已暂停 / 已停止（页面关闭或手动停止后的终态）。"""
    if engine and engine.get("state") == "stopped":
        return {"state": "stopped", "text": "已停止"}
    paused = bool(engine and engine.get("state") == "paused")
    cmd = _state_command()
    if cmd and cmd.get("action") in ("pause", "resume"):
        paused = cmd["action"] == "pause"
    return {"state": "paused" if paused else "running",
            "text": "已暂停" if paused else "运行正常"}

def _rail_info(st, rows, engine=None):
    """右侧小面板数据（供 /api/friendly 兼容旧调用；主界面自己组合 status+tasks）"""
    if engine:
        cur = engine.get("current")
        last = engine.get("last_result")
        if cur and engine.get("state") in ("running", "paused"):
            current = {"section": cur.get("section"), "title": cur.get("title"),
                       "status_text": "正在进行", "stage": cur.get("stage"),
                       "progress": cur.get("progress"), "sub_text": ""}
        elif last:
            current = {"section": last.get("section"), "title": last.get("title"),
                       "status_text": "已完成" if last.get("status") == "completed" else "有问题",
                       "stage": "", "progress": None,
                       "sub_text": ("成绩 %d 分" % last["score"]) if last.get("score") is not None else ""}
        else:
            current = {"section": "", "title": "当前无进行中任务", "status_text": "",
                       "stage": "", "progress": None, "sub_text": ""}
        nxt = None
        if engine.get("queue"):
            q = engine["queue"][0]
            nxt = {"section": q.get("section"), "title": q.get("title"), "status_text": "待开始"}
        else:
            for r in rows:
                if r.get("status") != "completed":
                    nxt = {"section": r.get("section"), "title": section_title(r.get("section") or ""),
                           "status_text": "待开始"}
                    break
        if nxt is None:
            nxt = {"section": "", "title": "所有章节都做完了", "status_text": ""}
        return {"current": current, "next": nxt, "source": "engine"}
    # 回退旧逻辑（没有 state.json 时）
    running = (st or {}).get("running") or None
    if running and running.get("section"):
        sec = running["section"]
        current = {"section": sec, "title": section_title(sec), "status_text": "正在进行",
                   "stage": "", "progress": None, "sub_text": ""}
    else:
        done_rows = [r for r in rows if r.get("status") == "completed"]
        done_rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        if done_rows:
            r = done_rows[0]
            current = {"section": r.get("section"),
                       "title": section_title(r.get("section") or ""),
                       "status_text": "已完成", "stage": "", "progress": None, "sub_text": ""}
        else:
            current = {"section": "", "title": "当前无进行中任务", "status_text": "",
                       "stage": "", "progress": None, "sub_text": ""}
    nxt = None
    for r in rows:
        if r.get("status") != "completed":
            nxt = r
            break
    if nxt:
        next_item = {"section": nxt.get("section"),
                     "title": section_title(nxt.get("section") or ""),
                     "status_text": "待开始"}
    else:
        next_item = {"section": "", "title": "所有章节都做完了", "status_text": ""}
    return {"current": current, "next": next_item, "source": "evidence"}

@app.get("/api/friendly")
def api_friendly():
    """兼容旧版聚合接口（界面已改为 /api/engine + /api/status + /api/tasks 分开取数）"""
    try:
        st = core.collect_status()
    except Exception:
        st = {}
    try:
        tasks = core.collect_tasks()
    except Exception:
        tasks = {"sections": [], "summary": {}}
    engine = _engine_snapshot()
    prog = _effective_progress(st, engine)
    n, total = prog.get("n", 0), prog.get("total", 59)
    rows = tasks.get("sections") or []
    done = [r for r in rows if r.get("status") == "completed"]
    current = None
    for r in rows:
        if r.get("status") == "video_done":
            current = r; break
    if current is None:
        for r in rows:
            if r.get("status") in ("failed", "pending"):
                current = r; break
    running = st.get("running")
    health = "运行正常"
    if not (st.get("env") or {}).get("cdp", {}).get("ok"):
        health = "浏览器未连接"
    elif running:
        health = "正在执行任务"
    return {
        "progress": {"done": n, "total": total,
                     "percent": round(n * 100.0 / total, 1) if total else 0,
                     "text": "已完成 %d 个任务点 / 共 %d 个" % (n, total)},
        "current": {"section": (current or {}).get("section"),
                    "title": section_title((current or {}).get("section") or ""),
                    "text": ("当前正在做：%s %s" % ((current or {}).get("section"),
                            section_title((current or {}).get("section") or ""))) if current else "暂无待做章节"},
        "health": health,
        "system": _system_state(engine),
        "engine": engine,
        "rail": _rail_info(st, rows, engine),
        "stats": {"done": len(done), "pending": (tasks.get("summary") or {}).get("pending", 0)},
        "sections": [{"section": r.get("section"), "title": section_title(r.get("section")),
                      "status": r.get("status"),
                      "status_text": STATUS_TEXT.get(r.get("status"), "待开始"),
                      "score": r.get("score"),
                      "score_text": ("%s 分" % r.get("score")) if r.get("score") not in (None, "") else ""}
                     for r in rows],
    }

# ================= AI 对话（规则解析 → 计划，不调用浏览器、不执行） =================
CHAT_UNKNOWN = "没听懂，请问是想做全部、下一章，还是某几节？"
PLAN_MINUTES_PER_SECTION = 4
CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

def _num_token(s):
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, b = s.split("十", 1)
        tens = CN_NUM.get(a, 1) if a else 1
        ones = CN_NUM.get(b, 0) if b else 0
        return tens * 10 + ones
    return CN_NUM.get(s)

def _course_rows():
    try:
        tasks = core.collect_tasks()
    except Exception:
        tasks = {"sections": []}
    rows = []
    for r in tasks.get("sections") or []:
        sec = str(r.get("section") or "").strip()
        if not sec:
            continue
        rows.append({"section": sec, "title": section_title(sec),
                     "status": r.get("status") or "pending",
                     "done": r.get("status") == "completed"})
    return rows

def _current_row(rows):
    for r in rows:
        if r["status"] == "video_done":
            return r
    for r in rows:
        if r["status"] == "failed":
            return r
    for r in rows:
        if not r["done"]:
            return r
    return None

def _scope_rows(rows, ch=None, sec=None):
    out = []
    for r in rows:
        parts = r["section"].split(".")
        if ch is not None and parts[0] != str(ch):
            continue
        if sec is not None and (len(parts) < 2 or parts[1] != str(sec)):
            continue
        if not r["done"]:
            out.append(r)
    return out

def _scope_text(scope):
    if not scope:
        return ""
    secs = [r["section"] for r in scope]
    if len(secs) == 1:
        return "第 %s 节" % secs[0]
    if len(secs) <= 3:
        return "、".join(secs) + "，共 %d 节" % len(secs)
    chs = sorted({s.split(".")[0] for s in secs},
                 key=lambda x: int(x) if x.isdigit() else 0)
    if len(chs) == 1:
        return "第 %s 章（%s 到 %s）" % (chs[0], secs[0], secs[-1])
    return "第 %s 到 %s 章（%s 到 %s）" % (chs[0], chs[-1], secs[0], secs[-1])

def _make_plan(scope, mode, action, label, extra=None):
    count = len(scope)
    plan = {
        "label": label,
        "action": action,
        "mode": mode,
        "scope_text": _scope_text(scope),
        "sections": [{"section": r["section"], "title": r["title"]} for r in scope],
        "count": count,
        "minutes": max(1, count * PLAN_MINUTES_PER_SECTION),
        "chapter_count": len({r["section"].split(".")[0] for r in scope}),
        "first_sec": scope[0]["section"] if scope else "",
        "first_title": scope[0]["title"] if scope else "",
        "last_sec": scope[-1]["section"] if scope else "",
    }
    if extra:
        plan.update(extra)
    return plan

def _plan_reply(scope, mode, action, label, reply_tpl, extra=None):
    if not scope:
        return {"understood": True, "reply": "这一部分已经全部做完了，不用再做了。", "plan": None}
    plan = _make_plan(scope, mode, action, label, extra)
    try:
        reply = reply_tpl.format(**plan)
    except Exception:
        reply = "好的，计划已经列好了，共 %d 节。" % plan["count"]
    return {"understood": True, "reply": reply, "plan": plan}

def _parse_chat(text):
    raw = (text or "").strip()
    t = re.sub(r"\s+", "", raw)
    if not t:
        return {"understood": False, "reply": CHAT_UNKNOWN, "plan": None}
    rows = _course_rows()
    remaining = [r for r in rows if not r["done"]]
    quiz = bool(re.search(r"测验|做题|刷题|答题|作业|题目", t))

    # 1) 区间：2.4-3.3 / 2.4到3.3
    m = re.search(r"(\d{1,2}\.\d{1,2})\s*(?:-|—|–|~|～|到|至)\s*(\d{1,2}\.\d{1,2})", t)
    if m:
        a, b = m.group(1), m.group(2)
        idx = {r["section"]: i for i, r in enumerate(rows)}
        if a in idx and b in idx:
            i, j = sorted((idx[a], idx[b]))
            scope = [r for r in rows[i:j + 1] if not r["done"]]
            mode = "quiz" if quiz else "full"
            action = ("只做 %s 到 %s 的测验题" if quiz else "做完 %s 到 %s 的这几节") % (a, b)
            label = "只做测验" if quiz else "做 %s 到 %s" % (a, b)
            tpl = ("好，从 {first_sec} 到 {last_sec} 的测验题一共 {count} 节，预计 {minutes} 分钟。"
                   if quiz else "好，从 {first_sec} 到 {last_sec} 一共 {count} 节，预计 {minutes} 分钟。")
            return _plan_reply(scope, mode, action, label, tpl)

    # 2) 第X章第Y节
    m = re.search(r"第([0-9一二三四五六七八九十两]{1,3})章(?:的)?第([0-9一二三四五六七八九十两]{1,3})节", t)
    if m:
        ch, sec = _num_token(m.group(1)), _num_token(m.group(2))
        if ch is not None and sec is not None:
            scope = _scope_rows(rows, ch=ch, sec=sec)
            mode = "quiz" if quiz else "full"
            action = ("只做第 %s.%s 节的测验题" if quiz else "做完第 %s.%s 节") % (ch, sec)
            label = "只做测验" if quiz else "做 %s.%s" % (ch, sec)
            tpl = ("好，第 {first_sec} 节的测验题，预计 {minutes} 分钟。"
                   if quiz else "好，第 {first_sec} 节「{first_title}」，预计 {minutes} 分钟。")
            return _plan_reply(scope, mode, action, label, tpl)

    # 3) 下一章
    if re.search(r"下一章", t):
        cur = _current_row(rows)
        cur_ch = int(cur["section"].split(".")[0]) if cur else None
        chs = sorted({int(r["section"].split(".")[0]) for r in remaining
                      if r["section"].split(".")[0].isdigit()})
        cand = [c for c in chs if cur_ch is None or c > cur_ch]
        ch = cand[0] if cand else (chs[0] if chs else None)
        scope = _scope_rows(rows, ch=ch) if ch is not None else []
        if scope:
            return _plan_reply(scope, "quiz" if quiz else "full",
                               ("只做第 %d 章剩下的测验题" if quiz else "做完第 %d 章的剩下部分") % ch,
                               "做下一章",
                               ("好，下一章是第 {chapter_no} 章，测验题还有 {count} 节，预计 {minutes} 分钟。"
                                if quiz else "好，下一章是第 {chapter_no} 章，还有 {count} 节，预计 {minutes} 分钟。"),
                               {"chapter_no": ch})

    # 4) 这一章 / 本章
    if re.search(r"这一章|这章|本章|当前章", t):
        cur = _current_row(rows)
        ch = int(cur["section"].split(".")[0]) if cur else None
        scope = _scope_rows(rows, ch=ch) if ch is not None else []
        if scope:
            return _plan_reply(scope, "quiz" if quiz else "full",
                               ("只做第 %d 章剩下的测验题" if quiz else "做完第 %d 章剩下的部分") % ch,
                               "做完这一章",
                               ("好，第 {chapter_no} 章的测验题还有 {count} 节，预计 {minutes} 分钟。"
                                if quiz else "好，第 {chapter_no} 章还有 {count} 节没做，预计 {minutes} 分钟。"),
                               {"chapter_no": ch})

    # 5) 第X章
    m = re.search(r"第([0-9一二三四五六七八九十两]{1,3})章", t)
    if m:
        ch = _num_token(m.group(1))
        if ch is not None:
            scope = _scope_rows(rows, ch=ch)
            if scope:
                return _plan_reply(scope, "quiz" if quiz else "full",
                                   ("只做第 %d 章剩下的测验题" if quiz else "做完第 %d 章剩下的部分") % ch,
                                   "做第 %d 章" % ch,
                                   ("好，第 {chapter_no} 章的测验题还有 {count} 节，预计 {minutes} 分钟。"
                                    if quiz else "好，第 {chapter_no} 章还有 {count} 节没做，预计 {minutes} 分钟。"),
                                   {"chapter_no": ch})

    # 6) 下一节 / 继续
    if re.search(r"下一[节个]|继续|接着|往下", t):
        nxt = next((r for r in rows if not r["done"]), None)
        scope = [nxt] if nxt else []
        return _plan_reply(scope, "quiz" if quiz else "full",
                           ("只做下一节的测验题" if quiz else "做完下一节"), "下一节",
                           ("好，下一节是「{first_sec} {first_title}」的测验题，预计 {minutes} 分钟。"
                            if quiz else "好，下一节是「{first_sec} {first_title}」，预计 {minutes} 分钟。"))

    # 7) 全部
    if re.search(r"全部|所有|全都|都做|全做|全刷|一起做|全做完", t):
        mode = "quiz" if quiz else "full"
        action = "把没做完的章节的测验题全部做完" if quiz else "把没做完的章节全部做完"
        label = "全部测验" if quiz else "全部做完"
        tpl = ("好，只做还没做完的测验题，涉及 {count} 节，预计 {minutes} 分钟。"
               if quiz else "好的，还有 {count} 节没做完，我一起安排上，预计 {minutes} 分钟。")
        return _plan_reply(remaining, mode, action, label, tpl)

    # 8) 剩下的
    if re.search(r"剩余|剩下|剩的|还没|没做|未做|其余", t):
        mode = "quiz" if quiz else "full"
        action = "把剩下章节的测验题做完" if quiz else "把剩下的章节做完"
        label = "剩下的测验" if quiz else "剩下的做完"
        tpl = ("好，剩下没做的测验题涉及 {count} 节，预计 {minutes} 分钟。"
               if quiz else "好的，剩下 {count} 节没做完，我安排上，预计 {minutes} 分钟。")
        return _plan_reply(remaining, mode, action, label, tpl)

    # 9) 某一节，如 2.4
    m = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)", t)
    if m:
        key = "%d.%d" % (int(m.group(1)), int(m.group(2)))
        if any(r["section"] == key for r in rows):
            scope = [r for r in rows if r["section"] == key and not r["done"]]
            mode = "quiz" if quiz else "full"
            action = ("只做第 %s 节的测验题" if quiz else "做完第 %s 节") % key
            label = "只做测验" if quiz else "做 %s" % key
            tpl = ("好，第 {first_sec} 节的测验题，预计 {minutes} 分钟。"
                   if quiz else "好，第 {first_sec} 节「{first_title}」，预计 {minutes} 分钟。")
            return _plan_reply(scope, mode, action, label, tpl)

    # 10) 只说做测验
    if quiz:
        return _plan_reply(remaining, "quiz", "只做还没做完的测验题", "只做测验",
                           "好，只做测验题，涉及 {count} 节，预计 {minutes} 分钟。")

    return {"understood": False, "reply": CHAT_UNKNOWN, "plan": None}

@app.post("/api/chat")
def api_chat(body: dict = Body(...)):
    """规则解析用户一句话，返回计划（不调用浏览器、不执行任何操作）"""
    text = str(body.get("text") or "")
    if not text.strip():
        return {"ok": True, "understood": False, "reply": CHAT_UNKNOWN, "plan": None}
    res = _parse_chat(text)
    res["ok"] = True
    return res

def _clean_sections(raw):
    out = []
    for it in raw or []:
        if isinstance(it, dict):
            sec = str(it.get("section") or "").strip()[:20]
            title = str(it.get("title") or "").strip()[:120]
        else:
            sec, title = str(it or "").strip()[:20], ""
        if not sec:
            continue
        out.append({"section": sec, "title": title or section_title(sec)})
        if len(out) >= 300:
            break
    return out

@app.post("/api/plan/start")
def api_plan_start(body: dict = Body(...)):
    """把计划写入 plans/ + queue.jsonl 交给引擎；开始前自动预检一次课程页真实进度。"""
    plan = body.get("plan") if isinstance(body.get("plan"), dict) else body
    if not isinstance(plan, dict):
        return JSONResponse({"ok": False, "error": "没有收到计划"}, status_code=400)
    sections = _clean_sections(plan.get("sections"))
    if not sections:
        return JSONResponse({"ok": False, "error": "计划里没有要做的小节"}, status_code=400)
    _clear_stopping("new_plan")   # 用户重新开始任务：撤销之前的停止标记
    # 每次「开始」前自动预检：浏览器未登录/离线时提示用户先处理（不阻塞其它页面/接口）
    pf = _run_preflight()
    if not pf.get("ok") and pf.get("code") in ("BROWSER_OFFLINE", "BROWSER_LOGIN_REQUIRED"):
        return JSONResponse({"ok": False, "code": pf.get("code"),
                             "error_text": pf.get("message") or "学习通浏览器未登录，请先打开并登录后重试",
                             "retryable": True, "preflight": pf}, status_code=200)
    try:
        minutes = int(plan.get("minutes") or len(sections) * PLAN_MINUTES_PER_SECTION)
    except Exception:
        minutes = len(sections) * PLAN_MINUTES_PER_SECTION
    plan_id = "plan-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    rec = {
        "id": plan_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "queued",
        "source_text": str(body.get("text") or "")[:300],
        "plan": {
            "label": str(plan.get("label") or "")[:60],
            "action": str(plan.get("action") or "")[:120],
            "mode": "quiz" if plan.get("mode") == "quiz" else "full",
            "count": len(sections),
            "minutes": max(1, minutes),
            "sections": sections,
        },
    }
    try:
        with open(os.path.join(PLANS_DIR, plan_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(PLANS_DIR, "queue.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": plan_id, "at": rec["created_at"], "status": "queued",
                                 "label": rec["plan"]["label"], "count": len(sections)},
                                ensure_ascii=False) + "\n")
    except Exception:
        return JSONResponse({"ok": False, "error": "暂时没能保存计划"}, status_code=500)
    return {"ok": True, "message": "已加入队列", "plan_id": plan_id,
            "count": len(sections), "status": "queued", "preflight": pf}

@app.get("/api/plan/latest")
def api_plan_latest():
    """最近一次计划（失败/完成都算），供「重试」按钮重新入队。"""
    try:
        items = []
        for name in os.listdir(PLANS_DIR):
            if name.startswith("plan-") and name.endswith(".json"):
                p = os.path.join(PLANS_DIR, name)
                items.append((os.path.getmtime(p), p))
        items.sort(reverse=True)
        for _m, p in items[:20]:
            j = core.read_json(p) or {}
            plan = j.get("plan") if isinstance(j.get("plan"), dict) else None
            if plan and plan.get("sections"):
                return {"ok": True, "id": j.get("id") or os.path.basename(p)[:-5],
                        "at": j.get("created_at"), "plan": plan}
    except Exception:
        pass
    return {"ok": False, "error": "还没有可重试的计划"}

@app.get("/api/plans")
def api_plans(limit: int = 10):
    """最近加入队列的计划（仅供界面确认，不驱动执行）"""
    items = []
    try:
        names = [n for n in os.listdir(PLANS_DIR) if n.endswith(".json")]
        names.sort(reverse=True)
        for n in names[:max(1, min(limit, 50))]:
            j = core.read_json(os.path.join(PLANS_DIR, n)) or {}
            items.append({"id": j.get("id"), "at": j.get("created_at"),
                          "status": j.get("status"), "label": (j.get("plan") or {}).get("label"),
                          "count": (j.get("plan") or {}).get("count")})
    except Exception:
        pass
    return {"ok": True, "items": items}

