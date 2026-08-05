"use strict";

// LongHorizon-Harness Dashboard — Codex-style serial timeline front-end.
// The harness is strictly serial, so the run renders as one top-to-bottom
// stream: task prompt -> per-round (manage / execute / audit) events.
// A right drawer shows full trajectories and raw artifacts on demand.
//
// The UI is bilingual (English default + Chinese) and supports light/dark
// themes (following the system by default). Both preferences persist in
// localStorage. Task prompts, model output, and artifact content stay verbatim.

// ---------- i18n ----------
const I18N = {
  en: {
    appTitle: "LongHorizon-Harness",
    pageTitle: "LongHorizon-Harness Dashboard",
    runsCap: "Runs",
    statusLoading: "Loading…",
    connFail: "Connection failed",
    statusPrefix: "Status: ",
    running: "Running",
    roundZero: "Round 0",
    roundPrefix: "Round ",
    runsEmpty: "Single run only (runs browsing disabled).",
    activityLead: "Live",
    overviewTitle: "Run overview",
    overviewStatus: "Status",
    overviewProgress: "Progress",
    overviewActive: "Now",
    overviewAudit: "Latest audit",
    overviewState: "Current verified state",
    overviewEvidence: "Latest audit evidence",
    overviewNoState: "No verified state has been recorded yet.",
    overviewNoAudit: "Waiting for the first audit result.",
    overviewIdle: "Waiting for the next role",
    placeholderTimeline: "Select a run on the left to view its serial Manager → Executor → Auditor timeline.",
    placeholderSelect: "Select a run on the left to view its serial timeline.",
    composerPlaceholder: "Inject an extra instruction for later models (non-blocking)…",
    composerHint: "Instructions are injected before the next management round, taking priority over the automatic flow.",
    detailTitle: "Details",
    artifactsSuffix: " · Artifacts",
    foldChildren: "Fold children",
    artifacts: "Artifacts",
    runningBadge: "Running",
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
    doingRepair: "Repairing the auditor report control headers",
    auditLabel: "Audit",
    taskContract: "Task Contract",
    auditReport: "Authoritative Audit Report",
    harnessFeedback: "Harness Feedback",
    languageTitle: "Switch language",
    sendInstruction: "Send instruction",
    closeDetails: "Close details",
    managerQuestion: "Manager question:",
    answerYes: "Yes",
    answerNo: "No",
    gateTitle_completed: "Task complete. Continue the run?",
    gateMessage_completed: "The Manager confirmed task completion. Continue to add rounds and instructions, or end this run.",
    gateTitle_max_rounds: "Round limit reached. Continue the run?",
    gateMessage_max_rounds: "The configured round budget is exhausted before completion. Continue to add rounds, or end this run.",
    gateTitle_needs_input: "Manager needs your decision",
    gateMessage_needs_input: "The Manager needs input before it can continue. Answer below and continue, or stop this run.",
    gateTitle_needs_human: "Task blocked; operator input required",
    gateMessage_needs_human: "The Manager cannot proceed automatically. Add instructions and continue, or stop this run.",
    gateTitle_repeated_failure: "Repeated failures require operator input",
    gateMessage_repeated_failure: "Several consecutive rounds failed to make progress. Add instructions and continue, or stop this run.",
    gateTitle_computer_use_plugin: "Codex Computer Use requires approval",
    gateMessage_computer_use_plugin: "The Codex GUI executor needs the official computer-use@openai-bundled plugin. Approve the requested installation or enablement to continue; no model process will start before this check passes.",
    gateInput_default: "Optional: add instructions for the next Manager round",
    gateInput_needs_input: "Your answer, injected into the next Manager round",
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
    installContinue: "Install and continue",
    enableContinue: "Enable and continue",
    cancelTask: "Cancel task",
    pluginInstalling: "Installing official plugin…",
    pluginEnabling: "Enabling official plugin…",
    pluginReady: "Plugin installed and enabled",
    pluginFailed: "Plugin setup failed",
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
    ev_role_harness_cancelled: "Run cancelled",
    ev_human_continue_after_finish: "Run continued by operator",
    status_complete: "Complete",
    status_incomplete: "Incomplete",
    status_blocked: "Blocked",
    status_cancelled: "Cancelled",
    status_done: "Done",
    status_error: "Error",
    status_timeout: "Timed out",
    badge_gui: "GUI",
    badge_cli: "CLI",
    badge_done: "Done",
    badge_blocked: "Blocked",
    badge_invalid: "Invalid",
    badge_ask: "Input needed",
    badge_complete: "Complete",
    badge_incomplete: "Incomplete",
    badge_clean: "Clean",
    badge_suspect: "Suspect",
    badge_violation: "Violation",
    badge_aligned: "Aligned",
    badge_unknown: "Unknown",
    badge_needs_revision: "Needs revision",
    badge_cancelled: "Cancelled",
    badge_error: "Error",
    badge_timeout: "Timed out",
    metaModel: "model",
    metaTools: "tools",
    metaThread: "thread",
    metaMcp: "MCP",
    metaTurns: "turns",
    metaDuration: "duration",
    metaCost: "cost",
    metaInputTokens: "input tokens",
    metaCachedInputTokens: "cached input tokens",
    metaOutputTokens: "output tokens",
    tg_completed: "Run completion",
    tg_max_rounds: "Round limit",
    tg_needs_input: "Manager query",
    tg_needs_human: "Blocked intervention",
    tg_repeated_failure: "Repeated-failure intervention",
    tg_computer_use_plugin: "GUI plugin authorization",
    langLabel: "EN",
    themeSystem: "System",
    themeLight: "Light",
    themeDark: "Dark",
  },
  zh: {
    appTitle: "LongHorizon-Harness",
    pageTitle: "LongHorizon-Harness Dashboard",
    runsCap: "运行记录",
    statusLoading: "加载中…",
    connFail: "连接失败",
    statusPrefix: "状态：",
    running: "运行中",
    roundZero: "第 0 轮",
    roundPrefix: "轮次 ",
    runsEmpty: "当前只有单个运行（未启用 runs 浏览）。",
    activityLead: "实时",
    overviewTitle: "运行概览",
    overviewStatus: "状态",
    overviewProgress: "进度",
    overviewActive: "当前阶段",
    overviewAudit: "最近审计",
    overviewState: "当前可信状态",
    overviewEvidence: "最近审计证据",
    overviewNoState: "尚未记录可信任务状态。",
    overviewNoAudit: "正在等待首个审计结果。",
    overviewIdle: "等待下一角色",
    placeholderTimeline: "选择左侧任一运行记录，查看串行的任务管理 → 执行 → 审计时间线。",
    placeholderSelect: "选择左侧任一运行记录，查看串行时间线。",
    composerPlaceholder: "向后续模型注入补充指令（不阻塞）…",
    composerHint: "指令会在下一轮任务管理前注入，优先级高于自动流程。",
    detailTitle: "详情",
    artifactsSuffix: " · 产物文件",
    foldChildren: "折叠子级",
    artifacts: "产物文件",
    runningBadge: "运行中",
    loadingTraj: "加载轨迹…",
    noTraj: "无轨迹步骤",
    roleManager: "Manager",
    roleAuditor: "Auditor",
    roleFormatRepair: "格式修复",
    roleExecGui: "GUI Executor",
    roleExecCli: "CLI Executor",
    roleExec: "Executor",
    doingManager: "正在规划本轮子任务与路由",
    doingExec: "正在执行子任务并生成产出",
    doingAudit: "正在审计产出的真实性与完整性",
    doingRepair: "正在修复 Auditor 报告的控制头",
    auditLabel: "Auditor",
    taskContract: "任务契约",
    auditReport: "权威审计报告",
    harnessFeedback: "Harness 反馈",
    languageTitle: "切换语言",
    sendInstruction: "发送指令",
    closeDetails: "关闭详情",
    managerQuestion: "Manager 的问题：",
    answerYes: "是",
    answerNo: "否",
    gateTitle_completed: "任务已完成，是否继续运行？",
    gateMessage_completed: "Manager 已确认任务完成。可继续追加轮次和指令，或结束本次运行。",
    gateTitle_max_rounds: "已到达轮次上限，是否继续运行？",
    gateMessage_max_rounds: "任务尚未完成，但预设轮次预算已用完。可继续追加轮次，或结束本次运行。",
    gateTitle_needs_input: "Manager 需要你的决定",
    gateMessage_needs_input: "Manager 需要输入才能继续。请在下方回答并继续，或终止本次运行。",
    gateTitle_needs_human: "任务已阻塞，需要人工介入",
    gateMessage_needs_human: "Manager 当前无法自动推进。请补充指令后继续，或终止本次运行。",
    gateTitle_repeated_failure: "连续失败，需要人工介入",
    gateMessage_repeated_failure: "连续多轮未取得进展。请补充指令后继续，或终止本次运行。",
    gateTitle_computer_use_plugin: "Codex Computer Use 插件需要授权",
    gateMessage_computer_use_plugin: "Codex GUI 执行器需要官方 computer-use@openai-bundled 插件。允许安装或启用后才能继续；本项检查通过前不会启动任何模型进程。",
    gateInput_default: "可选：补充指令，将注入下一轮 Manager",
    gateInput_needs_input: "你的回答，将注入下一轮 Manager",
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
    installContinue: "安装并继续",
    enableContinue: "启用并继续",
    cancelTask: "取消任务",
    pluginInstalling: "正在安装官方插件…",
    pluginEnabling: "正在启用官方插件…",
    pluginReady: "插件已安装并启用",
    pluginFailed: "插件配置失败",
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
    ev_role_harness_cancelled: "任务已取消",
    ev_human_continue_after_finish: "操作员继续运行",
    status_complete: "已完成",
    status_incomplete: "未完成",
    status_blocked: "已阻塞",
    status_cancelled: "已取消",
    status_done: "已完成",
    status_error: "错误",
    status_timeout: "超时",
    badge_gui: "GUI",
    badge_cli: "CLI",
    badge_done: "完成",
    badge_blocked: "阻塞",
    badge_invalid: "无效",
    badge_ask: "需要输入",
    badge_complete: "完成",
    badge_incomplete: "未完成",
    badge_clean: "正常",
    badge_suspect: "可疑",
    badge_violation: "违规",
    badge_aligned: "已对齐",
    badge_unknown: "未知",
    badge_needs_revision: "需修订",
    badge_cancelled: "已取消",
    badge_error: "错误",
    badge_timeout: "超时",
    metaModel: "model",
    metaTools: "工具",
    metaThread: "thread",
    metaMcp: "MCP",
    metaTurns: "轮次",
    metaDuration: "耗时",
    metaCost: "费用",
    metaInputTokens: "输入 token",
    metaCachedInputTokens: "缓存输入 token",
    metaOutputTokens: "输出 token",
    tg_completed: "运行完成确认",
    tg_max_rounds: "轮次上限确认",
    tg_needs_input: "任务管理器请示",
    tg_needs_human: "阻塞介入",
    tg_repeated_failure: "失败介入",
    tg_computer_use_plugin: "GUI 插件授权",
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
function hasTranslation(key) {
  return I18N.en[key] != null;
}
function statusLabel(status) {
  const key = "status_" + String(status || "").toLowerCase();
  return hasTranslation(key) ? t(key) : String(status || "");
}
function badgeLabel(value) {
  const key = "badge_" + String(value || "").toLowerCase();
  return hasTranslation(key) ? t(key) : String(value || "");
}
function eventRoleLabel(role) {
  if (role === "manager") return t("roleManager");
  if (role === "executor") return t("roleExec");
  if (role === "auditor") return t("roleAuditor");
  if (role === "auditor_format_repair") return t("roleFormatRepair");
  if (role === "gui") return t("roleExecGui");
  if (role === "cli") return t("roleExecCli");
  return role || "";
}
function gateText(a, field) {
  const trigger = (a.context && a.context.trigger) || "";
  const key = "gate" + field + "_" + trigger;
  return hasTranslation(key) ? t(key) : String(a[field.toLowerCase()] || "");
}
function approvalMessage(a) {
  const trigger = (a.context && a.context.trigger) || "";
  const base = gateText(a, "Message");
  const question = String((a.context && a.context.question) || "").trim();
  if (trigger === "needs_input" && question) return base + "\n\n" + t("managerQuestion") + "\n" + question;
  return base;
}
function approvalInputLabel(a) {
  const trigger = (a.context && a.context.trigger) || "";
  if (trigger === "needs_input") return t("gateInput_needs_input");
  if (hasTranslation("gateTitle_" + trigger)) return t("gateInput_default");
  return a.input_label || t("inputOptional");
}
function quickAnswerLabel(answer) {
  const normalized = String(answer || "").trim().toLowerCase();
  if (["yes", "y", "是"].includes(normalized)) return t("answerYes");
  if (["no", "n", "否"].includes(normalized)) return t("answerNo");
  return answer;
}
// Round-aware label: "Round 3" (en) / "第 3 轮" (zh)
function roundLabel(n) {
  return LANG === "zh" ? "第 " + n + " 轮" : "Round " + n;
}
function formatTime(value) {
  return value.toLocaleTimeString(LANG === "zh" ? "zh-CN" : "en-US");
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
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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
  document.title = t("pageTitle");
  document.querySelectorAll("[data-i18n]").forEach((n) => { n.textContent = t(n.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((n) => { n.setAttribute("placeholder", t(n.dataset.i18nPh)); });
  document.querySelectorAll("[data-i18n-title]").forEach((n) => { n.setAttribute("title", t(n.dataset.i18nTitle)); });
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
  renderControls();
  highlightDetailBtn();
}

function renderControls() {
  const composer = document.querySelector(".composer");
  if (composer) composer.style.display = STATE.control_enabled ? "" : "none";
}

function renderHead() {
  const report = STATE.report || {};
  $("title").textContent = STATE.task ? shorten(STATE.task, 70) : t("appTitle");
  $("title").title = STATE.task || "";
  const pill = $("statusPill");
  const status = report.status || "";
  pill.textContent = t("statusPrefix") + (status ? statusLabel(status) : t("running"));
  pill.className = "pill " + statusClass(report.status);
  $("roundPill").textContent = roundLabel(STATE.round_count || 0);
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
      (r.status ? '<div class="rst"><span class="badge ' + esc(r.status) + '">' + esc(statusLabel(r.status)) + "</span></div>" : "");
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
  const rnd = ev.round != null ? " · " + roundLabel(ev.round) : "";
  const role = ev.role ? " · " + eventRoleLabel(ev.role) : "";
  const time = ev.ts ? formatTime(new Date(ev.ts * 1000)) : "";
  $("activityNow").innerHTML = '<span class="t">' + esc(time) + "</span>" + esc(label + rnd + role);
}

// ---------- serial timeline ----------
const STEP_BADGE = { gui: "gui", cli: "cli", done: "done", blocked: "blocked", invalid: "invalid", ask: "ask" };

function auditorHeads(report) {
  // Parse the same bilingual three-line control header accepted by the harness.
  const out = {};
  (report || "").split("\n").slice(0, 4).forEach((ln) => {
    let m = ln.match(/^\s*(?:\*\*)?\s*(?:状态|status)\s*[:：]\s*(complete|incomplete|blocked|完成|未完成|阻塞)\s*(?:\*\*)?\s*$/i);
    if (m) out.status = ({ "完成": "complete", "未完成": "incomplete", "阻塞": "blocked" })[m[1]] || m[1].toLowerCase();
    m = ln.match(/^\s*(?:\*\*)?\s*(?:完整性|integrity)\s*[:：]\s*(clean|suspect|violation)\s*(?:\*\*)?\s*$/i);
    if (m) out.integrity = m[1].toLowerCase();
    m = ln.match(/^\s*(?:\*\*)?\s*(?:契约审计|contract(?:[_\s-]*audit)?)\s*[:：]\s*(aligned|unknown|needs[_\s-]*revision|invalid|对齐|未知|需修订|需要修订|无效)\s*(?:\*\*)?\s*$/i);
    if (m) out.contract = ({ "对齐": "aligned", "未知": "unknown", "需修订": "needs_revision", "需要修订": "needs_revision", "无效": "invalid" })[m[1]] || m[1].toLowerCase().replace(/[\s-]+/g, "_");
  });
  return out;
}

function streamSignature() {
  const rounds = STATE.rounds || [];
  const report = STATE.report || {};
  const events = STATE.events || [];
  const parts = rounds.map((r) =>
    r.round_index + ":" + (r.in_progress ? "L" : "F") + ":" +
    (r.plan_text || "").length + "/" + (r.executor_output || "").length + "/" +
    (r.auditor_report || "").length + "/" + (r.harness_feedback || "").length + "/" +
    (r.task_contract || "").length + "/" + JSON.stringify(r.manager_status || {}) + "/" +
    JSON.stringify(r.executor_status || {}) + "/" + JSON.stringify(r.auditor_status || {}) +
    "/" + String(r.active_role) +
    "/" + (r.roles || []).join(",") +
    // include live trajectory byte sizes so streamed steps trigger a re-render
    "/" + JSON.stringify(r.role_sizes || {})
  );
  const appr = (STATE.approvals || []).map((a) =>
    a.approval_id + ":" + a.status + ":" + String((a.context && a.context.plugin_status) || "")
  ).join(",");
  const lastEvent = events.length ? JSON.stringify(events[events.length - 1]) : "";
  return LANG + "|" + (STATE.current_run || "") + "|" + parts.join("|") +
    "|status:" + String(report.status || "") +
    "|state:" + String(report.current_task_state || "") +
    "|event:" + lastEvent + "|appr:" + appr;
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

  const overview = renderOverviewCard(rounds);
  if (overview) host.appendChild(overview);

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

  approvals.filter((a) => a.status === "pending").forEach((a) =>
    host.appendChild(renderApproval(a, !!STATE.control_enabled)));

  if (atBottom) followBottom();
  // Content (images, fold-open animations, streamed steps) keeps growing after
  // this render; a ResizeObserver re-pins the bottom as it does (see below).
  ensureStickObserver();
}

function effectiveRoundLimit() {
  const report = (STATE && STATE.report) || {};
  const events = (STATE && STATE.events) || [];
  let limit = Number(report.max_rounds || 0);
  events.forEach((event) => {
    if (event.event === "role_harness_start") {
      limit = Math.max(limit, Number(event.max_rounds || 0));
    } else if (event.event === "human_continue_after_finish") {
      limit = Math.max(limit, Number(event.round || 0) + Number(event.extra_rounds || 0));
    }
  });
  return limit;
}

function latestAuditRound(rounds) {
  for (let i = rounds.length - 1; i >= 0; i -= 1) {
    if (rounds[i].auditor_report) return rounds[i];
  }
  return null;
}

function auditEvidenceSummary(report) {
  const body = [];
  let controlHeaders = 0;
  String(report || "").split("\n").forEach((line) => {
    const value = line.trim();
    if (!value) return;
    if (controlHeaders < 3 && /^(?:\*\*)?\s*(?:状态|status|完整性|integrity|契约审计|contract(?:[_\s-]*audit)?)\s*[:：]/i.test(value)) {
      controlHeaders += 1;
      return;
    }
    body.push(value);
  });
  return body.join(" ");
}

function overviewMetric(label, value, className) {
  return '<div class="overview-metric' + (className ? " " + className : "") + '">' +
    '<span class="overview-metric-label">' + esc(label) + '</span>' +
    '<span class="overview-metric-value">' + esc(value) + "</span></div>";
}

function renderOverviewCard(rounds) {
  if (!STATE) return null;
  const report = STATE.report || {};
  const last = rounds.length ? rounds[rounds.length - 1] : null;
  const auditRound = latestAuditRound(rounds);
  const audit = auditorHeads(auditRound && auditRound.auditor_report);
  const status = report.status || "";
  const limit = effectiveRoundLimit();
  const currentRound = Number(STATE.round_count || (last && last.round_index) || 0);
  const active = last && activeRole(last);
  const events = STATE.events || [];
  const latestEvent = events.length ? eventLabel(events[events.length - 1].event) : t("overviewIdle");
  const progress = limit ? currentRound + " / " + limit : String(currentRound);
  const activeText = active ? active.label + " · " + active.doing : latestEvent;
  const auditText = auditRound
    ? [audit.status && statusLabel(audit.status), audit.integrity && badgeLabel(audit.integrity), audit.contract && badgeLabel(audit.contract)].filter(Boolean).join(" · ")
    : t("overviewNoAudit");
  const stateText = report.current_task_state || (last && last.task_state) || t("overviewNoState");
  const evidenceText = auditRound ? auditEvidenceSummary(auditRound.auditor_report) : t("overviewNoAudit");

  const card = el("section", "overview-card");
  card.innerHTML =
    '<div class="overview-head"><span class="overview-title">' + esc(t("overviewTitle")) + '</span></div>' +
    '<div class="overview-grid">' +
      overviewMetric(t("overviewStatus"), status ? statusLabel(status) : t("running"), statusClass(status)) +
      overviewMetric(t("overviewProgress"), progress, "") +
      overviewMetric(t("overviewActive"), shorten(activeText, 72), active ? "live" : "") +
      overviewMetric(t("overviewAudit"), shorten(auditText, 72), audit.status || "") +
    '</div>' +
    '<div class="overview-notes">' +
      '<div class="overview-note"><span>' + esc(t("overviewState")) + '</span><p title="' + esc(shorten(stateText, 1200)) + '">' + esc(shorten(stateText, 260)) + '</p></div>' +
      '<div class="overview-note"><span>' + esc(t("overviewEvidence")) + '</span><p title="' + esc(shorten(evidenceText, 1200)) + '">' + esc(shorten(evidenceText, 260)) + '</p></div>' +
    '</div>';
  return card;
}

// The role that is currently working in an in-progress round (its result text
// has not been written to disk yet). Drives the live "thinking" indicator.
function activeRole(r) {
  if (!r.in_progress) return null;
  if (Object.prototype.hasOwnProperty.call(r, "active_role")) {
    if (r.active_role === "manager") return { label: t("roleManager"), doing: t("doingManager") };
    if (r.active_role === "executor") {
      return { label: r.next_step === "gui" ? t("roleExecGui") : r.next_step === "cli" ? t("roleExecCli") : t("roleExec"), doing: t("doingExec") };
    }
    if (r.active_role === "auditor") return { label: t("auditLabel"), doing: t("doingAudit") };
    if (r.active_role === "auditor_format_repair") return { label: t("roleFormatRepair"), doing: t("doingRepair") };
    return null;
  }
  if (!r.plan_text) return { label: t("roleManager"), doing: t("doingManager") };
  if ((r.next_step === "gui" || r.next_step === "cli") && !r.executor_output) {
    return { label: r.next_step === "gui" ? t("roleExecGui") : t("roleExecCli"), doing: t("doingExec") };
  }
  if ((r.roles || []).includes("auditor_format_repair") && !r.auditor_report) {
    return { label: t("roleFormatRepair"), doing: t("doingRepair") };
  }
  if (r.executor_output && !r.auditor_report && r.next_step !== "done") {
    return { label: t("auditLabel"), doing: t("doingAudit") };
  }
  if (r.next_step === "done" && !r.auditor_report) return null;
  return null;
}

const ROLE_ORDER = ["manager", "executor", "auditor", "auditor_format_repair"];

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
  const status = role === "manager"
    ? r.manager_status
    : role === "executor"
      ? r.executor_status
      : role === "auditor_format_repair"
        ? ((r.auditor_status || {}).format_repair_status || {})
        : r.auditor_status;
  const episode = status && status.status
    ? '<span class="badge ' + esc(status.status) + '">' + esc(statusLabel(status.status)) + "</span>"
    : "";
  if (role === "executor") {
    const route = r.next_step ? '<span class="badge ' + (STEP_BADGE[r.next_step] || "") + '">' + esc(badgeLabel(r.next_step)) + "</span>" : "";
    return route + episode;
  }
  if (role === "auditor") {
    const vh = auditorHeads(r.auditor_report);
    return [
      vh.status ? '<span class="badge ' + vh.status + '">' + esc(badgeLabel(vh.status)) + "</span>" : "",
      vh.integrity ? '<span class="badge ' + vh.integrity + '">' + esc(badgeLabel(vh.integrity)) + "</span>" : "",
      vh.contract ? '<span class="badge ' + vh.contract + '">' + esc(badgeLabel(vh.contract)) + "</span>" : "",
      episode,
    ].join(" ");
  }
  return episode;
}

// The whole round folds via its header, for quick jumping between rounds.
function renderRound(r, isLastRound) {
  const group = el("div", "round-group");
  const roundKey = "round:" + r.round_index;
  group.dataset.foldkey = roundKey;
  const open = isOpen(roundKey, true);
  if (open) group.classList.add("open");
  const stepBadge = r.next_step ? '<span class="badge ' + (STEP_BADGE[r.next_step] || "") + '">' + esc(badgeLabel(r.next_step)) + "</span>" : "";
  const liveBadge = r.in_progress ? '<span class="badge live"><span class="live-dot"></span>' + esc(t("runningBadge")) + "</span>" : "";

  const rule = el("div", "round-rule",
    '<span class="chev">▶</span><span class="lbl">' + esc(roundLabel(r.round_index)) + "</span>" + stepBadge + liveBadge);
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

  if (r.task_contract) {
    body.appendChild(foldBlock(
      r.round_index + ":contract", "contract", t("taskContract"), brief(r.task_contract),
      '<div class="tp">' + esc(r.task_contract) + "</div>", false));
  }
  if (r.auditor_report) {
    body.appendChild(foldBlock(
      r.round_index + ":audit-report", "report", t("auditReport"), brief(r.auditor_report),
      '<div class="tp">' + esc(r.auditor_report) + "</div>", false));
  }
  if (r.harness_feedback) {
    body.appendChild(foldBlock(
      r.round_index + ":feedback", "err", t("harnessFeedback"), brief(r.harness_feedback),
      '<div class="tp">' + esc(r.harness_feedback) + "</div>", false));
  }

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
  const summaryText = roleResultSummary(r, role);
  if (summaryText) {
    const summary = el("div", "role-summary", esc(shorten(summaryText, 220)));
    summary.title = shorten(summaryText, 1200);
    sec.appendChild(summary);
  }
  sec.appendChild(bodyWrap);
  head.addEventListener("click", (e) => {
    if (e.target.closest(".role-actions")) return;
    const now = !sec.classList.contains("open");
    sec.classList.toggle("open", now);
    setOpen(secKey, now);
  });

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

function roleResultSummary(r, role) {
  if (role === "manager") return r.plan_text || "";
  if (role === "executor") return r.executor_output || "";
  if (role === "auditor") return auditEvidenceSummary(r.auditor_report);
  if (role === "auditor_format_repair") return r.auditor_report || "";
  return "";
}

function brief(t2, n) {
  return shorten(t2 || "", n || 68) || "—";
}

// Turn one parsed trajectory step into { label, sum, body }.
function stepBits(s) {
  if (s.kind === "session") {
    const thread = s.thread_id ? " · " + t("metaThread") + "=" + s.thread_id : "";
    return { label: t("stSession"), sum: t("metaModel") + "=" + (s.model || "") + " · " + t("metaTools") + "=" + (s.tool_count || 0) + thread,
      body: '<div class="meta">' + esc(t("metaModel")) + "=" + esc(s.model || "") + " · " + esc(t("metaMcp")) + "=" + esc((s.mcp_servers || []).join(",")) + " · " + esc(t("metaTools")) + "=" + (s.tool_count || 0) + esc(thread) + "</div>" };
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
    // Run metadata differs per agent backend (Claude reports turns/cost, Codex
    // does not), so only render the fields this step actually carries.
    const meta = [];
    if (s.num_turns != null) meta.push(t("metaTurns") + "=" + s.num_turns);
    if (s.duration_ms != null) meta.push(t("metaDuration") + "=" + s.duration_ms + "ms");
    if (s.cost_usd != null) meta.push(t("metaCost") + "=$" + s.cost_usd);
    if (s.input_tokens != null) meta.push(t("metaInputTokens") + "=" + s.input_tokens);
    if (s.cached_input_tokens != null) meta.push(t("metaCachedInputTokens") + "=" + s.cached_input_tokens);
    if (s.output_tokens != null) meta.push(t("metaOutputTokens") + "=" + s.output_tokens);
    return { label: t("stFinalResult"), sum: brief(s.text),
      body: '<div class="tp">' + esc(s.text) + "</div>" +
        (meta.length ? '<div class="meta">' + esc(meta.join(" · ")) + "</div>" : "") };
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
  const thread = s.thread_id ? " · " + t("metaThread") + "=" + s.thread_id : "";
  return el("div", "tstep session static",
    '<div class="tstep-head static"><span class="tk">' + esc(t("stSession")) + '</span>' +
    '<span class="tsum">' + esc(t("metaModel")) + "=" + esc(s.model || "") + " · " + esc(t("metaMcp")) + "=" + esc((s.mcp_servers || []).join(",")) +
    " · " + esc(t("metaTools")) + "=" + (s.tool_count || 0) + esc(thread) + "</span></div>");
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

// Render an approval as a question + option buttons. Option buttons are driven
// by `a.options` ([{value,label,style}]); known machine values (continue/stop)
// are localized so the buttons follow the dashboard language. `a.answers`
// (["是","否",...]) are harness-produced quick answers and shown verbatim.
function optionLabel(opt, isDecision) {
  if (opt.value === "continue") return t("continueRun");
  if (opt.value === "stop") return isDecision ? t("endRun") : t("stopRun");
  if (opt.value === "install") return t("installContinue");
  if (opt.value === "enable") return t("enableContinue");
  if (opt.value === "cancel") return t("cancelTask");
  return opt.label || opt.value;
}
function renderApproval(a, interactive = true) {
  const trigger = (a.context && a.context.trigger) || "";
  const isDecision = trigger === "completed" || trigger === "max_rounds";
  const isPluginSetup = trigger === "computer_use_plugin";
  const icon = isPluginSetup ? "🖥" : isDecision ? "⏸" : trigger === "needs_input" ? "❓" : "⚠";
  const prefix = a.round_index ? roundLabel(a.round_index) + " · " : "";
  const card = el("div", "approval-card" + (isDecision || isPluginSetup ? " end" : ""));

  const head = el("div", "h", icon + " " + prefix + esc(gateText(a, "Title") || t("needHuman")));
  card.appendChild(head);
  const message = approvalMessage(a);
  if (message) card.appendChild(el("div", "reason", esc(message)));

  if (!interactive) return card;

  const answers = a.answers || [];
  if (answers.length) {
    const arow = el("div", "answer-row");
    answers.forEach((ans) => {
      const b = el("button", "answer-btn", esc(quickAnswerLabel(ans)));
      b.addEventListener("click", () => resolveApproval(a.approval_id, "continue", ans));
      arow.appendChild(b);
    });
    card.appendChild(arow);
  }

  if (a.allow_input !== false) {
    const ta = el("textarea");
    ta.dataset.appr = a.approval_id;
    ta.placeholder = approvalInputLabel(a);
    card.appendChild(ta);
  }

  const row = el("div", "btn-row");
  const options = (a.options && a.options.length)
    ? a.options
    : [{ value: "continue", label: t("optContinue"), style: "primary" }];
  options.forEach((opt) => {
    const cls = "btn " + (opt.style === "danger" ? "danger" : opt.style === "primary" ? "approve" : "ghost");
    const b = el("button", cls, esc(optionLabel(opt, isDecision)));
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
  const context = a.context || {};
  const card = el("div", "approval-record");
  const tag = triggerLabel(trigger);
  const answer = (a.user_input || "").trim();
  const stopped = a.action === "stop";
  const isDecision = trigger === "completed" || trigger === "max_rounds";
  const isPluginSetup = trigger === "computer_use_plugin";
  const question = (a.context && a.context.question) || "";

  let html = '<div class="rec-head">' +
    '<span class="rec-tag">✓ ' + esc(tag) + "</span>" +
    (a.round_index ? '<span class="rec-round">' + esc(roundLabel(a.round_index)) + "</span>" : "") +
    "</div>";
  const prompt = question || approvalMessage(a);
  if (prompt) html += '<div class="rec-q">' + esc(prompt) + "</div>";

  if (isPluginSetup) {
    const cancelled = a.action === "cancel";
    const actionLabel = a.action === "enable" ? t("enableContinue") :
      a.action === "install" ? t("installContinue") : t("cancelTask");
    html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("yourChoice")) + "</span>" + esc(actionLabel) + "</div>";
    if (!cancelled) {
      const pluginStatus = context.plugin_status || "";
      const statusLabel = pluginStatus === "installing" ? t("pluginInstalling") :
        pluginStatus === "enabling" ? t("pluginEnabling") :
        pluginStatus === "ready" ? t("pluginReady") :
        pluginStatus === "failed" ? t("pluginFailed") : "";
      if (statusLabel) html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("overviewStatus")) + "</span>" + esc(statusLabel) + "</div>";
      if (pluginStatus === "failed" && context.plugin_status_message) {
        html += '<div class="rec-q">' + esc(context.plugin_status_message) + "</div>";
      }
    }
  } else if (trigger === "needs_input") {
    // an "ask" gate: the answer is the point.
    html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("yourAnswer")) + "</span>" +
      esc(answer || (stopped ? t("stopRun") : t("continueParen"))) + "</div>";
  } else {
    html += '<div class="rec-ans"><span class="rec-lbl">' + esc(t("yourChoice")) + "</span>" +
      (stopped ? (isDecision ? t("endRun") : t("stopRun")) : t("continueRun")) + "</div>";
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
  $("detailTitle").textContent = roundLabel(round) + t("artifactsSuffix");
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

setInterval(() => { $("clock").textContent = formatTime(new Date()); }, 1000);
setInterval(fetchState, 2000);
fetchState();
