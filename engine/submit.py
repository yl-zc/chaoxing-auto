# submit.py —— 提交模块（单一职责：提交 1 次 + 确认 1 次 + 读成绩）
"""纪律（硬约束）：
  - 提交按钮只点 1 次；确认按钮只点 1 次；任何异常都不允许重试提交
  - 确认弹窗未出现 -> SubmitError（已提交与否未知时也只上报，不重复点击）
  - 已提交但成绩读不到 -> 返回 submitted=True + score=None（由编排层记 error 留证据）
对外接口：
  submit_once(page, qf, cfg, log) -> dict
"""
import re
import time

import common


class SubmitError(RuntimeError):
    pass


POPUP_JS = r"""(ids) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const box = document.getElementById(ids[0]);
  const ok = document.getElementById(ids[1]);
  return {visible: !!(box && vis(box)),
          text: box ? (box.innerText||'').trim().slice(0, 300) : null,
          ok_visible: !!(ok && vis(ok)),
          ok_text: ok ? (ok.innerText||'').trim() : null};
}"""

CLICK_ID_JS = r"""(id) => {
  const el = document.getElementById(id);
  if (!el) return false;
  el.click();
  return true;
}"""

RESULT_TEXT_JS = r"""() => (document.body ? document.body.innerText : '').slice(0, 4000)"""


def _find_result_frame(page, cfg):
    key = cfg["submit"]["result_url_key"]
    return next((f for f in page.frames if key in (f.url or "")), None)


def wait_popup(page, cfg, log):
    sc = cfg["submit"]
    end = time.time() + float(sc["popup_wait_s"])
    last = None
    while time.time() < end:
        try:
            st = page.evaluate(POPUP_JS, [sc["popup_box_id"], sc["popup_ok_id"]])
        except Exception as e:
            st = {"error": str(e)[:100]}
        last = st
        if st.get("visible") and st.get("ok_visible") and st.get("ok_text") in (sc["confirm_texts"] or []):
            return st
        page.wait_for_timeout(300)
    log("确认弹窗异常: " + str(last)[:300])
    return None


def wait_result(page, cfg, log):
    sc = cfg["submit"]
    end = time.time() + float(sc["result_wait_s"])
    txt, resf = "", None
    while time.time() < end:
        resf = _find_result_frame(page, cfg)
        if resf is not None:
            try:
                txt = resf.evaluate(RESULT_TEXT_JS) or ""
            except Exception:
                txt = ""
            if "本次成绩" in txt:
                break
        page.wait_for_timeout(int(sc["result_poll_ms"]))
    return txt, resf


def parse_result(txt):
    m = re.search(r"本次成绩\s*(\d+(?:\.\d+)?)\s*分", txt or "")
    score = None
    if m:
        try:
            score = float(m.group(1))
            if score == int(score):
                score = int(score)
        except Exception:
            score = m.group(1)
    am = re.search(r"第(\d+)次作答", txt or "")
    attempt = None
    if am:
        try:
            attempt = int(am.group(1))
        except Exception:
            attempt = am.group(1)
    per = re.findall(r"(\d+\.\d)分", txt or "")
    notes = [l for l in (txt or "").splitlines() if ("次数" in l or "剩余" in l)][:3]
    return {"score": score, "attempt": attempt, "per_question": per,
            "status": "已完成" if "已完成" in (txt or "") else None,
            "attempt_notice": notes}


def submit_once(page, qf, cfg, log):
    sc = cfg["submit"]
    out = {"schema_version": "engine-submit/1", "submitted": False, "score": None,
           "clicks": {"submit": 0, "confirm": 0}, "popup": None, "result_text_head": ""}

    sel = sc["button_selector"]
    n = qf.locator(sel).count()
    if n <= 0:
        raise SubmitError("未找到提交按钮 %s" % sel)
    qf.locator(sel).first.evaluate("el => el.click()")
    out["clicks"]["submit"] = 1
    out["submitted"] = True
    log("已点击提交按钮 1 次（%s），等待确认弹窗" % sel)
    page.wait_for_timeout(int(sc["click_wait_ms"]))

    pop = wait_popup(page, cfg, log)
    if pop is None:
        raise SubmitError("提交确认弹窗未出现（未重复点击提交；请人工查看页面）")
    out["popup"] = pop
    if not page.evaluate(CLICK_ID_JS, sc["popup_ok_id"]):
        raise SubmitError("确认按钮 #%s 点击失败（未重试）" % sc["popup_ok_id"])
    out["clicks"]["confirm"] = 1
    log("已点击确认 1 次（#%s，文案=%r）" % (sc["popup_ok_id"], pop.get("ok_text")))

    txt, resf = wait_result(page, cfg, log)
    parsed = parse_result(txt)
    out.update(parsed)
    out["result_text_head"] = (txt or "")[:1200]
    out["result_frame_url"] = (resf.url or "")[:200] if resf else None
    out["finished_at"] = common.now_iso()
    if not txt:
        raise SubmitError("已提交，但结果页文本未读到（score 未知）")
    log("提交结果: score=%s attempt=%s status=%s" % (out.get("score"), out.get("attempt"), out.get("status")))
    return out
