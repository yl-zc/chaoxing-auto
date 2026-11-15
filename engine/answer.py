# answer.py —— 作答模块（单一职责：以子进程 CLI 调 pipeline 的 A→C→校验→闸门）
"""不复制 pipeline 内部逻辑：
  由 pipeline/run_pipeline.py 完成 A(视觉转写) -> C(文本作答) -> schema 校验 -> 闸门，
  本模块只负责：拼稳定 CLI 参数、超时控制、读取 JSON 产物、返回统一结构。
模型路径/mmproj/max_tokens 等由 pipeline/config.json 管理（换模型只改那一处）。
线程数/阈值由 engine/config.json 的 pipeline 段显式传 CLI 覆盖（优先级：CLI > pipeline config > 默认）。
对外接口：
  solve(cfg, manifest_path, outdir, log) -> dict
"""
import os
import subprocess
import time

import common


class PipelineError(RuntimeError):
    pass


def solve(cfg, manifest_path, outdir, log):
    pdir = common.path(cfg, "pipeline_dir")
    pc = cfg["pipeline"]
    py = common.resolve_path(cfg["python"]["pipeline"])
    runner = os.path.join(pdir, pc["runner"])
    os.makedirs(outdir, exist_ok=True)

    cmd = [py, runner,
           "--manifest", os.path.abspath(manifest_path),
           "--outdir", os.path.abspath(outdir),
           "--threshold", str(pc["min_conf"]),
           "--threads", str(pc["n_threads"]),
           "--n-ctx", str(pc["n_ctx"])]
    log("pipeline A->C: " + " ".join(cmd))
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=float(pc["timeout_s"]))
    except subprocess.TimeoutExpired:
        log("pipeline 超时（%.0fs）" % float(pc["timeout_s"]))
        return {"ok": False, "rc": None, "error": "pipeline 超时", "elapsed_s": round(time.time() - t0, 1)}

    stdout = p.stdout or ""
    stderr = p.stderr or ""
    with open(os.path.join(outdir, "console.log"), "w", encoding="utf-8") as fh:
        fh.write("CMD: " + " ".join(cmd) + "\n\n")
        fh.write(stdout)
        if stderr:
            fh.write("\n--- STDERR ---\n" + stderr)
    for line in stdout.strip().splitlines():
        log("  " + line)

    result = common.read_json(os.path.join(outdir, "pipeline-result.json")) or {}
    ex = common.read_json(os.path.join(outdir, "extracted.json")) or {}
    an = common.read_json(os.path.join(outdir, "answers.json")) or {}
    gate = result.get("gate") or {}
    ok = (p.returncode == 0 and bool(result.get("ok")) and bool(gate.get("pass")))
    out = {"schema_version": "engine-answer/1",
           "ok": bool(ok), "rc": p.returncode,
           "elapsed_s": round(time.time() - t0, 1),
           "cmd": cmd, "outdir": os.path.abspath(outdir),
           "gate": gate, "pipeline_result": result,
           "extracted": ex, "answers": an,
           "stdout_tail": stdout[-3000:], "stderr_tail": stderr[-2000:]}
    if not ok:
        errs = []
        if result.get("stage_failed"):
            errs.append("stage_failed=%s" % result["stage_failed"])
        for e in (gate.get("errors") or []):
            errs.append("gate: " + str(e))
        if p.returncode != 0 and not errs:
            errs.append("rc=%s" % p.returncode)
        out["error"] = "; ".join(errs)[:800]
    log("pipeline 完成 rc=%s gate=%s min_conf=%s 耗时=%.1fs" % (
        p.returncode, gate.get("pass"), gate.get("min_confidence"), out["elapsed_s"]))
    return out
