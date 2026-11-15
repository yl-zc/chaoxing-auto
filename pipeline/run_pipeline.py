# run_pipeline.py —— 两段式编排：A(视觉识别) 进程 -> 退出 -> C(文本推理) 进程 -> 退出 -> 闸门
# 配置：统一读 pipeline/config.json；CLI --threads/--n-ctx/--threshold 可覆盖
import argparse, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_json, save_json, validate_extract, validate_answer, check_value_against_options, gate, load_config, effective

HERE = os.path.dirname(os.path.abspath(__file__))

def run(cmd, label):
    t = time.time()
    print("[runner] %s: %s" % (label, " ".join(cmd)), flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    dt = round(time.time() - t, 1)
    if p.stdout:
        for line in p.stdout.strip().splitlines():
            print("  " + line, flush=True)
    if p.returncode != 0:
        print("[runner] %s FAILED rc=%d" % (label, p.returncode), flush=True)
        if p.stderr:
            print("  stderr: " + p.stderr.strip()[-800:], flush=True)
    return p.returncode, dt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--n-ctx", type=int, default=None)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--skip-a", action="store_true")
    ap.add_argument("--skip-c", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    eff = effective(cfg, args.threads, args.n_ctx, args.threshold)
    print("[runner] config=%s" % eff["config_path"], flush=True)
    print("[runner] threads effective=%d (src=%s) | n_ctx=%d (src=%s) | min_conf=%.2f (src=%s)" % (
        eff["n_threads"], eff["threads_src"], eff["n_ctx"], eff["n_ctx_src"],
        eff["min_conf"], eff["min_conf_src"]), flush=True)

    os.makedirs(args.outdir, exist_ok=True)
    ex_path = os.path.join(args.outdir, "extracted.json")
    an_path = os.path.join(args.outdir, "answers.json")
    result = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "stages": {},
              "effective": eff}
    t0 = time.time()

    common_args = []
    if args.threads is not None:
        common_args += ["--threads", str(args.threads)]
    if args.n_ctx is not None:
        common_args += ["--n-ctx", str(args.n_ctx)]
    if args.config:
        common_args += ["--config", args.config]

    if not args.skip_a:
        rc, dt = run([sys.executable, os.path.join(HERE, "A_extract.py"),
                      "--manifest", args.manifest, "--out", ex_path] + common_args, "A_extract")
        result["stages"]["A"] = {"rc": rc, "elapsed_s": dt, "out": ex_path}
        if rc != 0:
            result["ok"] = False; result["stage_failed"] = "A"
            save_json(os.path.join(args.outdir, "pipeline-result.json"), result)
            print("[runner] abort at A", flush=True); return rc

    ex = load_json(ex_path)
    e_errs = validate_extract(ex)
    result["stages"]["A_validation"] = {"pass": not e_errs, "errors": e_errs}
    if e_errs:
        result["ok"] = False; result["stage_failed"] = "A_validation"
        save_json(os.path.join(args.outdir, "pipeline-result.json"), result)
        print("[runner] abort: extract invalid", flush=True); return 3
    print("[runner] A validation PASS (%d questions)" % len(ex["questions"]), flush=True)

    if not args.skip_c:
        rc, dt = run([sys.executable, os.path.join(HERE, "C_answer.py"),
                      "--extract", ex_path, "--out", an_path] + common_args, "C_answer")
        result["stages"]["C"] = {"rc": rc, "elapsed_s": dt, "out": an_path}
        if rc != 0:
            result["ok"] = False; result["stage_failed"] = "C"
            save_json(os.path.join(args.outdir, "pipeline-result.json"), result)
            print("[runner] abort at C", flush=True); return rc

    an = load_json(an_path)
    errs = validate_answer(an, [q["index"] for q in ex["questions"]])
    errs += check_value_against_options(an, ex)
    result["stages"]["C_validation"] = {"pass": not errs, "errors": errs}
    if errs:
        result["ok"] = False; result["stage_failed"] = "C_validation"
        save_json(os.path.join(args.outdir, "pipeline-result.json"), result)
        print("[runner] abort: answers invalid", flush=True); return 3

    g = gate(an, eff["min_conf"])
    result["gate"] = g
    result["ok"] = bool(g["pass"])
    result["total_elapsed_s"] = round(time.time() - t0, 1)
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_json(os.path.join(args.outdir, "pipeline-result.json"), result)
    print("[runner] C validation PASS; gate.pass=%s min_conf=%.2f total=%.1fs" % (
        g["pass"], g["min_confidence"], result["total_elapsed_s"]), flush=True)
    for e in g["errors"]:
        print("[runner]   gate: " + e, flush=True)
    print("[runner] READY-TO-FILL" if g["pass"] else "[runner] NOT-READY (no submit)", flush=True)
    return 0 if g["pass"] else 4

if __name__ == "__main__":
    sys.exit(main())
