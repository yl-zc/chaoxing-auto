# -*- coding: utf-8 -*-
"""学习通自动化 —— 便携包启动器（无 GUI，不抢前台）

职责：
  1) 起 queue_daemon.py 队列守护（子进程，无黑窗），让控制台「开始」真正能执行任务
  2) 后台线程起 uvicorn 控制台 127.0.0.1:8770（console/app.py），日志写 console/server.log
  3) 就绪后用系统默认浏览器打开控制台（open_console：优先系统默认浏览器，失败回退 webbrowser.open / startfile）
  4) 心跳与停止监控：浏览器关闭（心跳丢失）或收到停止请求时，先写安全停止请求，
     等引擎停在安全点，再收掉 daemon 与 uvicorn（不半截提交）
  5) 不调用任何抢前台 API（无 SetForegroundWindow / ShowWindow / 激活窗口）

用法（由 cmd 启动，正常是 pythonw.exe）：
  pythonw launcher.py                 # 启动
  python  launcher.py --check         # 只做路径/依赖自检，不启动任何服务（打包校验用）
  python  launcher.py --stop          # 停止（等同 停止.cmd）
  python  launcher.py --no-browser    # 启动但不自动开浏览器
  python  launcher.py --port 8770     # 覆盖端口
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

APP_TITLE = "学习通自动化"
BASE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_DIR = os.path.join(BASE, "console")
ENGINE_DIR = os.path.join(BASE, "engine")
PIPELINE_DIR = os.path.join(BASE, "pipeline")
RUNTIME_DIR = os.path.join(BASE, "runtime", "python")
SITE_PACKAGES = os.path.join(RUNTIME_DIR, "Lib", "site-packages")
RUNTIME_PY = os.path.join(RUNTIME_DIR, "python.exe")
RUNTIME_PYW = os.path.join(RUNTIME_DIR, "pythonw.exe")
CONFIGS_DIR = os.path.join(BASE, "configs")
LOGS_DIR = os.path.join(BASE, "logs")
LAUNCHER_CFG = os.path.join(CONFIGS_DIR, "launcher.json")
ENGINE_CFG = os.path.join(ENGINE_DIR, "config.json")
PIPELINE_CFG = os.path.join(PIPELINE_DIR, "config.json")
LAUNCHER_LOG = os.path.join(LOGS_DIR, "launcher.log")
SERVER_LOG = os.path.join(CONSOLE_DIR, "server.log")
PID_PATH = os.path.join(LOGS_DIR, "launcher.pid")
STOP_FILE = os.path.join(CONSOLE_DIR, "stop.json")
STOP_REQUEST_FILE = os.path.join(CONSOLE_DIR, "stop-request.json")
STATE_CMD_FILE = os.path.join(CONSOLE_DIR, "state.cmd.json")
STATE_FILE = os.path.join(CONSOLE_DIR, "state.json")
HEARTBEAT_FILE = os.path.join(CONSOLE_DIR, "last_heartbeat.json")
CONSOLE_PROFILE = os.path.join(BASE, "console-profile")

DEFAULTS = {
    "port": 8770,
    "auto_open_browser": True,
    "start_daemon": True,
    "heartbeat_grace_s": 20,
    "heartbeat_interval_s": 5,
    "on_browser_close": "stop_and_exit",   # stop_and_exit | keep
    "idle_timeout_s": 0,                    # 0 = 关闭闲置自动退出
    "keep_daemon_after_close": False,
    "open_console_via": "default",          # default = 系统默认浏览器；也可以填浏览器 exe 绝对路径
}

ARGS = sys.argv[1:]


def arg_value(flag, cast=str, default=None):
    if flag in ARGS:
        i = ARGS.index(flag)
        if i + 1 < len(ARGS):
            try:
                return cast(ARGS[i + 1])
            except Exception:
                return default
    return default


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_atomic(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def launcher_log(msg):
    """启动器日志（追加、UTF-8，不抛异常）。"""
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(LAUNCHER_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s [launcher] %s\n" % (now(), msg))
    except Exception:
        pass


def health_ok(port, timeout=1.0):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def port_open(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.8)
        try:
            return s.connect_ex(("127.0.0.1", int(port))) == 0
        finally:
            s.close()
    except Exception:
        return False


def child_env():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = SITE_PACKAGES
    env.pop("PYTHONHOME", None)
    return env


def creation_no_window():
    """子进程不弹黑窗（pythonw 下也稳妥）。"""
    if os.name != "nt":
        return 0
    flags = 0
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    return flags


def browser_exe():
    """从 engine/config.json 的 browser 段找浏览器 exe（找不到返回 None）。"""
    cfg = read_json(ENGINE_CFG, {}) or {}
    b = cfg.get("browser") or {}
    cands = [b.get("exe_path")]
    fb = b.get("fallback")
    if isinstance(fb, dict):
        cands.append(fb.get("exe_path"))
    cands += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if not c:
            continue
        p = c if os.path.isabs(c) else os.path.join(BASE, c)
        if os.path.isfile(os.path.normpath(p)):
            return os.path.normpath(p)
    return None


def open_console(port):
    """打开控制台页面（参考 work/stage-d/launcher/launcher.py 的 open_console 实现）。

    顺序：
      1) configs/launcher.json 指定 open_console_via（浏览器 exe）→ 用独立 console-profile 打开
      2) 系统默认浏览器 webbrowser.open
      3) os.startfile 兜底
    不调用 SetForegroundWindow 等抢前台 API。
    """
    url = "http://127.0.0.1:%d/" % int(port)
    via = str((read_json(LAUNCHER_CFG, {}) or {}).get("open_console_via") or "default")
    if via and via.lower() not in ("default", "system", "auto"):
        exe = via if os.path.isabs(via) else os.path.join(BASE, via)
        if os.path.isfile(exe):
            try:
                os.makedirs(CONSOLE_PROFILE, exist_ok=True)
            except Exception:
                pass
            flags = creation_no_window()
            if os.name == "nt":
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            for extra in (["--user-data-dir=" + CONSOLE_PROFILE, "--no-first-run",
                           "--no-default-browser-check", "--new-window", url], [url]):
                try:
                    subprocess.Popen([exe] + extra, close_fds=True, creationflags=flags,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    launcher_log("已用指定浏览器打开控制台: %s" % exe)
                    return True
                except Exception as e:
                    launcher_log("指定浏览器启动失败(%r): %r" % (extra[0], e))
    try:
        import webbrowser
        if webbrowser.open(url):
            launcher_log("已用系统默认浏览器打开控制台 %s" % url)
            return True
        launcher_log("webbrowser.open 返回 False，回退 startfile")
    except Exception as e:
        launcher_log("webbrowser.open 失败: %r" % e)
    try:
        if hasattr(os, "startfile"):
            os.startfile(url)  # noqa: S606 - 用户双击启动器后打开页面
            launcher_log("已用 os.startfile 打开控制台 %s" % url)
            return True
    except Exception as e:
        launcher_log("os.startfile 失败: %r" % e)
    launcher_log("无法自动打开浏览器，请手动访问 %s" % url)
    return False


class FileLock(object):
    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            fh = open(self.path, "a+b")
            if os.fstat(fh.fileno()).st_size == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            self._fh = fh
            return True
        except Exception:
            try:
                fh.close()
            except Exception:
                pass
            return False

    def release(self):
        if not self._fh:
            return
        try:
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


class App(object):
    def __init__(self, cfg, no_browser=False):
        self.cfg = cfg
        self.port = int(cfg.get("port") or 8770)
        self.auto_open = bool(cfg.get("auto_open_browser")) and not no_browser
        self.start_daemon_flag = bool(cfg.get("start_daemon"))
        self.on_browser_close = str(cfg.get("on_browser_close") or "stop_and_exit")
        self.heartbeat_grace = float(cfg.get("heartbeat_grace_s") or 20)
        self.idle_timeout = float(cfg.get("idle_timeout_s") or 0)
        self.keep_daemon = bool(cfg.get("keep_daemon_after_close"))
        self.started_at = time.time()
        self.last_activity = time.time()
        self.last_heartbeat = 0.0
        self.heartbeat_seen = False
        self.server = None
        self.thread = None
        self.daemon_proc = None
        self.daemon_pid = None
        self.external = False
        self.ready = False
        self.closing = False
        self._closed = False
        self.log_fh = None
        self._stop_dir = None

    # ---------- 停止标记 ----------
    def _write_stop_markers(self, reason):
        self._stop_dir = os.path.join(CONSOLE_DIR, ".launcher-stop-%d" % os.getpid())
        os.makedirs(CONSOLE_DIR, exist_ok=True)
        for path, extra in ((STOP_FILE, {"source": "launcher"}),
                            (STOP_REQUEST_FILE, {"source": "launcher"})):
            try:
                write_json_atomic(path, {"schema_version": "engine-stop/1", "action": "stop",
                                         "reason": str(reason)[:120], "ts": time.time(),
                                         "at": now(), "source": extra.get("source")})
            except Exception as e:
                launcher_log("写停止请求失败 %s: %r" % (os.path.basename(path), e))
        try:
            write_json_atomic(STATE_CMD_FILE, {"action": "stop", "reason": "launcher:%s" % reason,
                                               "ts": time.time(), "at": now(), "source": "launcher"})
        except Exception:
            pass

    def _clean_stale(self):
        for name in ("stop.json", "stop-request.json", "last_heartbeat.json", "last_activity.json"):
            fp = os.path.join(CONSOLE_DIR, name)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass
        cmd = read_json(STATE_CMD_FILE, None)
        try:
            if os.path.isfile(STATE_CMD_FILE) and isinstance(cmd, dict) and str(cmd.get("action") or "").lower() == "stop":
                os.remove(STATE_CMD_FILE)
        except Exception:
            pass

    def _reset_state(self):
        if self._daemon_pid_from_state():
            launcher_log("检测到 queue_daemon 已在运行（state.json daemon_pid），保留 state.json")
            return
        st = read_json(STATE_FILE, None)
        if not isinstance(st, dict):
            st = {}
        st.update({"schema_version": st.get("schema_version") or "engine-state/1",
                   "state": "idle", "current": None, "paused": False, "queue": [],
                   "detail": "引擎未启动", "daemon_pid": None, "updated_at": now()})
        try:
            write_json_atomic(STATE_FILE, st)
        except Exception as e:
            launcher_log("重置 state.json 失败: %r" % e)

    def _daemon_pid_from_state(self):
        st = read_json(STATE_FILE, None)
        if not isinstance(st, dict):
            return None
        pid = st.get("daemon_pid")
        try:
            pid = int(pid)
        except Exception:
            return None
        return pid if pid > 0 else None

    # ---------- 子进程 ----------
    def start_daemon(self):
        if not self.start_daemon_flag:
            launcher_log("配置 start_daemon=false，不启动队列守护")
            return None
        script = os.path.join(ENGINE_DIR, "queue_daemon.py")
        if not os.path.isfile(script):
            launcher_log("找不到 engine/queue_daemon.py，跳过队列守护启动")
            return None
        if not os.path.isfile(RUNTIME_PY):
            launcher_log("找不到 runtime/python/python.exe，跳过队列守护启动")
            return None
        try:
            os.makedirs(os.path.join(ENGINE_DIR, "logs"), exist_ok=True)
            stdout = open(os.path.join(ENGINE_DIR, "daemon.stdout.log"), "a", encoding="utf-8", errors="replace")
            stderr = open(os.path.join(ENGINE_DIR, "daemon.stderr.log"), "a", encoding="utf-8", errors="replace")
            proc = subprocess.Popen([RUNTIME_PY, script], cwd=BASE, env=child_env(),
                                    stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                    creationflags=creation_no_window(), close_fds=True)
            self.daemon_proc = proc
            self.daemon_pid = proc.pid
            launcher_log("已启动队列守护 queue_daemon.py pid=%d" % proc.pid)
            return proc
        except Exception as e:
            launcher_log("启动队列守护失败: %r" % e)
            return None

    def stop_daemon(self):
        pid = self.daemon_pid or self._daemon_pid_from_state()
        if not pid:
            return []
        pids = [int(pid)]
        # 先温和结束，再 /T /F 兜底（只对本次/本包 daemon pid）
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20,
                           creationflags=creation_no_window() if os.name == "nt" else 0)
            launcher_log("已结束队列守护 pid=%s" % pid)
        except Exception as e:
            launcher_log("结束队列守护失败 pid=%s: %r" % (pid, e))
        self.daemon_proc = None
        return pids

    def _daemon_alive(self):
        proc = self.daemon_proc
        if proc is not None:
            return proc.poll() is None
        pid = self._daemon_pid_from_state()
        if not pid:
            return False
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=creation_no_window() if os.name == "nt" else 0)
            return str(pid) in (out.stdout or "")
        except Exception:
            return False

    # ---------- 控制台服务 ----------
    def _serve(self):
        try:
            self.server.run()
        except Exception:
            launcher_log("uvicorn 退出异常:\n" + traceback.format_exc()[-1500:])

    def start_service(self):
        if health_ok(self.port):
            self.external = True
            self.ready = True
            launcher_log("检测到控制台已在 %d 运行，不重复启动" % self.port)
            if self.auto_open:
                open_console(self.port)
            return
        if port_open(self.port):
            launcher_log("端口 %d 已被其它程序占用，启动终止" % self.port)
            raise RuntimeError("端口 %d 已被其它程序占用" % self.port)
        self._clean_stale()
        self._reset_state()
        os.makedirs(os.path.join(CONSOLE_DIR, "plans"), exist_ok=True)
        os.makedirs(os.path.join(CONSOLE_DIR, "exports"), exist_ok=True)
        try:
            self.log_fh = open(SERVER_LOG, "a", encoding="utf-8", buffering=1)
            sys.stdout = self.log_fh
            sys.stderr = self.log_fh
        except Exception:
            pass
        if CONSOLE_DIR not in sys.path:
            sys.path.insert(0, CONSOLE_DIR)
        self.start_daemon()
        launcher_log("启动控制台服务 port=%d console=%s" % (self.port, CONSOLE_DIR))
        import uvicorn
        config = uvicorn.Config("app:app", host="127.0.0.1", port=self.port,
                                log_level="info", access_log=False,
                                log_config=None if False else uvicorn.config.LOGGING_CONFIG)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._serve, name="uvicorn", daemon=True)
        self.thread.start()
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.time() + 60
        while time.time() < deadline:
            if health_ok(self.port):
                self.ready = True
                self.started_at = time.time()
                self.last_activity = time.time()
                launcher_log("控制台就绪: http://127.0.0.1:%d/" % self.port)
                if self.auto_open:
                    open_console(self.port)
                return True
            if self.server is not None and getattr(self.server, "should_exit", False):
                launcher_log("uvicorn 提前退出，未就绪")
                return False
            time.sleep(0.5)
        launcher_log("控制台启动超时（60s），请看 console/server.log")
        return False

    def stop_service(self):
        try:
            if self.server is not None:
                self.server.should_exit = True
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=10)
            launcher_log("控制台服务已停止")
        except Exception as e:
            launcher_log("停止控制台异常: %r" % e)

    # ---------- 状态 / 心跳 ----------
    def engine_state(self):
        st = read_json(STATE_FILE, None)
        return st if isinstance(st, dict) else {}

    def _read_heartbeat(self):
        j = read_json(HEARTBEAT_FILE, None)
        if not isinstance(j, dict):
            return None
        if j.get("stopping"):
            return "stopping"
        try:
            ts = float(j.get("ts") or 0)
        except Exception:
            return None
        if ts > self.last_heartbeat:
            self.last_heartbeat = ts
        if ts > 0:
            self.heartbeat_seen = True
        return "ok"

    def _stop_requested(self):
        for path in (STOP_FILE, STOP_REQUEST_FILE):
            j = read_json(path, None)
            if not isinstance(j, dict):
                continue
            if str(j.get("action") or "stop").lower() != "stop":
                continue
            try:
                ts = float(j.get("ts") or 0)
            except Exception:
                ts = 0.0
            if ts >= self.started_at - 2:
                return str(j.get("reason") or "stop")
        return None

    def wait_engine_idle(self, timeout=180):
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            state = str(self.engine_state().get("state") or "idle")
            if state not in ("running", "paused", "verifying"):
                launcher_log("引擎已停在安全点: state=%s" % state)
                return True
            time.sleep(1.0)
        launcher_log("等待引擎停止超时（%ss），仍按停止流程收尾" % timeout)
        return False

    def safe_stop(self, reason):
        if self.closing:
            return
        self.closing = True
        launcher_log("开始安全停止（reason=%s）" % reason)
        if reason not in ("manual_stop", "stop_cmd"):
            self._write_stop_markers(reason)
        self.wait_engine_idle(180 if reason != "manual_stop" else 120)
        if not self.keep_daemon:
            self.stop_daemon()
        self.stop_service()

    # ---------- 监控线程 ----------
    def _monitor_loop(self):
        while not self.closing:
            try:
                if self.external:
                    time.sleep(2.0)
                    continue
                reason = self._stop_requested()
                if reason:
                    self.safe_stop("manual_stop")
                    break
                if self.on_browser_close == "stop_and_exit":
                    hb = self._read_heartbeat()
                    now_ts = time.time()
                    if hb == "stopping":
                        self.safe_stop("heartbeat_stopping")
                        break
                    if self.heartbeat_seen:
                        if now_ts - self.last_heartbeat > self.heartbeat_grace:
                            self.safe_stop("browser_closed")
                            break
                    elif now_ts - self.started_at > (self.heartbeat_grace + 15):
                        self.safe_stop("browser_not_opened")
                        break
                if self.idle_timeout > 0:
                    st = self.engine_state()
                    state = str(st.get("state") or "idle")
                    if state not in ("running", "paused", "verifying"):
                        if self.daemon_proc is not None and self.daemon_proc.poll() is not None:
                            pass
                        if (time.time() - self.last_activity) >= self.idle_timeout:
                            self.safe_stop("idle_timeout")
                            break
                if self.daemon_proc is not None and self.daemon_proc.poll() is not None:
                    launcher_log("队列守护进程已退出 rc=%s（任务将不会被执行，请重启启动器）"
                                 % self.daemon_proc.returncode)
                    self.daemon_proc = None
            except Exception as e:
                launcher_log("监控线程异常: %r" % e)
            time.sleep(1.0)

    # ---------- 生命周期 ----------
    def run(self):
        self.start_service()
        if not self.ready:
            return 2
        th = threading.Thread(target=self._monitor_loop, daemon=True)
        th.start()
        while not self.closing:
            time.sleep(0.5)
        self._shutdown()
        return 0

    def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.stop_service()
        try:
            if not self.keep_daemon:
                self.stop_daemon()
        except Exception:
            pass
        try:
            if self.log_fh:
                self.log_fh.close()
        except Exception:
            pass
        try:
            if os.path.isfile(PID_PATH):
                os.remove(PID_PATH)
        except Exception:
            pass
        launcher_log("启动器已退出")


def load_config():
    cfg = dict(DEFAULTS)
    j = read_json(LAUNCHER_CFG, None)
    if isinstance(j, dict):
        for k in DEFAULTS:
            if k in j:
                cfg[k] = j[k]
    return cfg


def run_check():
    """只读自检：不启动服务。返回 0=通过。"""
    checks = []

    def add(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "required": bool(required), "detail": str(detail)[:300]})

    add("launcher_dir", os.path.isdir(BASE), BASE)
    add("runtime_python", os.path.isfile(RUNTIME_PY), RUNTIME_PY)
    add("runtime_pythonw", os.path.isfile(RUNTIME_PYW), RUNTIME_PYW)
    add("site_packages", os.path.isdir(SITE_PACKAGES), SITE_PACKAGES)
    add("console_app", os.path.isfile(os.path.join(CONSOLE_DIR, "app.py")), os.path.join(CONSOLE_DIR, "app.py"))
    add("console_static", os.path.isdir(os.path.join(CONSOLE_DIR, "static")), os.path.join(CONSOLE_DIR, "static"))
    add("engine_daemon", os.path.isfile(os.path.join(ENGINE_DIR, "queue_daemon.py")),
        os.path.join(ENGINE_DIR, "queue_daemon.py"))
    add("engine_config", bool(read_json(ENGINE_CFG, None)), ENGINE_CFG)
    pc = read_json(PIPELINE_CFG, None)
    add("pipeline_config", bool(pc), PIPELINE_CFG)
    if isinstance(pc, dict):
        for key in ("model", "mmproj"):
            p = pc.get(key) or ""
            if p and not os.path.isabs(p):
                p = os.path.normpath(os.path.join(BASE, p))
            add("model_%s" % key, os.path.isfile(p), p)
    try:
        import fastapi
        import uvicorn
        import llama_cpp
        import numpy
        import PIL
        import docx
        import pypdf
        add("imports", True, "fastapi=%s uvicorn=%s llama_cpp=%s numpy=%s PIL=%s docx=%s pypdf=%s" % (
            fastapi.__version__, uvicorn.__version__, getattr(llama_cpp, "__version__", "?"),
            numpy.__version__, PIL.__version__, getattr(docx, "__version__", "?"), pypdf.__version__))
    except Exception as e:
        add("imports", False, repr(e)[:250])
    add("port_%d_free_or_console" % int(load_config().get("port") or 8770),
        (not port_open(int(load_config().get("port") or 8770))) or health_ok(int(load_config().get("port") or 8770)),
        "端口空闲或已有控制台在跑", required=False)
    ok = all(c["ok"] for c in checks if c["required"])
    out = {"schema_version": "package-launcher-check/1", "at": now(), "base": BASE,
           "ok": ok, "checks": checks}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=creation_no_window() if os.name == "nt" else 0)
        return str(int(pid)) in (out.stdout or "")
    except Exception:
        return False


def _scan_launcher_pids():
    """兜底：按命令行匹配本包的 launcher.py（排除自身），用于 pid 文件缺失时停止。"""
    script = ("$ErrorActionPreference='SilentlyContinue'; "
              "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*launcher.py*' "
              "} | ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=25,
                           creationflags=creation_no_window() if os.name == "nt" else 0)
    except Exception:
        return []
    me = os.getpid()
    target = os.path.normpath(os.path.join(BASE, "launcher.py")).lower()
    pids = []
    for line in (p.stdout or "").splitlines():
        if "|" not in line:
            continue
        pid_s, cmd = line.split("|", 1)
        if not pid_s.strip().isdigit():
            continue
        pid = int(pid_s.strip())
        if pid == me:
            continue
        if target in os.path.normpath(cmd).lower():
            pids.append(pid)
    return pids


def _write_manual_stop_markers():
    for path in (STOP_FILE, STOP_REQUEST_FILE):
        try:
            write_json_atomic(path, {"schema_version": "engine-stop/1", "action": "stop",
                                     "reason": "manual_stop", "ts": time.time(), "at": now(),
                                     "source": "stop-cmd"})
        except Exception:
            pass
    try:
        write_json_atomic(STATE_CMD_FILE, {"action": "stop", "reason": "manual_stop",
                                           "ts": time.time(), "at": now(), "source": "stop-cmd"})
    except Exception:
        pass


def run_stop():
    """停止：先写安全停止请求，等引擎停在安全点，再结束启动器（含 daemon 子进程）。"""
    cfg = load_config()
    port = int(cfg.get("port") or 8770)
    launcher_log("收到停止请求（launcher.py --stop）")
    _write_manual_stop_markers()
    pid = None
    try:
        with open(PID_PATH, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0) or None
    except Exception:
        pid = None
    if pid and not _pid_alive(pid):
        pid = None
    if not pid:
        pids = _scan_launcher_pids()
        pid = pids[0] if pids else None
    if not pid:
        launcher_log("未发现运行中的启动器进程；已写入停止请求（若有 daemon 会在安全点退出）")
        print("未发现运行中的启动器进程；已写入停止请求（若引擎在跑，会在安全点自行停止）。")
        return 0
    launcher_log("等待启动器 pid=%d 安全退出…" % pid)
    deadline = time.time() + 200
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(1.0)
    if _pid_alive(pid):
        launcher_log("启动器 200s 未自行退出，执行强制结束 pid=%d" % pid)
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                           creationflags=creation_no_window() if os.name == "nt" else 0)
        except Exception as e:
            launcher_log("taskkill 失败: %r" % e)
    time.sleep(1.0)
    try:
        if os.path.isfile(PID_PATH):
            os.remove(PID_PATH)
    except Exception:
        pass
    still = health_ok(port, 1.0)
    print(("已停止：控制台端口 %d 已释放。" % port) if not still
          else ("停止请求已发出，但端口 %d 仍有服务在响应，请稍后再试或重启电脑。" % port))
    launcher_log("停止流程完成，port_open=%s" % still)
    return 0 if not still else 3


def main():
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
    except Exception:
        pass
    if "--check" in ARGS:
        return run_check()
    if "--stop" in ARGS:
        return run_stop()
    cfg = load_config()
    port = arg_value("--port", int, cfg.get("port") or 8770)
    cfg["port"] = port
    no_browser = "--no-browser" in ARGS
    launcher_log("=" * 40)
    launcher_log("启动器启动 base=%s port=%s auto_open=%s" % (BASE, port, cfg.get("auto_open_browser")))
    lock = FileLock(os.path.join(BASE, "launcher.lock"))
    if not lock.acquire():
        launcher_log("检测到已有启动器实例（launcher.lock 被占用），直接退出")
        return 0
    app = App(cfg, no_browser=no_browser)
    try:
        with open(PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass
    try:
        rc = app.run()
        return rc
    except Exception:
        launcher_log("启动器未捕获异常:\n" + traceback.format_exc()[-2000:])
        try:
            app._shutdown()
        except Exception:
            pass
        return 1
    finally:
        try:
            lock.release()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
