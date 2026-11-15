# -*- coding: utf-8 -*-
"""打包（给用户独立测试）：zip 附带引擎/流水线/控制台源码 + 最近 N 天证据，排除 venv/profile/大日志。
用法：
  .\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\package.py [--keep-days 7] [--dry-run]
"""
import argparse
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, ENGINE)

import common

EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".locks", "edge-profile", "chrome-profile",
                "node_modules", "dist", "exports"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".tmp")
EXCLUDE_PREFIX = ("work/.venv", "work/edge-profile", "work/chrome-profile")


def _skip(rel, keep_cutoff):
    rel_n = rel.replace("\\", "/")
    parts = rel_n.split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if rel_n.endswith(EXCLUDE_SUFFIX):
        return True
    if any(rel_n.startswith(x) for x in EXCLUDE_PREFIX):
        return True
    base = os.path.basename(rel_n)
    if base in ("daemon.stdout.log", "daemon.stderr.log", "server.err.log", "uvicorn.err.log"):
        return True
    if "/runs/" in rel_n and rel_n.lower().endswith(".png"):
        try:
            if os.path.getmtime(os.path.join(common.PROJECT_ROOT, rel_n)) < keep_cutoff:
                return True
        except Exception:
            pass
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="打包引擎（排除 venv/profile/大文件）")
    ap.add_argument("--keep-days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--outdir", default="dist")
    args = ap.parse_args(argv)
    root = common.PROJECT_ROOT
    keep_cutoff = time.time() - max(1, args.keep_days) * 86400
    includes = ["work/stage-d/engine", "work/stage-d/pipeline", "work/stage-d/console",
                "work/stage-d/MVP-REPORT.md"]
    files = []
    for inc in includes:
        base = os.path.join(root, inc)
        if os.path.isfile(base):
            files.append(os.path.relpath(base, root).replace("\\", "/"))
            continue
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if _skip(rel, keep_cutoff):
                    continue
                files.append(rel)
    files = sorted(set(files))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "dryrun" if args.dry_run else stamp
    outdir = os.path.join(root, args.outdir)
    os.makedirs(outdir, exist_ok=True)
    zip_path = os.path.join(outdir, "chaoxing-engine-%s.zip" % tag)
    total = 0
    if not args.dry_run:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in files:
                full = os.path.join(root, rel.replace("/", os.sep))
                try:
                    zf.write(full, rel)
                    total += os.path.getsize(full)
                except Exception:
                    pass
    manifest = {"schema_version": "engine-package/1", "at": common.now_iso(),
                "zip": zip_path if not args.dry_run else None,
                "keep_days": args.keep_days, "files": len(files), "bytes": total,
                "excluded": ["work/.venv", "work/edge-profile", "work/chrome-profile",
                             "__pycache__", "*.pyc", ".locks", "console/exports",
                             "runs/**/evidence/*.png 早于 keep-days"],
                "included_roots": includes}
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
