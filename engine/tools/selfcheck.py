# -*- coding: utf-8 -*-
"""环境自检（只读 + 最小写测试，不安装/不下载/不发消息）
用法：
  .\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\selfcheck.py [--json]
检查：python/依赖/浏览器可执行文件/CDP/登录/配置/pipeline 依赖/目录可写/磁盘/控制台。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, ENGINE)

import common
import browser as browser_layer


def _check(name, ok, detail="", required=True):
    return {"name": name, "ok": bool(ok), "required": bool(required), "detail": str(detail)[:300]}


def _run_py(py, code, timeout=25):
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode == 0, (p.stdout or "").strip() or (p.stderr or "").strip()[:200]
    except Exception as e:
        return False, repr(e)[:200]


def main(argv=None):
    ap = argparse.ArgumentParser(description="学习通自动化引擎环境自检")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    checks = []
    t0 = time.time()

    # 1) python
    checks.append(_check("python", sys.version_info >= (3, 8),
                         "%s (%s)" % (sys.version.split()[0], sys.executable)))

    # 2) 配置
    try:
        cfg = common.load_config(args.config)
        checks.append(_check("config", True, cfg.get("_config_path")))
    except Exception as e:
        cfg = None
        checks.append(_check("config", False, str(e)[:200]))

    if cfg:
        engine_py = common.resolve_path((cfg.get("python") or {}).get("engine") or sys.executable)
        ok, detail = _run_py(engine_py, "import playwright, websocket; print('ok')")
        checks.append(_check("engine_deps", ok and "ok" in detail, detail))

        pipeline_py = common.resolve_path((cfg.get("python") or {}).get("pipeline") or "")
        checks.append(_check("pipeline_python", bool(pipeline_py) and os.path.isfile(pipeline_py),
                             pipeline_py))
        if pipeline_py and os.path.isfile(pipeline_py):
            ok, detail = _run_py(pipeline_py, "import llama_cpp, PIL; print('ok')", timeout=60)
            checks.append(_check("pipeline_deps", ok and "ok" in detail, detail))

        try:
            r = browser_layer.resolve(cfg)
            checks.append(_check("browser_exe", os.path.isfile(r["exe"]),
                                 "%s (%s) profile=%s" % (r["kind"], r["exe"], r["profile"])))
        except Exception as e:
            checks.append(_check("browser_exe", False, str(e)[:200]))

        st = browser_layer.status(cfg)
        checks.append(_check("cdp", st.get("cdp_up"),
                             "%s %s" % (st.get("version") or "", st.get("error") or "")))
        checks.append(_check("logged_in", st.get("logged_in"),
                             st.get("page_url") or "未登录/无学习通页面"))

        try:
            test_dir = common.path(cfg, "logs_dir")
            os.makedirs(test_dir, exist_ok=True)
            p = os.path.join(test_dir, ".selfcheck.tmp")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(p)
            checks.append(_check("writable", True, test_dir))
        except Exception as e:
            checks.append(_check("writable", False, str(e)[:200]))

        try:
            du = shutil.disk_usage(common.PROJECT_ROOT)
            free_gb = round(du.free / 1073741824.0, 1)
            checks.append(_check("disk", du.free > 2 * 1073741824, "free=%s GB" % free_gb))
        except Exception as e:
            checks.append(_check("disk", False, str(e)[:150]))

        try:
            endpoint = "http://127.0.0.1:8770"
            with urllib.request.urlopen(endpoint + "/api/health", timeout=3) as resp:
                alive = resp.status == 200
            checks.append(_check("console", alive, endpoint, required=False))
        except Exception as e:
            checks.append(_check("console", False, str(e)[:150], required=False))

    try:
        ver = common.read_json(os.path.join(ENGINE, "version.json"), {}) or {}
        checks.append(_check("version", bool(ver.get("version")),
                             "%s (schemas=%s)" % (ver.get("version"), ver.get("schema_version"))))
    except Exception as e:
        checks.append(_check("version", False, str(e)[:150]))

    ok = all(c["ok"] for c in checks if c["required"])
    out = {"schema_version": "engine-selfcheck/1", "at": common.now_iso(),
           "elapsed_s": round(time.time() - t0, 1), "ok": ok,
           "python": sys.executable, "checks": checks}
    if args.json:
        print(json.dumps(common.sanitize_obj(out), ensure_ascii=False))
    else:
        print("=" * 60)
        print("学习通自动化引擎自检  %s" % ("PASS" if ok else "FAIL"))
        for c in checks:
            mark = "OK  " if c["ok"] else ("FAIL" if c["required"] else "WARN")
            print("[%s] %-14s %s" % (mark, c["name"], c["detail"]))
        print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
