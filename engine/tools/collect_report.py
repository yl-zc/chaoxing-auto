# -*- coding: utf-8 -*-
"""汇总报告：扫描 engine/runs/**/run.json -> report.md + report.json（打包给用户复查）
用法：
  .\work\.venv\Scripts\python.exe .\work\stage-d\engine\tools\collect_report.py [--json]
"""
import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, ENGINE)

import common


def _rel(path):
    try:
        return os.path.relpath(path, common.PROJECT_ROOT).replace("\\", "/")
    except Exception:
        return path


def load_runs(cfg):
    runs = []
    for p in glob.glob(os.path.join(common.path(cfg, "runs_dir"), "*", "*", "run.json")):
        j = common.read_json(p, None)
        if not isinstance(j, dict):
            continue
        run_dir = os.path.dirname(p)
        runs.append({
            "section": j.get("section"), "mode": j.get("mode"), "status": j.get("status"),
            "title": j.get("title"), "score": j.get("score"),
            "progress_total": j.get("progress_total"),
            "next_section": j.get("next_section"),
            "skipped": j.get("skipped"), "all_done": j.get("all_done"),
            "elapsed_s": j.get("elapsed_s"),
            "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
            "error": (j.get("error") or "")[:300],
            "run_dir": _rel(run_dir),
        })
    runs.sort(key=lambda r: (r.get("finished_at") or r.get("started_at") or ""))
    return runs


def build(cfg):
    runs = load_runs(cfg)
    by_section = {}
    for r in runs:
        by_section[r.get("section") or "-"] = r
    state = common.read_json(common.path(cfg, "state_file"), {}) or {}
    prog = state.get("progress") or {}
    failures = [r for r in runs if r.get("status") == "failed"]
    ok_runs = [r for r in runs if r.get("status") in ("completed", "already_done", "skipped", "dry_ok")]
    version = common.read_json(os.path.join(ENGINE, "version.json"), {}) or {}
    return {
        "schema_version": "engine-report/1",
        "generated_at": common.now_iso(),
        "version": version.get("version"),
        "summary": {
            "runs": len(runs), "ok_runs": len(ok_runs), "failed_runs": len(failures),
            "sections": len(by_section),
            "page_progress": "%s/%s" % (prog.get("n"), prog.get("total")) if prog else None,
        },
        "state": common.sanitize_obj({"state": state.get("state"),
                                      "last_result": state.get("last_result"),
                                      "progress": state.get("progress"),
                                      "queue": state.get("queue")}),
        "sections": [by_section[k] for k in sorted(by_section)],
        "runs": runs,
        "failures": failures,
    }


def render_md(report):
    s = report["summary"]
    lines = ["# 学习通自动化引擎运行报告", "",
             "- 生成时间：%s" % report["generated_at"],
             "- 引擎版本：%s（schema=engine-report/1）" % report.get("version"),
             "- 运行次数：%s；成功/跳过：%s；失败：%s" % (s["runs"], s["ok_runs"], s["failed_runs"]),
             "- 页面真实进度：%s" % (s.get("page_progress") or "未知"),
             "- 当前 state：%s" % ((report.get("state") or {}).get("state")),
             "",
             "## 每节最新状态", "",
             "| 节 | 状态 | 成绩 | 进度 | 耗时(s) | 完成时间 | 证据目录 |",
             "|---|---|---|---|---|---|---|"]
    for r in report["sections"]:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("section") or "-", r.get("status") or "-",
            r.get("score") if r.get("score") is not None else "-",
            r.get("progress_total") if r.get("progress_total") is not None else "-",
            r.get("elapsed_s") or "-", r.get("finished_at") or "-", r.get("run_dir") or "-"))
    lines += ["", "## 失败明细", ""]
    if report["failures"]:
        lines.append("| 节 | 时间 | 错误 | 证据目录 |")
        lines.append("|---|---|---|---|")
        for r in report["failures"]:
            lines.append("| %s | %s | %s | %s |" % (
                r.get("section") or "-", r.get("finished_at") or "-",
                (r.get("error") or "").replace("|", "/")[:160], r.get("run_dir") or "-"))
    else:
        lines.append("无失败记录。")
    lines += ["", "## 看哪里（复查入口）", "",
              "- 单节证据：runs/<节>/<时间戳>/（run.log 人读 / run.json 机读 / progress.json / evidence 截图）",
              "- 全局索引：logs/index.jsonl（每次运行一行）",
              "- 守护日志：engine/daemon.log（UTF-8、脱敏、>20MB 自动轮转）",
              "- 环境自检：work/.venv/Scripts/python.exe engine/tools/selfcheck.py",
              "- 重新生成报告：work/.venv/Scripts/python.exe engine/tools/collect_report.py",
              "- 错误码处置：见 engine/RUNBOOK.md 第 3 节",
              ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成引擎运行汇总报告")
    ap.add_argument("--config", default=None)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    cfg = common.load_config(args.config)
    outdir = args.outdir or common.path(cfg, "logs_dir")
    os.makedirs(outdir, exist_ok=True)
    report = build(cfg)
    md_path = os.path.join(outdir, "report.md")
    js_path = os.path.join(outdir, "report.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_md(report))
    common.write_json_atomic(js_path, report)
    if args.json:
        print(json.dumps({"ok": True, "report_md": _rel(md_path), "report_json": _rel(js_path),
                          "summary": report["summary"]}, ensure_ascii=False))
    else:
        print("report_md   = %s" % _rel(md_path))
        print("report_json = %s" % _rel(js_path))
        print("summary     = %s" % json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
