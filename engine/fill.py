# fill.py —— 回填模块（单一职责：把 A->C 答案点进 DOM，并做三对照校验）
"""三对照（与 auto-2.3 完全一致）：
  1) aria 显示字母  == 期望字母
  2) 选项提交码 data == 期望提交码（按 meta 的 shown->code 映射）
  3) 隐藏域 answerN   == 期望提交码拼接
任一项不符即失败，绝不提交。
对外接口：
  apply(frame, answers_obj, meta, cfg, log) -> dict {pass, questions}
"""
import time

import common


class FillError(RuntimeError):
    pass


QUIZ_STATE = r"""() => {
  const timus = Array.from(document.querySelectorAll('.TiMu'));
  return timus.map((t, i) => {
    const lis = Array.from(t.querySelectorAll('li'));
    const hid = Array.from(t.querySelectorAll('input[type=hidden]'))
        .filter(h => /^answer\d+$/.test(h.name)).map(h => ({name:h.name, value:h.value}));
    return {index: i+1, answer_value: hid.length ? hid[0].value : null,
      checked: lis.filter(l => l.getAttribute('aria-checked')==='true')
        .map(l => { const n=l.querySelector('.num_option,.num_option_dx'); return n?(n.textContent||'').trim():null; }),
      codes: lis.filter(l => l.getAttribute('aria-checked')==='true')
        .map(l => { const n=l.querySelector('.num_option,.num_option_dx'); return n?n.getAttribute('data'):null; })};
  });
}"""


def read_state(frame):
    return frame.evaluate(QUIZ_STATE) or []


def apply(frame, answers_obj, meta, cfg, log):
    fc = cfg["fill"]
    click_method = fc["click_method"]
    wait_ms = int(fc["click_wait_ms"])
    answers = answers_obj.get("answers") or []
    want = {a["index"]: [str(v) for v in (a.get("value") or [])] for a in answers}
    if not want:
        raise FillError("answers 为空，禁止回填")

    fill = {"schema_version": "engine-fill/1", "questions": [], "pass": False,
            "click_method": click_method}
    loc = frame.locator(".TiMu")
    total = loc.count()
    for idx in sorted(want):
        if idx < 1 or idx > total:
            raise FillError("Q%d 超出题目范围（共 %d 题）" % (idx, total))
        tq = loc.nth(idx - 1)
        item = {"index": idx, "want": want[idx], "method": click_method, "clicks": []}
        for key in want[idx]:
            kk = None
            for k in range(tq.locator("li").count()):
                num = tq.locator("li").nth(k).locator(".num_option, .num_option_dx").first
                try:
                    shown = (num.inner_text() or "").strip()
                except Exception:
                    shown = ""
                if shown == key:
                    kk = k
                    break
            if kk is None:
                raise FillError("Q%d 选项 %s 未找到" % (idx, key))
            li = tq.locator("li").nth(kk)
            if click_method == "locator":
                li.click(timeout=8000)
            else:
                li.evaluate("el => el.click()")
            item["clicks"].append({"key": key, "at": time.strftime("%H:%M:%S")})
            frame.page.wait_for_timeout(wait_ms)
        fill["questions"].append(item)

    states = read_state(frame)
    ok_all = True
    for item in fill["questions"]:
        idx = item["index"]
        st = states[idx - 1] if idx - 1 < len(states) else {}
        qmeta = meta[idx - 1] if idx - 1 < len(meta) else {}
        code_map = {}
        for o in (qmeta.get("options") or []):
            if o.get("shown") is not None:
                code_map[o["shown"]] = o.get("code")
        exp_codes = []
        for k in item["want"]:
            if k not in code_map:
                raise FillError("Q%d 选项 %s 无提交码映射" % (idx, k))
            exp_codes.append(code_map[k])
        got_codes = sorted(st.get("codes") or [])
        exp_codes_sorted = sorted(exp_codes)
        exp_letters = sorted(item["want"])
        got_letters = sorted(st.get("checked") or [])
        hid = st.get("answer_value") or ""
        hid_ok = (hid == exp_codes[0]) if len(exp_codes) == 1 else (sorted(str(hid)) == exp_codes_sorted)
        three = {"letters_match": got_letters == exp_letters,
                 "codes_match": got_codes == exp_codes_sorted,
                 "hidden": hid, "expected_hidden": "".join(exp_codes),
                 "hidden_ok": bool(hid_ok)}
        item["state"] = st
        item["three_way"] = three
        item["ok"] = bool(three["letters_match"] and three["codes_match"] and three["hidden_ok"])
        ok_all = ok_all and item["ok"]
        log("回填 Q%d want=%s checked=%s codes=%s hidden=%r ok=%s" % (
            idx, item["want"], st.get("checked"), st.get("codes"), hid, item["ok"]))
    fill["pass"] = bool(ok_all)
    return fill
