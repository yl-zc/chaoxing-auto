# common.py —— A->C 两段式共用：schema 校验 + JSON IO + 闸门
import json, os, re

TYPES = {"单选题", "多选题", "判断题", "填空题", "简答题"}

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

def save_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)

def validate_extract(obj):
    """校验视觉抽取产物。返回错误列表（空=通过）。"""
    errs = []
    if not isinstance(obj, dict):
        return ["root is not an object"]
    if obj.get("schema") != "chaoxing-quiz-extract/1":
        errs.append("schema != chaoxing-quiz-extract/1")
    qs = obj.get("questions")
    if not isinstance(qs, list) or not qs:
        return errs + ["questions missing/empty"]
    seen = set()
    for q in qs:
        if not isinstance(q, dict):
            errs.append("question entry not an object"); continue
        idx = q.get("index")
        if not isinstance(idx, int) or idx < 1:
            errs.append("bad index: %r" % (idx,)); continue
        if idx in seen:
            errs.append("duplicate index %d" % idx)
        seen.add(idx)
        if q.get("type") not in TYPES:
            errs.append("Q%d bad type: %r" % (idx, q.get("type")))
        stem = q.get("stem")
        if not isinstance(stem, str) or not stem.strip():
            errs.append("Q%d empty stem" % idx)
        opts = q.get("options")
        if not isinstance(opts, list):
            errs.append("Q%d options not a list" % idx); continue
        t = q.get("type")
        if t in ("单选题", "多选题") and len(opts) < 2:
            errs.append("Q%d %s needs >=2 options, got %d" % (idx, t, len(opts)))
        if t == "判断题" and len(opts) != 2:
            errs.append("Q%d judge needs exactly 2 options, got %d" % (idx, len(opts)))
        keys = set()
        for o in opts:
            if not isinstance(o, dict):
                errs.append("Q%d option not object" % idx); continue
            k = o.get("key")
            if not isinstance(k, str) or not k:
                errs.append("Q%d option missing key" % idx)
            elif k in keys:
                errs.append("Q%d duplicate option key %r" % (idx, k))
            else:
                keys.add(k)
            if not isinstance(o.get("text"), str):
                errs.append("Q%d option %r text not str" % (idx, k))
    return errs

def validate_answer(obj, expected_indices):
    """校验文本推理答案。返回错误列表。"""
    errs = []
    if not isinstance(obj, dict):
        return ["root is not an object"]
    if obj.get("schema") != "chaoxing-answer/1":
        errs.append("schema != chaoxing-answer/1")
    answers = obj.get("answers")
    unknown = obj.get("unknown") or []
    if not isinstance(answers, list):
        return errs + ["answers missing/not list"]
    if not isinstance(unknown, list):
        errs.append("unknown not list"); unknown = []
    got = {}
    for a in answers:
        if not isinstance(a, dict):
            errs.append("answer entry not object"); continue
        idx = a.get("index")
        if not isinstance(idx, int):
            errs.append("bad answer index %r" % (idx,)); continue
        if idx in got:
            errs.append("duplicate answer index %d" % idx)
        got[idx] = a
        val = a.get("value")
        if not isinstance(val, list) or not val:
            errs.append("Q%d empty value" % idx)
        else:
            if not all(isinstance(v, str) and v for v in val):
                errs.append("Q%d value items must be non-empty str" % idx)
        c = a.get("confidence")
        if not isinstance(c, (int, float)) or not (0 <= c <= 1):
            errs.append("Q%d confidence out of [0,1]: %r" % (idx, c))
    unk_idx = set()
    for u in unknown:
        if not isinstance(u, dict) or not isinstance(u.get("index"), int):
            errs.append("bad unknown entry")
        else:
            unk_idx.add(u["index"])
    exp = set(expected_indices)
    missing = sorted(exp - set(got) - unk_idx)
    if missing:
        errs.append("coverage missing indices: %s" % missing)
    extra = sorted((set(got) | unk_idx) - exp)
    if extra:
        errs.append("answers for unexpected indices: %s" % extra)
    return errs

def check_value_against_options(answers_obj, extract_obj):
    """答案字母必须落在该题选项 key 集合内。"""
    errs = []
    opts = {q["index"]: {o["key"] for o in q.get("options", [])} for q in extract_obj.get("questions", [])}
    for a in answers_obj.get("answers", []):
        idx = a.get("index")
        allowed = opts.get(idx)
        if allowed is None:
            continue
        val = a.get("value") or []
        if a.get("type") == "判断题":
            norm = {str(v).lower() for v in val}
            if not norm.issubset({"true", "false", "a", "b", "对", "错"}):
                errs.append("Q%d judge value not in true/false/A/B: %r" % (idx, val))
        else:
            bad = [v for v in val if v not in allowed]
            if bad:
                errs.append("Q%d value %r not in option keys %s" % (idx, bad, sorted(allowed)))
    return errs

def gate(answers_obj, threshold=0.8):
    """提交闸门：全题覆盖 + 无 unknown + 最小置信度 >= threshold。"""
    errs = []
    unknown = answers_obj.get("unknown") or []
    if unknown:
        errs.append("has unknown: %s" % [u.get("index") for u in unknown])
    confs = []
    for a in answers_obj.get("answers", []):
        c = a.get("confidence")
        confs.append(c if isinstance(c, (int, float)) else 0.0)
    min_conf = min(confs) if confs else 0.0
    if min_conf < threshold:
        errs.append("min confidence %.3f < %.3f" % (min_conf, threshold))
    return {"pass": not errs, "min_confidence": round(min_conf, 4), "threshold": threshold,
            "errors": errs}

# ---------------- 配置接口（统一入口） ----------------
DEFAULTS = {"n_threads": 12, "n_ctx": 4096, "min_conf": 0.8, "backend": "local", "api": {},
            "model": r"D:\\pyai\\pych\\models\\Qwen3-VL-4B-Instruct-Q4_K_M.gguf",
            "mmproj": r"D:\\pyai\\pych\\models\\mmproj-Qwen3-VL-4B-Instruct-F16.gguf",
            "max_tokens_A": 420, "max_tokens_C": 200}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def _pkg_root():
    """包根：pipeline 的上级目录（便携包=包根；开发目录=work/stage-d）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def resolve_model_path(p):
    """模型/投影文件路径：相对路径按包根解析（便携包 runtime/models/…），绝对路径原样返回。"""
    if not p:
        return p
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(_pkg_root(), p))

def load_config(path=None):
    cfg = dict(DEFAULTS)
    p = path or CONFIG_PATH
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                for k, v in user.items():
                    if k.startswith("_"):
                        continue
                    cfg[k] = v  # 允许新增字段（如 backend/api），_ 开头为注释
        except Exception as e:
            cfg["_config_error"] = str(e)[:200]
    cfg["_config_path"] = p
    cfg["model"] = resolve_model_path(cfg.get("model"))
    cfg["mmproj"] = resolve_model_path(cfg.get("mmproj"))
    return cfg

def resolve(cli_value, cfg, key):
    """优先级：CLI 显式值 > config.json > 默认。返回 (值, 来源)"""
    if cli_value is not None:
        return cli_value, "cli"
    if key in cfg and cfg[key] is not None:
        return cfg[key], "config"
    return DEFAULTS[key], "default"

def effective(cfg, threads=None, n_ctx=None, min_conf=None):
    t, ts = resolve(threads, cfg, "n_threads")
    c, cs = resolve(n_ctx, cfg, "n_ctx")
    m, ms = resolve(min_conf, cfg, "min_conf")
    return {"n_threads": int(t), "threads_src": ts,
            "n_ctx": int(c), "n_ctx_src": cs,
            "min_conf": float(m), "min_conf_src": ms,
            "config_path": cfg.get("_config_path")}
