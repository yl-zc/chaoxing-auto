# browser.py —— 浏览器层（默认 Edge，缺失自动回退 Chrome）
"""统一浏览器入口：启动 / CDP 探测 / 关闭旧自动化实例。
所有路径与端口来自 engine/config.json 的 browser 段，代码里不写死浏览器路径。

config 示例（engine/config.json）：
  "browser": {
    "kind": "edge",
    "exe_path": "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "fallback": {"kind": "chrome", "exe_path": "C:/Program Files/Google/Chrome/Application/chrome.exe"},
    "user_data_dir": "work/edge-profile",
    "port": 9222,
    "login_url": "https://i.chaoxing.com/base",
    "launch_args": ["--no-first-run", "--no-default-browser-check"],
    "launch_timeout_s": 30
  }

接口：
  resolve(cfg) -> dict   # 选中的浏览器 kind/exe/profile/port/endpoint
  endpoint(cfg) -> str   # http://127.0.0.1:<port>
  status(cfg) -> dict    # cdp_up/version/pages/page_title/page_url/login_page/logged_in
  launch(cfg, log=None, url=None) -> dict
  find_processes(cfg, port=None) -> [ {pid,name,cmdline,ours,top_level} ]
  close_legacy(cfg, log=None, require_login=True, dry_run=False) -> dict
CLI：
  python browser.py --status
  python browser.py --launch [--url https://i.chaoxing.com/base]
  python browser.py --close-legacy [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


class BrowserError(RuntimeError):
    pass


def resolve(cfg):
    b = cfg.get("browser") or {}
    if not b:
        raise BrowserError("配置缺少 browser 段")
    cands = []
    for tag, item in (("primary", b), ("fallback", b.get("fallback") or {})):
        if not item:
            continue
        exe = common.resolve_path(item.get("exe_path") or "")
        if not exe:
            continue
        cands.append({"kind": str(item.get("kind") or "").lower(), "exe": exe,
                      "exists": os.path.isfile(exe), "source": tag})
    chosen = next((c for c in cands if c["exists"]), None)
    if chosen is None:
        raise BrowserError("找不到可用浏览器: " + ", ".join(
            "%s=%s(exists=%s)" % (c["kind"], c["exe"], c["exists"]) for c in cands))
    port = int(b.get("port") or 9222)
    return {"kind": chosen["kind"], "exe": chosen["exe"], "source": chosen["source"],
            "profile": common.resolve_path(b["user_data_dir"]),
            "port": port, "endpoint": "http://127.0.0.1:%d" % port,
            "login_url": b.get("login_url") or "https://i.chaoxing.com/base",
            "console_url": b.get("console_url") or "http://127.0.0.1:8770/",
            "launch_args": list(b.get("launch_args") or []),
            "launch_timeout_s": float(b.get("launch_timeout_s") or 30)}


def endpoint(cfg):
    return resolve(cfg)["endpoint"]


def _http_json(url, timeout_s=3.0):
    with urllib.request.urlopen(url, timeout=timeout_s) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def status(cfg):
    """探测 CDP 与页面状态（只读，不启动浏览器）。"""
    try:
        r = resolve(cfg)
    except Exception as e:
        return {"cdp_up": False, "error": str(e)[:300], "pages": []}
    out = {"kind": r["kind"], "exe": r["exe"], "profile": r["profile"],
           "port": r["port"], "endpoint": r["endpoint"], "cdp_up": False,
           "pages": [], "page_title": None, "page_url": None,
           "login_page": False, "logged_in": False}
    try:
        ver = _http_json(r["endpoint"] + "/json/version", 3.0)
        out["cdp_up"] = True
        out["version"] = ver.get("Browser")
    except Exception as e:
        out["error"] = "CDP 不可达: %s" % str(e)[:150]
        return out
    try:
        raw = _http_json(r["endpoint"] + "/json/list", 3.0)
    except Exception as e:
        out["error"] = "读取页面失败: %s" % str(e)[:150]
        return out
    pages = [t for t in (raw or []) if t.get("type") == "page"
             and str(t.get("url") or "").startswith("http")]
    out["pages"] = [{"title": str(t.get("title") or "")[:80],
                     "url": str(t.get("url") or "")[:220]} for t in pages]
    login_keys = ("passport2", "i.chaoxing.com/base", "/login?")
    normal, logins = [], []
    for t in pages:
        url, title = str(t.get("url") or ""), str(t.get("title") or "")
        is_chaoxing = ("chaoxing" in url or "mooc" in url)
        if is_chaoxing and any(k in url for k in login_keys):
            logins.append({"url": url[:220], "title": title[:120]})
        elif is_chaoxing:
            normal.append({"url": url[:220], "title": title[:120]})
    if normal:
        # 有已登录的学习通页面：残留登录页不误判
        out["login_page"] = False
        out["logged_in"] = bool(out["cdp_up"])
        out["page_url"], out["page_title"] = normal[0]["url"], normal[0]["title"]
    elif logins:
        out["login_page"] = True
        out["logged_in"] = False
        out["page_url"], out["page_title"] = logins[0]["url"], logins[0]["title"]
    return out


def _browser_ws_url(endpoint):
    return _http_json(endpoint + "/json/version", 3.0).get("webSocketDebuggerUrl")


def create_target(cfg, url, background=True):
    """用 CDP Target.createTarget 新开标签（background=True 不抢前台）。"""
    r = resolve(cfg)
    try:
        import websocket  # engine venv 自带；daemon 侧不需要
        ws = websocket.create_connection(_browser_ws_url(r["endpoint"]), timeout=10,
                                         max_size=(1 << 24), suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                "params": {"url": url, "background": bool(background)}}))
            end = time.time() + 10
            while time.time() < end:
                ws.settimeout(max(0.2, end - time.time()))
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return (msg.get("result") or {}).get("targetId")
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception:
        pass
    # 兜底：HTTP /json/new（会激活标签，仅在 websocket 不可用时使用）
    try:
        req = urllib.request.Request(
            r["endpoint"] + "/json/new?" + urllib.parse.quote(url, safe=""),
            method="PUT")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (json.loads(resp.read().decode("utf-8", errors="replace")) or {}).get("id")
    except Exception:
        return None


def _has_url(st, needle):
    return any(needle.lower() in (p.get("url") or "").lower() for p in (st.get("pages") or []))


def ensure_tabs(cfg, log=None):
    """确保同一窗口里有「学习通」与「控制台」两个标签；只补缺失的，不关任何标签。"""
    r = resolve(cfg)
    st = status(cfg)
    if not st.get("cdp_up"):
        return []
    created = []
    if not _has_url(st, "chaoxing.com"):
        tid = create_target(cfg, r["login_url"], background=True)
        created.append({"url": r["login_url"], "targetId": tid})
        if log:
            log("补开学习通标签: %s" % r["login_url"])
    if r.get("console_url") and not _has_url(st, "127.0.0.1:8770"):
        tid = create_target(cfg, r["console_url"], background=True)
        created.append({"url": r["console_url"], "targetId": tid})
        if log:
            log("补开控制台标签: %s" % r["console_url"])
    return created


def build_argv(cfg, url=None):
    """从 config 生成启动参数（学习通 + 控制台两标签，控制台最后打开）。"""
    r = resolve(cfg)
    urls = [url or r["login_url"]]
    if r.get("console_url") and r["console_url"] not in urls:
        urls.append(r["console_url"])
    return [r["exe"], "--user-data-dir=" + r["profile"],
            "--remote-debugging-port=%d" % r["port"]] + r["launch_args"] + urls


def launch(cfg, log=None, url=None, ensure=True):
    """按 config 启动浏览器：同一窗口双标签——学习通 + 控制台（控制台最后打开、可见）。
    独立 profile 才能开 CDP 调试口；已在运行时只补缺失标签，不重复开窗口。
    """
    r = resolve(cfg)
    st = status(cfg)
    if st.get("cdp_up"):
        created = ensure_tabs(cfg, log) if ensure else []
        return {"launched": False, "already_running": True, "kind": r["kind"],
                "profile": r["profile"], "endpoint": r["endpoint"],
                "created_tabs": created, "status": status(cfg)}
    os.makedirs(r["profile"], exist_ok=True)
    argv = build_argv(cfg, url)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    if log:
        log("启动浏览器: " + " ".join(argv))
    proc = subprocess.Popen(argv, cwd=common.PROJECT_ROOT, close_fds=True,
                            creationflags=flags, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    end = time.time() + r["launch_timeout_s"]
    st = status(cfg)
    while time.time() < end and not st.get("cdp_up"):
        time.sleep(0.7)
        st = status(cfg)
    created = []
    if st.get("cdp_up") and ensure:
        created = ensure_tabs(cfg, log)
        st = status(cfg)
    return {"launched": bool(st.get("cdp_up")), "already_running": False,
            "argv": argv, "popen_pid": proc.pid, "kind": r["kind"],
            "profile": r["profile"], "endpoint": r["endpoint"],
            "created_tabs": created, "status": st}


def pick_page_index(urls, cfg):
    """引擎选页规则：只选学习通页面（allow），排除控制台/浏览器内部页（deny），
    优先课程页（course_page_hints）；无合适页面返回 None。绝不选控制台标签。
    """
    b = cfg.get("browser") or {}
    allow = [s.lower() for s in (b.get("page_url_allow") or ["chaoxing.com"])]
    deny = [s.lower() for s in (b.get("page_url_deny")
                               or ["127.0.0.1:8770", "localhost:8770", "chrome://",
                                   "edge://", "about:", "devtools://"])]
    hints = [s.lower() for s in (b.get("course_page_hints")
                                 or ["studentstudy", "mycourse", "knowledge", "mooc"])]
    cands = []
    for i, u in enumerate(urls):
        ul = (u or "").lower()
        if any(d in ul for d in deny):
            continue
        if not any(a in ul for a in allow):
            continue
        cands.append(i)
    login_markers = ("passport2", "/login?")
    normal = [i for i in cands if not any(m in (urls[i] or "").lower() for m in login_markers)]
    pool = normal or cands
    for i in pool:
        if any(h in (urls[i] or "").lower() for h in hints):
            return i
    return pool[0] if pool else None


def _ps_json(script):
    full = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); " + script
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", full],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=25)
    except Exception:
        return []
    raw = (p.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return data if isinstance(data, list) else []


PS_PROCS = (r"""$ErrorActionPreference='SilentlyContinue'; """
            r"""Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" """
            r"""| Where-Object { $_.CommandLine -like '*--remote-debugging-port=*' } """
            r"""| Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress""")


def find_processes(cfg, port=None):
    r = resolve(cfg)
    port = int(port or r["port"])
    out = []
    for it in _ps_json(PS_PROCS):
        cmd = str(it.get("CommandLine") or "")
        if ("--remote-debugging-port=%d" % port) not in cmd:
            continue
        out.append({"pid": it.get("ProcessId"), "name": it.get("Name"),
                    "cmdline": cmd[:400],
                    "ours": r["profile"] in cmd,
                    "top_level": "--type=" not in cmd})
    return out


def close_legacy(cfg, log=None, require_login=True, dry_run=False):
    """关闭占用同 CDP 端口但不是当前 profile 的旧自动化浏览器。
    只在当前浏览器 CDP 可用（require_login 时还要求已登录）后执行。
    """
    st = status(cfg)
    if not st.get("cdp_up"):
        return {"ok": False, "refused": "CDP 未就绪，拒绝关闭旧浏览器", "status": st}
    if require_login and not st.get("logged_in"):
        return {"ok": False, "refused": "当前浏览器尚未登录学习通，暂不关闭旧浏览器（避免双端互踢）", "status": st}
    procs = [p for p in find_processes(cfg) if not p["ours"] and p["top_level"]]
    killed = []
    for p in procs:
        rec = {"pid": p["pid"], "name": p["name"], "cmdline": p["cmdline"][:240]}
        if dry_run:
            rec["dry_run"] = True
            killed.append(rec)
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(p["pid"]), "/T", "/F"],
                           capture_output=True, timeout=20)
            rec["killed"] = True
        except Exception as e:
            rec["killed"] = False
            rec["error"] = str(e)[:150]
        killed.append(rec)
        if log:
            log("已关闭旧浏览器 pid=%s (%s)" % (p["pid"], p["name"]))
    return {"ok": True, "killed": killed, "count": len(killed), "status": st}


def main(argv=None):
    ap = argparse.ArgumentParser(description="浏览器层（Edge 默认 / Chrome 回退）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--url", default=None)
    ap.add_argument("--close-legacy", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pids", action="store_true")
    ap.add_argument("--print-cmd", action="store_true")
    args = ap.parse_args(argv)
    cfg = common.load_config(args.config)
    try:
        if args.launch:
            print(json.dumps(launch(cfg, log=lambda m: print(m, file=sys.stderr),
                                    url=args.url), ensure_ascii=False, indent=2))
            return 0
        if args.print_cmd:
            print(json.dumps({"argv": build_argv(cfg, args.url)}, ensure_ascii=False, indent=2))
            return 0
        if args.close_legacy:
            print(json.dumps(close_legacy(cfg, log=lambda m: print(m, file=sys.stderr),
                                          dry_run=args.dry_run),
                             ensure_ascii=False, indent=2))
            return 0
        out = status(cfg)
        if args.pids:
            out["processes"] = find_processes(cfg)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except BrowserError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
