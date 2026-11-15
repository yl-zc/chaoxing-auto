# -*- coding: utf-8 -*-
"""切换「跳过答题」开关：configs/engine-config.json 与 engine/config.json 的 answer.skip_quiz 同步切换。
skip_quiz=true：只做课程视频，不做章节测验、不回填、不提交（该节记 skipped_quiz=true 并继续下一节）。
用法：python toggle_skip_quiz.py [--json]
"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
TARGETS = [
    os.path.join(BASE, "configs", "engine-config.json"),
    os.path.join(BASE, "engine", "config.json"),
]


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def atomic_write(path, obj):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main():
    as_json = "--json" in sys.argv[1:]
    result = {"ok": True, "changed": [], "errors": [], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    current = None
    for path in TARGETS:
        try:
            cfg = read_json(path)
        except Exception as e:
            result["errors"].append("%s: %r" % (path, e))
            continue
        ans = cfg.setdefault("answer", {})
        ans["enabled"] = True
        if current is None:
            current = bool(ans.get("skip_quiz"))
            ans["skip_quiz"] = not current
        else:
            ans["skip_quiz"] = not current
        try:
            atomic_write(path, cfg)
            result["changed"].append(os.path.relpath(path, BASE))
        except Exception as e:
            result["errors"].append("%s: %r" % (path, e))
    if result["errors"]:
        result["ok"] = False
    state = "开启（只做视频，不做章节测验 / 不提交）" if (not current) else "关闭（正常：视频 + 章节测验 + 提交）"
    result["skip_quiz"] = (not current) if current is not None else None
    result["state_text"] = state
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 56)
        print("跳过答题（skip_quiz）已切换为：%s" % state)
        print("已写入：%s" % (", ".join(result["changed"]) or "（无）"))
        if result["errors"]:
            print("错误：%s" % "; ".join(result["errors"]))
        print("说明：开关=engine/config.json 的 answer.skip_quiz；下个计划/重启启动器后生效。")
        print("=" * 56)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
