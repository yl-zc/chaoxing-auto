# nav.py —— 导航模块（单一职责：小节定位 / 进入 / 任务点 / 目录顺序 / 返回课程 / 下一节）
"""所有导航只使用页面自身元素：
  - 课程目录：DIV.chapter_item  onclick="toOld(courseId, chapterId, clazzid, 0)"
  - 小节页目录：SPAN.posCatalog_name onclick="getTeacherAjax(courseId, clazzid, chapterId)"
  - 返回课程：页面里的「返回课程」链接（href 运行时读取，不硬编码 enc）
  - 下一节：「下一节」按钮
不拼 URL、不调用窗口/焦点 API。

对外接口（供 run_section 编排）：
  resolve_section(page, section) -> 目标候选 dict
  goto_section(page, cfg, section, log) -> dict（已进入目标小节）
  resolve_catalog(page) -> [ {section,title,chapter_id,...} ]（页面目录顺序）
  read_task_count(page, section) -> {"value": "2", "text": "..."} | None
  read_current_section(page) -> "2.4" | None（按 URL chapterId 对照目录）
  read_back_to_course_href(page) -> str | None
  read_course_progress(page) -> {"n": int, "total": int, "text": str, "frame": int} | None
  click_next_section(page, cfg, expected_chapter_id, log) -> bool
"""
import re
import time

SECTION_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?![\d.])")


class NavError(RuntimeError):
    pass


# ---------------- 页面 DOM 脚本 ----------------
COLLECT_JS = r"""() => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const out = [];
  const add = (el, strategy, course_id, clazzid, chapter_id) => {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 90) return;
    out.push({strategy: strategy, course_id: course_id, clazzid: clazzid,
              chapter_id: chapter_id, text: t, tag: el.tagName,
              cls: el.className ? String(el.className).slice(0, 80) : '',
              visible: vis(el), children: el.children.length});
  };
  for (const el of document.querySelectorAll('[onclick]')) {
    const oc = el.getAttribute('onclick') || '';
    let m = oc.match(/toOld\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'/);
    if (m) { add(el, 'toOld', m[1], m[3], m[2]); continue; }
    m = oc.match(/getTeacherAjax\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'/);
    if (m) { add(el, 'getTeacherAjax', m[1], m[2], m[3]); continue; }
  }
  return out;
}"""

CLICK_JS = r"""(cid) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const hits = [];
  for (const el of document.querySelectorAll('[onclick]')) {
    const oc = el.getAttribute('onclick') || '';
    if (oc.indexOf(cid) < 0) continue;
    if (!/(toOld|getTeacherAjax)\(/.test(oc)) continue;
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 90) continue;
    hits.push(el);
  }
  if (!hits.length) return {n: 0};
  hits.sort((a, b) => {
    if (vis(a) !== vis(b)) return vis(a) ? -1 : 1;
    return a.children.length - b.children.length;
  });
  hits[0].click();
  return {n: hits.length, tag: hits[0].tagName, text: (hits[0].innerText || '').trim().slice(0, 60)};
}"""

TASK_COUNT_JS = r"""(sec) => {
  for (const li of document.querySelectorAll('li')) {
    const t = (li.innerText || '').trim().replace(/\s+/g, ' ');
    if (t.indexOf(sec) !== 0) continue;
    const i = li.querySelector('input.jobUnfinishCount');
    if (i) return {value: i.value, text: t.slice(0, 80)};
  }
  return null;
}"""

NEXT_CLICK_JS = r"""(label) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const c = [];
  for (const el of document.querySelectorAll('a,span,div,button,li')) {
    const t = (el.innerText || '').trim();
    if (t !== label || !vis(el)) continue;
    c.push(el);
  }
  if (!c.length) return {n: 0};
  c.sort((a, b) => a.children.length - b.children.length);
  c[0].click();
  return {n: c.length, tag: c[0].tagName};
}"""

BACK_HREF_JS = r"""() => {
  for (const a of document.querySelectorAll('a')) {
    const t = (a.innerText || '').trim();
    const h = a.getAttribute('href') || '';
    if (t.indexOf('返回课程') >= 0 && h.indexOf('mycourse/') >= 0) return h;
  }
  return null;
}"""

PROGRESS_TEXT_JS = r"""() => (document.body ? document.body.innerText : '')"""

COURSE_ENTRY_JS = r"""(name) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const hits = [];
  for (const el of document.querySelectorAll('a,div,li,span,dd,dt')) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 160 || t.indexOf(name) < 0 || !vis(el)) continue;
    hits.push(el);
  }
  if (!hits.length) return {n: 0};
  hits.sort((a, b) => a.children.length - b.children.length);
  hits[0].click();
  return {n: hits.length, tag: hits[0].tagName, text: (hits[0].innerText || '').trim().slice(0, 80)};
}"""


# ---------------- 工具 ----------------
def parse_item_text(text):
    """'2.4 青年要… 2' -> ('2.4', '青年要…')；不是小节条目返回 (None, '')。"""
    m = SECTION_RE.match(text or "")
    if not m:
        return None, ""
    sec = "%d.%d" % (int(m.group(1)), int(m.group(2)))
    title = (text or "")[m.end():].strip()
    title = re.sub(r"\s+\d+$", "", title).strip()
    return sec, title


def _collect(page):
    out = []
    for fi, fr in enumerate(page.frames):
        try:
            items = fr.evaluate(COLLECT_JS)
        except Exception:
            continue
        for it in items or []:
            rec = dict(it)
            rec["frame_index"] = fi
            rec["frame"] = fr
            out.append(rec)
    return out


def candidates_for(page, section):
    out = []
    for c in _collect(page):
        sec, title = parse_item_text(c.get("text"))
        if sec != section:
            continue
        c["section"] = sec
        c["title"] = title
        out.append(c)
    out.sort(key=lambda c: (0 if c.get("visible") else 1, c.get("children", 99)))
    return out


def resolve_section(page, section):
    cands = candidates_for(page, section)
    if not cands:
        raise NavError("页面目录中找不到小节 %s（可能未登录或页面不在课程内）" % section)
    return cands[0]


def resolve_catalog(page):
    """返回页面里最完整的一份目录（保持 DOM 顺序）。"""
    best = []
    for fi, fr in enumerate(page.frames):
        try:
            items = fr.evaluate(COLLECT_JS)
        except Exception:
            continue
        rows, seen = [], set()
        for it in items or []:
            sec, title = parse_item_text(it.get("text"))
            if not sec or sec in seen:
                continue
            seen.add(sec)
            rows.append({"section": sec, "title": title,
                         "chapter_id": it.get("chapter_id"), "course_id": it.get("course_id"),
                         "clazzid": it.get("clazzid"), "strategy": it.get("strategy"),
                         "visible": it.get("visible"), "frame_index": fi})
        if len(rows) > len(best):
            best = rows
    return best


def read_current_chapter_id(page):
    m = re.search(r"chapterId=(\d+)", page.url or "")
    return m.group(1) if m else None


def read_current_section(page):
    cid = read_current_chapter_id(page)
    if not cid:
        return None
    for row in resolve_catalog(page):
        if row.get("chapter_id") == cid:
            return row.get("section")
    return None


def _wait_url(page, needle, timeout_s, poll_ms):
    end = time.time() + float(timeout_s)
    while time.time() < end:
        if needle in (page.url or ""):
            return True
        page.wait_for_timeout(poll_ms)
    return needle in (page.url or "")


def _wait_url_changed(page, before, timeout_s, poll_ms):
    end = time.time() + float(timeout_s)
    while time.time() < end:
        if (page.url or "") != before:
            return True
        page.wait_for_timeout(poll_ms)
    return (page.url or "") != before


def has_catalog(page):
    """页面（含 iframe）里是否能解析出小节目录（toOld / getTeacherAjax 条目）。"""
    for c in _collect(page):
        sec, _title = parse_item_text(c.get("text"))
        if sec:
            return True
    return False


def click_course_entry(page, course_name, log):
    """在学习空间/课程列表里点开目标课程（按课程名文字）。"""
    if not course_name:
        return False
    for fi, fr in enumerate(page.frames):
        try:
            r = fr.evaluate(COURSE_ENTRY_JS, course_name)
        except Exception:
            continue
        if r and r.get("n"):
            log("点击课程入口「%s」（frame %d）" % (course_name, fi))
            return True
    return False


TEXT_CLICK_JS = r"""(texts) => {
  const vis = el => !!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  for (const want of texts) {
    const hits = [];
    for (const el of document.querySelectorAll('a,div,li,span,button')) {
      const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
      if (t !== want || !vis(el)) continue;
      hits.push(el);
    }
    if (hits.length) {
      hits.sort((a, b) => a.children.length - b.children.length);
      hits[0].click();
      return {text: want, n: hits.length, tag: hits[0].tagName};
    }
  }
  return null;
}"""


def click_visible_text(page, texts, log):
    """点击页面中文本精确等于 texts 之一的可见元素（如「我学的课」「我的课程」）。"""
    for fr in page.frames:
        try:
            r = fr.evaluate(TEXT_CLICK_JS, list(texts))
        except Exception:
            continue
        if r:
            log("点击入口「%s」" % r.get("text"))
            return r
    return None


def read_task_count(page, section):
    """读侧栏该小节「未完成任务点」数；先主文档，再各 frame。"""
    for fi, fr in enumerate(page.frames):
        try:
            r = fr.evaluate(TASK_COUNT_JS, section)
        except Exception:
            continue
        if r:
            r["frame_index"] = fi
            return r
    return None


def goto_section(page, cfg, section, log):
    nav = cfg["nav"]
    cands = candidates_for(page, section)
    if not cands:
        raise NavError("页面目录中找不到小节 %s（可能未登录或页面不在课程内）" % section)
    target = cands[0]
    if target["chapter_id"] in (page.url or ""):
        log("已在小节 %s（chapterId=%s），跳过导航" % (section, target["chapter_id"]))
        return target
    last = None
    for attempt in range(2):
        hit = None
        for c in cands:
            try:
                r = c["frame"].evaluate(CLICK_JS, c["chapter_id"])
            except Exception as e:
                last = "eval: %s" % str(e)[:80]
                continue
            if r and r.get("n"):
                hit = (c, r)
                break
        log("导航 %s 第 %d 次: %s" % (section, attempt + 1,
                                     ("命中 %s" % hit[1].get("text")) if hit else ("未命中元素 (%s)" % last)))
        if hit and _wait_url(page, target["chapter_id"], nav["wait_chapter_s"], nav["poll_ms"]):
            page.wait_for_timeout(nav["settle_ms"])
            log("已进入小节 %s: %s" % (section, (page.url or "")[:120]))
            return target
        page.wait_for_timeout(1000)
    raise NavError("导航失败：URL 未出现 chapterId=%s（小节 %s）" % (target["chapter_id"], section))


def click_next_section(page, cfg, expected_chapter_id, log):
    nav = cfg["nav"]
    before = page.url or ""
    hit = None
    for fi, fr in enumerate(page.frames):
        try:
            r = fr.evaluate(NEXT_CLICK_JS, nav["next_label"])
        except Exception:
            continue
        if r and r.get("n"):
            hit = (fi, r)
            break
    log("点击「%s」: %s" % (nav["next_label"], ("frame %s" % hit[0]) if hit else "未找到按钮"))
    if not hit:
        return False
    if expected_chapter_id:
        return _wait_url(page, expected_chapter_id, nav["wait_chapter_s"], nav["poll_ms"])
    return _wait_url_changed(page, before, nav["wait_chapter_s"], nav["poll_ms"])


def read_back_to_course_href(page):
    for fr in page.frames:
        try:
            href = fr.evaluate(BACK_HREF_JS)
        except Exception:
            continue
        if href:
            return href
    return None


def read_course_progress(page):
    pat = re.compile(r"已完成任务点[:：]?\s*(\d+)\s*/\s*(\d+)")
    for fi, fr in enumerate(page.frames):
        try:
            text = fr.evaluate(PROGRESS_TEXT_JS)
        except Exception:
            continue
        if not text:
            continue
        m = pat.search(text)
        if m:
            return {"n": int(m.group(1)), "total": int(m.group(2)),
                    "text": m.group(0), "frame_index": fi}
    return None
