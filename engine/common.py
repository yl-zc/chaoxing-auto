# common.py —— engine 公共设施：配置加载 / 原子 JSON / 文件锁 / 日志 / 进度上报
# 纪律：
#  - 只做基础设施，不含任何学习通页面逻辑（页面逻辑在 nav/video/capture/answer/fill/submit/verify）
#  - 不 import 控制台代码；与控制台只通过文件契约交互
#  - 所有 JSON 原子写（临时文件 + os.replace），避免控制台轮询读到半截
import json
import os
import re
import sys
import time

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE_D = os.path.dirname(ENGINE_DIR)
WORK = os.path.dirname(STAGE_D)
# 便携包：<包根>/engine + <包根>/runtime/python 存在时，以包根为相对路径基准
_PKG_PARENT = os.path.dirname(ENGINE_DIR)
if os.path.isdir(os.path.join(_PKG_PARENT, "runtime", "python")):
    PROJECT_ROOT = _PKG_PARENT
    PACKAGE_ROOT = _PKG_PARENT
else:
    PROJECT_ROOT = os.path.dirname(WORK)
    PACKAGE_ROOT = None
DEFAULT_CONFIG_PATH = os.path.join(ENGINE_DIR, "config.json")

REQUIRED_TOP_KEYS = ("schema_version", "browser", "cdp", "paths", "python", "pipeline", "nav",
                     "video", "quiz", "capture", "fill", "submit", "verify", "real",
                     "locks", "daemon", "logging")


class ConfigError(Exception):
    pass


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def epoch():
    return time.time()


def resolve_path(path):
    """相对路径按项目根解析；绝对路径原样返回（统一 normpath）。"""
    if not path:
        return ""
    p = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
    return os.path.normpath(p)


def load_config(path=None):
    p = allow = resolve_path(path) if path else DEFAULT_CONFIG_PATH
    if not os.path.isfile(p):
        raise ConfigError("配置文件不存在: %s" % p)
    with open(p, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ConfigError("配置根节点必须是对象: %s" % p)
    missing = [k for k in REQUIRED_TOP_KEYS if k not in cfg]
    if missing:
        raise ConfigError("配置缺少字段: %s (%s)" % (", ".join(missing), p))
    cfg["_config_path"] = p
    return cfg


def path(cfg, key):
    """取 paths 段并解析为绝对路径。"""
    try:
        rel = cfg["paths"][key]
    except KeyError:
        raise ConfigError("配置缺少 paths.%s" % key)
    return resolve_path(rel)


def stop_requested(cfg, since_ts):
    """检查停止请求：console/stop.json、console/stop-request.json、state.cmd.json(action=stop)。
    只认 ts > since_ts（旧请求不回溯）。返回 dict 或 None。
    """
    for key in ("stop_file", "stop_request_file", "cmd_file"):
        try:
            p = path(cfg, key)
        except Exception:
            continue
        obj = read_json(p, None)
        if not isinstance(obj, dict):
            continue
        action = str(obj.get("action") or "").lower()
        if key == "cmd_file" and action != "stop":
            continue
        if key != "cmd_file" and action not in ("", "stop"):
            continue
        try:
            ts = float(obj.get("ts") or 0)
        except Exception:
            ts = 0.0
        if ts > float(since_ts or 0):
            return {"file": key, "ts": ts, "reason": str(obj.get("reason") or action or "stop"),
                    "raw": obj}
    return None


def sanitize_text(text):
    """日志/证据脱敏：去掉 URL 的 enc/cpi/token 等参数值，以及 cookie/密码字段。"""
    s = str(text)
    s = re.sub(r"(?i)([?&](?:enc|cpi|token|ticket|stoken|p_auth|session|skey|auth|sign)=)[^&\s\"'<>]+",
               r"\1***", s)
    s = re.sub(r"(?i)((?:cookie|authorization|password|passwd|pwd)\s*[:=]\s*)[^\s,;\"'<>]+",
               r"\1***", s)
    return s


def sanitize_obj(obj):
    """递归脱敏（用于日志行与 JSON 证据）。"""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, list):
        return [sanitize_obj(x) for x in obj]
    if isinstance(obj, dict):
        return dict((k, sanitize_obj(v)) for k, v in obj.items())
    return obj


def cdp_endpoint(cfg):
    """CDP 地址统一由 browser.port 推导（不写死 9222）。"""
    port = int((cfg.get("browser") or {}).get("port") or 9222)
    return "http://127.0.0.1:%d" % port


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_atomic(path, obj, indent=2):
    """原子写 JSON：同目录临时文件 -> fsync -> os.replace（落盘前自动脱敏）。"""
    obj = sanitize_obj(obj)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def append_jsonl(path, obj):
    """追加一行 JSONL（单写者才安全；跨进程由调用方保证；自动脱敏）。"""
    obj = sanitize_obj(obj)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()


def read_jsonl(path):
    """容错读 JSONL：坏行/半截行跳过，返回 (对象列表, 坏行数)。"""
    out, bad = [], 0
    if not os.path.isfile(path):
        return out, bad
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                bad += 1
                continue
            if isinstance(obj, dict):
                out.append(obj)
            else:
                bad += 1
    return out, bad


class Log(object):
    """结构化日志：UTF-8、脱敏、带时间戳/级别/plan_id/section，支持按大小轮转。
    行格式：2026-11-08T00:53:30+0800 INFO [prefix plan=… sec=…] message
    """

    def __init__(self, path=None, prefix="", plan_id=None, section=None,
                 max_bytes=0, backup_count=3):
        self.path = path
        self.prefix = prefix
        self.plan_id = plan_id
        self.section = section
        self._fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._rotate(path, int(max_bytes or 0), int(backup_count or 1))
            self._ensure_utf8_bom(path)
            self._fh = open(path, "a", encoding="utf-8")

    def set_context(self, plan_id=None, section=None):
        """设置后续日志行的 plan/section 上下文（daemon 逐节执行时使用）。"""
        if plan_id is not None:
            self.plan_id = plan_id
        if section is not None:
            self.section = section

    @staticmethod
    def _rotate(path, max_bytes, backup_count):
        """超过 max_bytes 时滚动：path -> path.1 -> path.2 ...（保留 backup_count 份）。"""
        try:
            if max_bytes <= 0 or not os.path.isfile(path):
                return
            if os.path.getsize(path) < max_bytes:
                return
            backup_count = max(1, int(backup_count))
            oldest = path + ".%d" % backup_count
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(backup_count - 1, 0, -1):
                src = path + ".%d" % i
                dst = path + ".%d" % (i + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(path, path + ".1")
        except Exception:
            pass

    @staticmethod
    def _ensure_utf8_bom(path):
        """已有日志若无 BOM，重写为 UTF-8 BOM，避免 Windows 工具按 GBK 读成乱码。"""
        try:
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                return
            with open(path, "rb") as fh:
                head = fh.read(3)
            if head == b"\xef\xbb\xbf":
                return
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(data)
        except Exception:
            pass

    def __call__(self, msg, level="INFO"):
        ctx = []
        if self.prefix:
            ctx.append(self.prefix.strip("[]"))
        if self.plan_id:
            ctx.append("plan=%s" % self.plan_id)
        if self.section:
            ctx.append("sec=%s" % self.section)
        line = "%s %s %s%s" % (
            now_iso(), str(level).upper(),
            ("[" + " ".join(ctx) + "] ") if ctx else "",
            sanitize_text(msg))
        try:
            print(line, flush=True)
        except Exception:
            pass
        if self._fh:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:
                pass

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


class FileLock(object):
    """跨进程单实例文件锁。
    Windows 用 msvcrt 字节锁（进程退出/崩溃自动释放）；其它平台退回 fcntl。
    """

    def __init__(self, path, timeout_s=60.0, poll_s=0.2):
        self.path = path
        self.timeout_s = float(timeout_s)
        self.poll_s = float(poll_s)
        self._fh = None

    def _try_lock(self, fh):
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fh = open(self.path, "a+b")
        if os.fstat(fh.fileno()).st_size == 0:
            try:
                fh.write(b"\0")
                fh.flush()
            except Exception:
                pass
        deadline = time.time() + self.timeout_s
        while True:
            if self._try_lock(fh):
                self._fh = fh
                try:
                    fh.seek(1)
                    fh.truncate()
                    fh.write(("pid=%d at=%s\n" % (os.getpid(), now_iso())).encode("utf-8"))
                    fh.flush()
                except Exception:
                    pass
                return True
            if time.time() >= deadline:
                try:
                    fh.close()
                except Exception:
                    pass
                return False
            time.sleep(self.poll_s)

    def release(self):
        fh, self._fh = self._fh, None
        if not fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("文件锁被占用: %s" % self.path)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class Progress(object):
    """把「当前阶段/进度/文案」原子写成 progress 文件，供 queue_daemon 翻译成 state.json。
    stage 只用控制台约定的三值之一：video | quiz | submit。
    """

    def __init__(self, path, section, plan_id=None, enabled=True):
        self.path = path
        self.section = section
        self.plan_id = plan_id
        self.enabled = bool(path) and enabled
        self.last = None
        if self.enabled:
            self.set("video", 0.0, "准备开始 %s" % section)

    def set(self, stage, progress, detail, **extra):
        if stage not in ("video", "quiz", "submit"):
            raise ValueError("stage 只能是 video/quiz/submit: %r" % stage)
        rec = {"schema_version": "engine-progress/1",
               "section": self.section,
               "stage": stage,
               "progress": round(max(0.0, min(1.0, float(progress))), 4),
               "detail": str(detail)[:300],
               "plan_id": self.plan_id,
               "updated_at": now_iso()}
        rec.update(extra)
        self.last = rec
        if self.enabled:
            write_json_atomic(self.path, rec)

    def clear(self):
        if self.enabled and os.path.isfile(self.path):
            try:
                os.remove(self.path)
            except Exception:
                pass


def mmss(seconds):
    try:
        seconds = float(seconds)
    except Exception:
        return "?"
    if seconds < 0:
        return "?"
    m, s = divmod(int(seconds), 60)
    return "%d:%02d" % (m, s)
