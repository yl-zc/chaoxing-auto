#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习通自动化控制台 M1（零依赖：仅 Python 标准库）
- ThreadingHTTPServer，绑定 127.0.0.1:8770
- 静态文件 + JSON API（status/tasks/evidence/log）
- 只读：不触发任何自动化，不碰浏览器
"""
import http.server
import json
import threading
import os
import re
import socket
import sys
import time
import ctypes
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
STAGE_D = os.path.dirname(HERE)              # work/stage-d
WORK = os.path.dirname(STAGE_D)              # work
HOST, PORT = "127.0.0.1", 8770

SECTION_DIRS = {
    "1.1": STAGE_D,
    "1.2": os.path.join(STAGE_D, "auto-1.2"),
    "2.2": os.path.join(STAGE_D, "auto-2.2"),
    "2.3": os.path.join(STAGE_D, "auto-2.3"),
}

SUBMIT_FILES = ["submit.json", "submit-2.2.json", "submit-record.json"]
VIDEO_FILES = ["video.json"]
STATUS_FILES = ["status.json"]
VERIFY_FILES = ["verify.json", "verify-fill.json", "verify-before-submit.json"]

DEFAULT_SECTIONS = (
    [("1.%d" % i) for i in range(1, 5)] +
    [("2.%d" % i) for i in range(1, 5)] +
    [("3.%d" % i) for i in range(1, 7)] +
    [("4.%d" % i) for i in range(1, 5)] +
    [("5.%d" % i) for i in range(1, 5)] +
    ["6.1", "6.2", "6.3", "7.1", "7.2", "7.3"]
)

# ---------------- 系统指标（ctypes，无第三方库） ----------------
class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetSystemTimes = _k32.GetSystemTimes
_GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
_GetSystemTimes.restype = ctypes.c_int


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

_GlobalMemoryStatusEx = _k32.GlobalMemoryStatusEx
_GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
_GlobalMemoryStatusEx.restype = ctypes.c_int

_last_cpu = {"idle": None, "total": None, "ts": None}
_cpu_lock = threading.Lock()

def _filetime_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

def cpu_percent():
    try:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not _GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        i, k, u = _filetime_to_int(idle), _filetime_to_int(kernel), _filetime_to_int(user)
        total = k + u
        with _cpu_lock:
            prev_total = _last_cpu.get("total")
            prev_idle = _last_cpu.get("idle")
        if prev_total is None or total <= prev_total:
            return None
        dt = total - prev_total
        di = i - (prev_idle or 0)
        if dt <= 0:
            return None
        return round(max(0.0, min(100.0, (1.0 - di / dt) * 100.0)), 1)
    except Exception:
        return None

def cpu_sample():
    """仅由后台线程调用：更新基线（约 1s 窗口）"""
    try:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not _GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return
        i = _filetime_to_int(idle)
        total = _filetime_to_int(kernel) + _filetime_to_int(user)
        with _cpu_lock:
            _last_cpu.update({"idle": i, "total": total, "ts": time.time()})
    except Exception:
        pass

def memory_info():
    try:
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not _GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        total = round(m.ullTotalPhys / 1048576.0, 0)
        avail = round(m.ullAvailPhys / 1048576.0, 0)
        return {"total_mb": int(total), "avail_mb": int(avail),
                "used_mb": int(total - avail), "percent": int(m.dwMemoryLoad)}
    except Exception:
        return None

def port_open(port, host="127.0.0.1", timeout=0.25):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass

# ---------------- 数据聚合 ----------------
def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None

def first_existing(directory, names):
    for n in names:
        p = os.path.join(directory, n)
        if os.path.exists(p):
            return p
    return None

def mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0

def find_progress():
    """从证据 JSON 里找最新的 已完成任务点 N/59。"""
    best = {"n": 0, "total": 59, "source": None, "at": None}
    pat = re.compile(r"已完成任务点[:：]?\s*(\d+)\s*/\s*(\d+)")
    for sec, d in SECTION_DIRS.items():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            p = os.path.join(d, name)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    text = fh.read(200000)
            except Exception:
                continue
            for m in pat.finditer(text):
                n, total = int(m.group(1)), int(m.group(2))
                if n >= best["n"]:
                    best = {"n": n, "total": total,
                            "source": "%s/%s" % (sec, name), "at": mtime(p)}
    if best["source"] and best["at"]:
        best["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(best["at"]))
    return best

def section_state(sec):
    d = SECTION_DIRS.get(sec)
    info = {"section": sec, "dir": d, "status": "pending", "score": None,
            "video": None, "submitted": None, "ok": None, "evidence_count": 0,
            "updated_at": None, "input": None}
    if not d or not os.path.isdir(d):
        return info
    vid = first_existing(d, VIDEO_FILES)
    stt = first_existing(d, STATUS_FILES)
    ver = first_existing(d, VERIFY_FILES)
    sc = None
    sub_used = None
    for _name in SUBMIT_FILES:
        _p = os.path.join(d, _name)
        if not os.path.exists(_p):
            continue
        if sub_used is None:
            sub_used = _p
        j = read_json(_p) or {}
        cand = j.get("score")
        if cand is None and isinstance(j.get("result"), dict):
            cand = j["result"].get("score")
        if cand is None and isinstance(j.get("submit"), dict):
            cand = j["submit"].get("score")
        if cand is not None:
            sc = cand
            sub_used = _p
            info["submitted"] = j.get("status") or "已完成"
            break
    info["score"] = sc
    if sc is not None and not info.get("submitted"):
        info["submitted"] = "已完成"
    sub = sub_used
    newest = 0
    for p in (sub, vid, stt, ver):
        if p:
            newest = max(newest, mtime(p))
    info["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else None
    if vid:
        j = read_json(vid) or {}
        if j.get("skipped"):
            info["video"] = "done(skipped)"
        else:
            info["video"] = "done" if j.get("side_after") in ("1", 1) else (j.get("stop_reason") or "?")
    if stt:
        j = read_json(stt) or {}
        info["ok"] = j.get("ok")
        if j.get("abort"):
            info["abort"] = j.get("abort")
    if info["score"] is not None:
        info["status"] = "completed"
    elif info["ok"] is False:
        info["status"] = "failed"
    elif info["video"]:
        info["status"] = "video_done"
    ev_dir = os.path.join(d, "evidence")
    if os.path.isdir(ev_dir):
        info["evidence_count"] = sum(1 for x in os.listdir(ev_dir) if x.lower().endswith(".png"))
    return info

def all_sections():
    secs = list(DEFAULT_SECTIONS)
    for s in SECTION_DIRS:
        if s not in secs:
            secs.append(s)
    return secs

def collect_tasks():
    secs = all_sections()
    rows = [section_state(s) for s in secs]
    done = [r for r in rows if r["status"] == "completed"]
    return {
        "sections": rows,
        "summary": {
            "total": len(rows),
            "completed": len(done),
            "video_done": len([r for r in rows if r["status"] == "video_done"]),
            "failed": len([r for r in rows if r["status"] == "failed"]),
            "pending": len([r for r in rows if r["status"] == "pending"]),
        },
    }

def collect_status():
    prog = find_progress()
    tasks = collect_tasks()
    recent = []
    for r in tasks["sections"]:
        if r["status"] != "pending" and r["updated_at"]:
            recent.append(r)
    recent.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    running = None
    # 运行中判定：最近 120s 内有日志更新
    for sec, d in SECTION_DIRS.items():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".log"):
                continue
            p = os.path.join(d, name)
            age = time.time() - mtime(p)
            if age < 120:
                running = {"section": sec, "log": name, "age_s": int(age)}
                break
        if running:
            break
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "progress": prog,
        "running": running,
        "env": {
            "server": {"ok": True, "port": PORT},
            "cdp": {"ok": port_open(9222), "port": 9222},
            "python": sys.version.split()[0],
            "cpu_percent": cpu_percent(),
            "memory": memory_info(),
        },
        "tasks": tasks["summary"],
        "recent": recent[:6],
    }

def collect_evidence(sec):
    d = SECTION_DIRS.get(sec)
    if not d or not os.path.isdir(d):
        return {"section": sec, "dir": None, "items": []}
    items = []
    ev = os.path.join(d, "evidence")
    roots = [(d, ""), (ev, "evidence/")] if os.path.isdir(ev) else [(d, "")]
    for root, prefix in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isfile(p):
                continue
            low = name.lower()
            if not (low.endswith(".png") or low.endswith(".json") or low.endswith(".jsonl") or low.endswith(".md")):
                continue
            kind = "image" if low.endswith(".png") else ("json" if low.endswith(".json") else "text")
            items.append({
                "name": prefix + name, "kind": kind,
                "size": os.path.getsize(p),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime(p))),
                "url": "/evidence/%s/%s" % (sec, prefix + name),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"section": sec, "dir": d, "items": items}

def collect_log(tail=200):
    best = None
    for sec, d in SECTION_DIRS.items():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".log"):
                continue
            p = os.path.join(d, name)
            if best is None or mtime(p) > mtime(best[1]):
                best = (sec, p)
    if not best:
        return {"section": None, "file": None, "lines": []}
    sec, p = best
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
        lines = text.replace("\x00", "").splitlines()
    except Exception as e:
        return {"section": sec, "file": p, "lines": ["读取失败: %s" % e]}
    return {"section": sec, "file": os.path.basename(p), "lines": lines[-tail:]}

# ---------------- HTTP ----------------
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".json": "application/json; charset=utf-8",
        ".jsonl": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ChaoxingConsole/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _file(self, path):
        if not os.path.isfile(path):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        try:
            u = urlparse(self.path)
            path = unquote(u.path)
            qs = parse_qs(u.query)
            if path in ("/", "/index.html"):
                self._file(os.path.join(STATIC, "index.html")); return
            if path.startswith("/static/"):
                self._file(os.path.join(STATIC, path[len("/static/"):])); return
            if path == "/api/status":
                self._json(collect_status()); return
            if path == "/api/tasks":
                self._json(collect_tasks()); return
            if path == "/api/evidence":
                sec = (qs.get("section") or [""])[0]
                self._json(collect_evidence(sec)); return
            if path == "/api/log":
                tail = int((qs.get("tail") or ["200"])[0])
                self._json(collect_log(max(10, min(tail, 2000)))); return
            if path.startswith("/evidence/"):
                rest = path[len("/evidence/"):]
                parts = rest.split("/", 1)
                if len(parts) != 2:
                    self._send(404, b"bad evidence path", "text/plain; charset=utf-8"); return
                sec, rel = parts
                base = SECTION_DIRS.get(sec)
                if not base:
                    self._send(404, b"unknown section", "text/plain; charset=utf-8"); return
                target = os.path.normpath(os.path.join(base, rel))
                if not target.startswith(os.path.normpath(base)):
                    self._send(403, b"forbidden", "text/plain; charset=utf-8"); return
                self._file(target); return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:
            self._json({"error": str(e)[:300]}, 500)

def main():
    if not os.path.isdir(STATIC):
        print("缺少 static 目录: " + STATIC); return 2
    print("=" * 56)
    print(" 学习通自动化控制台 M1（只读）")
    print(" 地址: http://%s:%d" % (HOST, PORT))
    print(" 根目录: %s" % STAGE_D)
    print(" 按 Ctrl+C 停止")
    print("=" * 56, flush=True)

    def _cpu_sampler():
        cpu_sample()
        while True:
            time.sleep(1.0)
            cpu_sample()
    threading.Thread(target=_cpu_sampler, daemon=True).start()

    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
