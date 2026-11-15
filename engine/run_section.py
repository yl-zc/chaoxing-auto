# run_section.py —— 单节统一运行器（编排层，只编排不实现页面细节）
"""用法：
  python run_section.py --section 2.4 --mode dry
  python run_section.py --section 2.4 --mode real --progress-file <run_dir>/progress.json

模式：
  dry  —— 只读检查：能否进入小节 / 任务点数 / 视频是否存在 / 题型；不播放、不答题、不提交
  real —— 完整流程：视频(>=90% 或 marker) -> 测验捕获 -> A->C 子进程 -> 闸门
          -> DOM 回填三对照 -> 提交(按钮/确认各 1 次) -> 跳下一节并暂停 -> 读课程进度
失败即停：闸门不过/回填不一致/弹窗异常/成绩读不到 -> 立即停止并落证据，绝不重复提交。
退出码：0 成功；2 业务中止；3 异常；4 real 未放行；5 已有实例在跑（文件锁）。
"""
import argparse
import json
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import browser as browser_layer
import nav
import preflight
import video
import capture
import answer
import fill
import submit
import verify

from playwright.sync_api import sync_playwright

EXIT_OK = 0
EXIT_ABORT = 2
EXIT_EXCEPTION = 3
EXIT_REAL_DISABLED = 4
EXIT_LOCKED = 5

SECTION_RE = re.compile(r"^\d{1,2}\.\d{1,2}$")


class Abort(RuntimeError):
    pass


class StopRequested(RuntimeError):
    pass


def check_stop(cfg, since_ts, log):
    req = common.stop_requested(cfg, since_ts)
    if req:
        log("收到停止请求（%s/%s），执行安全收尾（不提交半截答案）" % (
            req.get("file"), req.get("reason")))
        raise StopRequested("stop_requested:%s" % req.get("reason"))
    return None


def parse_int_task_count(side):
    """nav.read_task_count 的返回 -> int | None"""
    if not isinstance(side, dict):
        return None
    v = side.get("value")
    try:
        return int(str(v).strip())
    except Exception:
        return None


def write_index(cfg, run, run_dir, plan_id=None):
    """全局索引 logs/index.jsonl：每次运行一行（打包复查入口）。"""
    try:
        rec = {"schema_version": "engine-index/1", "at": common.now_iso(),
               "mode": run.get("mode"), "section": run.get("section"),
               "plan_id": plan_id, "status": run.get("status"),
               "score": run.get("score"), "progress_total": run.get("progress_total"),
               "next_section": run.get("next_section"),
               "elapsed_s": run.get("elapsed_s"), "run_dir": run_dir,
               "error": str(run.get("error") or "")[:200]}
        common.append_jsonl(common.path(cfg, "index_file"), rec)
    except Exception:
        pass


def _run_dir_default(cfg, section):
    ts = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(common.path(cfg, "runs_dir"), section, ts)


def _connect(cfg, log):
    endpoint = common.cdp_endpoint(cfg)
    timeout_ms = int(float(cfg["cdp"]["connect_timeout_s"]) * 1000)
    r = browser_layer.resolve(cfg)
    log("连接 CDP: %s（%s profile=%s）" % (endpoint, r["kind"], r["profile"]))
    return endpoint, timeout_ms


def _pick_page(pw_browser, cfg, log):
    """选页规则（见 browser.pick_page_index）：只选学习通页面，排除控制台/内部页。"""
    pages = [p for c in pw_browser.contexts for p in c.pages]
    urls = [(p.url or "") for p in pages]
    if not urls:
        raise Abort("CDP 没有可用 page")
    idx = browser_layer.pick_page_index(urls, cfg)
    if idx is None:
        raise Abort("浏览器里没有学习通页面（请打开并登录学习通；控制台标签不会被操作）")
    page = pages[idx]
    log("选中学习通标签[%d/%d]: %s" % (idx + 1, len(pages), (page.url or "")[:140]))
    if "passport2" in (page.url or ""):
        raise Abort("学习通未登录（URL 含 passport2），请先在浏览器扫码登录")
    return page


def ensure_course_page(page, cfg, pw_browser, log):
    """若当前不是课程目录页（例如登录后停在「学习空间」），自动点进课程。
    返回 (page, switched)；找不到课程入口时给出明确中止信息。
    """
    if nav.has_catalog(page):
        return page, False
    name = (cfg.get("nav") or {}).get("course_name") or ""
    log("当前页面无课程目录，尝试进入课程「%s」" % name)
    wait_s = float((cfg.get("nav") or {}).get("course_entry_wait_s") or 20)
    for attempt in range(2):
        clicked = nav.click_course_entry(page, name, log)
        if not clicked:
            # 学习空间可能默认在其它 tab：先点「我学的课 / 我的课程」再找课程
            tab = nav.click_visible_text(page, ["我学的课", "我的课程", "我的课堂"], log)
            page.wait_for_timeout(1500)
            clicked = bool(tab) and nav.click_course_entry(page, name, log)
        if not clicked:
            log("未找到课程入口「%s」" % name)
            break
        end = time.time() + wait_s
        while time.time() < end:
            page.wait_for_timeout(int((cfg.get("nav") or {}).get("poll_ms") or 700))
            if nav.has_catalog(page):
                log("已进入课程目录页: " + (page.url or "")[:140])
                return page, True
            # 可能在新标签打开
            try:
                pages = [p for c in pw_browser.contexts for p in c.pages]
                idx = browser_layer.pick_page_index([p.url or "" for p in pages], cfg)
                if idx is not None and nav.has_catalog(pages[idx]):
                    log("课程目录页在新标签，切换 target: " + (pages[idx].url or "")[:140])
                    return pages[idx], True
            except Exception:
                pass
        page.wait_for_timeout(800)
    raise Abort("未能进入课程目录页（当前可能在学习空间/登录页）；请先手动打开一次课程，或检查 config.nav.course_name=%r" % name)


# ================= dry 只读检查 =================
def dry_check(page, cfg, section, run_dir, log, prog, run, pw_browser):
    checks, warnings = {}, []
    prog.set("video", 0.12, "正在确认课程目录页")
    page, switched = ensure_course_page(page, cfg, pw_browser, log)
    run["phases"]["P0_course_entry"] = {"ok": True, "switched": switched,
                                        "url": (page.url or "")[:200]}
    prog.set("video", 0.15, "正在定位小节 %s" % section)
    cand = nav.goto_section(page, cfg, section, log)

    prog.set("video", 0.35, "已进入 %s，读取任务点" % section)
    wait_task_s = float((cfg.get("nav") or {}).get("task_count_wait_s") or 8)
    side = None
    n = None
    deadline = time.time() + wait_task_s
    while True:
        side = nav.read_task_count(page, section)
        n = parse_int_task_count(side)
        if n is not None or time.time() >= deadline:
            break
        page.wait_for_timeout(700)
    checks["task_count"] = side
    checks["task_count_source"] = "section_page"
    run["task_count"] = n
    if n is None:
        warnings.append("节内任务点数读不到（unknown；不跳过/不默认0）")
        log("dry: 节内任务点数 unknown（%s）" % section)
    else:
        log("dry: 节内任务点数=%s（%s）" % (n, section))

    try:
        capture.restore_video_tab(page, cfg, log)  # 先确保在「课程视频」标签，再只读探测
    except Exception:
        pass
    vp = {"present": False}
    for _ in range(6):  # 只读等待视频元素出现（不播放、不 load）
        page.wait_for_timeout(500)
        vp = video.probe(page)
        if vp.get("present") and (vp.get("state") or {}).get("count"):
            break
    run["video"] = vp
    if not vp.get("present"):
        warnings.append("未找到视频 frame（可能本节无视频或标签页不在视频）")

    prog.set("quiz", 0.6, "正在进入章节测验（只读，不答题）")
    qf, meta, info = capture.enter_quiz(page, cfg, log, allow_start=False)
    run["quiz"] = {"frame_url": info.get("frame_url"), "questions": len(meta),
                   "needs_start": info.get("needs_start"),
                   "types": [m.get("type") for m in meta],
                   "detail": [{"index": m.get("index"), "type": m.get("type"),
                               "n_options": m.get("n_options")} for m in meta]}
    if not meta:
        warnings.append("未读到题目（可能需要先点「开始答题」，dry 模式不代点）")

    shot = os.path.join(run_dir, "evidence", "dry-quiz.png")
    try:
        capture.screenshot(page, shot, cfg)
        run["evidence"] = {"quiz_screenshot": shot}
    except Exception as e:
        warnings.append("截图失败: %s" % str(e)[:120])

    try:
        capture.restore_video_tab(page, cfg, log)
    except Exception:
        pass
    video.pause_all(page, log)  # dry 绝不播放；若平台自动播放则立即暂停

    run["checks"] = checks
    run["warnings"] = warnings
    run["url_after"] = (page.url or "")[:200]
    run["elapsed_s"] = round(time.time() - run["_t0"], 1)
    run["title"] = cand.get("title") or ""
    run["chapter_id"] = cand.get("chapter_id")
    run["ok"] = bool(meta) and bool(cand.get("chapter_id"))
    run["status"] = "dry_ok" if run["ok"] else "dry_failed"
    prog.set("submit", 1.0, "dry 检查完成：%s" % ("通过" if run["ok"] else "有告警"))
    log("dry 结果: ok=%s 任务点=%s 题型=%s 告警=%s" % (
        run["ok"], n, run["quiz"]["types"], warnings))
    return EXIT_OK if run["ok"] else EXIT_ABORT


# ================= real 完整流程 =================
def real_flow(page, cfg, section, run_dir, log, prog, run, pw_browser, stop_since, skip_quiz=False):
    evid = os.path.join(run_dir, "evidence")
    os.makedirs(evid, exist_ok=True)
    vc = cfg["video"]

    # P0 课程入口 + 页面真实任务点核对（用户可能已手动完成）
    prog.set("video", 0.02, "准备进入 %s" % section)
    page, switched = ensure_course_page(page, cfg, pw_browser, log)
    run["phases"]["P0_course_entry"] = {"ok": True, "switched": switched,
                                        "url": (page.url or "")[:200]}
    check_stop(cfg, stop_since, log)
    prog.set("video", 0.03, "正在定位小节 %s" % section)
    cand = nav.goto_section(page, cfg, section, log)
    run["title"] = cand.get("title") or ("第 %s 节" % section)
    run["chapter_id"] = cand.get("chapter_id")
    run["url"] = (page.url or "")[:200]
    run["phases"]["P0_nav"] = {"ok": True, "title": run["title"],
                               "chapter_id": run["chapter_id"], "url": run["url"]}
    log("目标: %s %s（chapterId=%s）" % (section, run["title"], run["chapter_id"]))

    # P1 实时节内任务点（唯一依据；不再用课程目录/预检数字做 already_done 判断）
    prog.set("video", 0.04, "读取节内任务点数")
    wait_task_s = float((cfg.get("nav") or {}).get("task_count_wait_s") or 8)
    side = None
    n_side = None
    deadline = time.time() + wait_task_s
    while True:
        side = nav.read_task_count(page, section)
        n_side = parse_int_task_count(side)
        if n_side is not None:
            break
        if time.time() >= deadline:
            break
        page.wait_for_timeout(700)
    run["baseline"] = {"url": run["url"], "task_count": side, "n": n_side,
                       "source": "section_page"}
    run["phases"]["P1_baseline"] = {"ok": n_side is not None, "task_count": side,
                                    "n": n_side, "source": "section_page"}
    if n_side is None:
        run["status"] = "unknown"
        run["error"] = ("TASK_COUNT_UNKNOWN: 进入 %s 后节内任务点数读不到"
                        "（不跳过、不默认 0，需人工确认）" % section)
        run["elapsed_s"] = round(time.time() - run["_t0"], 1)
        log("UNKNOWN: " + run["error"])
        return EXIT_OK
    n = n_side
    if n <= 0:
        cands = nav.resolve_catalog(page)
        order = [r.get("section") for r in cands]
        nxt = ""
        if section in order and order.index(section) + 1 < len(order):
            nxt = order[order.index(section) + 1]
        run["status"] = "already_done"
        run["next_section"] = nxt
        run["skip_reason"] = "节内实时任务点数明确为 0（本节已完成，跳过，不改课程）"
        run["elapsed_s"] = round(time.time() - run["_t0"], 1)
        log("跳过：%s" % run["skip_reason"])
        return EXIT_OK

    # P2 视频
    need_video = (n >= 2)
    if need_video:
        try:
            capture.restore_video_tab(page, cfg, log)
        except Exception:
            pass
        prog.set("video", 0.05, "正在准备播放视频")
        vf = video.find_video_frame(page)
        if vf is None:
            raise Abort("未找到视频 frame（modules/video）")

        def on_sample(st, ratio, dur):
            prog.set("video", 0.05 + 0.40 * float(ratio),
                     "正在播放视频 %s/%s" % (common.mmss(st.get("currentTime")), common.mmss(dur)))

        vid = video.ensure_done(page, cfg, section, log,
                                lambda: nav.read_task_count(page, section), on_sample,
                                should_stop=lambda: bool(common.stop_requested(cfg, stop_since)))
        run["video"] = vid
        samples = vid.get("samples") or []
        with open(os.path.join(evid, "video-samples.jsonl"), "w", encoding="utf-8") as fh:
            for s in samples:
                fh.write(json.dumps(s, ensure_ascii=False) + "\n")
        ok_reasons = ("marker", "task_credited", "ratio_final")
        run["phases"]["P2_video"] = {"ok": vid.get("stop_reason") in ok_reasons,
                                     "stop_reason": vid.get("stop_reason"),
                                     "final_ratio": vid.get("final_ratio"),
                                     "duration": vid.get("duration"),
                                     "elapsed_s": vid.get("elapsed_s")}
        if vid.get("stop_reason") == "stop_requested":
            run["stopped"] = True
            run["stop_reason"] = "stop_requested_during_video"
            raise StopRequested("stop_requested_during_video")
        if vid.get("stop_reason") not in ok_reasons:
            raise Abort("视频未达标停止：%s（final_ratio=%.4f）" % (
                vid.get("stop_reason"), float(vid.get("final_ratio") or 0)))
        # 实时读任务点数确认 2 -> 1（读不到则 unknown，不跳过）
        credited, last = False, side
        for _ in range(int(vc["credit_wait_s"])):
            last = nav.read_task_count(page, section)
            cur = parse_int_task_count(last)
            if cur is not None and cur <= 1:
                credited = True
                break
            page.wait_for_timeout(1000)
        last_n = parse_int_task_count(last)
        try:
            run["video"]["side_before"] = n
            run["video"]["side_after"] = last
        except Exception:
            pass
        if last_n is None:
            raise Abort("TASK_COUNT_UNKNOWN: 视频完成后节内任务点数读不到（不跳过/不默认0）")
        if not credited:
            raise Abort("视频任务点未计入（task_count=%s）" % last)
        log("视频任务点数确认：%s -> %s" % (n, last_n))
        prog.set("video", 0.45, "视频任务点已完成（%.0f%%）" % (100.0 * float(vid.get("final_ratio") or 0)))
        log("视频任务点已计入")
    else:
        run["video"] = {"skipped": True, "reason": "任务点数=%s（视频已完成，仅剩测验）" % n}
        run["phases"]["P2_video"] = {"ok": True, "skipped": True, "n": n,
                                     "note": "视频任务点已计入（页面任务点=%s），本次只做测验" % n}
        prog.set("video", 0.45, "视频已完成，本次只做测验")
        log("视频任务点已计入（任务点=%s，仅剩测验），跳过视频" % n)

    # skip_quiz 模式：视频完成/已计入后，不做章节测验、不提交，记录 skipped_quiz 并继续下一节
    if skip_quiz:
        try:
            cands = nav.resolve_catalog(page)
            order = [r.get("section") for r in cands]
            nxt = ""
            if section in order and order.index(section) + 1 < len(order):
                nxt = order[order.index(section) + 1]
        except Exception:
            nxt = ""
        run["status"] = "skipped_quiz"
        run["skipped_quiz"] = True
        run["next_section"] = nxt
        run["skip_reason"] = "skip_quiz=true：只做视频，不做章节测验、不提交"
        prog.set("submit", 0.95, "skip_quiz 已开启：视频完成，跳过章节测验")
        try:
            jump = verify.click_next(page, cfg, nav, log, section)
            run["phases"]["P9_jump"] = {"ok": bool(jump.get("ok")),
                                        "next_section": (jump.get("next") or {}).get("section")}
        except Exception as e:
            run["phases"]["P9_jump"] = {"ok": False, "error": str(e)[:150]}
        run["elapsed_s"] = round(time.time() - run["_t0"], 1)
        log("skip_quiz=true：视频已完成，跳过章节测验并继续下一节（未提交）")
        return EXIT_OK

    check_stop(cfg, stop_since, log)
    # P3 测验入口
    prog.set("quiz", 0.48, "正在进入章节测验")
    qf, meta, info = capture.enter_quiz(page, cfg, log, allow_start=True)
    if not meta:
        if info.get("already_done"):
            run["status"] = "already_done"
            run["skip_reason"] = "测验页显示已完成/已有成绩，无题目可做（跳过，不提交）"
            run["elapsed_s"] = round(time.time() - run["_t0"], 1)
            log("跳过：%s" % run["skip_reason"])
            return EXIT_OK
        raise Abort("测验无题目（可能未开始/未加载）")
    common.write_json_atomic(os.path.join(run_dir, "quiz-meta.json"), {
        "schema_version": "engine-quiz-meta/1", "section": section,
        "frame_url": info.get("frame_url"), "questions": meta})
    log("测验: %d 题，类型=%s" % (len(meta), [m.get("type") for m in meta]))
    run["phases"]["P3_quiz"] = {"ok": True, "questions": len(meta),
                                "types": [m.get("type") for m in meta],
                                "frame_url": info.get("frame_url")}

    check_stop(cfg, stop_since, log)
    # P4 题图捕获 + 裁剪（pipeline CLI）
    prog.set("quiz", 0.52, "正在截取题图（共 %d 题）" % len(meta))
    pipeline_dir = common.path(cfg, "pipeline_dir")
    pipeline_py = common.resolve_path(cfg["python"]["pipeline"])
    cap = capture.capture_questions(page, qf, run_dir, section, cfg, log,
                                    pipeline_py, pipeline_dir)
    manifest = capture.build_manifest(section, cap["quiz"], meta)
    manifest_path = os.path.join(run_dir, "manifest.json")
    common.write_json_atomic(manifest_path, manifest)
    run["phases"]["P4_capture"] = {"ok": True, "questions": len(manifest["questions"]),
                                   "manifest": manifest_path}
    log("题图捕获完成: %d 张" % len(manifest["questions"]))

    check_stop(cfg, stop_since, log)
    # P5 A->C + 闸门（子进程 CLI，复用 pipeline 全流程）
    prog.set("quiz", 0.58, "A→C 作答中（模型推理，%d 题）" % len(meta))
    pdir = os.path.join(run_dir, "pipeline")
    ans = answer.solve(cfg, manifest_path, pdir, log)
    gate_doc = {"schema_version": "engine-gate/1", "section": section,
                "min_conf": cfg["pipeline"]["min_conf"],
                "pass": bool(ans.get("ok")), "gate": ans.get("gate"),
                "extract_validation": (ans.get("extracted") or {}).get("validation"),
                "answer_validation": (ans.get("answers") or {}).get("validation"),
                "rc": ans.get("rc"), "elapsed_s": ans.get("elapsed_s"),
                "error": ans.get("error")}
    common.write_json_atomic(os.path.join(run_dir, "gate.json"), gate_doc)
    run["phases"]["P5_answer"] = {k: gate_doc.get(k) for k in
                                  ("pass", "min_conf", "rc", "elapsed_s", "error")}
    if not ans.get("ok"):
        raise Abort("闸门未通过，禁止回填/提交：%s" % (ans.get("error") or "unknown"))
    prog.set("quiz", 0.80, "闸门通过（min_conf=%s）" % (ans.get("gate") or {}).get("min_confidence"))
    log("闸门通过 min_conf=%s" % (ans.get("gate") or {}).get("min_confidence"))

    check_stop(cfg, stop_since, log)
    # P6 回填 + 三对照
    prog.set("submit", 0.82, "正在回填答案（%d 题）" % len(meta))
    fill_data = fill.apply(qf, ans.get("answers") or {}, meta, cfg, log)
    common.write_json_atomic(os.path.join(run_dir, "fill.json"), fill_data)
    run["phases"]["P6_fill"] = {"ok": bool(fill_data.get("pass")),
                                "questions": len(fill_data.get("questions") or []),
                                "click_method": fill_data.get("click_method")}
    if not fill_data.get("pass"):
        raise Abort("回填三对照失败，禁止提交")
    try:
        capture.screenshot(page, os.path.join(evid, "filled.png"), cfg)
    except Exception as e:
        log("回填后截图失败（不阻断）: " + str(e)[:120])
    prog.set("submit", 0.90, "回填校验通过，准备提交")

    check_stop(cfg, stop_since, log)  # 提交前最后检查：有 stop 就绝不点提交
    # P7 提交（各 1 次，禁止重复）
    prog.set("submit", 0.92, "正在提交（提交/确认各 1 次）")
    sub = submit.submit_once(page, qf, cfg, log)
    common.write_json_atomic(os.path.join(run_dir, "submit.json"), sub)
    run["submit"] = {k: sub.get(k) for k in ("submitted", "score", "attempt", "status",
                                             "per_question", "clicks", "attempt_notice",
                                             "finished_at", "result_frame_url")}
    run["phases"]["P7_submit"] = {"ok": sub.get("score") is not None,
                                  "score": sub.get("score"), "attempt": sub.get("attempt"),
                                  "clicks": sub.get("clicks")}
    if sub.get("score") is None:
        raise Abort("已提交，但未读到成绩（不可重试，请人工核对）")
    run["score"] = sub.get("score")
    run["attempt"] = sub.get("attempt")
    if common.stop_requested(cfg, stop_since):
        run["stopped"] = True
        run["stop_reason"] = "stop_requested_after_submit"
        run["elapsed_s"] = round(time.time() - run["_t0"], 1)
        log("提交已完成；检测到停止请求，不再跳转下一节，按停止收尾")
        return EXIT_OK
    try:
        capture.screenshot(page, os.path.join(evid, "submit-result.png"), cfg)
    except Exception as e:
        log("结果页截图失败（不阻断）: " + str(e)[:120])
    # 提交后实时确认任务点 1 -> 0（读不到记 unknown，用成绩/进度佐证，不阻断已完成事实）
    tc_before = None
    try:
        tc_before = parse_int_task_count(nav.read_task_count(page, section))
    except Exception:
        tc_before = None
    tc_after = None
    for _ in range(10):
        page.wait_for_timeout(1000)
        try:
            tc_after = parse_int_task_count(nav.read_task_count(page, section))
        except Exception:
            tc_after = None
        if tc_after is not None and tc_after <= 0:
            break
    run["phases"]["P8b_taskcount_confirm"] = {
        "before": tc_before, "after": tc_after,
        "explicit_zero": bool(tc_after is not None and tc_after <= 0),
        "source": "section_page"}
    if tc_after is None:
        run["warnings"].append("提交后节内任务点数读不到（unknown；以成绩+课程进度佐证完成）")
        log("提交后任务点数 unknown（以成绩/进度佐证）")
    elif tc_after > 0:
        run["warnings"].append("提交后任务点数仍为 %s（可能侧栏刷新滞后）" % tc_after)
    else:
        log("提交后任务点数确认：%s -> %s（已清零）" % (tc_before, tc_after))
    prog.set("submit", 0.96, "成绩 %s 分，正在跳转下一节" % sub.get("score"))

    # P8 跳下一节并暂停 + 可选课程进度
    jump = verify.click_next(page, cfg, nav, log, section)
    next_sec = (jump.get("next") or {}).get("section")
    if jump.get("ok") and next_sec:
        log("已跳到下一节 %s 并暂停" % next_sec)
    else:
        run["warnings"].append("跳下一节失败（不阻断本节完成）: %s" % (jump.get("error") or "unknown"))
    pr = {"ok": False, "skipped": True}
    if cfg["verify"]["read_course_progress"]:
        prog.set("submit", 0.98, "已跳转下一节，读取课程进度")
        pr = verify.read_progress(page, cfg, nav, log, next_sec)
        if not pr.get("ok"):
            run["warnings"].append("课程进度读取失败（不阻断）: %s" % (pr.get("error") or "unknown"))
    progress_total = ((pr.get("progress") or {}).get("n"))
    common.write_json_atomic(os.path.join(run_dir, "verify.json"), {
        "schema_version": "engine-verify/1", "section": section,
        "score": run.get("score"), "jump": jump, "course_progress": pr,
        "progress_total": progress_total,
        "side_after": nav.read_task_count(page, section),
        "finished_at": common.now_iso()})
    run["next_section"] = next_sec
    run["progress_total"] = progress_total
    run["phases"]["P8_verify"] = {"ok": bool(jump.get("ok")), "next_section": next_sec,
                                  "progress_total": progress_total,
                                  "progress_ok": bool(pr.get("ok"))}
    prog.set("submit", 1.0, "已完成 %s（成绩 %s 分）" % (section, run.get("score")))
    run["status"] = "completed"
    log("完成 %s：成绩=%s 进度=%s 下一节=%s" % (section, run.get("score"), progress_total, next_sec))
    return EXIT_OK


# ================= 主流程 =================
def main(argv=None):
    ap = argparse.ArgumentParser(description="学习通单节统一运行器（dry/real）")
    ap.add_argument("--section", default=None, help="小节号，如 2.4（--preflight-only 时可省略）")
    ap.add_argument("--mode", choices=("dry", "real"), default="dry")
    ap.add_argument("--preflight-only", action="store_true",
                    help="只读核对：页面进度 + 计划各节任务点，不执行任何课程动作")
    ap.add_argument("--sections", default=None, help="preflight 要核对的节，逗号分隔，如 2.4,3.1")
    ap.add_argument("--config", default=None)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--progress-file", default=None)
    ap.add_argument("--plan-id", default=None)
    ap.add_argument("--skip-quiz", action="store_true",
                    help="跳过答题：只做视频，不做章节测验、不提交（也可由 config.answer.skip_quiz 开启）")
    args = ap.parse_args(argv)

    if not args.preflight_only and not SECTION_RE.match(args.section or ""):
        print("ERROR: --section 需要形如 2.4 的小节号", flush=True)
        return EXIT_ABORT
    if args.preflight_only and args.section and not SECTION_RE.match(args.section):
        print("ERROR: --section 需要形如 2.4 的小节号", flush=True)
        return EXIT_ABORT
    try:
        cfg = common.load_config(args.config)
    except Exception as e:
        print("ERROR: 配置加载失败: %s" % e, flush=True)
        return EXIT_EXCEPTION

    if args.mode == "real" and not bool((cfg.get("real") or {}).get("enabled")):
        print("real 模式未放行：engine/config.json real.enabled=false（dry 可用）", flush=True)
        return EXIT_REAL_DISABLED
    skip_quiz = bool(args.skip_quiz or bool((cfg.get("answer") or {}).get("skip_quiz")))

    lock_dir = common.path(cfg, "lock_dir")
    os.makedirs(lock_dir, exist_ok=True)
    lock = common.FileLock(os.path.join(lock_dir, "section.lock"),
                           timeout_s=float(cfg["locks"]["section_timeout_s"]))
    if not lock.acquire():
        print("ERROR: 已有 run_section 实例在运行（section.lock 被占用），本次退出", flush=True)
        return EXIT_LOCKED

    if args.run_dir:
        run_dir = os.path.abspath(args.run_dir)
    elif args.preflight_only:
        run_dir = os.path.join(common.path(cfg, "runs_dir"), "_preflight",
                               time.strftime("%Y%m%d-%H%M%S"))
    else:
        run_dir = _run_dir_default(cfg, args.section)
    os.makedirs(os.path.join(run_dir, "evidence"), exist_ok=True)
    _lg = cfg.get("logging") or {}
    log = common.Log(os.path.join(run_dir, "run.log"),
                     prefix="[%s/%s]" % (args.section or "-", args.mode),
                     plan_id=args.plan_id, section=args.section,
                     max_bytes=int(_lg.get("max_bytes") or 0),
                     backup_count=int(_lg.get("backup_count") or 3))
    prog = common.Progress(args.progress_file, args.section, args.plan_id)
    stop_since = time.time() - 2.0
    run = {"schema_version": "engine-run/1", "section": args.section, "mode": args.mode,
           "started_at": common.now_iso(), "phases": {}, "warnings": [], "errors": [],
           "run_dir": run_dir, "config_path": cfg.get("_config_path"),
           "_t0": time.time()}
    exit_code = EXIT_EXCEPTION
    page = None

    if args.preflight_only:
        secs = [s.strip() for s in (args.sections or "").split(",") if s.strip()]
        if args.section and args.section not in secs:
            secs.insert(0, args.section)
        run["mode"] = "preflight"
        run["sections"] = secs
        try:
            endpoint, timeout_ms = _connect(cfg, log)
            with sync_playwright() as pw:
                pw_browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
                page = _pick_page(pw_browser, cfg, log)
                data = preflight.collect(page, cfg, nav, log, secs)
                data["run_dir"] = run_dir
                data["restored"] = False
                preflight.write_evidence(common.path(cfg, "runs_dir") + os.sep + "_preflight" + os.sep
                                         + os.path.basename(run_dir) + ".preflight.json", data)
                common.write_json_atomic(os.path.join(run_dir, "preflight.json"), data)
                run["preflight"] = data
                run["status"] = "preflight_ok" if data.get("progress") else "preflight_failed"
                exit_code = EXIT_OK if data.get("progress") else EXIT_ABORT
                print(json.dumps(data, ensure_ascii=False), flush=True)
        except Abort as e:
            run["status"] = "preflight_failed"
            run["error"] = str(e)
            exit_code = EXIT_ABORT
            log("preflight ABORT: %s" % e)
            print(json.dumps({"ok": False, "code": "BROWSER_LOGIN_REQUIRED" if "登录" in str(e)
                              else "PREFLIGHT_FAILED", "message": str(e),
                              "retryable": "登录" in str(e)}, ensure_ascii=False), flush=True)
        except Exception as e:
            run["status"] = "preflight_failed"
            run["error"] = repr(e)[:300]
            exit_code = EXIT_EXCEPTION
            log("preflight EXCEPTION: %s" % repr(e)[:200])
            print(json.dumps({"ok": False, "code": "PREFLIGHT_EXCEPTION",
                              "message": repr(e)[:200], "retryable": True}, ensure_ascii=False), flush=True)
        finally:
            run["finished_at"] = common.now_iso()
            run["elapsed_s"] = round(time.time() - run.pop("_t0", time.time()), 1)
            try:
                common.write_json_atomic(os.path.join(run_dir, "run.json"), run)
            except Exception:
                pass
            write_index(cfg, run, run_dir, args.plan_id)
            log.close()
            lock.release()
        return exit_code

    try:
        if args.mode == "real" and not bool((cfg.get("real") or {}).get("enabled")):
            log("real 模式未放行：engine/config.json real.enabled=false（dry 可用）")
            run["status"] = "blocked"
            run["error"] = "real.enabled=false（等待放行）"
            exit_code = EXIT_REAL_DISABLED
            return exit_code

        endpoint, timeout_ms = _connect(cfg, log)
        with sync_playwright() as pw:
            pw_browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
            page = _pick_page(pw_browser, cfg, log)
            run["url_before"] = (page.url or "")[:200]
            if args.mode == "dry":
                log("=== dry 只读检查开始（不播放/不答题/不提交） ===")
                exit_code = dry_check(page, cfg, args.section, run_dir, log, prog, run, pw_browser)
            else:
                log("=== real 完整流程开始 ===")
                exit_code = real_flow(page, cfg, args.section, run_dir, log, prog, run,
                                      pw_browser, stop_since, skip_quiz=skip_quiz)
    except StopRequested as e:
        run["status"] = "stopped"
        run["stopped"] = True
        run["stop_reason"] = str(e)
        exit_code = EXIT_OK
        log("=== STOPPED: %s（未提交/未完成，证据保留） ===" % e)
    except Abort as e:
        run["status"] = "failed"
        run["errors"].append(str(e))
        run["error"] = str(e)
        exit_code = EXIT_ABORT
        log("=== ABORT: %s ===" % e)
    except Exception as e:
        run["status"] = "failed"
        run["errors"].append(repr(e)[:400])
        run["error"] = repr(e)[:400]
        exit_code = EXIT_EXCEPTION
        log("=== EXCEPTION: %s ===" % repr(e)[:300])
        log(traceback.format_exc()[-1500:])
    finally:
        if page is not None and run.get("mode") == "real":
            try:
                video.pause_all(page, log)
            except Exception:
                pass
        run["finished_at"] = common.now_iso()
        run["elapsed_s"] = round(time.time() - run.pop("_t0", time.time()), 1)
        run["ok"] = run.get("status") in ("completed", "skipped", "already_done", "skipped_quiz",
                                           "dry_ok", "stopped")
        try:
            common.write_json_atomic(os.path.join(run_dir, "run.json"), run)
        except Exception as e:
            log("run.json 写入失败: %s" % e)
        write_index(cfg, run, run_dir, args.plan_id)
        log("退出码=%s status=%s 耗时=%.1fs run_dir=%s" % (
            exit_code, run.get("status"), run.get("elapsed_s"), run_dir))
        log.close()
        lock.release()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
