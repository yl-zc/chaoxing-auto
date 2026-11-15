# queue_daemon.py —— 队列守护（单一职责：队列/计划 -> 串行调 run_section -> 写 state.json）
"""与控制台只通过文件契约交互（不 import 控制台代码）：
  读：console/plans/queue.jsonl、console/plans/<plan_id>.json、console/state.cmd.json
  写：console/state.json（唯一写者，原子写）、engine/queue_state.jsonl（引擎账本）、engine/daemon.log
  调：run_section.py 子进程（real 模式），逐节串行

状态语义（state=error 时同时写 error / last_error 对象，字段 {code,message,retryable,...}）：
  BROWSER_OFFLINE       CDP 不可达 -> 不执行，保留队列，浏览器就绪后自动继续（retryable=true）
  BROWSER_LOGIN_REQUIRED 检测到登录页 -> 不执行，等待扫码（retryable=true）
  SECTION_FAILED        某节执行失败 -> 失败即停（计划记 failed，不自动重试）
  QUIZ_ONLY_UNSUPPORTED 只做测验的计划暂不支持

用法：
  python queue_daemon.py                 # 常驻
  python queue_daemon.py --once          # 只处理一个计划（或一次错误态）后退出
  python queue_daemon.py --self-test     # dry 自测第一个待执行计划的第一节
  python queue_daemon.py --plan <plan_id>
"""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import browser as browser_layer

RUN_SECTION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_section.py")
EXIT_OK, EXIT_ABORT, EXIT_EXCEPTION, EXIT_LOCKED = 0, 2, 3, 5


class StateWriter(object):
    """console/state.json 的唯一写者（schema_version=engine-state/1，字段只增不改）。"""

    def __init__(self, path):
        self.path = path
        old = common.read_json(path, {}) or {}
        self.last_result = old.get("last_result")
        self.progress = old.get("progress")
        self.queue = []
        self.daemon_pid = os.getpid()
        self.last_state = "idle"

    def set_queue(self, queue):
        self.queue = list(queue or [])

    def set_progress(self, prog):
        """页面真实进度（source=page），写入 state.json 顶层 progress。"""
        self.progress = prog

    def write(self, state, current=None, error=None, last_error=None, detail=None):
        rec = {"schema_version": "engine-state/1",
               "state": state,
               "current": current,
               "last_result": self.last_result,
               "queue": self.queue,
               "progress": self.progress,
               "progress_source": (self.progress or {}).get("source"),
               "paused": state == "paused",
               "daemon_pid": self.daemon_pid,
               "updated_at": common.now_iso()}
        if detail:
            rec["detail"] = str(detail)[:300]
        if error:
            rec["error"] = error
        if last_error:
            rec["last_error"] = last_error
        common.write_json_atomic(self.path, rec)
        self.last_state = state
        return rec

    def ensure_file(self):
        cur = common.read_json(self.path, None)
        if not isinstance(cur, dict) or "schema_version" not in cur or "state" not in cur:
            self.write(self.last_state, current=None,
                       detail="engine 心跳恢复 state.json（检测到缺失/非契约内容）")


def _error(code, message, retryable, detail=None, **extra):
    e = {"code": code, "message": message, "retryable": bool(retryable),
         "at": common.now_iso()}
    if detail:
        e["detail"] = str(detail)[:300]
    e.update(extra)
    return e


def append_ledger(path, event, **kw):
    rec = {"schema_version": "engine-queue/1", "at": common.now_iso(), "event": event}
    rec.update(kw)
    common.append_jsonl(path, rec)


def load_ledger(path):
    recs, _bad = common.read_jsonl(path)
    done, plan_done, plan_failed = set(), set(), set()
    for r in recs:
        pid, sec, ev = r.get("plan_id"), r.get("section"), r.get("event")
        if ev in ("section_completed", "section_skipped"):
            done.add((pid, sec))
        elif ev == "plan_completed":
            plan_done.add(pid)
        elif ev == "plan_failed":
            plan_failed.add(pid)
    return done, plan_done, plan_failed


def scan_queue(cfg):
    """同一 id 以最后一条记录状态为准（控制台/引擎可追加 cancelled 覆盖 queued）。"""
    entries, _bad = common.read_jsonl(common.path(cfg, "queue_file"))
    latest, order = {}, []
    for e in entries:
        pid = e.get("id")
        if not pid:
            continue
        if pid not in latest:
            order.append(pid)
        latest[pid] = e
    out = []
    for pid in order:
        e = latest[pid]
        status = str(e.get("status") or "queued").lower()
        if status in ("done", "completed", "cancelled", "failed"):
            continue
        out.append(e)
    return out


def cancel_pending(cfg, pending, ledger_path, reason):
    """停止请求：队列标记 cancelled + 账本记 plan_cancelled（不改控制台代码，仅追加 JSONL）。"""
    if not pending:
        return
    qf = common.path(cfg, "queue_file")
    for p in pending:
        try:
            common.append_jsonl(qf, {"id": p["plan_id"], "status": "cancelled",
                                     "at": common.now_iso(), "reason": reason,
                                     "source": "engine"})
        except Exception:
            pass
        append_ledger(ledger_path, "plan_cancelled", plan_id=p["plan_id"], reason=reason)


def read_plan(cfg, plan_id):
    return common.read_json(os.path.join(common.path(cfg, "plans_dir"), plan_id + ".json"))


def normalize_sections(plan_doc):
    """兼容 console 的 legacy plan JSON（无 schema_version）与未来带 schema_version 的版本。"""
    plan = (plan_doc or {}).get("plan") if isinstance(plan_doc, dict) else None
    if not isinstance(plan, dict):
        return None, []
    sections = []
    for it in (plan.get("sections") or []):
        if isinstance(it, dict):
            sec = str(it.get("section") or "").strip()
            title = str(it.get("title") or "").strip()
        else:
            sec, title = str(it or "").strip(), ""
        if sec:
            sections.append({"section": sec, "title": title})
    return plan, sections


def build_pending(cfg, entries, done, plan_done, plan_failed, log, missing_notified):
    pending = []
    for e in entries:
        pid = e.get("id")
        if pid in plan_done or pid in plan_failed:
            continue
        doc = read_plan(cfg, pid)
        if not doc:
            if pid not in missing_notified:
                log("跳过计划 %s（计划文件不存在）" % pid)
                missing_notified.add(pid)
            continue
        plan, sections = normalize_sections(doc)
        if plan is None:
            if pid not in missing_notified:
                log("跳过计划 %s（plan 字段损坏）" % pid)
                missing_notified.add(pid)
            continue
        remaining = [s for s in sections if (pid, s["section"]) not in done]
        if remaining:
            pending.append({"plan_id": pid, "label": plan.get("label") or "",
                            "mode": plan.get("mode") or "full",
                            "sections": remaining, "count": len(remaining)})
    return pending


def queue_snapshot(pending):
    q = []
    for p in pending:
        for s in p["sections"]:
            q.append({"section": s["section"], "title": s.get("title") or "",
                      "plan_id": p["plan_id"], "status": "queued"})
    return q


def read_cmd(cfg, last_ts):
    obj = common.read_json(common.path(cfg, "cmd_file"), None)
    if not isinstance(obj, dict):
        return None, last_ts
    try:
        ts = float(obj.get("ts") or 0)
    except Exception:
        return None, last_ts
    action = str(obj.get("action") or "").lower()
    if action not in ("pause", "resume") or ts <= last_ts:
        return None, last_ts
    return action, ts


def preflight(cfg):
    """real 执行前探测浏览器：CDP / 登录页 / 学习通页面。
    返回 (ok, error_dict)；不通过时不消费计划。
    """
    st = browser_layer.status(cfg)
    if not st.get("cdp_up"):
        return False, _error("BROWSER_OFFLINE", "学习通浏览器未连接，请先打开并登录",
                             True, st.get("error"))
    if st.get("login_page") or not st.get("logged_in"):
        return False, _error("BROWSER_LOGIN_REQUIRED", "学习通浏览器未登录，请在浏览器里扫码登录",
                             True, st.get("page_url") or st.get("error"))
    return True, None


def _stop_and_exit(cfg, state, ledger_path, pending, reason, detail):
    """安全停止：队列标记 cancelled、写 state=stopped、返回 EXIT_OK（由 daemon 退出）。"""
    cancel_pending(cfg, pending, ledger_path, reason)
    state.write("stopped", current=None, detail=detail)
    return EXIT_OK


def _run_dir(cfg, section):
    return os.path.join(common.path(cfg, "runs_dir"), section,
                        time.strftime("%Y%m%d-%H%M%S"))


def execute_section(cfg, state, section, title, plan_id, run_dir, mode, log, skip_quiz=False):
    """子进程跑一节，期间把 progress 文件翻译成 state.current。"""
    os.makedirs(run_dir, exist_ok=True)
    progress_path = os.path.join(run_dir, "progress.json")
    engine_py = common.resolve_path((cfg.get("python") or {}).get("engine") or sys.executable)
    if not os.path.isfile(engine_py):
        engine_py = sys.executable
    cmd = [engine_py, RUN_SECTION, "--section", section, "--mode", mode,
           "--config", cfg["_config_path"], "--run-dir", run_dir,
           "--progress-file", progress_path, "--plan-id", str(plan_id or "")]
    if skip_quiz:
        cmd.append("--skip-quiz")
    log("执行节 %s（%s）: %s" % (section, mode, " ".join(cmd)))
    started_at = common.now_iso()
    state.write("running", current={"section": section, "title": title, "stage": "video",
                                    "progress": 0.0, "detail": "准备开始 %s" % section,
                                    "plan_id": plan_id, "started_at": started_at})
    t0 = time.time()
    child = open(os.path.join(run_dir, "child-console.log"), "ab")
    try:
        proc = subprocess.Popen(cmd, cwd=common.PROJECT_ROOT, stdout=child,
                                stderr=subprocess.STDOUT)
    finally:
        child.close()
    last_prog = None
    max_wait = float(cfg["daemon"]["run_section_wait_s"])
    poll_s = max(0.2, float(cfg["daemon"]["progress_poll_s"]))
    while True:
        rc = proc.poll()
        pr = common.read_json(progress_path, None)
        if isinstance(pr, dict) and pr != last_prog:
            last_prog = pr
            try:
                state.write("running", current={
                    "section": pr.get("section") or section, "title": title,
                    "stage": pr.get("stage") or "video",
                    "progress": float(pr.get("progress") or 0),
                    "detail": pr.get("detail") or "",
                    "plan_id": plan_id, "started_at": started_at})
            except Exception:
                pass
        if rc is not None:
            break
        if time.time() - t0 > max_wait:
            log("单节执行超时 %.0fs，强制结束进程" % max_wait)
            try:
                proc.kill()
            except Exception:
                pass
            rc = proc.wait()
            break
        time.sleep(poll_s)
    elapsed = round(time.time() - t0, 1)
    run = common.read_json(os.path.join(run_dir, "run.json"), {}) or {}
    log("节 %s 结束 rc=%s status=%s 耗时=%.1fs" % (section, rc, run.get("status"), elapsed))
    return rc, run, elapsed


def run_plan_preflight(cfg, plan, log):
    """计划开始前：子进程跑 run_section --preflight-only，读页面真实进度 + 各节任务点（只读）。"""
    sections = [s["section"] for s in plan["sections"]]
    run_dir = os.path.join(common.path(cfg, "runs_dir"), "_preflight",
                           "%s-%s" % (plan["plan_id"], time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(run_dir, exist_ok=True)
    engine_py = common.resolve_path((cfg.get("python") or {}).get("engine") or sys.executable)
    if not os.path.isfile(engine_py):
        engine_py = sys.executable
    cmd = [engine_py, RUN_SECTION, "--preflight-only",
           "--sections", ",".join(sections), "--config", cfg["_config_path"],
           "--run-dir", run_dir, "--plan-id", plan["plan_id"]]
    log("plan preflight（只读）: " + " ".join(cmd))
    try:
        p = subprocess.run(cmd, cwd=common.PROJECT_ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        log("plan preflight 超时（180s）")
        return None
    for line in (p.stdout or "").strip().splitlines()[-2:]:
        log("  preflight: " + line[:300])
    data = common.read_json(os.path.join(run_dir, "preflight.json"), None)
    if not isinstance(data, dict):
        cand = [l for l in (p.stdout or "").strip().splitlines() if l.strip().startswith("{")]
        if cand:
            try:
                data = json.loads(cand[-1])
            except Exception:
                data = None
    if not isinstance(data, dict):
        log("plan preflight 未取得 JSON（rc=%s）" % p.returncode)
        return None
    data["run_dir"] = run_dir
    prog = data.get("progress") or {}
    log("plan preflight: 页面进度=%s/%s 计划各节任务点=%s" % (
        prog.get("n"), prog.get("total"),
        {r.get("section"): r.get("task_count") for r in (data.get("sections") or [])}))
    return data


def _error_current(section, title, plan_id, detail):
    return {"section": section, "title": title, "stage": "submit",
            "progress": 0.0, "detail": detail, "plan_id": plan_id,
            "started_at": common.now_iso()}


def run_daemon(cfg, args, log):
    state_path = common.path(cfg, "state_file")
    ledger_path = common.path(cfg, "ledger_file")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    state = StateWriter(state_path)
    state.write("idle", current=None, detail="engine 守护启动")
    daemon_start = time.time()
    last_cmd_ts = daemon_start - 1.0
    paused = False
    missing_notified = set()
    last_idle_log = 0.0
    latched = None            # 执行失败错误：保留，直到出现新的待执行计划（用户重试）
    legacy_closed = False    # 旧自动化浏览器只清理一次（Edge 已就绪且已登录后）
    final_state = "idle"
    final_detail = None
    final_error = None

    while True:
        cfg = common.load_config(args.config)  # 热加载 real.enabled / 轮询间隔
        poll_s = max(0.5, float(cfg["daemon"]["poll_s"]))

        action, last_cmd_ts = read_cmd(cfg, last_cmd_ts)
        if action == "pause" and not paused:
            paused = True
            log("收到 pause 命令：本节结束后暂停")
        elif action == "resume" and paused:
            paused = False
            log("收到 resume 命令：恢复队列")

        done, plan_done, plan_failed = load_ledger(ledger_path)
        pending = build_pending(cfg, scan_queue(cfg), done, plan_done, plan_failed,
                                log, missing_notified)
        state.set_queue(queue_snapshot(pending))
        state.ensure_file()

        # 停止请求（launcher/控制台写 stop.json / stop-request.json / state.cmd.json action=stop）
        stop_req = common.stop_requested(cfg, daemon_start - 1.0)
        if stop_req:
            log("收到停止请求（%s/%s），按安全停止退出" % (stop_req.get("file"), stop_req.get("reason")))
            cancel_pending(cfg, pending, ledger_path, "stop_requested")
            state.write("stopped", current=None,
                        detail="已收到停止请求（%s），引擎安全停止" % stop_req.get("reason"))
            return EXIT_OK

        if paused:
            state.write("paused", current=None, detail="已暂停（等待 state.cmd.json resume）")
            if args.once or args.self_test:
                final_state = "paused"
                break
            time.sleep(poll_s)
            continue

        if latched:
            if pending:
                log("检测到新的待执行计划（%s），清除上次失败状态并继续" % pending[0]["plan_id"])
                latched = None
            else:
                state.write("error", current=None, error=latched, last_error=latched,
                            detail="失败即停：%s" % latched.get("message"))
                if args.once or args.self_test:
                    final_state, final_error = "error", latched
                    break
                time.sleep(poll_s)
                continue

        real_on = bool((cfg.get("real") or {}).get("enabled"))
        if real_on and not args.self_test:
            ready, perr = preflight(cfg)
            if ready and not legacy_closed:
                legacy_closed = True
                try:
                    res = browser_layer.close_legacy(cfg, log, require_login=True)
                    if res.get("ok"):
                        log("旧自动化浏览器清理：关闭 %d 个（避免双端互踢）" % int(res.get("count") or 0))
                    else:
                        log("旧自动化浏览器未清理：%s" % res.get("refused"))
                except Exception as e:
                    log("旧自动化浏览器清理异常（忽略）：%s" % str(e)[:150])
            if not ready:
                if pending:
                    state.write("error", current=None, error=perr, last_error=perr,
                                detail="%s（队列 %d 节，浏览器就绪后自动继续）" % (
                                    perr["message"], len(state.queue)))
                    if time.time() - last_idle_log > 30:
                        log("浏览器未就绪：%s；队列待执行 %d 节" % (perr["message"], len(state.queue)))
                        last_idle_log = time.time()
                else:
                    state.write("idle", current=None,
                                detail="%s；队列为空" % perr["message"])
                if args.once:
                    final_state = "error" if pending else "idle"
                    final_error = perr if pending else None
                    break
                time.sleep(poll_s)
                continue

        if not real_on and not args.self_test:
            state.write("idle", current=None,
                        detail="real 模式未放行（engine/config.json real.enabled=false）；待执行 %d 节" % len(state.queue))
            if time.time() - last_idle_log > 60:
                log("real 未放行，等待放行；queue 待执行 %d 节" % len(state.queue))
                last_idle_log = time.time()
            if args.once:
                break
            time.sleep(poll_s)
            continue

        if not pending:
            lr = state.last_result or {}
            if lr.get("all_done"):
                pr = state.progress or {}
                detail = "没有需要做的小节（页面进度 %s/%s 已全部完成）" % (pr.get("n"), pr.get("total"))
            else:
                detail = "队列为空"
            state.write("idle", current=None, detail=detail)
            if args.once or args.self_test:
                break
            time.sleep(poll_s)
            continue

        if args.plan:
            pending = [p for p in pending if p["plan_id"] == args.plan]
            if not pending:
                log("没有指定计划 %s 的待执行节" % args.plan)
                state.write("idle", current=None, detail="指定计划无待执行节")
                break

        plan = pending[0]
        if plan.get("mode") == "quiz":
            err = _error("QUIZ_ONLY_UNSUPPORTED",
                         "计划是只做测验（quiz-only），M2 引擎只支持完整流程；请在控制台改选完整流程",
                         False, plan_id=plan["plan_id"])
            append_ledger(ledger_path, "plan_failed", plan_id=plan["plan_id"],
                          reason="quiz_only_unsupported")
            state.write("error", current=None, error=err, last_error=err, detail=err["message"])
            log("plan %s 为 quiz-only，M2 不支持，已写 error" % plan["plan_id"])
            latched = err
            if args.once:
                final_state, final_error = "error", err
                break
            time.sleep(poll_s)
            continue

        if args.self_test:
            sec = plan["sections"][0]
            run_dir = _run_dir(cfg, sec["section"])
            log("=== self-test（dry）计划 %s 第一节 %s ===" % (plan["plan_id"], sec["section"]))
            rc, run, elapsed = execute_section(
                cfg, state, sec["section"], sec.get("title") or "", plan["plan_id"],
                run_dir, "dry", log)
            ok = (rc == 0 and bool(run.get("ok")))
            append_ledger(ledger_path, "dry_test", plan_id=plan["plan_id"],
                          section=sec["section"], rc=rc, ok=ok, run_dir=run_dir, elapsed_s=elapsed)
            final_detail = (("dry 自测通过：%s" % sec["section"]) if ok else
                            ("dry 自测失败：%s（详见 %s）" % (sec["section"], run_dir)))
            state.write("idle", current=None, detail=final_detail)
            log("self-test 结果: ok=%s rc=%s run_dir=%s" % (ok, rc, run_dir))
            return EXIT_OK if ok else EXIT_ABORT

        log.set_context(plan_id=plan["plan_id"], section=None)
        # ---- plan preflight：仅用于进度显示/诊断；不得据此跳过小节 ----
        pre = run_plan_preflight(cfg, plan, log)
        prog_page = (pre or {}).get("progress")
        if prog_page:
            pn = {"n": prog_page.get("n"), "total": prog_page.get("total"),
                  "source": "page", "at": common.now_iso()}
            prev_n = (state.progress or {}).get("n")
            state.set_progress(pn)
            if prev_n is not None and pn["n"] is not None and int(prev_n) != int(pn["n"]):
                log("进度以页面为准：本地 %s → 页面 %s/%s" % (prev_n, pn["n"], pn["total"]))
        if pre:
            append_ledger(ledger_path, "plan_preflight", plan_id=plan["plan_id"],
                          progress=prog_page, sections=pre.get("sections"),
                          run_dir=pre.get("run_dir"))
            log("plan preflight（仅显示用，不用于跳过）：页面进度=%s/%s；目录读数仅供参考" % (
                (prog_page or {}).get("n"), (prog_page or {}).get("total")))
        else:
            log("plan preflight 未取得页面数据（仅影响进度显示，不用于跳过）")

        # ---- real：逐节串行（skip_quiz 由 config.answer.skip_quiz 控制） ----
        skip_quiz = bool((cfg.get("answer") or {}).get("skip_quiz"))
        if skip_quiz:
            log("skip_quiz=true：本次只做视频，跳过章节测验/提交")
        log("=== 开始计划 %s（%s，共 %d 节待做） ===" % (
            plan["plan_id"], plan.get("label") or "", plan["count"]))
        append_ledger(ledger_path, "plan_started", plan_id=plan["plan_id"],
                      label=plan.get("label") or "", count=plan["count"])
        failed = None
        unknown_streak = 0
        had_unknown = False
        for sec in plan["sections"]:
            action, last_cmd_ts = read_cmd(cfg, last_cmd_ts)
            if action == "pause":
                paused = True
            elif action == "resume":
                paused = False
            if paused:
                log("已暂停（节间生效），计划 %s 保留待续" % plan["plan_id"])
                break
            stop_req = common.stop_requested(cfg, daemon_start - 1.0)
            if stop_req:
                log("收到停止请求（%s/%s），停止计划 %s，不再取下一节" % (
                    stop_req.get("file"), stop_req.get("reason"), plan["plan_id"]))
                append_ledger(ledger_path, "plan_cancelled", plan_id=plan["plan_id"],
                              reason="stop_requested", section=sec.get("section"))
                cancel_pending(cfg, [p for p in pending if p["plan_id"] == plan["plan_id"]],
                               ledger_path, "stop_requested")
                state.write("stopped", current=None,
                            detail="已收到停止请求（%s），引擎安全停止" % stop_req.get("reason"))
                return EXIT_OK
            sec_no = sec["section"]
            title = sec.get("title") or ""
            log.set_context(plan_id=plan["plan_id"], section=sec_no)
            run_dir = _run_dir(cfg, sec_no)
            append_ledger(ledger_path, "section_started", plan_id=plan["plan_id"],
                          section=sec_no, title=title, run_dir=run_dir)
            rc, run, elapsed = execute_section(
                cfg, state, sec_no, title, plan["plan_id"], run_dir, "real", log,
                skip_quiz=skip_quiz)
            if run.get("status") == "stopped":
                append_ledger(ledger_path, "section_stopped", plan_id=plan["plan_id"],
                              section=sec_no, reason=run.get("stop_reason"),
                              run_dir=run_dir)
                append_ledger(ledger_path, "plan_cancelled", plan_id=plan["plan_id"],
                              reason="stop_requested", section=sec_no)
                cancel_pending(cfg, [p for p in pending if p["plan_id"] == plan["plan_id"]],
                               ledger_path, "stop_requested")
                state.write("stopped",
                            current=_error_current(sec_no, title, plan["plan_id"],
                                                   "已按停止请求安全收尾（本节未完成/未提交）"),
                            detail="浏览器关闭/手动停止：本节已安全停止，证据保留")
                log("=== STOPPED：节 %s 已安全停止（未提交/未完成） ===" % sec_no)
                return EXIT_OK
            if run.get("status") == "unknown":
                unknown_streak += 1
                had_unknown = True
                append_ledger(ledger_path, "section_unknown", plan_id=plan["plan_id"],
                              section=sec_no, error=str(run.get("error") or "")[:300],
                              run_dir=run_dir)
                state.write("idle",
                            current=_error_current(sec_no, title, plan["plan_id"],
                                                   "UNKNOWN：节内任务点数读不到（未跳过/未执行）"),
                            detail="%s 节内任务点数读不到；连续 2 节将停止（安全阀）" % sec_no)
                log("节 %s 任务点数 unknown（连续 %d 次）：%s" % (
                    sec_no, unknown_streak, str(run.get("error") or "")[:200]))
                if unknown_streak >= 2:
                    err = _error("TASK_COUNT_UNKNOWN",
                                 "连续 2 节读不到节内任务点数，已停止（安全阀）",
                                 False, section=sec_no, run_dir=run_dir,
                                 plan_id=plan["plan_id"])
                    append_ledger(ledger_path, "plan_failed", plan_id=plan["plan_id"],
                                  reason="task_count_unknown_safety_valve", section=sec_no)
                    state.write("error",
                                current=_error_current(sec_no, title, plan["plan_id"],
                                                       err["message"]),
                                error=err, last_error=err, detail=err["message"])
                    log("=== 安全阀触发：连续 2 节 unknown，停止 ===")
                    return EXIT_ABORT
                continue
            unknown_streak = 0
            if rc == 0 and run.get("status") in ("completed", "skipped", "already_done",
                                                 "skipped_quiz"):
                skipped_quiz = run.get("status") == "skipped_quiz"
                was_skip = run.get("status") in ("skipped", "already_done", "skipped_quiz")
                if skipped_quiz:
                    ev = "section_skipped_quiz"
                elif was_skip:
                    ev = "section_skipped"
                else:
                    ev = "section_completed"
                append_ledger(ledger_path, ev, plan_id=plan["plan_id"], section=sec_no,
                              title=title, score=run.get("score"),
                              progress_total=run.get("progress_total"), skipped=was_skip,
                              run_dir=run_dir, elapsed_s=elapsed)
                try:
                    ver = common.read_json(os.path.join(run_dir, "verify.json"), {}) or {}
                    cp = (ver.get("course_progress") or {}).get("progress")
                    if cp and cp.get("n") is not None:
                        state.set_progress({"n": cp.get("n"), "total": cp.get("total"),
                                            "source": "page", "at": common.now_iso()})
                except Exception:
                    pass
                finished = run.get("finished_at") or common.now_iso()
                state.last_result = {"section": sec_no,
                                     "title": run.get("title") or title,
                                     "status": "completed",
                                     "score": run.get("score"),
                                     "progress_total": run.get("progress_total"),
                                     "skipped": bool(was_skip),
                                     "skipped_quiz": bool(skipped_quiz),
                                     "next_section": run.get("next_section") or "",
                                     "finished_at": finished, "at": finished,
                                     "run_dir": run_dir}
                if skipped_quiz:
                    state.write("idle", current=None,
                                detail="%s 视频完成，按 skip_quiz 跳过章节测验（未提交）" % sec_no)
                    log("节 %s 跳过章节测验（skip_quiz=true，未提交），继续下一节" % sec_no)
                elif was_skip:
                    state.write("idle", current=None,
                                detail="%s 你之前已经完成了，已跳过" % sec_no)
                    log("节 %s 判定已完成（已跳过）：%s" % (sec_no, run.get("skip_reason") or ""))
                else:
                    state.write("idle", current=None,
                                detail="%s %s 完成（成绩 %s）" % (sec_no, title, run.get("score")))
                    log("节 %s 完成：score=%s progress=%s" % (
                        sec_no, run.get("score"), run.get("progress_total")))
                continue
            # 失败即停：写 error 并保留（不静默回 idle）
            msg = str(run.get("error") or run.get("abort") or ("rc=%s" % rc))[:180]
            err = _error("SECTION_FAILED", "小节 %s 执行失败：%s" % (sec_no, msg),
                         False, str(run.get("error") or "")[:300],
                         section=sec_no, rc=rc, run_dir=run_dir, plan_id=plan["plan_id"])
            append_ledger(ledger_path, "section_failed", plan_id=plan["plan_id"],
                          section=sec_no, rc=rc, error=err["detail"], run_dir=run_dir)
            append_ledger(ledger_path, "plan_failed", plan_id=plan["plan_id"],
                          section=sec_no, reason="section_failed")
            state.write("error",
                        current=_error_current(sec_no, title, plan["plan_id"], err["message"]),
                        error=err, last_error=err, detail=err["message"])
            log("=== 失败即停：节 %s rc=%s error=%s ===" % (sec_no, rc, msg[:200]))
            failed = err
            latched = err
            break

        if failed:
            if args.once:
                final_state, final_error = "error", failed
                break
            time.sleep(poll_s)
            continue

        if paused:
            state.write("paused", current=None,
                        detail="已暂停（计划 %s 待续）" % plan["plan_id"])
            if args.once:
                final_state = "paused"
                break
            time.sleep(poll_s)
            continue

        if had_unknown:
            err = _error("TASK_COUNT_UNKNOWN",
                         "计划含读不到任务点数的节，计划已暂停（未达连续2节安全阀，需人工确认后重试）",
                         False, plan_id=plan["plan_id"])
            append_ledger(ledger_path, "plan_failed", plan_id=plan["plan_id"],
                          reason="task_count_unknown")
            state.write("error", current=None, error=err, last_error=err,
                        detail=err["message"])
            log("计划 %s 含 unknown 节，暂停并置 error" % plan["plan_id"])
            return EXIT_ABORT
        append_ledger(ledger_path, "plan_completed", plan_id=plan["plan_id"],
                      sections=[s["section"] for s in plan["sections"]])
        log.set_context(plan_id=None, section=None)
        state.write("idle", current=None, detail="计划 %s 完成" % plan["plan_id"])
        log("=== 计划 %s 完成 ===" % plan["plan_id"])
        if args.once:
            break

    if final_state == "error" and final_error is not None:
        state.write("error", current=None, error=final_error, last_error=final_error,
                    detail="失败即停：%s" % final_error.get("message"))
    else:
        state.write("paused" if paused else "idle", current=None, detail=final_detail)
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(description="学习通队列守护（M2）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="只处理一个计划后退出")
    ap.add_argument("--self-test", action="store_true", help="dry 自测第一个待执行计划的第一节")
    ap.add_argument("--plan", default=None, help="只处理指定 plan_id")
    args = ap.parse_args(argv)

    try:
        cfg = common.load_config(args.config)
    except Exception as e:
        print("ERROR: 配置加载失败: %s" % e, flush=True)
        return EXIT_EXCEPTION

    os.makedirs(common.path(cfg, "lock_dir"), exist_ok=True)
    lock = common.FileLock(os.path.join(common.path(cfg, "lock_dir"), "daemon.lock"),
                           timeout_s=float(cfg["locks"]["daemon_timeout_s"]))
    if not lock.acquire():
        print("ERROR: 已有 queue_daemon 实例在运行（daemon.lock 被占用），本次退出", flush=True)
        return EXIT_LOCKED

    _lg = cfg.get("logging") or {}
    log = common.Log(common.path(cfg, "daemon_log"), prefix="[daemon]",
                     max_bytes=int(_lg.get("max_bytes") or 0),
                     backup_count=int(_lg.get("backup_count") or 3))
    try:
        log("启动 daemon pid=%s config=%s self_test=%s once=%s plan=%s" % (
            os.getpid(), cfg.get("_config_path"), args.self_test, args.once, args.plan))
        return run_daemon(cfg, args, log)
    except Exception as e:
        log("=== daemon EXCEPTION: %s ===" % repr(e)[:400])
        log(traceback.format_exc()[-1500:])
        try:
            st = StateWriter(common.path(cfg, "state_file"))
            err = _error("DAEMON_EXCEPTION", "队列守护异常：%s" % repr(e)[:200], False)
            st.write("error", current=None, error=err, last_error=err, detail=err["message"])
        except Exception:
            pass
        return EXIT_EXCEPTION
    finally:
        log.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
