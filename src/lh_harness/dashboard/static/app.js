"use strict";

// LongHorizon-Harness Dashboard — Codex-style serial timeline front-end.
// The harness is strictly serial, so the run renders as one top-to-bottom
// stream: task prompt -> per-round (manage / execute / audit) events.
// A right drawer shows full trajectories and raw artifacts on demand.
//
// The UI is bilingual (English default + Chinese) and supports light/dark
// themes (following the system by default). Both preferences persist in
// localStorage. Dynamic content produced by the harness itself (approval
// titles/messages, quick-answers) stays in the harness language; only the
// dashboard chrome is translated.

// ---------- i18n ----------
const I18N = {
  en: {
    appTitle: "LongHorizon-Harness",
    runsCap: "Runs",
    statusLoading: "Loading…",
    connFail: "Connection failed",
    statusPrefix: "Status ",
    running: "Running",
    roundPrefix: "Round ",
    runsEmpty: "Single run only (runs browsing disabled).",
    activityLead: "Live",
    placeholderTimeline: "Select a run on the left to view its serial Manager → Executor → Auditor timeline.",
    placeholderSelect: "Select a run on the left to view its serial timeline.",
    composerPlaceholder: "Inject an extra instruction for later models (non-blocking)…",
    composerHint: "Instructions are injected before the next management round, taking priority over the automatic flow.",
    detailTitle: "Details",
    artifactsSuffix: " · Artifacts",
    foldChildren: "Fold children",
    artifacts: "Artifacts",
    runningBadge: "Running",
    inputPrompt: "Input Prompt",
    loadingTraj: "Loading trajectory…",
    noTraj: "No trajectory steps",
    roleManager: "Manager",
    roleAuditor: "Auditor",
    roleFormatRepair: "Format Repair",
    roleExecGui: "GUI Executor",
    roleExecCli: "CLI Executor",
    roleExec: "Executor",
    doingManager: "Planning this round's subtask and route",
    doingExec: "Executing the subtask and producing outputs",
    doingAudit: "Auditing output authenticity and integrity",
    auditLabel: "Audit",
    stSession: "Session",
    stThinking: "Thinking",
    stOutput: "Output",
    stToolCall: "Tool call",
    stToolResult: "Tool result",
    stToolResultErr: "Tool result (error)",
    stFinalResult: "Final result",
    stNoContent: "No content",
    shots: " screenshots",
    needHuman: "Human confirmation needed",
    optContinue: "Continue",
    inputOptional: "Optional: add a note",
    clickToView: "Click a file name to view raw content",
    noArtifacts: "No artifacts this round",
    readFailArtifacts: "Failed to read artifacts",
    loading: "Loading…",
    readFail: "Read failed",
    yourAnswer: "Your answer",
    yourChoice: "Your choice",
    extraInstr: "Extra instruction",
    stopRun: "Stop run",
    continueParen: "(continue)",
    endRun: "End run",
    continueRun: "Continue run",
    recHuman: "Human confirmation",
    roundWord: "Round ",
    ev_role_harness_start: "Run started",
    ev_manager_round_start: "Manage start",
    ev_manager_round_done: "Manage done",
    ev_executor_role_start: "Execute start",
    ev_executor_role_done: "Execute done",
    ev_auditor_role_start: "Audit start",
    ev_auditor_role_done: "Audit done",
    ev_auditor_format_repair_start: "Format repair",
    ev_auditor_format_repair_done: "Format repair done",
    ev_human_instructions_injected: "Human instruction injected",
    ev_human_abort: "Human abort",
    ev_managed_round_recorded: "Round recorded",
    ev_role_harness_done: "Run finished",
    tg_completed: "Run completion",
    tg_max_rounds: "Round limit",
    tg_needs_input: "Manager query",
    tg_needs_human: "Blocked intervention",
    tg_repeated_failure: "Repeated-failure intervention",
    langLabel: "EN",
    themeSystem: "System",
    themeLight: "Light",
    themeDark: "Dark",
  },
  zh: {
    appTitle: "LongHorizon-Harness",
    runsCap: "运行记录",
    statusLoading: "加载中…",
    connFail: "连接失败",
    statusPrefix: "状态 ",
    running: "运行中",
    roundPrefix: "轮次 ",
    runsEmpty: "当前只有单个运行（未启用 runs 浏览）。",
    activityLead: "实时",
    placeholderTimeline: "选择左侧任一运行记录，查看串行的任务管理 → 执行 → 审计时间线。",
    placeholderSelect: "选择左侧任一运行记录，查看串行时间线。",
    composerPlaceholder: "向后续模型注入补充指令（不阻塞）…",
    composerHint: "指令会在下一轮任务管理前注入，优先级高于自动流程。",
    detailTitle: "详情",
    artifactsSuffix: " · 产物文件",
    foldChildren: "折叠子级",
    artifacts: "产物文件",
    runningBadge: "运行中",
    inputPrompt: "输入 Prompt",
    loadingTraj: "加载轨迹…",
    noTraj: "无轨迹步骤",
    roleManager: "任务管理器",
    roleAuditor: "审计",
    roleFormatRepair: "格式修复",
    roleExecGui: "GUI 执行",
    roleExecCli: "CLI 执行",
    roleExec: "任务执行",
    doingManager: "正在制定本轮子任务与路由",
    doingExec: "正在执行子任务并产出",
    doingAudit: "正在校验产出的真实性与完整性",
    auditLabel: "审计",
    stSession: "会话",
    stThinking: "思考",
    stOutput: "输出",
    stToolCall: "工具调用",
    stToolResult: "工具结果",
    stToolResultErr: "工具结果 (错误)",
    stFinalResult: "最终结果",
    stNoContent: "无内容",
    shots: " 张截图",
    needHuman: "需要人工确认",
    optContinue: "继续",
    inputOptional: "可选：补充说明",
    clickToView: "点击文件名查看原始内容",
    noArtifacts: "本轮无产物文件",
    readFailArtifacts: "产物读取失败",
    loading: "加载中…",
    readFail: "读取失败",
    yourAnswer: "你的回答",
    yourChoice: "你的选择",
    extraInstr: "补充指令",
    stopRun: "终止运行",
    continueParen: "（继续）",
    endRun: "结束运行",
    continueRun: "继续运行",
    recHuman: "人工确认",
    roundWord: "第 ",
    ev_role_harness_start: "任务启动",
    ev_manager_round_start: "任务管理开始",
    ev_manager_round_done: "任务管理完成",
    ev_executor_role_start: "执行开始",
    ev_executor_role_done: "执行完成",
    ev_auditor_role_start: "审计开始",
    ev_auditor_role_done: "审计完成",
    ev_auditor_format_repair_start: "格式修复",
    ev_auditor_format_repair_done: "格式修复完成",
    ev_human_instructions_injected: "注入人工指令",
    ev_human_abort: "人工终止",
    ev_managed_round_recorded: "记录轮次",
    ev_role_harness_done: "任务结束",
    tg_completed: "运行完成确认",
    tg_max_rounds: "轮次上限确认",
    tg_needs_input: "任务管理器请示",
    tg_needs_human: "阻塞介入",
    tg_repeated_failure: "失败介入",
    langLabel: "中",
    themeSystem: "跟随系统",
    themeLight: "亮色",
    themeDark: "暗色",
  },
};

let LANG = "en";
function loadLang() {
  try {
    const v = localStorage.getItem("lh_lang");
    LANG = v === "zh" ? "zh" : "en";
  } catch (e) { LANG = "en"; }
}
function t(key) {
  const table = I18N[LANG] || I18N.en;
  return table[key] != null ? table[key] : (I18N.en[key] != null ? I18N.en[key] : key);
}
// Round-aware label: "Round 3" (en) / "第 3 轮" (zh)
function roundLabel(n) {
  return LANG === "zh" ? "第 " + n + " 轮" : "Round " + n;
}

// ---------- theme ----------
// mode is one of "system" | "light" | "dark"; "system" removes the override so
// the CSS prefers-color-scheme media query decides.
function loadThemeMode() {
  try { return localStorage.getItem("lh_theme") || "system"; }
  catch (e) { return "system"; }
}
function applyThemeMode(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
  else root.removeAttribute("data-theme");
  try {
    if (mode === "system") localStorage.removeItem("lh_theme");
    else localStorage.setItem("lh_theme", mode);
  } catch (e) {}
}
function themeIcon(mode) {
  return mode === "light" ? "☀" : mode === "dark" ? "🌙" : "🖥";
}
function themeTitle(mode) {
  return mode === "light" ? t("themeLight") : mode === "dark" ? t("themeDark") : t("themeSystem");
}

let STATE = null;
let STREAM_SIG = null;          // signature of rendered timeline (avoid reflow)
// Manual fold overrides shared by rounds, role sections and steps. A key is in
// `on` if the user forced it open, in `off` if forced closed; otherwise the
// element's own default applies. This persists across the 2s refresh.
const FOLD = { on: new Set(), off: new Set() };
const TRAJ = {};                // cache: `${round}:${role}` -> { steps } | { loading }
const INPUTS = {};              // cache: `${round}:${role}` -> text | null | { loading }
let DRAWER = null;              // { round } — the inline artifacts panel
// Whether the timeline should stick to the bottom. Async content (images that
// grow after load, streamed steps) would otherwise scroll out of view once it
// finishes loading. The scroll listener flips this off when the user scrolls up
// and back on when they return to the bottom, so auto-follow respects the user.
let STICK_BOTTOM = true;

function isOpen(key, def) {
  if (FOLD.on.has(key)) return true;
  if (FOLD.off.has(key)) return false;
  return def;
}
function setOpen(key, open) {
  if (open) { FOLD.on.add(key); FOLD.off.delete(key); }
  else { FOLD.off.add(key); FOLD.on.delete(key); }
}

// "Fold children": fold (or, if all already folded, unfold) only the DIRECT
// collapsible children of `container` — not grandchildren — and without
// touching the container's own fold state. A node counts as a direct child when
// its nearest [data-foldkey] ancestor is `container` itself.
function toggleDescendants(container) {
  const nodes = Array.from(container.querySelectorAll("[data-foldkey]"))
    .filter((n) => n.parentElement.closest("[data-foldkey]") === container);
  if (!nodes.length) return;
  const anyOpen = nodes.some((n) => n.classList.contains("open"));
  const next = !anyOpen; // if any direct child is open -> fold all; else unfold
  nodes.forEach((n) => {
    n.classList.toggle("open", next);
    setOpen(n.dataset.foldkey, next);
  });
}

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}

// Apply translations to static [data-i18n] / [data-i18n-ph] elements.
function applyStaticI18n() {
  document.documentElement.setAttribute("lang", LANG === "zh" ? "zh-CN" : "en");
  document.querySelectorAll("[data-i18n]").forEach((n) => { n.textContent = t(n.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((n) => { n.setAttribute("placeholder", t(n.dataset.i18nPh)); });
  $("langBtn").textContent = t("langLabel");
  const mode = loadThemeMode();
  $("themeBtn").textContent = themeIcon(mode);
  $("themeBtn").title = themeTitle(mode);
}

// ---------- data ----------
async function fetchState() {
  try {
    const res = await fetch("/api/state");
    STATE = await res.json();
    render();
  } catch (e) {
    $("statusPill").textContent = t("connFail");
    $("statusPill").className = "pill bad";
  }
}
function statusClass(s) {
  if (s === "complete") return "ok";
  if (s === "blocked") return "bad";
  return "warn";
}

// ---------- top render ----------
function render() {
  if (!STATE) return;
  renderRuns();
  renderHead();
  renderActivity();
  renderStream();
  highlightDetailBtn();
}

function renderHead() {
  const report = STATE.report || {};
  $("title").textContent = STATE.task ? shorten(STATE.task, 70) : t("appTitle");
  $("title").title = STATE.task || "";
  const pill = $("statusPill");
  pill.textContent = t("statusPrefix") + (report.status || t("running"));
  pill.className = "pill " + statusClass(report.status);
  $("roundPill").textContent = t("roundPrefix") + (STATE.round_count || 0);
}

function shorten(s, n) {
  s = String(s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// ---------- runs (left rail) ----------
function renderRuns() {
  const box = $("runList");
  const runs = STATE.runs || [];
  if (!runs.length) {
    box.innerHTML = '<div class="run-empty">' + esc(t("runsEmpty")) + "</div>";
    return;
  }
  const current = STATE.current_run || "";
  const sig = runs.map((r) => r.id + ":" + r.status).join("|") + "#" + current + "#" + LANG;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = "";
  runs.forEach((r) => {
    const item = el("div", "run-item" + (r.id === current ? " active" : ""));
    item.innerHTML = '<div class="rid">' + esc(r.id) + "</div>" +
      (r.status ? '<div class="rst"><span class="badge ' + esc(r.status) + '">' + esc(r.status) + "</span></div>" : "");
    item.addEventListener("click", () => selectRun(r.id));
    box.appendChild(item);
  });
}

async function selectRun(runId) {
  if (!runId) return;
  await fetch("/api/select-run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId }),
  });
  STREAM_SIG = null;
  FOLD.on.clear();
  FOLD.off.clear();
  for (const k of Object.keys(TRAJ)) delete TRAJ[k];
  for (const k of Object.keys(INPUTS)) delete INPUTS[k];
  closeDrawer();
  await fetchState();
}

// ---------- activity bar ----------
function eventLabel(ev) {
  const key = "ev_" + ev;
  const table = I18N[LANG] || I18N.en;
  return table[key] != null ? table[key] : (I18N.en[key] != null ? I18N.en[key] : ev);
}
function renderActivity() {
  const bar = $("activityBar");
  const events = STATE.events || [];
  if (!events.length) { bar.style.display = "none"; return; }
  const ev = events[events.length - 1];
  bar.style.display = "";
  const label = eventLabel(ev.event);
  const rnd = ev.round != null ? " · R" + ev.round : "";
  const role = ev.role ? " · " + ev.role : "";
  const time = ev.ts ? new Date(ev.ts * 1000).toLocaleTimeString() : "";
  $("activityNow").innerHTML = '<span class="t">' + esc(time) + "</span>" + esc(label + rnd + role);
}

// ---------- serial timeline ----------
const STEP_BADGE = { gui: "gui", cli: "cli", done: "done", blocked: "blocked", invalid: "invalid", ask: "ask" };

function auditorHeads(report) {
  // Parse leading "状态: X" / "完整性: Y" for compact badges.
  const out = {};
  (report || "").split("\n").slice(0, 4).forEach((ln) => {
    let m = ln.match(/^\s*(?:状态|status)\s*[:：]\s*(complete|incomplete|blocked|完成|未完成|阻塞)/i);
    if (m) out.status = m[1].toLowerCase().replace("完成", "complete").replace("未完成", "incomplete").replace("阻塞", "blocked");
    m = ln.match(/^\s*(?:完整性|integrity)\s*[:：]\s*(clean|suspect|violation)/i);
    if (m) out.integrity = m[1].toLowerCase();
  });
  return out;
}

function streamSignature() {
  const rounds = STATE.rounds || [];
  const parts = rounds.map((r) =>
    r.round_index + ":" + (r.in_progress ? "L" : "F") + ":" +
    (r.plan_text || "").length + "/" + (r.executor_output || "").length + "/" +
    (r.auditor_report || "").length + "/" + (r.harness_feedback || "").length +
    "/" + (r.roles || []).join(",") +
    // include live trajectory byte sizes so streamed steps trigger a re-render
    "/" + JSON.stringify(r.role_sizes || {})
  );
  const appr = (STATE.approvals || []).map((a) => a.approval_id + ":" + a.status).join(",");
  return LANG + "|" + (STATE.current_run || "") + "|" + parts.join("|") + "|appr:" + appr;
}

function renderStream() {
  const sig = streamSignature();
  if (sig === STREAM_SIG) return; // nothing changed; don't disturb reading
  STREAM_SIG = sig;

  const host = $("streamInner");
  const rounds = STATE.rounds || [];
  if (!STATE.task && !rounds.length) {
    host.innerHTML = '<p class="placeholder">' + esc(t("placeholderSelect")) + "</p>";
    return;
  }
  // Follow the bottom whenever the user is already pinned there (STICK_BOTTOM),
  // so a re-render triggered by streamed steps / late image loads stays put.
  const atBottom = STICK_BOTTOM || isNearBottom();
  host.innerHTML = "";

  // resolved human interactions are saved records — show each after its round.
  const approvals = STATE.approvals || [];
  const resolvedByRound = {};
  approvals.filter((a) => a.status === "resolved").forEach((a) => {
    const ri = a.round_index || 0;
    (resolvedByRound[ri] = resolvedByRound[ri] || []).push(a);
  });
  const shownRounds = new Set();

  // The task itself is shown in the header title; the timeline focuses on
  // trajectories/results rather than echoing the prompt.
  rounds.forEach((r, i) => {
    host.appendChild(renderRound(r, i === rounds.length - 1));
    shownRounds.add(r.round_index);
    (resolvedByRound[r.round_index] || []).forEach((a) => host.appendChild(renderApprovalRecord(a)));
  });

  // resolved interactions not tied to a rendered round still get shown.
  Object.keys(resolvedByRound).forEach((ri) => {
    if (!shownRounds.has(Number(ri))) {
      resolvedByRound[ri].forEach((a) => host.appendChild(renderApprovalRecord(a)));
    }
  });

  // pending approvals (interactive) at the end of the stream.
  approvals.filter((a) => a.status === "pending").forEach((a) => host.appendChild(renderApproval(a)));

  if (atBottom) followBottom();
  // Content (images, fold-open animations, streamed steps) keeps growing after
  // this render; a ResizeObserver re-pins the bottom as it does (see below).
  ensureStickObserver();
}

// The role that is currently working in an in-progress round (its result text
// has not been written to disk yet). Drives the live "thinking" indicator.
function activeRole(r) {
  if (!r.in_progress) return null;
  if (!r.plan_text) return { label: t("roleManager"), doing: t("doingManager") };
  if ((r.next_step === "gui" || r.next_step === "cli") && !r.executor_output) {
    return { label: r.next_step === "gui" ? t("roleExecGui") : t("roleExecCli"), doing: t("doingExec") };
  }
  if (r.executor_output && !r.auditor_report && r.next_step !== "done") {
    return { label: t("auditLabel"), doing: t("doingAudit") };
  }
  if (r.next_step === "done" && !r.auditor_report) return null;
  return null;
}

const ROLE_ORDER = ["manager", "executor", "auditor_format_repair", "auditor"];

function roleDisplayName(r, role) {
  if (role === "manager") return t("roleManager");
  if (role === "auditor") return t("roleAuditor");
  if (role === "auditor_format_repair") return t("roleFormatRepair");
  if (role === "executor") return r.next_step === "gui" ? t("roleExecGui") : r.next_step === "cli" ? t("roleExecCli") : t("roleExec");
  return role;
}

// Role-colored accent class (matches the gallery's role bands):
// Manager=amber, GUI executor=sky, CLI executor=emerald, Auditor=red, repair=violet.
function roleAccentClass(r, role) {
  if (role === "manager") return "acc-manager";
  if (role === "auditor") return "acc-auditor";
  if (role === "auditor_format_repair") return "acc-repair";
  if (role === "executor") return r.next_step === "gui" ? "acc-gui" : "acc-cli";
  return "";
}

function roleBadges(r, role) {
  if (role === "executor") {
    return r.next_step ? '<span class="badge ' + (STEP_BADGE[r.next_step] || "") + '">' + esc(r.next_step) + "</span>" : "";
  }
  if (role === "auditor") {
    const vh = auditorHeads(r.auditor_report);
    return [
      vh.status ? '<span class="badge ' + vh.status + '">' + vh.status + "</span>" : "",
      vh.integrity ? '<span class="badge ' + vh.integrity + '">' + vh.integrity + "</span>" : "",
    ].join(" ");
  }
  return "";
}

// input prompt file per role (shown as a default-folded step)
const ROLE_INPUT_FILE = {
  manager: "manager_input.txt",
  executor: "executor_prompt.txt",
  auditor: "auditor_input.txt",
  auditor_format_repair: "auditor_format_repair_input.txt",
};

// The whole round folds via its header, for quick jumping between rounds.
function renderRound(r, isLastRound) {
  const group = el("div", "round-group");
  const roundKey = "round:" + r.round_index;
  group.dataset.foldkey = roundKey;
  const open = isOpen(roundKey, true);
  if (open) group.classList.add("open");
  const stepBadge = r.next_step ? '<span class="badge ' + (STEP_BADGE[r.next_step] || "") + '">' + esc(r.next_step) + "</span>" : "";
  const liveBadge = r.in_progress ? '<span class="badge live"><span class="live-dot"></span>' + esc(t("runningBadge")) + "</span>" : "";

  const rule = el("div", "round-rule",
    '<span class="chev">▶</span><span class="lbl">Round ' + r.round_index + "</span>" + stepBadge + liveBadge);
  const actions = el("div", "round-actions");
  const foldAll = el("button", "rbtn ghost", esc(t("foldChildren")));
  foldAll.addEventListener("click", (e) => { e.stopPropagation(); toggleDescendants(group); });
  actions.appendChild(foldAll);
  const ab = el("button", "rbtn", esc(t("artifacts")));
  ab.dataset.detail = r.round_index + ":art";
  ab.addEventListener("click", (e) => { e.stopPropagation(); openDrawer(r.round_index, "art"); });
  actions.appendChild(ab);
  rule.appendChild(actions);
  group.appendChild(rule);

  const bodyWrap = el("div", "round-body-wrap");
  const body = el("div", "round-body");
  bodyWrap.appendChild(body);

  // one collapsible section per role, in canonical order. Across the WHOLE
  // timeline only one step defaults to expanded: the last step of the last role
  // of the last round. Everything else defaults to folded.
  const presentRoles = ROLE_ORDER.filter((role) => (r.roles || []).includes(role));
  presentRoles.forEach((role, i) =>
    body.appendChild(renderRoleSection(r, role, isLastRound && i === presentRoles.length - 1)));

  // live status while a role is running (no trajectory saved yet)
  const active = activeRole(r);
  if (active) {
    body.appendChild(el("div", "think",
      '<span class="dots"><i></i><i></i><i></i></span>' +
      '<span class="who">' + esc(active.label) + "</span>" +
      '<span class="doing">' + esc(active.doing) + "…</span>"));
  }
  group.appendChild(bodyWrap);

  rule.addEventListener("click", (e) => {
    if (e.target.closest(".round-actions")) return; // don't fold when clicking the button
    const now = !group.classList.contains("open");
    group.classList.toggle("open", now);
    setOpen(roundKey, now);
  });
  return group;
}

// Each role section (Manager / Executor / Auditor) folds via its header.
// `isLastRole` marks the round's final role — only there does the last step
// default to expanded.
function renderRoleSection(r, role, isLastRole) {
  const secKey = "sec:" + r.round_index + ":" + role;
  const open = isOpen(secKey, true);
  const accent = roleAccentClass(r, role);
  const sec = el("div", "role-sec" + (accent ? " " + accent : "") + (open ? " open" : ""));
  sec.dataset.foldkey = secKey;
  const head = el("div", "role-head",
    '<span class="chev">▶</span><span class="rname">' + esc(roleDisplayName(r, role)) + "</span>" + roleBadges(r, role));
  const acts = el("div", "role-actions");
  const foldAll = el("button", "rbtn ghost", esc(t("foldChildren")));
  foldAll.addEventListener("click", (e) => { e.stopPropagation(); toggleDescendants(sec); });
  acts.appendChild(foldAll);
  head.appendChild(acts);
  const bodyWrap = el("div", "role-body-wrap");
  const box = el("div", "tsteps");
  bodyWrap.appendChild(box);
  sec.appendChild(head);
  sec.appendChild(bodyWrap);
  head.addEventListener("click", (e) => {
    if (e.target.closest(".role-actions")) return;
    const now = !sec.classList.contains("open");
    sec.classList.toggle("open", now);
    setOpen(secKey, now);
  });

  // input prompt as the first step, folded by default.
  const promptStep = renderPromptStep(r, role);
  if (promptStep) box.appendChild(promptStep);

  const key = r.round_index + ":" + role;
  const size = (r.role_sizes || {})[role] || 0;
  const cached = TRAJ[key];
  // Refetch when the trajectory file has grown (live streaming) so the newest
  // steps show up without a full page reload.
  if (cached === undefined || cached.sig !== size) ensureTraj(r.round_index, role, size);

  if (cached && cached.steps) {
    if (!cached.steps.length) box.appendChild(el("div", "tempty", esc(t("noTraj"))));
    else {
      // only the last role's last collapsible step defaults to expanded; the
      // session row is a flat info line and never counts as the "last item".
      let lastIdx = -1;
      if (isLastRole) cached.steps.forEach((s, i) => { if (s.kind !== "session") lastIdx = i; });
      cached.steps.forEach((s, i) => box.appendChild(renderStep(r, role, i, s, i === lastIdx)));
    }
  } else {
    box.appendChild(el("div", "tloading", '<span class="dots"><i></i><i></i><i></i></span>' + esc(t("loadingTraj"))));
  }
  return sec;
}

function renderPromptStep(r, role) {
  const key = r.round_index + ":" + role;
  const cached = INPUTS[key];
  if (cached === undefined) { ensureInput(r.round_index, role); return null; }
  if (cached == null || (cached.loading)) return null; // no input file, or still loading
  return foldBlock(r.round_index + ":" + role + ":prompt", "prompt", t("inputPrompt"), brief(cached), '<div class="tp">' + esc(cached) + "</div>", false);
}

function brief(t2, n) {
  return shorten(t2 || "", n || 68) || "—";
}

// Turn one parsed trajectory step into { label, sum, body }.
function stepBits(s) {
  if (s.kind === "session") {
    return { label: t("stSession"), sum: "model=" + (s.model || "") + " · tools=" + (s.tool_count || 0),
      body: '<div class="meta">model=' + esc(s.model || "") + " · mcp=" + esc((s.mcp_servers || []).join(",")) + " · tools=" + (s.tool_count || 0) + "</div>" };
  }
  if (s.kind === "thinking") return { label: t("stThinking"), sum: brief(s.text), body: '<div class="tp">' + esc(s.text) + "</div>" };
  if (s.kind === "text") return { label: t("stOutput"), sum: brief(s.text), body: '<div class="tp">' + esc(s.text) + "</div>" };
  if (s.kind === "tool_use") {
    return { label: t("stToolCall") + " · " + (s.name || ""), sum: brief(JSON.stringify(s.input || {})),
      body: '<div class="code">' + esc(JSON.stringify(s.input || {}, null, 2)) + "</div>" };
  }
  if (s.kind === "tool_result") {
    let body = "";
    if (s.text && s.text.trim()) body += '<div class="code">' + esc(s.text) + "</div>";
    (s.images || []).forEach((src) => { body += '<img class="shot" data-full="' + esc(src) + '" src="' + esc(src) + '" />'; });
    const sum = (s.images && s.images.length) ? s.images.length + t("shots") : brief(s.text);
    return { label: s.is_error ? t("stToolResultErr") : t("stToolResult"), sum, body: body || '<div class="tempty">' + esc(t("stNoContent")) + "</div>" };
  }
  if (s.kind === "result") {
    return { label: t("stFinalResult"), sum: brief(s.text),
      body: '<div class="tp">' + esc(s.text) + "</div>" +
        '<div class="meta">turns=' + (s.num_turns != null ? s.num_turns : "-") +
        (s.duration_ms != null ? " · " + s.duration_ms + "ms" : "") +
        (s.cost_usd != null ? " · $" + s.cost_usd : "") + "</div>" };
  }
  return { label: s.kind, sum: brief(s.text || ""), body: '<div class="tp">' + esc(s.text || "") + "</div>" };
}

// Generic collapsible step: header (chevron + label + summary) over a body that
// folds via a CSS grid-template-rows trick. Fold state persists via FOLD.
function foldBlock(key, kindCls, label, sum, bodyHtml, defOpen) {
  const open = isOpen(key, defOpen);
  const wrap = el("div", "tstep " + kindCls + (open ? " open" : ""));
  wrap.dataset.foldkey = key;
  const head = el("div", "tstep-head",
    '<span class="chev">▶</span><span class="tk">' + esc(label) + '</span><span class="tsum">' + esc(sum) + "</span>");
  const bodyWrap = el("div", "tbody-wrap");
  const body = el("div", "tbody");
  body.innerHTML = bodyHtml;
  bodyWrap.appendChild(body);
  wrap.appendChild(head);
  wrap.appendChild(bodyWrap);
  head.addEventListener("click", () => {
    const now = !wrap.classList.contains("open");
    wrap.classList.toggle("open", now);
    setOpen(key, now);
  });
  body.querySelectorAll("img.shot").forEach((img) => img.addEventListener("click", () => openLightbox(img.dataset.full)));
  return wrap;
}

// The session step is default info — always shown, never collapsible.
function sessionRow(s) {
  return el("div", "tstep session static",
    '<div class="tstep-head static"><span class="tk">' + esc(t("stSession")) + '</span>' +
    '<span class="tsum">model=' + esc(s.model || "") + " · mcp=" + esc((s.mcp_servers || []).join(",")) +
    " · tools=" + (s.tool_count || 0) + "</span></div>");
}

// A single trajectory step. Every step defaults to folded except the very last
// one in the section (isLast). Session is shown flat (not collapsible).
function renderStep(r, role, idx, s, isLast) {
  if (s.kind === "session") return sessionRow(s);
  const key = r.round_index + ":" + role + ":" + idx;
  const bits = stepBits(s);
  return foldBlock(key, s.kind + (s.is_error ? " err" : ""), bits.label, bits.sum, bits.body, !!isLast);
}

// Fetch a role's trajectory and cache it keyed by the byte size at fetch time.
async function ensureTraj(round, role, sig) {
  const key = round + ":" + role;
  const cur = TRAJ[key];
  if (cur && cur.pending === sig) return; // already fetching this exact size
  const prevSteps = cur && cur.steps ? cur.steps : undefined;
  TRAJ[key] = { steps: prevSteps, sig: cur ? cur.sig : undefined, pending: sig };
  try {
    const res = await fetch("/api/round/" + round + "/trajectory/" + role);
    const data = await res.json();
    TRAJ[key] = { steps: (data && data.steps) || [], sig };
  } catch (e) {
    TRAJ[key] = { steps: prevSteps || [], sig };
  }
  STREAM_SIG = null;
  render();
}

// Fetch a role's input prompt file once (immutable). Missing file -> null.
async function ensureInput(round, role) {
  const key = round + ":" + role;
  if (INPUTS[key] !== undefined) return;
  const file = ROLE_INPUT_FILE[role];
  if (!file) { INPUTS[key] = null; return; }
  INPUTS[key] = { loading: true };
  try {
    const res = await fetch("/api/round/" + round + "/" + encodeURIComponent(file));
    INPUTS[key] = res.ok ? await res.text() : null;
  } catch (e) {
    INPUTS[key] = null;
  }
  STREAM_SIG = null;
  render();
}

// Render an approval as a question + option buttons. Option buttons are driven
// by `a.options` ([{value,label,style}]); known machine values (continue/stop)
// are localized so the buttons follow the dashboard language. `a.answers`
// (["是","否",...]) are harness-produced quick answers and shown verbatim.
function optionLabel(opt) {
  if (opt.value === "continue") return t("continueRun");
  if (opt.value === "stop") return opt.style === "danger" ? t("stopRun") : t("endRun");
  return opt.label || opt.value;
}
function renderApproval(a) {
  const trigger = (a.context && a.context.trigger) || "";
  const isDecision = trigger === "completed" || trigger === "max_rounds";
  const icon = isDecision ? "⏸" : trigger === "needs_input" ? "❓" : "⚠";
  const prefix = a.round_index ? roundLabel(a.round_index) + " · " : "";
  const card = el("div", "approval-card" + (isDecision ? " end" : ""));

  const head = el("div", "h", icon + " " + prefix + esc(a.title || t("needHuman")));
  card.appendChild(head);
  if (a.message) card.appendChild(el("div", "reason", esc(a.message)));

  // quick-answer buttons (e.g. 是 / 否) — one click continues with that answer.
  const answers = a.answers || [];
  if (answers.length) {
    const arow = el("div", "answer-row");
    answers.forEach((ans) => {
      const b = el("button", "answer-btn", esc(ans));
      b.addEventListener("click", () => resolveApproval(a.approval_id, "continue", ans));
      arow.appendChild(b);
    });
    card.appendChild(arow);
  }

  if (a.allow_input !== false) {
    const ta = el("textarea");
    ta.dataset.appr = a.approval_id;
    ta.placeholder = a.input_label || t("inputOptional");
    card.appendChild(ta);
  }

  const row = el("div", "btn-row");
  const options = (a.options && a.options.length)
    ? a.options
    : [{ value: "continue", label: t("optContinue"), style: "primary" }];
  options.forEach((opt) => {
    const cls = "btn " + (opt.style === "danger" ? "danger" : opt.style === "primary" ? "approve" : "ghost");
    const b = el("button", cls, esc(optionLabel(opt)));
    b.addEventListener("click", () => resolveApproval(a.approval_id, opt.value));
    row.appendChild(b);
  });
  card.appendChild(row);
  return card;
}

async function resolveApproval(id, action, answerOverride) {
  const ta = document.querySelector('textarea[data-appr="' + id + '"]');
  // A quick-answer button passes its answer directly; otherwise use the textarea.
  const userInput = answerOverride != null ? answerOverride : (ta ? ta.value : "");
  await fetch("/api/approvals/" + id + "/resolve", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, user_input: userInput }),
  });
  STREAM_SIG = null;
  fetchState();
}

function triggerLabel(trigger) {
  const key = "tg_" + trigger;
  const table = I18N[LANG] || I18N.en;
  return table[key] != null ? table[key] : t("recHuman");
}

// A resolved human interaction, saved as a read-only record in the timeline:
// what was asked and what you chose / answered.
function renderApprovalRecord(a) {
  const trigger = (a.context && a.context.trigger) || "";
  const card = el("div", "approval-record");
  const tag = triggerLabel(trigger);
  const answer = (a.user_input || "").trim();
  const stopped = a.action === "stop";
  const question = (a.context && a.context.question) || "";

  let html = '<div class="rec-head">' +
    '<span class="rec-tag">✓ ' + esc(tag) + "</span>" +
    (a.round_index ? '<span class="rec-round">' + esc(roundLabel(a.round_index)) + "</span>" : "") +
    "</div>";
  const prompt = question || a.message || "";
  if (prompt) html += '<div class="rec-q">' + esc(prompt) + "</div>";

  if (trigger === "needs_input") {
    // an "ask" gate: the answer is the point.
    html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("yourAnswer")) + "</span>" +
      esc(answer || (stopped ? t("stopRun") : t("continueParen"))) + "</div>";
  } else {
    html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("yourChoice")) + "</span>" +
      (stopped ? t("endRun") : t("continueRun")) + "</div>";
    if (answer) html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("extraInstr")) + "</span>" + esc(answer) + "</div>";
  }
  card.innerHTML = html;
  return card;
}

// ---------- scroll helpers ----------
function scroller() { return document.querySelector(".stream"); }
function isNearBottom() {
  const s = scroller();
  return s ? s.scrollHeight - s.scrollTop - s.clientHeight < 120 : true;
}
// While we (or a layout change we caused) move the scroll position, scroll
// events must NOT be read as "the user scrolled up". Any scrollToBottom() opens
// a short ignore window; scroll events inside it only update bookkeeping. This
// is what makes long text robust: when an expanded long step collapses (it is
// no longer the last step) the container shrinks and the browser clamps
// scrollTop downward, firing a "scrollTop decreased" event that would otherwise
// be misread as a manual scroll-up and disable auto-follow forever.
let _ignoreScrollUntil = 0;
function scrollToBottom() {
  const s = scroller();
  if (!s) return;
  _ignoreScrollUntil = performance.now() + 250;
  s.scrollTop = s.scrollHeight;
}
// Pin to the bottom now and again across the next few frames/ticks, so late
// layout (fold-open animation, reflow after fonts/images) can't leave us a few
// pixels short. Only runs while the user is still following the bottom.
function followBottom() {
  if (!STICK_BOTTOM) return;
  scrollToBottom();
  requestAnimationFrame(() => { if (STICK_BOTTOM) scrollToBottom(); });
  setTimeout(() => { if (STICK_BOTTOM) scrollToBottom(); }, 60);
  setTimeout(() => { if (STICK_BOTTOM) scrollToBottom(); }, 300);
}
// Keep the timeline pinned to the bottom while content keeps growing AFTER a
// render: late-loading images, fold-open animations (grid-template-rows 0->1fr
// over ~0.24s), and streamed trajectory steps all change height asynchronously.
// A single ResizeObserver on the timeline content re-scrolls on every size
// change while the user is still following the bottom (STICK_BOTTOM), which a
// one-shot scrollToBottom() after render cannot catch.
let _stickObserver = null;
function ensureStickObserver() {
  if (_stickObserver || typeof ResizeObserver === "undefined") return;
  const inner = $("streamInner");
  if (!inner) return;
  _stickObserver = new ResizeObserver(() => {
    if (STICK_BOTTOM) scrollToBottom();
  });
  _stickObserver.observe(inner);
  // Also observe the scroll container itself (viewport resizes).
  const s = scroller();
  if (s) _stickObserver.observe(s);
}

// ---------- right detail panel (inline artifacts, squeezes the timeline) ----------
function highlightDetailBtn() {
  const key = DRAWER ? DRAWER.round + ":art" : null;
  document.querySelectorAll(".round-actions .rbtn").forEach((b) => {
    b.classList.toggle("on", !!key && b.dataset.detail === key);
  });
}

function openDrawer(round) {
  // Toggle off if the same round's artifacts panel is already open.
  if (DRAWER && DRAWER.round === round) { closeDrawer(); return; }
  DRAWER = { round };
  document.querySelector(".app").classList.add("detail-open");
  $("detailTitle").textContent = "Round " + round + t("artifactsSuffix");
  $("detailTabs").innerHTML = "";
  highlightDetailBtn();
  loadDrawerArtifacts();
}
function closeDrawer() {
  DRAWER = null;
  document.querySelector(".app").classList.remove("detail-open");
  highlightDetailBtn();
}

async function loadDrawerArtifacts() {
  const body = $("detailBody");
  body.innerHTML = '<div class="chips" id="artChips"></div><pre class="block" id="artView">' + esc(t("clickToView")) + "</pre>";
  try {
    const res = await fetch("/api/round/" + DRAWER.round);
    const data = await res.json();
    const chips = $("artChips");
    (data.artifacts || []).forEach((name) => {
      const chip = el("button", "chip", esc(name));
      chip.addEventListener("click", () => {
        document.querySelectorAll("#artChips .chip").forEach((c) => c.classList.remove("on"));
        chip.classList.add("on");
        viewArtifact(DRAWER.round, name);
      });
      chips.appendChild(chip);
    });
    if (!(data.artifacts || []).length) chips.innerHTML = '<span class="empty">' + esc(t("noArtifacts")) + "</span>";
  } catch (e) { body.innerHTML = '<p class="empty">' + esc(t("readFailArtifacts")) + "</p>"; }
}

async function viewArtifact(round, name) {
  const view = $("artView");
  view.textContent = t("loading");
  try {
    const res = await fetch("/api/round/" + round + "/" + encodeURIComponent(name));
    view.textContent = await res.text();
  } catch (e) { view.textContent = t("readFail"); }
}

// ---------- injection ----------
async function submitInject() {
  const ta = $("injectText");
  const text = ta.value.trim();
  if (!text) return;
  await fetch("/api/inject", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instructions: text }),
  });
  ta.value = "";
  autosize(ta);
  fetchState();
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

// ---------- lightbox ----------
function openLightbox(src) { $("lightboxImg").src = src; $("lightbox").classList.add("open"); }
function closeLightbox() { $("lightbox").classList.remove("open"); $("lightboxImg").src = ""; }

// ---------- language + theme toggles ----------
function toggleLang() {
  LANG = LANG === "en" ? "zh" : "en";
  try { localStorage.setItem("lh_lang", LANG); } catch (e) {}
  applyStaticI18n();
  STREAM_SIG = null;
  if (STATE) render();
}
function cycleTheme() {
  const order = ["system", "light", "dark"];
  const cur = loadThemeMode();
  const next = order[(order.indexOf(cur) + 1) % order.length];
  applyThemeMode(next);
  $("themeBtn").textContent = themeIcon(next);
  $("themeBtn").title = themeTitle(next);
}

// ---------- wiring ----------
loadLang();
applyThemeMode(loadThemeMode());
applyStaticI18n();

$("detailClose").addEventListener("click", closeDrawer);
$("injectBtn").addEventListener("click", submitInject);
$("lightbox").addEventListener("click", closeLightbox);
$("langBtn").addEventListener("click", toggleLang);
$("themeBtn").addEventListener("click", cycleTheme);
$("injectText").addEventListener("input", (e) => autosize(e.target));
$("injectText").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitInject(); }
});

// Track whether the user is following the bottom.
//
// The subtle bug this guards against: a large screenshot finishing loading
// grows scrollHeight, which makes the distance-from-bottom jump. If we decided
// "not following" from that distance alone (isNearBottom()), a real image would
// silently disable auto-follow forever — exactly the "real images break it"
// symptom. So we only RELEASE follow when scrollTop actually DECREASES (a
// genuine upward move by the user); content growth and image loads never
// decrease scrollTop. Returning to the bottom re-enables following.
(function wireStickScroll() {
  const s = scroller();
  if (!s) return;
  let last = s.scrollTop;
  s.addEventListener("scroll", () => {
    const st = s.scrollTop;
    // Ignore scroll events caused by our own scrollToBottom() or the layout
    // clamp that follows a shrink (collapsing a long step). These are not user
    // intent; reading them as "scrolled up" is what broke long-text runs.
    if (performance.now() < _ignoreScrollUntil) { last = st; return; }
    if (st < last - 2) {
      STICK_BOTTOM = false;            // genuine user scroll-up to read history
    } else if (isNearBottom()) {
      STICK_BOTTOM = true;             // back at the bottom -> resume following
    }
    last = st;
  }, { passive: true });
  // Explicit "read history" gestures release follow immediately, independent of
  // any layout thrash from late-loading images / long text.
  s.addEventListener("wheel", (e) => { if (e.deltaY < 0) STICK_BOTTOM = false; }, { passive: true });
  s.addEventListener("touchmove", () => { if (!isNearBottom()) STICK_BOTTOM = false; }, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (["PageUp", "ArrowUp", "Home"].includes(e.key) && !isNearBottom()) STICK_BOTTOM = false;
  });
})();

setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString(); }, 1000);
setInterval(fetchState, 2000);
fetchState();
