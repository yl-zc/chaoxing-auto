# verify.py —— 验收模块（单一职责：跳下一节并暂停 / 读任务点 / 读课程进度）
"""验收尽可能只读：
  - 点「下一节」（页面按钮）跳到下一节，并暂停可能自动播放的视频
  - 可选：通过页面「返回课程」链接回课程页读「已完成任务点 N/59」，再点目录回到下一节
  - 课程进度读取失败只记 warning，不影响已提交事实
对外接口：
  jump_next(page, cfg, nav, log, current_section) -> dict
  read_progress(page, cfg, nav, log, next_section) -> dict
"""
import time

import common
import video


def click_next(page, cfg, nav, log, current_section):
    catalog = nav.resolve_catalog(page)
    idx = next((i for i, r in enumerate(catalog) if r.get("section") == current_section), None)
    nxt = catalog[idx + 1] if (idx is not None and idx + 1 < len(catalog)) else None
    expected = nxt.get("chapter_id") if nxt else None
    ok = nav.click_next_section(page, cfg, expected, log)
    info = {"ok": bool(ok), "strategy": "next_button", "next": nxt,
            "expected_chapter_id": expected}
    if not ok and nxt:
        log("「下一节」未生效，改用目录项 %s" % nxt["section"])
        try:
            nav.goto_section(page, cfg, nxt["section"], log)
            info["ok"] = True
            info["strategy"] = "catalog"
        except Exception as e:
            info["error"] = str(e)[:250]
    if info["ok"]:
        page.wait_for_timeout(cfg["nav"]["settle_ms"])
        info["url"] = (page.url or "")[:200]
        info["next_chapter_landed"] = nav.read_current_chapter_id(page)
        if cfg["verify"]["pause_next_video"]:
            try:
                n = video.pause_all(page, log)
                info["paused_videos"] = n
            except Exception as e:
                info["pause_error"] = str(e)[:150]
        if nxt:
            try:
                info["next_task_count"] = nav.read_task_count(page, nxt["section"])
            except Exception:
                info["next_task_count"] = None
    return info


def read_progress(page, cfg, nav, log, next_section):
    href = nav.read_back_to_course_href(page)
    if not href:
        return {"ok": False, "error": "未找到「返回课程」链接"}
    log("回课程页读进度: " + href[:120])
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return {"ok": False, "error": "返回课程失败: %s" % str(e)[:200]}
    page.wait_for_timeout(int(float(cfg["verify"]["progress_wait_s"]) * 1000))
    prog = nav.read_course_progress(page)
    out = {"ok": bool(prog), "progress": prog, "course_url": (page.url or "")[:180]}
    if next_section:
        try:
            nav.goto_section(page, cfg, next_section, log)
            out["back_to"] = next_section
            out["back_to_url"] = (page.url or "")[:180]
        except Exception as e:
            out["back_error"] = str(e)[:250]
    return out
