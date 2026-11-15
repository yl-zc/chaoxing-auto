# -*- coding: utf-8 -*-
"""学习通自动化 便携包自检（不启动服务、不碰浏览器）

检查：①包内 runtime/python 依赖能否 import（fastapi/uvicorn/llama_cpp/numpy/PIL/docx/pypdf）
     ②包内文件/配置是否齐全（runtime python、模型、configs、console/engine/pipeline）
     ③端口 8770 / 9222 状态（只探测，不启动）
用法：python selfcheck.py [--json]
退出码：0=通过；1=有问题
"""
import importlib
import json
import os
import socket
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_DIR = os.path.join(BASE, "console")
ENGINE_DIR = os.path.join(BASE, "engine")
PIPELINE_DIR = os.path.join(BASE, "pipeline")
RUNTIME_PY = os.path.join(BASE, "runtime", "python", "python.exe")
SITE_PACKAGES = os.path.join(BASE, "runtime", "python", "Lib", "site-packages")
MODELS_DIR = os.path.join(BASE, "runtime", "models")
CONFIGS_DIR = os.path.join(BASE, "configs")
LOGS_DIR = os.path.join(BASE, "logs")
REQUIRED_MODULES = ["fastapi", "uvicorn", "llama_cpp", "numpy", "PIL", "docx", "pypdf"]
MODEL_FILES = ["Qwen3-VL-4B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3-VL-4B-Instruct-F16.gguf"]


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def port_open(port, timeout=0.8):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            return s.connect_ex(("127.0.0.1", int(port))) == 0
        finally:
            s.close()
    except Exception:
        return False


def http_ok(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    as_json = "--json" in sys.argv[1:]
    checks = []

    def add(name, ok, detail="", required=True):
        checks.append({"name": name, "ok": bool(ok), "required": bool(required), "detail": str(detail)[:300]})

    add("package_root", os.path.isdir(BASE), BASE)
    add("runtime_python", os.path.isfile(RUNTIME_PY), RUNTIME_PY)
    add("site_packages", os.path.isdir(SITE_PACKAGES), SITE_PACKAGES)
    add("console_app", os.path.isfile(os.path.join(CONSOLE_DIR, "app.py")), os.path.join(CONSOLE_DIR, "app.py"))
    add("engine_daemon", os.path.isfile(os.path.join(ENGINE_DIR, "queue_daemon.py")), os.path.join(ENGINE_DIR, "queue_daemon.py"))
    add("pipeline_runner", os.path.isfile(os.path.join(PIPELINE_DIR, "run_pipeline.py")), os.path.join(PIPELINE_DIR, "run_pipeline.py"))
    add("launcher", os.path.isfile(os.path.join(BASE, "launcher.py")), os.path.join(BASE, "launcher.py"))
    add("configs_dir", os.path.isdir(CONFIGS_DIR), CONFIGS_DIR)
    for name in ("engine-config.json", "pipeline-config.json", "launcher.json"):
        p = os.path.join(CONFIGS_DIR, name)
        ok = False
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                ok = isinstance(json.load(fh), dict)
        except Exception:
            ok = False
        add("config_" + name, ok, p)

    # 模型
    for name in MODEL_FILES:
        p = os.path.join(MODELS_DIR, name)
        ok = os.path.isfile(p) and os.path.getsize(p) > 100 * 1024 * 1024
        add("model_" + name.split("-")[0], ok, "%s (%s MB)" % (p, round(os.path.getsize(p) / 1048576, 1) if os.path.isfile(p) else 0))

    # 配置里的模型路径可解析
    try:
        with open(os.path.join(PIPELINE_DIR, "config.json"), "r", encoding="utf-8-sig") as fh:
            pc = json.load(fh)
        for key in ("model", "mmproj"):
            p = pc.get(key) or ""
            if p and not os.path.isabs(p):
                p = os.path.normpath(os.path.join(BASE, p))
            add("pipeline_%s_exists" % key, os.path.isfile(p), p)
        with open(os.path.join(ENGINE_DIR, "config.json"), "r", encoding="utf-8-sig") as fh:
            ec = json.load(fh)
        for key in ("engine", "pipeline"):
            p = (ec.get("python") or {}).get(key) or ""
            if p and not os.path.isabs(p):
                p = os.path.normpath(os.path.join(BASE, p))
            add("engine_python_%s" % key, os.path.isfile(p), p)
    except Exception as e:
        add("config_parse", False, repr(e)[:200])

    # 依赖
    versions, missing = {}, []
    for mod in REQUIRED_MODULES:
        try:
            m = importlib.import_module(mod)
            versions[mod] = getattr(m, "__version__", "?")
        except Exception as e:
            missing.append("%s(%r)" % (mod, e))
    add("imports", not missing,
        ("缺失: " + "; ".join(missing)) if missing else
        " ".join("%s=%s" % (k, v) for k, v in versions.items()))

    # 端口（仅探测）
    console_up = port_open(8770)
    add("console_port_8770", console_up or not port_open(8770),
        "8770 " + ("在运行（控制台已启动）" if console_up else "空闲（未启动）"), required=False)
    health = http_ok("http://127.0.0.1:8770/api/health")
    add("console_health", health, "http://127.0.0.1:8770", required=False)
    cdp_up = http_ok("http://127.0.0.1:9222/json/version", timeout=1.0)
    add("browser_cdp_9222", cdp_up, "浏览器 CDP " + ("可达" if cdp_up else "未启动/未登录（启动自动化浏览器.cmd 后再看）"), required=False)

    ok = all(c["ok"] for c in checks if c["required"])
    out = {"schema_version": "package-selfcheck/1", "at": now(), "base": BASE,
           "python": sys.version.split()[0], "ok": ok, "checks": checks}
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(os.path.join(LOGS_DIR, "selfcheck.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("学习通自动化 便携包自检  %s" % now())
        print("包目录: %s" % BASE)
        print("=" * 60)
        for c in checks:
            tag = "OK  " if c["ok"] else ("FAIL" if c["required"] else "WARN")
            print("%s %-28s %s" % (tag, c["name"], c["detail"]))
        print("=" * 60)
        print("结论：%s" % ("通过（可以启动使用）" if ok else "有问题，见上面 FAIL 项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
