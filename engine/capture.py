# capture.py —— 题目捕获模块（单一职责：进测验标签页 / 读题型 / 截图 / 裁剪 / 生成 manifest）
"""流程与 auto-2.3 一致：整视口截图 + PIL 裁剪（裁剪复用 pipeline/crop_boxes.py，不复制逻辑）。
对外接口：
  enter_quiz(page, cfg, log, allow_start=False) -> (quiz_frame, meta, info)
  read_meta(frame) -> [ {index,type,n_options,options:[{shown,code}]} ]
  capture_questions(page, qf, outdir, section, cfg, log, pipeline_py, pipeline_dir) -> dict
  restore_video_tab(page, cfg, log)
  screenshot(page, path, cfg)
"""
import base64
import json
import os
import subprocess
import time
import urllib.request

import websocket

import common


class CaptureError(RuntimeError):
    pass


QUIZ_META = r"""() => {
  const timus = Array.from(document.querySelectorAll('.TiMu'));
  return timus.map((t, i) => {
    const txt = t.innerText || '';
    const m = txt.match(/(单选题|多选题|判断题|填空题|简答题)/);
    const lis = Array.from(t.querySelectorAll('li'));
    return {index: i+1, type: m ? m[1] : null, n_options: lis.length,
      options: lis.map(li => { const x = li.querySelector('.num_option,.num_option_dx');
        return {shown: x ? (x.textContent||'').trim() : null, code: x ? x.getAttribute('data') : null}; })};
  });
}"""

COUNT_JS = r"""() => ({timu: document.querySelectorAll('.TiMu').length,
                     submit: document.querySelectorAll('a.btnSubmit').length})"""

TAB_CLICK_JS = r"""(label) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const re = new RegExp('^\\d+\\s*' + label + '$');
  const c = [];
  for (const el of document.querySelectorAll('a,span,li,div')) {
    const t = (el.innerText || '').trim();
    if (!re.test(t) || !vis(el)) continue;
    c.push(el);
  }
  if (!c.length) return {n: 0};
  c.sort((a, b) => a.children.length - b.children.length);
  c[0].click();
  return {n: c.length, id: c[0].id || '', text: (c[0].innerText || '').trim()};
}"""

VIDEO_TAB_JS = r"""(label) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const re = new RegExp('^\\d+\\s*' + label + '$');
  for (const el of document.querySelectorAll('a,span,li,div')) {
    const t = (el.innerText || '').trim();
    if (re.test(t) && vis(el)) { el.click(); return true; }
  }
  return false;
}"""

START_BUTTON_JS = r"""() => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  for (const el of document.querySelectorAll('a,span,div,button,input')) {
    const t = (el.innerText || el.value || '').trim();
    if ((t === '开始答题' || t === '开始' || t === '进入答题') && vis(el)) { el.click(); return t; }
  }
  return null;
}"""

VIEWPORT_JS = r"""() => ({w: innerWidth, h: innerHeight, dpr: devicePixelRatio,
                         visibilityState: document.visibilityState})"""


def find_quiz_frame(page, cfg):
    key = cfg["quiz"]["frame_url_key"]
    return next((f for f in page.frames if key in (f.url or "")), None)


def count_timu(frame):
    try:
        return frame.evaluate(COUNT_JS) or {"timu": 0, "submit": 0}
    except Exception:
        return {"timu": 0, "submit": 0}


def read_meta(frame):
    return frame.evaluate(QUIZ_META) or []


def click_quiz_tab(page, cfg, log):
    label = cfg["nav"]["tab_quiz_regex"]
    hit = None
    for fi, fr in enumerate(page.frames):
        try:
            r = fr.evaluate(TAB_CLICK_JS, label)
        except Exception:
            continue
        if r and r.get("n"):
            hit = (fi, r)
            break
    log("点击「%s」标签: %s" % (label, ("frame %s %s" % (hit[0], hit[1])) if hit else "未找到"))
    return bool(hit)


def enter_quiz(page, cfg, log, allow_start=False):
    """进测验标签页并等题目渲染。返回 (frame, meta, info)。"""
    qc = cfg["quiz"]
    qf = find_quiz_frame(page, cfg)
    if qf is None:
        if not click_quiz_tab(page, cfg, log):
            raise CaptureError("未找到「%s」标签" % cfg["nav"]["tab_quiz_regex"])
        waits = int(float(cfg["nav"]["wait_frame_s"]) * 1000 / cfg["nav"]["poll_ms"])
        for _ in range(max(1, waits)):
            page.wait_for_timeout(int(cfg["nav"]["poll_ms"]))
            qf = find_quiz_frame(page, cfg)
            if qf is not None:
                break
    if qf is None:
        raise CaptureError("测验 frame 未出现（%s）" % cfg["quiz"]["frame_url_key"])

    page.wait_for_timeout(int(qc["settle_ms"]))
    meta = []
    steps = max(1, int(float(qc["wait_timu_s"]) * 1000 / qc["meta_poll_ms"]))
    for _ in range(steps):
        cnt = count_timu(qf)
        if cnt.get("timu", 0) > 0:
            meta = read_meta(qf)
            if meta:
                break
        page.wait_for_timeout(int(qc["meta_poll_ms"]))

    info = {"frame_url": (qf.url or "")[:200], "needs_start": False}
    if not meta:
        try:
            body = qf.evaluate("() => (document.body ? document.body.innerText : '')") or ""
        except Exception:
            body = ""
        info["body_head"] = body.strip().replace("\n", " ")[:200]
        if ("已完成" in body or "本次成绩" in body or "已提交" in body) and "开始答题" not in body:
            info["already_done"] = True
        if allow_start:
            try:
                clicked = qf.evaluate(START_BUTTON_JS)
            except Exception:
                clicked = None
            if clicked:
                info["needs_start"] = True
                info["start_clicked"] = clicked
                log("测验需要先点「%s」，已点击 1 次" % clicked)
                for _ in range(steps):
                    page.wait_for_timeout(int(qc["meta_poll_ms"]))
                    meta = read_meta(qf)
                    if meta:
                        break
        else:
            info["needs_start"] = True
    info["questions"] = len(meta)
    return qf, meta, info


def _cdp_targets(endpoint):
    host = endpoint.replace("http://", "").replace("https://", "").rstrip("/")
    url = "http://%s/json/list" % host
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def screenshot(page, path, cfg):
    """用 CDP Page.captureScreenshot 截图。
    不用 page.screenshot()：它会等待 webfont 加载，学习通页面常挂起导致超时；
    CDP 截图不经过 OS 焦点、不需要窗口置前（与 auto-2.3 的 shot() 一致）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    timeout_s = int(float(cfg["capture"]["shot_timeout_s"]))
    try:
        targets = _cdp_targets(common.cdp_endpoint(cfg))
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            raise RuntimeError("CDP 无 page target")
        cur = (page.url or "")
        target = next((t for t in pages if (t.get("url") or "") == cur), None)             or next((t for t in pages if (t.get("url") or "")[:80] == cur[:80]), None)             or pages[0]
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=timeout_s,
                                         max_size=(1 << 28), suppress_origin=True)
        try:
            ws.settimeout(timeout_s)
            ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot",
                                "params": {"format": "png"}}))
            end = time.time() + timeout_s
            while time.time() < end:
                ws.settimeout(max(0.2, end - time.time()))
                try:
                    msg = json.loads(ws.recv())
                except Exception as e:
                    raise RuntimeError("recv: %s" % str(e)[:80])
                if msg.get("id") == 1:
                    if "result" not in msg:
                        raise RuntimeError("captureScreenshot error: %s" % str(msg)[:200])
                    with open(path, "wb") as fh:
                        fh.write(base64.b64decode(msg["result"]["data"]))
                    return path
            raise RuntimeError("截图超时")
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception as e:
        # 兜底：Playwright 截图（可能因字体等待而慢，再失败就抛错）
        page.screenshot(path=path, timeout=5000)
        return path


def capture_questions(page, qf, outdir, section, cfg, log, pipeline_py, pipeline_dir):
    """整视口截图 + pill 裁剪（调用 pipeline/crop_boxes.py，CLI 契约）。"""
    cc = cfg["capture"]
    evid = os.path.join(outdir, "evidence")
    os.makedirs(evid, exist_ok=True)
    dims = page.evaluate(VIEWPORT_JS)
    cap = {"schema_version": "engine-capture/1", "section": section,
           "viewport": dims, "questions": [], "errors": []}
    loc = qf.locator(".TiMu")
    n = loc.count()
    if n <= 0:
        raise CaptureError("测验没有题目（.TiMu=0）")
    for i in range(n):
        loc.nth(i).scroll_into_view_if_needed(timeout=10000)
        page.wait_for_timeout(int(cc["scroll_wait_ms"]))
        box = loc.nth(i).bounding_box()
        png = os.path.join(evid, "_vp_%d.png" % (i + 1))
        screenshot(page, png, cfg)
        cap["questions"].append({"index": i + 1, "viewport_png": png, "bbox": box})
        log("截图 Q%d/%d" % (i + 1, n))
    raw_path = os.path.join(outdir, "capture-raw.json")
    common.write_json_atomic(raw_path, cap)

    crop_script = os.path.join(pipeline_dir, cfg["pipeline"]["crop"])
    cmd = [pipeline_py, crop_script, "--raw", raw_path, "--outdir", evid,
           "--scale", str(cc["crop_scale"])]
    log("裁剪: " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    for line in (p.stdout or "").strip().splitlines():
        log("  " + line)
    if p.returncode != 0:
        raise CaptureError("裁剪失败 rc=%s: %s" % (p.returncode, (p.stderr or "")[-400:]))
    quiz = common.read_json(os.path.join(evid, "quiz.json"))
    if not quiz or not quiz.get("questions"):
        raise CaptureError("裁剪产物 quiz.json 缺失/无题目")
    return {"raw": cap, "quiz": quiz, "evidence_dir": evid}


def build_manifest(section, quiz, meta):
    by = {m["index"]: m for m in meta}
    questions = []
    for it in quiz.get("questions") or []:
        idx = it.get("index")
        questions.append({"index": idx, "image": it.get("image"),
                          "dom_type": (by.get(idx) or {}).get("type")})
    return {"schema_version": "engine-manifest/1", "session": section,
            "questions": questions}


def restore_video_tab(page, cfg, log):
    """把标签页切回「课程视频」，保持页面状态与进入时一致（dry 收尾用）。"""
    for fr in page.frames:
        try:
            if fr.evaluate(VIDEO_TAB_JS, cfg["nav"]["video_tab_regex"]):
                log("已切回视频标签")
                return True
        except Exception:
            continue
    return False
