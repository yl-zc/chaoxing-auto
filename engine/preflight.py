# preflight.py —— 开始前核对页面真实进度与各节任务点（只读）
"""需求：每次开始前先以「页面」为准核对学习内容是否已更新（用户可能手动做过），避免重复做。
接口：
  read_progress(page, cfg, nav, log) -> {"n","total","text","url"} | None
  read_catalog_counts(page, nav, log) -> {"items":[...], "map":{section:row}}
  collect(page, cfg, nav, log, sections) -> {"schema_version":"engine-preflight/1", progress, sections, ...}
只读：只导航到课程页/目录读取文字，不播放、不答题、不提交。
"""
import re
import time

import common

SECTION_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?![\d.])")

CATALOG_JS = r"""() => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const out = [];
  for (const el of document.querySelectorAll('.chapter_item, .posCatalog_name')) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 120) continue;
    const oc = el.getAttribute('onclick') || '';
    let chapter_id = null;
    const m1 = oc.match(/toOld\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'/);
    if (m1) chapter_id = m1[2];
    if (chapter_id === null) {
      const m2 = oc.match(/getTeacherAjax\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'/);
      if (m2) chapter_id = m2[3];
    }
    out.push({text: t, chapter_id: chapter_id, visible: vis(el),
              cls: el.className ? String(el.className).slice(0, 40) : ''});
  }
  return out;
}"""


def parse_count(text):
    """目录条目尾部「 N」是未完成任务点数；没有尾数视为 0（已完成）。"""
    m = re.search(r"\s+(\d+)\s*$", text or "")
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def parse_section(text):
    m = SECTION_RE.match(text or "")
    if not m:
        return None, ""
    sec = "%d.%d" % (int(m.group(1)), int(m.group(2)))
    title = (text or "")[m.end():].strip()
    title = re.sub(r"\s+\d+\s*$", "", title).strip()
    return sec, title


def read_progress(page, cfg, nav, log, wait_s=None):
    """读「已完成任务点 N/T」；当前页没有就先回课程页。返回含 url/text。"""
    prog = nav.read_course_progress(page)
    if prog:
        prog = dict(prog)
        prog["url"] = (page.url or "")[:220]
        return prog
    href = nav.read_back_to_course_href(page)
    wait_s = float(wait_s if wait_s is not None else (cfg["verify"]["progress_wait_s"] + 6))
    if not href:
        end = time.time() + wait_s
        while time.time() < end:
            page.wait_for_timeout(700)
            prog = nav.read_course_progress(page)
            if prog:
                prog = dict(prog)
                prog["url"] = (page.url or "")[:220]
                return prog
        return None
    log("preflight: 回课程页读真实进度: " + href[:110])
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log("preflight: 回课程页失败: %s" % str(e)[:150])
        return None
    end = time.time() + wait_s
    while time.time() < end:
        page.wait_for_timeout(700)
        prog = nav.read_course_progress(page)
        if prog:
            prog = dict(prog)
            prog["url"] = (page.url or "")[:220]
            return prog
    return None


def read_catalog_counts(page, nav, log):
    """从课程页目录读所有小节的任务点数（尾数；无尾数=0）。"""
    best, best_frame = [], None
    for fi, fr in enumerate(page.frames):
        try:
            raw = fr.evaluate(CATALOG_JS) or []
        except Exception:
            continue
        rows, seen = [], set()
        for it in raw:
            sec, title = parse_section(it.get("text"))
            if not sec or sec in seen:
                continue
            seen.add(sec)
            rows.append({"section": sec, "title": title,
                         "chapter_id": it.get("chapter_id"),
                         "task_count": parse_count(it.get("text")),
                         "text": (it.get("text") or "")[:100],
                         "frame_index": fi})
        if len(rows) > len(best):
            best, best_frame = rows, fi
    if not best:
        return {"items": [], "map": {}, "frame_index": None}
    log("preflight: 目录条目 %d 条（frame %s）" % (len(best), best_frame))
    return {"items": best, "map": {r["section"]: r for r in best}, "frame_index": best_frame}


def collect(page, cfg, nav, log, sections):
    """读取页面进度 + 计划各节任务点。sections: ["2.4","3.1",...]"""
    progress = read_progress(page, cfg, nav, log)
    cats = read_catalog_counts(page, nav, log)
    rows = []
    for sec in sections or []:
        sec = str(sec).strip()
        item = cats["map"].get(sec)
        if item is None:
            rows.append({"section": sec, "title": "", "chapter_id": None,
                         "task_count": None, "status": "unknown"})
        else:
            rows.append({"section": sec, "title": item["title"],
                         "chapter_id": item["chapter_id"],
                         "task_count": item["task_count"],
                         "status": "done" if item["task_count"] == 0 else "pending"})
    return {"schema_version": "engine-preflight/1",
            "at": common.now_iso(),
            "progress": progress,
            "progress_source": "page" if progress else None,
            "progress_text": (progress or {}).get("text"),
            "sections": rows,
            "catalog_items": len(cats["items"]),
            "url": (page.url or "")[:220]}


def _json_line(obj):
    import json
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv=None):
    """CLI（控制台「刷新进度」用）：
      python preflight.py --json [--sections 2.4,3.1] [--no-restore]
    stdout 最后一行只输出一个 JSON；日志走 stderr。
    """
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="页面真实进度 preflight（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--config", default=None)
    ap.add_argument("--sections", default=None)
    ap.add_argument("--no-restore", action="store_true", help="读完不恢复原页面（默认恢复）")
    args = ap.parse_args(argv)
    log = Log = (lambda m: print("[preflight] " + str(m), file=sys.stderr, flush=True))
    try:
        cfg = common.load_config(args.config)
    except Exception as e:
        _json_line({"ok": False, "code": "CONFIG_ERROR", "message": str(e)[:200],
                    "retryable": False})
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        _json_line({"ok": False, "code": "PLAYWRIGHT_MISSING",
                    "message": "请在 work/.venv 下运行: %s" % str(e)[:120], "retryable": False})
        return 2
    secs = [s.strip() for s in (args.sections or "").split(",") if s.strip()]
    import browser as browser_layer
    import nav as nav_mod
    try:
        endpoint = common.cdp_endpoint(cfg)
        with sync_playwright() as pw:
            pw_browser = pw.chromium.connect_over_cdp(
                endpoint, timeout=int(float(cfg["cdp"]["connect_timeout_s"]) * 1000))
            pages = [p for c in pw_browser.contexts for p in c.pages]
            urls = [(p.url or "") for p in pages]
            idx = browser_layer.pick_page_index(urls, cfg)
            if idx is None:
                _json_line({"ok": False, "code": "BROWSER_NO_PAGE",
                            "message": "浏览器里没有学习通页面，请先打开并登录学习通",
                            "retryable": True})
                return 2
            page = pages[idx]
            origin = page.url or ""
            if "passport2" in origin:
                _json_line({"ok": False, "code": "BROWSER_LOGIN_REQUIRED",
                            "message": "学习通未登录，请在浏览器里扫码登录",
                            "retryable": True})
                return 2
            data = collect(page, cfg, nav_mod, log, secs)
            if not args.no_restore and origin:
                try:
                    page.goto(origin, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
            prog = data.get("progress") or {}
            _json_line({"ok": bool(prog), "logged_in": True,
                        "n": prog.get("n"), "total": prog.get("total"),
                        "text": prog.get("text"), "at": data.get("at"),
                        "source": "course_page",
                        "sections": data.get("sections"), "url": data.get("url")})
            return 0 if prog else 2
    except Exception as e:
        _json_line({"ok": False, "code": "PREFLIGHT_EXCEPTION",
                    "message": repr(e)[:200], "retryable": True})
        return 3


def write_evidence(path, data):
    common.write_json_atomic(path, data)
    return path


if __name__ == "__main__":
    import sys
    sys.exit(main())
