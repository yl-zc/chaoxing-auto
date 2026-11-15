# A_extract.py —— 阶段A：视觉识别（读图 -> 纯文本题目 JSON），跑完即退出
# 配置：统一读 pipeline/config.json；CLI --threads/--n-ctx 可覆盖；默认 n_threads=12
import argparse, base64, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_json, save_json, validate_extract, load_config, effective
import model_layer as ml

def b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()

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

PROMPT = (
    "\u8bf7\u8bfb\u53d6\u56fe\u4e2d\u7684\u9898\u76ee\uff0c\u53ea\u505a\u6587\u5b57\u8f6c\u5199\uff0c\u4e0d\u8981\u4f5c\u7b54\u3002"
    "\u53ea\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61\uff1a"
    "{\"type\":\"\u5355\u9009\u9898\u6216\u591a\u9009\u9898\u6216\u5224\u65ad\u9898\u6216\u586b\u7a7a\u9898\u6216\u7b80\u7b54\u9898\","
    "\"stem\":\"\u9898\u5e72\u5b8c\u6574\u6587\u5b57\","
    "\"options\":[{\"key\":\"A\",\"text\":\"\u9009\u9879\u6587\u5b57\"}]}\u3002"
    "\u9009\u9879\u5b57\u6bcd\u4ece A \u5f00\u59cb\uff1b\u5224\u65ad\u9898\u9009\u9879\u4e3a A(\u5bf9)/B(\u9519)\u3002\u4e0d\u8981\u989d\u5916\u6587\u5b57\u3002"
)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--mmproj", default=None)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--n-ctx", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    eff = effective(cfg, args.threads, args.n_ctx, None)
    model = args.model or cfg["model"]
    mmproj = args.mmproj or cfg["mmproj"]
    print("[A] config=%s" % eff["config_path"], flush=True)
    print("[A] threads effective=%d (src=%s) | n_ctx=%d (src=%s)" % (
        eff["n_threads"], eff["threads_src"], eff["n_ctx"], eff["n_ctx_src"]), flush=True)

    man = load_json(args.manifest)
    items = man.get("questions") or []
    if not items:
        print("ERROR: manifest has no questions", flush=True); return 2

    api_mode = ml.is_api(cfg)
    local_fallback = bool((cfg.get("api") or {}).get("fallback_local", True))
    api_model = (cfg.get("api") or {}).get("model_vision") or "qwen3-vl-plus"
    t0 = time.time()
    llm = None
    handler = None
    load_s = 0.0

    def ensure_local():
        nonlocal llm, handler, load_s
        if llm is not None:
            return
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import MTMDChatHandler
        _t = time.time()
        handler = MTMDChatHandler(clip_model_path=mmproj, verbose=False)
        llm = Llama(model_path=model, chat_handler=handler, n_ctx=eff["n_ctx"],
                    n_threads=eff["n_threads"], verbose=False)
        load_s = round(time.time() - _t, 2)
        print("[A] local model loaded %.2fs (threads=%d)" % (load_s, eff["n_threads"]), flush=True)

    if api_mode:
        _meta = ml.api_meta(cfg)
        print("[A] backend=api model=%s base=%s key_source=%s" % (
            api_model, _meta.get("base_url"), _meta.get("key_source")), flush=True)
    else:
        ensure_local()

    questions, errs = [], []
    api_used = local_used = False
    for it in items:
        idx = it.get("index"); img = it.get("image")
        if not img or not os.path.exists(img):
            errs.append({"index": idx, "error": "image missing"}); continue
        t = time.time()
        txt = ""
        q_backend = "local"
        api_s = None
        if api_mode:
            try:
                txt, api_s = ml.api_chat(
                    cfg, ml.vision_messages(PROMPT, b64(img)),
                    max_tokens=int(cfg.get("max_tokens_A", 420)),
                    temperature=0.1, model=api_model)
                q_backend = "api"
                api_used = True
            except Exception as e:
                print("[A] API 失败 Q%s: %s（fallback_local=%s）" % (
                    idx, str(e)[:250], local_fallback), flush=True)
                if not local_fallback:
                    errs.append({"index": idx, "error": "api_failed: " + str(e)[:250]})
                    continue
                api_mode = False
                ensure_local()
        if q_backend == "local":
            resp = llm.create_chat_completion(messages=[{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(img)}},
            ]}], max_tokens=int(cfg.get("max_tokens_A", 420)), temperature=0.1)
            txt = resp["choices"][0]["message"]["content"]
            local_used = True
        dt = api_s if api_s is not None else round(time.time() - t, 2)
        p = parse_json(txt) or {}
        q = {"index": idx, "type": p.get("type"), "stem": p.get("stem"),
             "options": p.get("options") or [], "elapsed_s": dt,
             "backend": q_backend, "raw": txt[:400]}
        if it.get("dom_type"):
            q["dom_type"] = it["dom_type"]
        questions.append(q)
        print("[A] Q%s type=%s opts=%d %.2fs backend=%s" % (
            idx, q["type"], len(q["options"]), dt, q_backend), flush=True)

    out = {"schema": "chaoxing-quiz-extract/1",
           "source": {"manifest": args.manifest, "session": man.get("session")},
           "engine": {"model": api_model if api_used else os.path.basename(model),
                      "backend": ("openai_compatible_api" if (api_used and not local_used)
                                  else ("mixed_api_local" if api_used else "llama_cpp_python")),
                      "load_s": load_s, "n_threads": eff["n_threads"], "n_ctx": eff["n_ctx"],
                      "api": ml.api_meta(cfg) if api_used else None},
           "questions": questions, "errors": errs,
           "elapsed_s": round(time.time() - t0, 1)}
    errs_v = validate_extract(out)
    out["validation"] = {"pass": not errs_v, "errors": errs_v}
    save_json(args.out, out)
    print("[A] written %s | validation_pass=%s" % (args.out, out["validation"]["pass"]), flush=True)
    for e in errs_v:
        print("[A]   schema: " + e, flush=True)
    return 0 if out["validation"]["pass"] else 3

if __name__ == "__main__":
    sys.exit(main())
