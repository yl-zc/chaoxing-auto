/* 学习通自动化控制台 · 界面脚本（面向普通用户，不显示技术信息）
 * M2：引擎状态（/api/engine）、进度摘要（/api/status）、任务证据（/api/tasks）分开取数，互不影响；
 * 对话只在引擎状态变化时追加消息，不扫描证据。
 */
(function () {
  "use strict";

  /* ---------- 文案表：所有面向用户的状态/环节措辞集中在这里，便于以后改文案 ---------- */
  var TEXT = {
    stage: { video: "正在播放视频", quiz: "正在答题", submit: "正在提交" },
    engineState: { running: "运行正常", idle: "运行正常", paused: "已暂停", stopped: "已停止" },
    lastStatus: { completed: "已完成", failed: "有问题", unknown: "已完成" },
    tag: { doing: "正在进行", paused: "已暂停", done: "已完成", failed: "有问题", pending: "待开始" },
    fallback: {
      noCurrent: "当前无进行中任务",
      noNext: "所有章节都做完了",
      loading: "正在读取…",
      nextLoading: "稍后读取…",
      statusPaused: "已暂停",
      tasksError: "任务信息暂时读不到，稍后会自动重试",
      statusError: "正在连接…",
      engineError: "学习通浏览器未连接，请先打开并登录后重试",
      retry: "重试",
      retryReply: "好的，我再试一次。请先确认学习通已在浏览器中打开并登录。",
      retryNoPlan: "没有找到可以重试的计划，请重新告诉我想做哪一部分。",
      retryQueued: "已经重新加入队列了。",
      retryFail: "刚才没能重新加入队列，请稍后再点一次「重试」。",
      preflightFail: "暂时读不到课程进度，请稍后再试",
      refreshLabel: "刷新进度",
      stoppedMsg: "浏览器已关闭/即将停止，本任务已结束"
    }
  };

  var state = {
    view: "chat", planSeq: 0, plans: {},
    status: null, tasks: null, engineStatus: null,
    lastProgress: null, engineInit: false, engineFp: "", enginePrev: null,
    lastStartedPlan: null, lastChatPlan: null,
    preflighting: false, lastActionAt: Date.now(), recentSkip: null
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function scoreText(v) {
    if (v === null || v === undefined || v === "") return "暂无成绩";
    return v + " 分";
  }
  function stageText(stage) { return TEXT.stage[stage] || ""; }
  function pctText(p) {
    return (p === null || p === undefined || p === "") ? "" : p + "%";
  }
  /* /api/friendly 用 done，/api/status 用 n —— 两个都兼容 */
  function progressDone(p) {
    if (!p) return null;
    if (p.done !== null && p.done !== undefined) return p.done;
    if (p.n !== null && p.n !== undefined) return p.n;
    return null;
  }
  function progressTotal(p) {
    if (!p) return null;
    if (p.total !== null && p.total !== undefined) return p.total;
    return null;
  }
  function progressPercent(p) {
    if (!p) return 0;
    if (p.percent !== null && p.percent !== undefined) return p.percent;
    var d = progressDone(p), t = progressTotal(p);
    if (d === null || !t) return 0;
    return Math.round(d * 1000 / t) / 10;
  }
  function progressText(p) {
    var d = progressDone(p), t = progressTotal(p);
    if (d === null || t === null) return TEXT.fallback.loading;
    return "已完成 " + d + " 个任务点 / 共 " + t + " 个";
  }

  /* ---------- 导航 ---------- */
  function switchView(v) {
    state.view = v;
    if (history.replaceState) history.replaceState(null, "", "#" + v);
    var items = document.querySelectorAll(".nav-item");
    for (var i = 0; i < items.length; i++) items[i].classList.toggle("active", items[i].getAttribute("data-view") === v);
    var views = document.querySelectorAll(".view");
    for (var j = 0; j < views.length; j++) views[j].classList.remove("active");
    var t = $("view-" + v);
    if (t) t.classList.add("active");
    if (v === "chat") scrollChat();
    if (v === "settings") loadAdvanced();
  }

  /* ---------- AI 对话 ---------- */
  function scrollChat() {
    var log = $("chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  }

  function addMsg(role, text) {
    var log = $("chat-log");
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;
    wrap.appendChild(b);
    log.appendChild(wrap);
    scrollChat();
    return wrap;
  }

  function lastAssistantText() {
    var bs = document.querySelectorAll("#chat-log .msg.assistant .bubble");
    return bs.length ? bs[bs.length - 1].textContent : "";
  }
  /* 引擎状态/暂停确认等系统消息，连续相同则不重复刷屏 */
  function addAssistant(text) {
    if (!text) return;
    if (lastAssistantText() === text) return;
    addMsg("assistant", text);
  }

  /* 助手消息 + 一个操作按钮（如「重试」） */
  function addAssistantAction(text, label, onclick) {
    var wrap = addMsg("assistant", text);
    var bubble = wrap.querySelector(".bubble");
    var btn = document.createElement("button");
    btn.className = "chat-action";
    btn.type = "button";
    btn.textContent = label;
    btn.addEventListener("click", onclick);
    bubble.appendChild(btn);
    scrollChat();
    return wrap;
  }

  function addTyping() { return addMsg("assistant typing", "正在想…"); }
  function removeNode(node) {
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  function addPlanCard(plan) {
    var log = $("chat-log");
    state.planSeq += 1;
    var pid = "p" + state.planSeq;
    state.plans[pid] = plan;
    var wrap = document.createElement("div");
    wrap.className = "msg assistant";
    var card = document.createElement("div");
    card.className = "plan-card";
    card.setAttribute("data-plan", pid);
    card.innerHTML =
      '<div class="plan-title">计划：' + esc(plan.label || "这次要做的事") + "</div>" +
      '<div class="plan-rows">' +
      '<div class="plan-row"><span>要做什么</span><b>' + esc(plan.action || "") + "</b></div>" +
      '<div class="plan-row"><span>共几节</span><b>' + esc(String(plan.count || 0)) + " 节</b></div>" +
      '<div class="plan-row"><span>预计几分钟</span><b>约 ' + esc(String(plan.minutes || 0)) + " 分钟</b></div>" +
      '<div class="plan-row"><span>范围</span><b>' + esc(plan.scope_text || "") + "</b></div>" +
      "</div>" +
      '<div class="plan-actions">' +
      '<button class="btn primary" data-plan-act="start">开始</button>' +
      '<button class="btn" data-plan-act="edit">改范围</button>' +
      '<button class="btn" data-plan-act="cancel">取消</button>' +
      "</div>" +
      '<div class="plan-status" hidden></div>';
    wrap.appendChild(card);
    log.appendChild(wrap);
    scrollChat();
    return card;
  }

  function setCardStatus(card, text, cls) {
    var st = card.querySelector(".plan-status");
    if (!st) return;
    st.hidden = false;
    st.className = "plan-status " + (cls || "");
    st.textContent = text;
  }
  function cardButtons(card) { return card.querySelectorAll("[data-plan-act]"); }
  function disableCard(card) {
    var btns = cardButtons(card);
    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;
  }
  function enableCard(card) {
    var btns = cardButtons(card);
    for (var i = 0; i < btns.length; i++) btns[i].disabled = false;
  }

  function startPlan(card, plan) {
    state.lastStartedPlan = plan;
    disableCard(card);
    card.classList.add("started");
    setCardStatus(card, "正在加入…", "");
    fetch("/api/plan/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: plan, text: plan.label || "" })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        setCardStatus(card, d.message || "已加入队列", "ok");
        addMsg("assistant", "好的，已经加入队列了。");
        pollStatus();
        return;
      }
      card.classList.remove("started");
      enableCard(card);
      var text = (d && (d.error_text || d.error)) || "暂时没能加入，请稍后再点一次";
      setCardStatus(card, text, "canceled");
      addAssistantAction(text, TEXT.fallback.retry, function () { startPlan(card, plan); });
    }).catch(function () {
      card.classList.remove("started");
      enableCard(card);
      setCardStatus(card, "暂时没能加入，请稍后再点一次", "canceled");
    });
  }

  function editPlan(plan) {
    var input = $("chat-input");
    input.value = plan.mode === "quiz" ? "只做测验 " : "只做 ";
    input.focus();
    addMsg("assistant", "好的，直接告诉我要改成哪几节就行，比如「只做第 2 章」或「2.4-3.3」。");
  }

  function cancelPlan(card) {
    disableCard(card);
    setCardStatus(card, "已取消", "canceled");
    addMsg("assistant", "好，这次先不做。有别的要求随时告诉我。");
  }

  function handlePlanClick(ev) {
    var log = $("chat-log");
    var el = ev.target;
    while (el && el !== log && !(el.getAttribute && el.getAttribute("data-plan-act"))) el = el.parentNode;
    if (!el || el === log || !el.getAttribute) return;
    var act = el.getAttribute("data-plan-act");
    var card = el;
    while (card && !(card.classList && card.classList.contains("plan-card"))) card = card.parentNode;
    if (!card) return;
    var plan = state.plans[card.getAttribute("data-plan")];
    if (!plan) return;
    if (act === "start") startPlan(card, plan);
    else if (act === "edit") editPlan(plan);
    else if (act === "cancel") cancelPlan(card);
  }

  function sendMessage(text) {
    text = String(text || "").trim();
    if (!text) return;
    addMsg("user", text);
    $("chat-input").value = "";
    var typing = addTyping();
    var btn = $("chat-send");
    btn.disabled = true;
    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    }).then(function (r) { return r.json(); }).then(function (d) {
      removeNode(typing);
      if (d && d.reply) addMsg("assistant", d.reply);
      if (d && d.plan && d.plan.count) { state.lastChatPlan = d.plan; addPlanCard(d.plan); }
    }).catch(function () {
      removeNode(typing);
      addMsg("assistant", "刚才没连上，请稍后再试一次。");
    }).then(function () { btn.disabled = false; });
  }

  function bindChat() {
    $("chat-log").addEventListener("click", handlePlanClick);
    $("chat-send").addEventListener("click", function () { sendMessage($("chat-input").value); });
    $("chat-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); sendMessage(this.value); }
    });
    var quicks = document.querySelectorAll("[data-quick]");
    for (var i = 0; i < quicks.length; i++) {
      quicks[i].addEventListener("click", function () { sendMessage(this.getAttribute("data-quick")); });
    }
  }

  /* ---------- 引擎状态变化 → 对话消息（只在状态变化时追加） ---------- */
  function engineFingerprint(payload) {
    var e = payload.engine;
    if (!e) return "";
    return JSON.stringify({
      state: e.state,
      cur: e.current ? [e.current.section, e.current.stage] : null,
      last: e.last_result ? [e.last_result.section, e.last_result.at, e.last_result.status, e.last_result.score] : null,
      sys: payload.system ? payload.system.state : "",
      hint: e.hint_text || "",
      err: e.error_text || "",
      skip: e.last_skipped ? [e.last_skipped.section, e.last_skipped.at] : null,
      skippedFlag: !!(e.last_result && e.last_result.skipped),
      stopping: !!payload.stopping,
      stopReq: payload.stop_request ? payload.stop_request.at : ""
    });
  }

  function engineMessages(prev, payload) {
    var out = [];
    var e = payload.engine;
    if (!e) return out;
    var sys = payload.system || {};
    var prevE = prev.engine || null;
    var prevSys = prev.system || {};
    var paused = sys.state === "paused";
    var prevPaused = prevSys.state === "paused";
    if (paused && !prevPaused) out.push("已暂停。需要的时候点「继续」。");
    if (!paused && prevPaused) out.push("继续了，接着做。");
    if (e.state === "idle" && e.hint_text && (!prevE || prevE.hint_text !== e.hint_text)) {
      out.push(e.hint_text + "。");
    }

    var cur = e.current;
    var pcur = prevE && prevE.current;
    if (cur && e.state === "running") {
      if (!pcur || pcur.section !== cur.section) {
        if (cur.section) out.push("开始做 " + cur.section + "：" + (stageText(cur.stage) || "正在处理") + "…");
      } else if (pcur.stage !== cur.stage) {
        var st = stageText(cur.stage);
        if (st) out.push(cur.section + "：" + st + "…");
      }
    }

    var lr = e.last_result;
    var plr = prevE && prevE.last_result;
    if (lr && lr.section && (!plr || plr.section !== lr.section || plr.at !== lr.at
        || plr.score !== lr.score || plr.status !== lr.status)) {
      var recentlySkipped = state.recentSkip && state.recentSkip.section === lr.section
        && (Date.now() - state.recentSkip.ts < 20000);
      if (lr.skipped) {
        if (!recentlySkipped) out.push(lr.section + " 你之前已经完成了，已跳过");
      } else if (!recentlySkipped) {
        var pg = payload.progress || {};
        var pd = progressDone(pg), pt = progressTotal(pg);
        var tail = "，进度 " + (pd === null ? "?" : pd) + "/" + (pt === null ? "?" : pt);
        if (lr.status === "failed") out.push(lr.section + " 有问题，需要你看一下" + tail);
        else if (lr.score !== null && lr.score !== undefined) out.push(lr.section + " 已完成，成绩 " + lr.score + " 分" + tail);
        else out.push(lr.section + " 已完成" + tail);
      }
    }
    return out;
  }

  function noteEngine(payload) {
    if (!payload) return;
    var e = payload.engine;
    if (!e) { state.engineFp = ""; state.enginePrev = null; return; }
    var fp = engineFingerprint(payload);
    if (!state.engineInit) {
      state.engineInit = true;
      state.engineFp = fp;
      state.enginePrev = payload;
      /* 首次打开就遇到出错/等待登录：直接在对话里给提示 */
      if (e.state === "error") {
        addAssistantAction(e.error_text || TEXT.fallback.engineError, TEXT.fallback.retry, retryEngine);
      } else if (e.state === "idle" && e.hint_text) {
        addAssistant(e.hint_text + "。");
      }
      if (payload.stopping || e.state === "stopped") addAssistant(TEXT.fallback.stoppedMsg);
      return;
    }
    if (fp === state.engineFp) { state.enginePrev = payload; return; }
    var prev = state.enginePrev || {};
    state.engineFp = fp;
    state.enginePrev = payload;
    if ((payload.stopping && !prev.stopping) || (e.state === "stopped" && (!prev.engine || prev.engine.state !== "stopped"))) {
      addAssistant(TEXT.fallback.stoppedMsg);
    }
    var skip = e.last_skipped;
    var prevSkip = prev.engine && prev.engine.last_skipped;
    var skipIsNew = !!(skip && skip.section
      && (!prevSkip || prevSkip.section !== skip.section || prevSkip.at !== skip.at));
    if (skipIsNew) {
      addAssistant(skip.section + " 你之前已经完成了，已跳过");
      state.recentSkip = { section: skip.section, ts: Date.now() };
    }
    var msgs = engineMessages(prev, payload);
    for (var i = 0; i < msgs.length; i++) addAssistant(msgs[i]);
    var prevE = prev.engine || null;
    if (e.state === "error" && (!prevE || prevE.state !== "error")) {
      addAssistantAction(e.error_text || TEXT.fallback.engineError, TEXT.fallback.retry, retryEngine);
    }
  }

  function requeuePlan(plan) {
    fetch("/api/plan/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: plan, text: plan.label || "" })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        addAssistant(TEXT.fallback.retryQueued);
        pollEngine();
        pollStatus();
        pollTasks();
        return;
      }
      var text = (d && d.error_text) || TEXT.fallback.retryFail;
      addAssistantAction(text, TEXT.fallback.retry, function () { retryEngine(); });
    }).catch(function () {
      addAssistantAction(TEXT.fallback.retryFail, TEXT.fallback.retry, function () { retryEngine(); });
    });
  }

  /* ---------- 刷新进度：调用引擎 preflight-only 读课程页真实 N/59 ---------- */
  function refreshProgress(manual) {
    if (state.preflighting) return;
    state.preflighting = true;
    var btn = $("rail-refresh");
    if (btn) { btn.disabled = true; btn.textContent = "正在读取…"; }
    fetch("/api/preflight", { method: "POST" }).then(function (r) { return r.json(); }).then(function (d) {
      var pf = d && d.preflight;
      if (pf && pf.ok) {
        pollStatus();
        if (manual) addAssistant("进度已刷新：已完成 " + pf.n + " / " + pf.total + " 个任务点。");
      } else {
        var text = (pf && pf.message) || TEXT.fallback.preflightFail;
        if (manual) addAssistantAction(text, TEXT.fallback.retry, function () { refreshProgress(true); });
      }
    }).catch(function () {
      if (manual) addAssistantAction(TEXT.fallback.preflightFail, TEXT.fallback.retry, function () { refreshProgress(true); });
    }).then(function () {
      state.preflighting = false;
      if (btn) { btn.disabled = false; btn.textContent = TEXT.fallback.refreshLabel; }
    });
  }

  function noteUserAction() { state.lastActionAt = Date.now(); }

  function maybeAutoRefresh() {
    if (document.visibilityState !== "visible") return;   /* 仅用户在前台页面时 */
    if (state.preflighting) return;
    var eng = currentEngine();
    if (eng && (eng.state === "running" || eng.state === "paused" || eng.state === "error")) return;
    if (Date.now() - state.lastActionAt < 30000) return;  /* 近 30s 有操作就不打扰 */
    refreshProgress(false);
  }

  function retryEngine() {
    addAssistant(TEXT.fallback.retryReply);
    var plan = state.lastStartedPlan || state.lastChatPlan || null;
    if (plan) { requeuePlan(plan); return; }
    fetch("/api/plan/latest").then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.plan) {
        state.lastChatPlan = d.plan;
        requeuePlan(d.plan);
      } else {
        addAssistant(TEXT.fallback.retryNoPlan);
      }
    }).catch(function () {
      addAssistant(TEXT.fallback.retryNoPlan);
    });
  }

  /* ---------- 右侧面板（由 status + engine + tasks 组合，任一缺失只影响自己） ---------- */
  function currentEngine() {
    var es = state.engineStatus;
    if (es && es.engine !== undefined) return es.engine;
    return (state.status && state.status.engine) || null;
  }
  function currentSystem() {
    var es = state.engineStatus;
    if (es && es.system) return es.system;
    return (state.status && state.status.system) || null;
  }
  function tasksSections() {
    var t = state.tasks;
    return (t && t.sections) || [];
  }
  function sectionNextRow(sec, strict) {
    var rows = tasksSections();
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].section === sec) {
        return { section: sec, title: rows[i].title || sec, tag: TEXT.tag.pending, tagClass: "pending" };
      }
    }
    if (strict) return null;   /* 引擎给的下一节不在课程列表里就不显示（防脏数据） */
    return { section: sec, title: sec, tag: TEXT.tag.pending, tagClass: "pending" };
  }
  function nextFromTasks() {
    var rows = tasksSections();
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].status !== "completed") {
        return sectionNextRow(rows[i].section);
      }
    }
    return null;
  }

  function composeRail() {
    var e = currentEngine();
    var cur = null;
    var next = null;
    if (e) {
      if (e.state === "error") {
        cur = {
          title: e.error_text || TEXT.fallback.engineError,
          tag: TEXT.tag.failed, tagClass: "failed", sub: ""
        };
      } else if (e.current && (e.state === "running" || e.state === "paused")) {
        var isPaused = e.state === "paused";
        var parts = [];
        if (stageText(e.current.stage)) parts.push(stageText(e.current.stage));
        else if (e.current.detail) parts.push(e.current.detail);
        if (pctText(e.current.progress)) parts.push(pctText(e.current.progress));
        var sub = parts.join(" · ");
        if (isPaused && !sub) sub = TEXT.fallback.statusPaused;
        cur = {
          section: e.current.section, title: e.current.title,
          tag: isPaused ? TEXT.tag.paused : TEXT.tag.doing,
          tagClass: isPaused ? "paused" : "doing",
          sub: sub
        };
      } else if (e.last_result && e.last_result.section) {
        var lr = e.last_result;
        cur = {
          section: lr.section, title: lr.title,
          tag: TEXT.lastStatus[lr.status] || TEXT.tag.done,
          tagClass: lr.status === "failed" ? "failed" : "done",
          sub: (lr.score === null || lr.score === undefined) ? "" : "成绩 " + lr.score + " 分"
        };
      } else {
        cur = { title: TEXT.fallback.noCurrent, tag: "", sub: e.hint_text || "" };
      }
      if (e.queue && e.queue.length) {
        next = { section: e.queue[0].section, title: e.queue[0].title, tag: TEXT.tag.pending, tagClass: "pending" };
      } else if (e.last_result && e.last_result.next_section) {
        /* 引擎给了下一节就用引擎的（校验在课程列表里），避免停在刚完成的节 */
        next = sectionNextRow(e.last_result.next_section, true);
      }
    } else {
      /* 没有引擎状态时，用任务板数据兜底（旧逻辑），读取失败也不报错 */
      var rows = tasksSections();
      var doneRows = [];
      for (var i = 0; i < rows.length; i++) if (rows[i].status === "completed") doneRows.push(rows[i]);
      doneRows.sort(function (a, b) { return String(b.updated_at || "").localeCompare(String(a.updated_at || "")); });
      if (doneRows.length) {
        var d = doneRows[0];
        cur = {
          section: d.section, title: d.title || d.section,
          tag: TEXT.tag.done, tagClass: "done",
          sub: (d.score === null || d.score === undefined || d.score === "") ? "" : "成绩 " + d.score + " 分"
        };
      } else {
        cur = { title: rows.length ? TEXT.fallback.noCurrent : TEXT.fallback.loading, tag: "", sub: "" };
      }
    }
    if (!next) next = nextFromTasks();
    if (!next) {
      var hasTasks = !!(state.tasks && state.tasks.sections);
      next = { title: hasTasks ? TEXT.fallback.noNext : TEXT.fallback.nextLoading, tag: "", sub: "" };
    }
    return { cur: cur, next: next };
  }

  function renderRail() {
    var s = state.status || {};
    var p = s.progress;
    if (p) state.lastProgress = p;
    else p = state.lastProgress || {};
    var doneN = progressDone(p), totalN = progressTotal(p);
    $("rail-done").textContent = (doneN === null ? "—" : doneN);
    $("rail-total").textContent = (totalN === null ? "—" : totalN);
    var pct = progressPercent(p);
    $("rail-bar").style.width = Math.max(0, Math.min(100, pct)) + "%";
    $("rail-percent").textContent = "已完成 " + pct + "%";

    var rail = composeRail();

    $("rail-current").textContent = rail.cur.title
      ? ((rail.cur.section ? rail.cur.section + " " : "") + rail.cur.title)
      : TEXT.fallback.noCurrent;
    var curTag = $("rail-current-tag");
    if (rail.cur.tag) {
      curTag.textContent = rail.cur.tag;
      curTag.className = "tag " + (rail.cur.tagClass || "doing");
      curTag.hidden = false;
    } else { curTag.hidden = true; }
    var curSub = $("rail-current-sub");
    if (rail.cur.sub) { curSub.textContent = rail.cur.sub; curSub.hidden = false; }
    else { curSub.hidden = true; }

    $("rail-next").textContent = rail.next.title
      ? ((rail.next.section ? rail.next.section + " " : "") + rail.next.title)
      : TEXT.fallback.nextLoading;
    var nextTag = $("rail-next-tag");
    if (rail.next.tag) { nextTag.textContent = rail.next.tag; nextTag.hidden = false; }
    else { nextTag.hidden = true; }

    var sys = currentSystem();
    var sysText = sys ? (TEXT.engineState[sys.state] || TEXT.engineState.running) : TEXT.fallback.statusError;
    $("rail-health-text").textContent = sysText;
    $("rail-dot").className = "dot " + (!sys ? "warn" : (sys.state === "paused" || sys.state === "stopped" ? "warn" : "ok"));
    updatePauseButton(sys);
    var eng = currentEngine();
    var retryBtn = $("rail-retry");
    if (retryBtn) retryBtn.hidden = !(eng && eng.state === "error");
  }

  function updatePauseButton(sys) {
    var btn = $("rail-pause");
    if (!btn) return;
    var stopped = !!(sys && sys.state === "stopped");
    var paused = !!(sys && sys.state === "paused");
    btn.textContent = stopped ? "已停止" : (paused ? "继续" : "暂停");
    btn.disabled = stopped;
    btn.classList.toggle("paused", paused);
    btn.setAttribute("data-paused", paused ? "1" : "0");
  }

  function togglePause() {
    var sys = currentSystem();
    var paused = !!(sys && sys.state === "paused");
    var action = paused ? "resume" : "pause";
    var btn = $("rail-pause");
    btn.disabled = true;
    fetch("/api/state/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) throw new Error("fail");
      addAssistant(action === "pause" ? "已暂停。需要的时候点「继续」。" : "继续了，接着做。");
      pollEngine();
      pollStatus();
    }).catch(function () {
      addAssistant("刚才没操作成功，请再试一次。");
    }).then(function () { btn.disabled = false; });
  }

  /* ---------- 总览（进度来自 status，最近完成来自 tasks） ---------- */
  function renderOverview() {
    var s = state.status || {};
    var p = s.progress || {};
    var doneN = progressDone(p), totalN = progressTotal(p);
    $("ov-progress").textContent = (doneN === null ? "—" : doneN) + " / " + (totalN === null ? "—" : totalN);
    $("ov-progress-text").textContent = p.text || progressText(p);
    var pct = progressPercent(p);
    $("ov-bar").style.width = Math.max(0, Math.min(100, pct)) + "%";
    var cur = s.current || {};
    $("ov-current").textContent = cur.text || "暂无待做章节";
    var health = s.health || "";
    var h = $("ov-health");
    h.textContent = health || TEXT.fallback.statusError;
    h.className = (health === "运行正常") ? "ok" : "";
    $("ov-updated").textContent = s.generated_at ? "刚刚更新" : "";

    var rows = tasksSections();
    var doneRows = rows.filter(function (x) { return x.status === "completed"; });
    $("ov-done").textContent = (state.tasks && state.tasks.sections) ? doneRows.length + " 章" : "—";
    doneRows.sort(function (a, b) { return String(b.updated_at || "").localeCompare(String(a.updated_at || "")); });
    var box = $("ov-recent");
    if (!state.tasks) { box.innerHTML = '<div class="empty">' + TEXT.fallback.loading + '</div>'; }
    else if (state.tasks.error) { box.innerHTML = '<div class="empty">' + TEXT.fallback.tasksError + '</div>'; }
    else if (!doneRows.length) { box.innerHTML = '<div class="empty">还没有完成的章节</div>'; }
    else {
      var html = "";
      for (var i = 0; i < Math.min(5, doneRows.length); i++) {
        var it = doneRows[i];
        html += '<div class="recent-row" data-section="' + esc(it.section) + '">' +
          '<div class="recent-name">' + esc(it.title || it.section) + '</div>' +
          '<div class="recent-score">' + esc(scoreText(it.score)) + '</div></div>';
      }
      box.innerHTML = html;
      var rr = box.querySelectorAll(".recent-row");
      for (var k = 0; k < rr.length; k++) {
        rr[k].addEventListener("click", function () { openSection(this.getAttribute("data-section")); });
      }
    }
  }

  /* ---------- 任务板（只依赖 /api/tasks，失败不影响其它区） ---------- */
  function renderTasksBoard() {
    var t = state.tasks;
    if (!t) { $("task-board").innerHTML = '<div class="empty">' + TEXT.fallback.loading + '</div>'; return; }
    if (t.error || !t.sections) {
      $("task-board").innerHTML = '<div class="empty">' + TEXT.fallback.tasksError + '</div>';
      return;
    }
    var secs = t.sections;
    var groups = {};
    for (var i = 0; i < secs.length; i++) {
      var ch = String(secs[i].section).split(".")[0];
      (groups[ch] = groups[ch] || []).push(secs[i]);
    }
    var keys = Object.keys(groups).sort(function (a, b) { return Number(a) - Number(b); });
    var html = "";
    for (var g = 0; g < keys.length; g++) {
      var rows = groups[keys[g]];
      var doneN = rows.filter(function (x) { return x.status === "completed"; }).length;
      html += '<div class="chapter"><div class="chapter-head"><span>第 ' + esc(keys[g]) + ' 章</span>' +
        '<span class="line"></span><span>' + doneN + ' / ' + rows.length + ' 节完成</span></div><div class="tasks">';
      for (var r = 0; r < rows.length; r++) {
        var it = rows[r];
        html += '<div class="task ' + esc(it.status) + '" data-section="' + esc(it.section) + '">' +
          '<div class="task-title">' + esc(it.title || it.section) + '</div>' +
          '<div class="task-meta"><span class="badge ' + esc(it.status) + '">' + esc(it.status_text || "") + '</span>' +
          '<span class="task-score">' + esc(it.score_text || "") + '</span></div></div>';
      }
      html += '</div></div>';
    }
    $("task-board").innerHTML = html || '<div class="empty">暂无任务</div>';
    var cards = $("task-board").querySelectorAll(".task");
    for (var c = 0; c < cards.length; c++) {
      cards[c].addEventListener("click", function () { openSection(this.getAttribute("data-section")); });
    }
  }

  /* ---------- 章节详情（只依赖 /api/section，独立取数） ---------- */
  function openSection(sec) {
    $("drawer-title").textContent = "正在读取…";
    $("drawer-sub").textContent = "";
    $("drawer-body").innerHTML = '<div class="empty">正在读取…</div>';
    $("drawer").classList.add("open");
    fetch("/api/section/" + encodeURIComponent(sec))
      .then(function (r) { return r.json(); })
      .then(function (d) { renderSection(d); })
      .catch(function () {
        $("drawer-title").textContent = "读取失败";
        $("drawer-body").innerHTML = errorBox("暂时读不到这一节的内容，请稍后再试。");
      });
  }

  function renderSection(d) {
    $("drawer-title").textContent = d.title || ("第 " + d.section + " 节");
    $("drawer-sub").textContent = (d.status_text || "") + " · 共 " + (d.question_count || 0) + " 题";
    var html = "";
    html += '<div class="score-box"><div class="score-num">' + esc(scoreText(d.score)) + '</div>' +
      '<div class="score-cap">' + (d.status_text === "已完成" ? "本章已完成" : d.status_text) + '</div></div>';

    if (d.shots && d.shots.length) {
      html += '<div class="block-title">完成截图</div><div class="shots">';
      for (var i = 0; i < d.shots.length; i++) {
        html += '<img class="shot" loading="lazy" src="' + esc(d.shots[i].url) + '" data-full="' + esc(d.shots[i].url) + '" alt="完成截图">';
      }
      html += '</div>';
    } else {
      html += '<div class="block-title">完成截图</div><div class="empty">这一节还没有截图</div>';
    }

    if (d.questions && d.questions.length) {
      html += '<div class="block-title">题目（共 ' + d.questions.length + ' 题）</div>';
      for (var q = 0; q < d.questions.length; q++) {
        var it = d.questions[q];
        html += '<div class="q-card"><div class="q-head"><span class="q-no">第 ' + esc(it.no) + ' 题</span>' +
          '<span class="q-type">' + esc(it.type_text) + '</span>' +
          '<span class="q-score">' + (it.score === null || it.score === undefined ? "" : esc(it.score) + " 分") + '</span></div>' +
          '<div class="q-stem">' + esc(it.stem) + '</div><div class="q-opts">';
        for (var o = 0; o < (it.options || []).length; o++) {
          html += '<div class="q-opt">' + esc(it.options[o].key) + '．' + esc(it.options[o].text) + '</div>';
        }
        html += '</div>';
        if (it.my_answer) html += '<div class="q-my">填入的答案：' + esc(it.my_answer) + '</div>';
        html += '</div>';
      }
    }
    $("drawer-body").innerHTML = html;

    var shots = $("drawer-body").querySelectorAll(".shot");
    for (var s = 0; s < shots.length; s++) {
      shots[s].addEventListener("click", function () { openLightbox(this.getAttribute("data-full")); });
    }
  }

  function openLightbox(url) {
    $("lightbox-img").src = url;
    $("lightbox").classList.add("open");
  }
  function closeLightbox() {
    $("lightbox").classList.remove("open");
    $("lightbox-img").src = "";
  }

  /* ---------- 提示 ---------- */
  function errorBox(msg) {
    return '<div class="error-box"><div>' + esc(msg) + '</div><button class="retry" onclick="location.reload()">重试</button></div>';
  }

  /* ---------- 高级设置（技术信息只在这里出现） ---------- */
  function loadAdvanced() {
    fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
      $("adv-url").textContent = "http://127.0.0.1:8770";
      $("adv-fw").textContent = "FastAPI + uvicorn";
      $("adv-cdp").textContent = (s.env && s.env.cdp && s.env.cdp.ok) ? "已连接" : "未连接";
    }).catch(function () { });
  }

  /* ---------- 分开轮询：引擎 / 进度摘要 / 任务证据，任一失败不影响其它 ---------- */
  function pollEngine() {
    fetch("/api/engine").then(function (r) { return r.json(); }).then(function (d) {
      state.engineStatus = d;
      noteEngine({ engine: d.engine, system: d.system, progress: (state.status || {}).progress || null,
                   stopping: d.stopping, stop_request: d.stop_request });
      renderRail();
    }).catch(function () { /* 引擎暂时读不到：保留上次显示 */ });
  }

  function pollStatus() {
    fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
      state.status = s;
      noteEngine({ engine: s.engine, system: s.system, progress: s.progress || null,
                   stopping: s.stopping, stop_request: s.stop_request });
      renderOverview();
      renderRail();
      $("foot-dot").className = "dot ok";
      $("foot-text").textContent = "正在运行";
    }).catch(function () {
      $("foot-dot").className = "dot bad";
      $("foot-text").textContent = "连接中断";
    });
  }

  function pollTasks() {
    fetch("/api/tasks").then(function (r) { return r.json(); }).then(function (t) {
      state.tasks = t;
      renderTasksBoard();
      renderOverview();
      renderRail();
    }).catch(function () {
      state.tasks = { sections: null, error: true };
      renderTasksBoard();
      renderRail();
    });
  }

  /* ---------- 窄屏抽屉 ---------- */
  function openRail() {
    $("rail").classList.add("open");
    $("rail-mask").classList.add("open");
  }
  function closeRail() {
    $("rail").classList.remove("open");
    $("rail-mask").classList.remove("open");
  }

  function init() {
    var items = document.querySelectorAll(".nav-item");
    for (var i = 0; i < items.length; i++) {
      items[i].addEventListener("click", function () { switchView(this.getAttribute("data-view")); });
    }
    $("drawer-close").addEventListener("click", function () { $("drawer").classList.remove("open"); });
    $("lightbox").addEventListener("click", closeLightbox);
    $("rail-toggle").addEventListener("click", openRail);
    $("rail-close").addEventListener("click", closeRail);
    $("rail-mask").addEventListener("click", closeRail);
    $("rail-pause").addEventListener("click", togglePause);
    $("rail-retry").addEventListener("click", retryEngine);
    $("rail-refresh").addEventListener("click", function () { refreshProgress(true); });
    document.addEventListener("click", noteUserAction, true);
    document.addEventListener("keydown", noteUserAction, true);
    document.addEventListener("input", noteUserAction, true);
    setInterval(maybeAutoRefresh, 10000);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeLightbox(); closeRail(); $("drawer").classList.remove("open"); }
    });
    bindChat();
    /* 刷新一律回到「AI 对话」首页（不沿用上次的 #页面） */
    switchView("chat");
    pollEngine();
    pollStatus();
    pollTasks();
    setInterval(pollEngine, 2000);   /* 引擎状态：轻量，2 秒 */
    setInterval(pollStatus, 5000);   /* 进度/健康摘要 */
    setInterval(pollTasks, 5000);    /* 任务证据 */
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();


/* 引擎/启动器：页面心跳与关闭告别（每 5s，可见性变化也发） */
(function () {
  function beat() {
    try {
      fetch('/api/heartbeat', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ua: navigator.userAgent}), cache: 'no-store'}).catch(function(){});
    } catch (e) {}
  }
  function bye(reason) {
    try {
      var body = JSON.stringify({reason: reason, ua: navigator.userAgent});
      var data = (typeof Blob !== "undefined") ? new Blob([body], {type: "application/json"}) : body;
      if (navigator.sendBeacon) navigator.sendBeacon('/api/bye', data);
    } catch (e) {}
  }
  beat();
  setInterval(beat, 5000);
  document.addEventListener('visibilitychange', beat);
  window.addEventListener('pagehide', function(){ bye('pagehide'); });
  window.addEventListener('beforeunload', function(){ bye('beforeunload'); });
})();
