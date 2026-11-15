# C_answer.py —— 阶段C：纯文本推理作答（无图像），跑完即退出
# 配置：统一读 pipeline/config.json；CLI --threads/--n-ctx 可覆盖；默认 n_threads=12
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_json, save_json, validate_answer, check_value_against_options, load_config, effective
import model_layer as ml

PROMPT = (
    "\u4f60\u662f\u4e2d\u56fd\u515a\u5efa\u4e0e\u65f6\u653f\u77e5\u8bc6\u9898\u5e93\u3002\u8bf7\u4f5c\u7b54\u4e0b\u9762\u8fd9\u9053\u9898\u3002"
    "\u53ea\u8f93\u51fa JSON\uff1a{\"answers\":[\"A\"],\"reason\":\"\u4e00\u53e5\u8bdd\u7406\u7531\",\"confidence\":0.9}\u3002"
    "\u591a\u9009\u9898\u7ed9\u51fa\u6240\u6709\u6b63\u786e\u9009\u9879\u5b57\u6bcd\uff1b\u5224\u65ad\u9898\u7528 A(\u5bf9)/B(\u9519)\u3002"
    "\u4e0d\u786e\u5b9a\u5219 confidence \u8c03\u4f4e\u3002"
)

def parse_json(txt):
    try:
        return json.loads(txt)
    except Exception:
        s, e = txt.find("{"), txt.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(txt[s:e+1])
            except Exception:
                return None
    return None

def body_of(q):
    opts = q.get("options") or []
    lines = ["%s. %s" % (o.get("key"), o.get("text")) for o in opts]
    return "\u9898\u578b\uff1a%s\n\u9898\u5e72\uff1a%s\n\u9009\u9879\uff1a\n%s" % (
        q.get("type"), q.get("stem"), "\n".join(lines))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--n-ctx", type=int, default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    eff = effective(cfg, args.threads, args.n_ctx, None)
    model = args.model or cfg["model"]
    print("[C] config=%s" % eff["config_path"], flush=True)
    print("[C] threads effective=%d (src=%s) | n_ctx=%d (src=%s)" % (
        eff["n_threads"], eff["threads_src"], eff["n_ctx"], eff["n_ctx_src"]), flush=True)

    ex = load_json(args.extract)
    qs = ex.get("questions") or []
    if not qs:
        print("ERROR: extract has no questions", flush=True); return 2

    try:
        import psutil
        _proc = psutil.Process()
    except Exception:
        _proc = None
    rss0 = round(_proc.memory_info().rss/1048576, 1) if _proc else None
    api_mode = ml.is_api(cfg)
    local_fallback = bool((cfg.get("api") or {}).get("fallback_local", True))
    api_model = (cfg.get("api") or {}).get("model_text") or "qwen3.8-flash"
    t0 = time.time()
    llm = None
    load_s = 0.0

    def ensure_local():
        nonlocal llm, load_s
        if llm is not None:
            return
        from llama_cpp import Llama
        _t = time.time()
        llm = Llama(model_path=model, n_ctx=eff["n_ctx"], n_threads=eff["n_threads"], verbose=False)
        load_s = round(time.time() - _t, 2)
        print("[C] local model loaded %.2fs (text-only, threads=%d)" % (load_s, eff["n_threads"]), flush=True)

    if api_mode:
        _meta = ml.api_meta(cfg)
        print("[C] backend=api model=%s base=%s key_source=%s" % (
            api_model, _meta.get("base_url"), _meta.get("key_source")), flush=True)
    else:
        ensure_local()
    rss1 = round(_proc.memory_info().rss/1048576, 1) if _proc else None
    peak_rss = [rss1 or 0]
    print("[C] rss_mb: base=%s loaded=%s" % (rss0, rss1), flush=True)
    answers, unknown = [], []
    api_used = local_used = False
    for q in qs:
        idx = q.get("index")
        t = time.time()
        txt = ""
        q_backend = "local"
        api_s = None
        if api_mode:
            try:
                txt, api_s = ml.api_chat(
                    cfg, [{"role": "user", "content": PROMPT + "\n\n" + body_of(q)}],
                    max_tokens=int(args.max_tokens or cfg.get("max_tokens_C", 200)),
                    temperature=0.0, model=api_model)
                q_backend = "api"
                api_used = True
            except Exception as e:
                print("[C] API 失败 Q%s: %s（fallback_local=%s）" % (
                    idx, str(e)[:250], local_fallback), flush=True)
                if not local_fallback:
                    unknown.append({"index": idx, "reason": "api_failed: " + str(e)[:200]})
                    continue
                api_mode = False
                ensure_local()
        if q_backend == "local":
            resp = llm.create_chat_completion(
                messages=[{"role": "user", "content": PROMPT + "\n\n" + body_of(q)}],
                max_tokens=int(args.max_tokens or cfg.get("max_tokens_C", 200)), temperature=0.0)
            txt = resp["choices"][0]["message"]["content"]
            local_used = True
        dt = api_s if api_s is not None else round(time.time() - t, 2)
        if _proc:
            peak_rss.append(round(_proc.memory_info().rss/1048576, 1))
        p = parse_json(txt) or {}
        val = p.get("answers")
        if isinstance(val, str):
            val = [val]
        conf = p.get("confidence")
        if not val or not isinstance(conf, (int, float)):
            unknown.append({"index": idx, "reason": "parse_or_missing", "raw": txt[:200],
                            "backend": q_backend})
            print("[C] Q%s UNKNOWN %.2fs backend=%s" % (idx, dt, q_backend), flush=True)
            continue
        if q.get("type") == "\u5224\u65ad\u9898":
            val = ["A" if str(v).lower() in ("true", "a", "\u5bf9") else "B" for v in val]
        answers.append({"index": idx, "type": q.get("type"), "value": val,
                        "confidence": round(float(conf), 3),
                        "reason": (p.get("reason") or "")[:200],
                        "elapsed_s": dt, "backend": q_backend})
        print("[C] Q%s %s conf=%.2f %.2fs backend=%s" % (idx, val, conf, dt, q_backend), flush=True)

    out = {"schema": "chaoxing-answer/1",
           "engine": {"model": api_model if api_used else os.path.basename(model),
                      "backend": ("openai_compatible_api" if (api_used and not local_used)
                                  else ("mixed_api_local" if api_used else "llama_cpp_python_text_only")),
                      "load_s": load_s, "n_threads": eff["n_threads"], "n_ctx": eff["n_ctx"],
                      "api": ml.api_meta(cfg) if api_used else None},
           "answers": answers, "unknown": unknown,
           "meta": {"elapsed_s": round(time.time() - t0, 1), "questions": len(qs),
                    "rss_base_mb": rss0, "rss_loaded_mb": rss1,
                    "rss_peak_mb": max(peak_rss) if peak_rss else None}}
    errs = validate_answer(out, [q.get("index") for q in qs])
    errs += check_value_against_options(out, ex)
    out["validation"] = {"pass": not errs, "errors": errs}
    save_json(args.out, out)
    print("[C] written %s | validation_pass=%s" % (args.out, out["validation"]["pass"]), flush=True)
    for e in errs:
        print("[C]   schema: " + e, flush=True)
    return 0 if out["validation"]["pass"] else 3

if __name__ == "__main__":
    sys.exit(main())
