# -*- coding: utf-8 -*-
"""模型层：backend=local|api（OpenAI 兼容）。
- backend=local：本模块不发任何网络请求，A/C 仍走 llama_cpp 本地模型。
- backend=api：A 段视觉用 api.model_vision（默认 qwen3-vl-plus），C 段文本用 api.model_text（默认 qwen3.8-flash）。
- 密钥：优先环境变量 api.api_key_env（默认 MAAS_API_KEY），否则读 api.credentials_file 的 refs.<KEY>。
  密钥只保存在内存，不写日志/证据；异常信息会去除 Authorization/Bearer 内容。
- 失败回退：调用方根据 api.fallback_local 决定是否回退 local。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request


def is_api(cfg):
    return str((cfg or {}).get("backend") or "local").strip().lower() == "api"


def api_cfg(cfg):
    return (cfg or {}).get("api") or {}


def load_api_key(cfg):
    api = api_cfg(cfg)
    env_name = api.get("api_key_env") or "MAAS_API_KEY"
    val = os.environ.get(env_name)
    if val:
        return val.strip()
    path = api.get("credentials_file") or ""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        m = re.search(r"(?m)^\s*" + re.escape(env_name) + r"\s*:\s*(.+?)\s*$", text)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _clean(msg):
    s = str(msg)
    s = re.sub(r"(?i)(authorization|bearer)\s*[:=]?\s*\S+", r"\1 ***", s)
    s = re.sub(r"(?i)(api[_-]?key)\s*[:=]\s*\S+", r"\1 ***", s)
    return s[:600]


def _endpoint(base):
    base = (base or "").rstrip("/")
    if not base:
        raise RuntimeError("api.base_url 未配置")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def api_chat(cfg, messages, max_tokens=512, temperature=0.0, model=None, timeout_s=None):
    """调用 OpenAI 兼容 /chat/completions，返回 (text, elapsed_s)。失败抛 RuntimeError（已脱敏）。"""
    api = api_cfg(cfg)
    key = load_api_key(cfg)
    if not key:
        raise RuntimeError("API 密钥缺失（环境变量 %s 或 credentials_file 未读到）"
                           % (api.get("api_key_env") or "MAAS_API_KEY"))
    url = _endpoint(api.get("base_url"))
    payload = {"model": model or api.get("model_text") or "qwen3.8-flash",
               "messages": messages,
               "max_tokens": int(max_tokens),
               "temperature": float(temperature)}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s or api.get("timeout_s") or 180)) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("API HTTP %s: %s" % (e.code, _clean(body)))
    except Exception as e:
        raise RuntimeError("API 请求失败: %s" % _clean(e))
    elapsed = round(time.time() - t0, 3)
    try:
        j = json.loads(raw)
        content = j["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
        return str(content), elapsed
    except Exception as e:
        raise RuntimeError("API 响应解析失败: %s / %s" % (_clean(e), _clean(raw[:200])))


def vision_messages(prompt, image_b64, mime="image/png"):
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, image_b64)}}]}]


def api_meta(cfg):
    api = api_cfg(cfg)
    return {"base_url": api.get("base_url"),
            "model_vision": api.get("model_vision"),
            "model_text": api.get("model_text"),
            "key_source": "env" if os.environ.get(api.get("api_key_env") or "MAAS_API_KEY") else "credentials_file"}
