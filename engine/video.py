# video.py —— 视频模块（单一职责：真实 1x 播放到「任务点计入」，或只读探测）
"""完成判据（与 pipeline/纪律对齐）：
  - marker：cards 帧出现 .ans-job-finished 且 currentTime/duration >= marker_ratio
  - credit：播放到 >= credit_ratio 先暂停，回调 read_side() 确认任务点已计入
  - final：>= final_ratio 兜底
异常即停（超时 / 60s 无增长 / 视频帧缺失），不重试。
对外接口：
  probe(page) -> 只读视频状态（不播放、不 load）
  ensure_done(page, cfg, section, log, read_side, on_sample) -> dict
  pause_all(page) -> 安全暂停已在播放的视频（只用于收尾/失败清理）
"""
import time

import common


class VideoError(RuntimeError):
    pass


VID_STATE = r"""() => {
  const vs = Array.from(document.querySelectorAll('video'));
  if (!vs.length) return {count: 0};
  const v = vs[0];
  return {count: vs.length, duration: v.duration, currentTime: v.currentTime,
          paused: v.paused, ended: v.ended, muted: v.muted, playbackRate: v.playbackRate,
          readyState: v.readyState, error: v.error ? v.error.code : null};
}"""

PAUSE_JS = r"""() => { const v=document.querySelector('video'); if(v) v.pause(); return true; }"""

PLAY_JS = r"""(rate) => {
  const v = document.querySelector('video');
  if (!v) return false;
  v.muted = true;
  try { v.playbackRate = rate; } catch (e) {}
  try { v.play(); } catch (e) {}
  return true;
}"""

LOAD_JS = r"""(rate) => {
  const v = document.querySelector('video');
  if (!v) return false;
  v.muted = true;
  try { v.playbackRate = rate; } catch (e) {}
  try { v.load(); } catch (e) {}
  return true;
}"""

BIG_PLAY_JS = r"""() => { const b=document.querySelector('.vjs-big-play-button'); if(b){ b.click(); return true; } return false; }"""

MARKER_JS = r"""() => !!document.querySelector('.ans-job-finished')"""


def find_video_frame(page):
    return next((f for f in page.frames if "modules/video" in (f.url or "")), None)


def find_cards_frame(page):
    return next((f for f in page.frames if "knowledge/cards" in (f.url or "")), None)


def read_state(vf):
    try:
        return vf.evaluate(VID_STATE) or {"count": 0}
    except Exception as e:
        return {"count": 0, "error": str(e)[:100]}


def pause(vf):
    try:
        vf.evaluate(PAUSE_JS)
        return True
    except Exception:
        return False


def pause_all(page, log=None):
    n = 0
    for fr in page.frames:
        if "modules/video" not in (fr.url or ""):
            continue
        try:
            st = fr.evaluate(VID_STATE)
            if st and st.get("paused") is False:
                fr.evaluate(PAUSE_JS)
                n += 1
        except Exception:
            continue
    if log and n:
        log("已暂停 %d 个视频（收尾保护）" % n)
    return n


def probe(page):
    """只读：视频帧是否存在、时长/进度/暂停态。绝不 play/load。"""
    vf = find_video_frame(page)
    if vf is None:
        return {"present": False, "frame_url": None, "state": None}
    st = read_state(vf)
    dur = st.get("duration")
    out = {"present": True, "frame_url": (vf.url or "")[:160],
           "state": {k: st.get(k) for k in ("count", "duration", "currentTime", "paused",
                                            "muted", "playbackRate", "readyState", "error")}}
    try:
        out["duration_s"] = float(dur) if dur and dur == dur else None
    except Exception:
        out["duration_s"] = None
    return out


def ensure_done(page, cfg, section, log, read_side, on_sample=None, should_stop=None):
    """真实播放（1x、静音）直到任务点计入。read_side() -> nav.read_task_count 的原始值。"""
    vc = cfg["video"]
    rate = float(vc["playback_rate"])
    vf = find_video_frame(page)
    if vf is None:
        raise VideoError("未找到视频 frame（modules/video）")
    cards = find_cards_frame(page)

    vf.evaluate(LOAD_JS, rate)
    dur = None
    for i in range(45):
        st = read_state(vf)
        d = st.get("duration")
        if isinstance(d, (int, float)) and d == d and d > 0:
            dur = float(d)
            break
        if i == 10:
            try:
                vf.evaluate(BIG_PLAY_JS)
            except Exception:
                pass
        page.wait_for_timeout(1000)
    if dur is None:
        raise VideoError("视频时长不可读")

    timeout_s = dur * float(vc["timeout_margin"]) + float(vc["timeout_add_s"])
    log("视频 duration=%.1fs timeout=%.0fs rate=%.1f" % (dur, timeout_s, rate))
    vf.evaluate(PLAY_JS, rate)
    page.wait_for_timeout(1500)

    samples = []
    last_ct = -1.0
    last_growth = time.time()
    resumed = False
    stop_reason = None
    t0 = time.time()
    while True:
        if should_stop is not None:
            try:
                if should_stop():
                    vf.evaluate(PAUSE_JS)
                    stop_reason = "stop_requested"
                    break
            except Exception:
                pass
        el = time.time() - t0
        st = read_state(vf)
        ct = float(st.get("currentTime") or 0.0)
        ratio = ct / dur if dur else 0.0
        try:
            marker = bool(cards.evaluate(MARKER_JS)) if cards is not None else None
        except Exception:
            marker = None
        side = None
        try:
            side = read_side()
        except Exception:
            pass
        samples.append({"t": round(el, 1), "ct": round(ct, 1), "ratio": round(ratio, 4),
                        "paused": st.get("paused"), "marker": marker, "side": side})
        log("视频 t=%5.1f ct=%7.1f ratio=%.3f paused=%s marker=%s side=%s" % (
            el, ct, ratio, st.get("paused"), marker, side))
        if on_sample:
            try:
                on_sample(st, ratio, dur)
            except Exception:
                pass
        if ct > last_ct + 0.5:
            last_ct = ct
            last_growth = time.time()
        if marker is True and ratio >= float(vc["marker_ratio"]):
            stop_reason = "marker"
            break
        if ratio >= float(vc["credit_ratio"]) and not resumed:
            vf.evaluate(PAUSE_JS)
            page.wait_for_timeout(2500)
            side2 = None
            try:
                side2 = read_side()
            except Exception:
                pass
            log("到达 %.3f 先暂停查任务点 side=%s" % (ratio, side2))
            try:
                credited = int((side2 or {}).get("value")) < 2 if isinstance(side2, dict) else False
            except Exception:
                credited = False
            if credited:
                stop_reason = "task_credited"
                break
            resumed = True
            vf.evaluate(PLAY_JS, rate)
            last_growth = time.time()
        if ratio >= float(vc["final_ratio"]):
            stop_reason = "ratio_final"
            break
        if el > timeout_s:
            stop_reason = "timeout"
            break
        if time.time() - last_growth > float(vc["no_progress_s"]):
            stop_reason = "no_progress"
            break
        page.wait_for_timeout(int(float(vc["poll_s"]) * 1000))

    vf.evaluate(PAUSE_JS)
    end_st = read_state(vf)
    try:
        final_ratio = round(float(end_st.get("currentTime") or 0) / dur, 4)
    except Exception:
        final_ratio = 0.0
    return {"skipped": False, "duration": dur, "timeout_s": timeout_s, "stop_reason": stop_reason,
            "final_ratio": final_ratio, "elapsed_s": round(time.time() - t0, 1),
            "end_state": end_st, "samples": samples}
