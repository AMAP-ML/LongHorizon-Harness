// Kusudaemon web dashboard — DASHBOARD-UX.md implementation.
// Layout: RAIL (34px) / NAV(220px)+STREAM+INSPECTOR(drag-resize 380-840) /
// COMMAND BAR (38px). Design doc sections cited inline as §N references.
// Preserves every backend API hook and the battle-tested mechanics of the
// earlier views (full-teardown render rule §9.4, draft maps in `state`,
// focus/scroll restore, SSE-then-polling, per-node gates/review from
// audit/<node>.json). No build step, no framework — vanilla JS, `node --check`
// is the syntax gate.

/* ========================= PART A ========================= */

const PHASES_ALL = ["classify", "intake", "explore", "survey", "plan", "pilot", "research", "execute", "review", "assemble", "verify"];

// §8 status vocabulary — one glyph per state, same glyph in rail, nav,
// tree, and agent list. Color is never the only signal.
const NODE_GLYPH = {
  pending: "·",
  ready: "○",
  dispatched: "◐",
  awaiting_review: "◑",
  passed: "●",
  failed: "✕",
  blocked: "⊘",
  stale: "◌",
  split: "⑂",
};
const PHASE_GLYPH = {
  in_progress: "▶",
  done: "✓",
  failed: "✕",
  error: "✕",
  awaiting_approval: "⏸",
  halted: "⏸",
  escalated: "⇡",
  stalled: "☠",
  pending: "·",
  created: "·",
};
const SUB_GLYPH = { pending: "·", running: "◐", done: "✓", error: "✕", timeout: "⏱" };
const SHAPE2 = {
  "prose-dominant": "pr",
  "derivation-dominant": "de",
  "problem-set-dominant": "ps",
  "reference-dominant": "re",
};

const GATE_PIP_PASS = "▪";
const GATE_PIP_FAIL = "▫";

const state = {
  snapshot: { attached: false, runs: [], control_enabled: true },
  workbenchTab: "tree",   // 'tree' | 'node' | 'doc' | 'asm' | 'term' — inspector tabs (glyph+word)
  selectedNode: null,
  nodeDetail: null,
  nodeSubagent: null,
  nodeDetailLoading: false,
  agentTab: "overview",   // node sub-tabs: 'overview' | 'chat' | 'gates' | 'artifact' | 'versions' | 'diff'
  nodeDiff: null,
  nodeThinking: null,
  artifactsDetail: null,
  selectedArtifactTag: undefined,
  selectedArtifactText: null,
  mainAgentThinking: null,
  newRunOpen: false,
  busy: false,
  toast: null,
  contractData: { text: "", tokens: 0, ceiling: 1500 },
  specText: "",
  spineText: "",
  manifestLines: null,
  assembly: null,
  promptText: "",          // command-bar text (msg target, amend rule, reopen spec)
  promptMode: "msg_agent", // 'msg_agent' | 'command' | 'amend' | 'reopen' — 'command' when text starts with ">"
  targetAgentId: "main",
  targetAgentManual: false,
  interjectDrafts: {},
  reopenDrafts: {},
  redispatchDrafts: {},
  approvalDrafts: {},
  approvalAnswerDrafts: {},
  pilotDrafts: {},
  newRun: { runId: "", goal: "", source: "", model: "", compile: "", workspace: "", tier: "", dispatch_policy: "model", survey_mode: "model", max_rounds: 100, max_attempts: 3, document_review: false, inline_spans: false },
  // §3/§6/§7/§10 additions
  sseLive: true,
  authRequired: false,
  authToken: "",
  authDraft: "",
  runSwitcherOpen: false,
  navCollapsed: {},
  treeFilter: "",
  treeCollapsed: {},       // folder segment -> collapsed
  contextMenu: null,       // {x, y, nodeId|runId}
  approvalTakeover: false,
  takeoverFor: "",         // approval id the takeover is (or was) for
  triageOpen: {},          // approvalId -> 'clean'|'patchable'|'regenerate' (expanded chip)
  helpOpen: false,
  inspectorWidth: 480,
  docTab: "contract",      // 'spec' | 'contract' | 'spine' | 'manifest'
  terminalFilter: "all",
  lastCliCommand: "",      // §5.5 copyable CLI equivalent of the last UI action
  escalationFlash: false,
};

const root = document.getElementById("app");

/* ------------------------- API transport ------------------------- */
function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (state.authToken) h["Authorization"] = `Bearer ${state.authToken}`;
  return h;
}

async function apiGet(path, opts) {
  const res = await fetch(path, { headers: authHeaders() });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !(opts && opts.allowAuthPrompt)) {
    state.authRequired = true;
    render();
    throw new Error(data.error || "authentication required");
  }
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

async function apiPost(path, body = {}) {
  state.busy = true;
  render();
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      state.authRequired = true;
      render();
      throw new Error(data.error || "authentication required");
    }
    if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
    return data;
  } finally {
    state.busy = false;
    render();
  }
}

function showToast(msg, isError = false) {
  state.toast = { message: msg, isError };
  render();
  setTimeout(() => {
    state.toast = null;
    render();
  }, 4000);
}

async function guarded(fn) {
  state.busy = true;
  render();
  try {
    await fn();
  } catch (err) {
    showToast(String(err.message || err), true);
  } finally {
    state.busy = false;
    render();
  }
}

// §5.5: every mutating UI action records the equivalent CLI command, shown
// on the Terminal tab — the escape-hatch-and-teaching-device line.
function recordCli(kind, detail) {
  const runId = state.snapshot ? state.snapshot.run_id : "";
  const forms = {
    approve: () => `kusudaemon approve ${runId}`,
    amend: () => `kusudaemon amend ${runId} --text "${(detail || "").slice(0, 60)}"`,
    reopen: () => `kusudaemon reopen ${runId} --node ${detail} --defect "…"`,
    redispatch: () => `kusudaemon reopen ${runId} --node ${detail}`,
    escalate: () => `kusudaemon escalate ${runId}`,
    halt: () => `kusudaemon run --run-id ${runId}  (set halt.flag)`,
    pilot: () => `kusudaemon approve ${runId} --file out/.versions/${detail}/pilot-original.md`,
    interject: () => `kusudaemon pipeline interject ${runId} ${detail} "…"`,
  };
  state.lastCliCommand = (forms[kind] || (() => ""))();
  render();
}

// §10 Stalled banner + palette: resume a (possibly dead-driver) run the way
// §11's inventory says — `POST /api/runs` with an existing run id, which
// re-hosts it (run.spec.json on disk is authoritative for a resume).
function resumeRun() {
  const id = state.snapshot && state.snapshot.run_id;
  if (!id) { showToast("No run attached", true); return; }
  recordCli("halt", "");
  apiPost("/api/runs", { run_id: id })
    .then(() => showToast("Resume requested"))
    .catch((err) => showToast(String(err.message || err), true));
}

/* ------------------- live SSE stream / polling ------------------- */
function snapshotFingerprint(snap) {
  const { server_time, ...rest } = snap || {};
  return JSON.stringify(rest);
}

function loadMainAgentThinking() {
  const snap = state.snapshot;
  const subagents = (snap && snap.subagents) || [];
  const liveSub = subagents.find((s) => s.live);
  const targetId = liveSub ? liveSub.id : (subagents.length ? subagents[subagents.length - 1].id : "main");
  apiGet(`/api/node/${encodeURIComponent(targetId)}/thinking`)
    .then((d) => {
      const sub = subagents.find((s) => s.id === targetId);
      const next = { id: targetId, label: sub ? `${targetId} (${sub.role || sub.kind})` : targetId, entries: d.entries || [], live: sub ? sub.live : false };
      if (JSON.stringify(next) !== JSON.stringify(state.mainAgentThinking)) {
        state.mainAgentThinking = next;
        schedulePatch(patchCenter);  // the thinking stream lives in the center pane — patch only that
      }
    })
    .catch(() => {});
}

function applySnapshot(snap) {
  if (snap) {
    if (snap.control_enabled === undefined) snap.control_enabled = true;
    if (snap.max_concurrent_runs === undefined) snap.max_concurrent_runs = 4;
  }
  if (snap && snap.attached && !snap.goal && state.snapshot && state.snapshot.run_id === snap.run_id && state.snapshot.goal) {
    snap.goal = state.snapshot.goal;
  }
  const unchanged = snapshotFingerprint(snap) === snapshotFingerprint(state.snapshot);
  const prevEsc = (state.snapshot.escalation_history || []).length;
  const prevPending = (state.snapshot.pending_approvals || []).map((a) => a.approval_id);
  const nextPending = (snap.pending_approvals || []).map((a) => a.approval_id);
  state.snapshot = snap;
  // §6.1: a fresh pending approval takes the inspector over; once
  // dismissed for an id, stay dismissed for that id.
  if (nextPending.length && nextPending[0] !== state.takeoverFor) {
    state.approvalTakeover = true;
  }
  if (!nextPending.length) {
    state.approvalTakeover = false;
    state.takeoverFor = "";
  }
  // §10: escalation fired → rail tier chip flashes once.
  const esc = (snap.escalation_history || []).length;
  if (esc > prevEsc && !state.escalationFlash) {
    state.escalationFlash = true;
    setTimeout(() => { state.escalationFlash = false; render(); }, 1800);
  }
  state.sseLive = true;
  updateChrome(nextPending.length > 0);
  if (snap && snap.attached) loadMainAgentThinking();
  if (state.selectedNode && isLive(state.selectedNode)) {
    loadThinkingIfNeeded(true);
  }
  if (!unchanged) {
    // §Responsive: never rebuild the command bar (and so drop the operator's
    // caret / typed text) when they're actively typing in it. The cmdbar
    // still reflects promptText/promptMode from state, so the next mutation
    // outside typing rebuilds it correctly.
    const typingInCmdbar = (() => {
      const a = document.activeElement;
      return !!(a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT") && els.cmdbar && els.cmdbar.contains(a));
    })();
    if (typingInCmdbar) {
      schedulePatch(patchRail, patchHeader, patchNav, patchCenter, patchInspector, patchJobs, patchOverlays, patchToast);
    } else {
      scheduleAll();
    }
  }
}

function updateChrome(hasPending) {
  const icon = document.querySelector("link[rel='icon']");
  if (hasPending) {
    document.title = "⏸ kusudaemon";
    if (icon) icon.href = RED_FAVICON;
  } else {
    document.title = "Kusudaemon";
    if (icon) icon.href = DEFAULT_FAVICON;
  }
}

function startLive() {
  let usedSSE = false;
  try {
    const es = new EventSource("/api/stream");
    es.addEventListener("snapshot", (ev) => {
      usedSSE = true;
      applySnapshot(JSON.parse(ev.data));
    });
    es.onerror = () => {
      if (!usedSSE) {
        es.close();
        startPolling();
      }
    };
  } catch (e) {
    startPolling();
  }
}

function startPolling() {
  state.sseLive = false; // §10: rail shows ⟳ — stream dropped, polling
  const tick = () => apiGet("/api/snapshot").then(applySnapshot).catch(() => {});
  tick();
  setInterval(tick, 2000);
}

/* --------------------------- DOM helpers --------------------------- */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(child) : child);
  }
  return node;
}

function badge(status) {
  return el("span", { class: "badge", "data-status": status }, status || "-");
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtDur(secs) {
  if (!secs || secs < 0) return "-";
  secs = Math.round(secs);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return `${m}m${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

// §4 left-truncated ids: run-…a4f — the distinguishing END stays visible.
function ltrunc(text, n) {
  if (!text) return "";
  return text.length <= n ? text : "…" + text.slice(text.length - n + 1);
}

function truncate(text, n) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "\n…[truncated]" : text;
}

function words(text) {
  return (text || "").trim() ? String(text).trim().split(/\s+/).length : 0;
}

function diffLineKind(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "header";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

const _CHAT_ROLE_LABEL = {
  assistant: "🤖 Agent",
  thinking: "💭 Thinking",
  tool_call: "🔧 Tool call",
  tool: "🔧 Tool result",
  system: "⚙️ System",
  error: "❌ Error",
  user: "Prompt",
  diff: "📝 File change",
  logdir: "Session",
  raw: "",
};

function renderAgentChatEntry(e) {
  if (e.role === "diff") {
    return el("div", { class: "stream-card agent-diff-card" }, [
      el("div", { class: "card-title" }, el("span", null, _CHAT_ROLE_LABEL.diff)),
      el(
        "pre",
        { class: "diff-pre trace-diff-pre" },
        (e.text || "").split("\n").map((line) => el("div", { class: `diff-line diff-${diffLineKind(line)}` }, line))
      ),
    ]);
  }
  const label = _CHAT_ROLE_LABEL[e.role] || e.role;
  return el("div", { class: `stream-msg agent-chat-entry role-${e.role}` }, [
    label ? el("div", { class: "msg-hdr" }, el("span", { class: "author" }, label)) : null,
    el("div", { class: "msg-body" }, e.text),
  ]);
}

const _EVENT_LABEL = {
  phase_started: "▶️ Phase started",
  phase_done: "✅ Phase completed",
  node_dispatched: "🚀 Subagent spawned",
  node_redispatched: "🔁 Subagent re-dispatched",
  session_captured: "🔗 Subagent session attached",
  episode_completed: "🏁 Subagent finished",
  run_tier_escalated: "⇡ Tier escalated",
  node_split: "⑂ Node split",
  split_proposal: "⑂ Split proposed",
};

function renderEventEntry(ev) {
  const isAutoResume = ev.type === "phase_auto_resuming";
  const isFailure = ev.type === "phase_failed" || ev.type === "run_escalated" || (ev.type === "phase_done" && ev.status === "escalated");
  // §10: escalation fired → inline feed marker at its own timestamp:
  // `T2 → T3 · split_accepted · node-04`, amber, never re-pinned.
  if (ev.type === "run_tier_escalated") {
    const from = ev.from || "-", to = ev.to || "-";
    const tail = [ev.trigger ? `trigger: ${ev.trigger}` : null, ev.node_id ? `node: ${ev.node_id}` : null].filter(Boolean).join(" · ");
    return el("div", { class: "stream-msg agent" }, [
      el("div", { class: "msg-hdr" }, [
        el("span", { class: "author", style: "color:var(--accent-amber); font-weight:700;" }, `⇡ Tier escalated`),
        el("span", null, fmtTime(ev.ts)),
      ]),
      el("div", { class: "msg-body", style: "color:var(--accent-amber); font-weight:500;" }, `${from} → ${to}${tail ? " · " + tail : ""}`),
    ]);
  }
  if (isFailure) {
    return el("div", { class: "stream-card phase-error-card" }, [
      el("div", { class: "card-title" }, [
        el("span", { style: "color:var(--accent-red); font-weight:700;" }, `❌ Phase Failure (${ev.phase ? ev.phase.toUpperCase() : "FAILURE"})`),
        el("span", null, fmtTime(ev.ts)),
      ]),
      el("div", { class: "error-body" }, ev.error || ev.reason || "Phase execution failed. Review details or click Resume below to retry."),
    ]);
  }
  let msgText = `${ev.type}${ev.phase ? ` [${ev.phase}]` : ""}${ev.status ? ` - ${ev.status}` : ""}`;
  if (isAutoResume) {
    msgText = `🔄 Auto-resuming phase [${ev.phase}] (attempt ${ev.attempt || 1}) — Previous failure error: "${ev.error || "unknown"}"`;
  } else if (ev.error) {
    msgText += ` — Error: "${ev.error}"`;
  }
  const author = isAutoResume ? "🔄 Auto-Resume" : (_EVENT_LABEL[ev.type] || "Event");
  return el("div", { class: "stream-msg agent", style: isAutoResume ? "border-left: 3px solid var(--accent-amber); background: rgba(245, 158, 11, 0.05);" : "" }, [
    el("div", { class: "msg-hdr" }, [
      el("span", { class: "author", style: isAutoResume ? "color:var(--accent-amber);" : "" }, author),
      el("span", null, fmtTime(ev.ts)),
      ev.node_id && ev.node_id !== "-" ? el("span", { class: "node-link", onclick: () => openNode(ev.node_id) }, ev.node_id) : null,
    ]),
    el("div", { class: "msg-body", style: isAutoResume ? "font-weight:500; color:var(--text-bright);" : "" }, msgText),
  ]);
}

// §6.3: intake objections — amber, {claim, why, options[]} intact. An
// objection is the model pushing back; it reads differently from a question.
function renderObjections(ctx) {
  const objections = (ctx && ctx.objections) || [];
  if (!objections.length) return null;
  return el("div", { class: "approval-objections" }, objections.map((o) =>
    el("div", { class: "approval-objection" }, [
      el("div", { style: "font-weight:700; color:var(--accent-amber);" }, `⚠ objection: ${o.claim || ""}`),
      o.why ? el("div", { class: "dim", style: "font-size:12px; margin-top:2px;" }, o.why) : null,
      (o.options || []).length ? el("div", { class: "dim", style: "font-size:11px; margin-top:2px;" }, `options: ${o.options.join(" · ")}`) : null,
    ])
  ));
}

// §6.4: amend-triage → three stacked count chips, each expanding to its
// node list, each node clickable through to §5.2 before you approve.
function renderTriageChips(a) {
  const ctx = a.context || {};
  const counts = ctx.counts || {};
  const triage = ctx.triage || {};
  const classes = [["clean", "gate-pass"], ["patchable", "gate-amber"], ["regenerate", "gate-fail"]];
  const chips = classes.filter(([c]) => counts[c] !== undefined).map(([cls, cssClass]) => {
    const num = counts[cls] || 0;
    const open = state.triageOpen[a.approval_id] === cls;
    const nodes = Object.entries(triage)
      .filter(([, rec]) => (rec.classification || rec.class || "regenerate") === cls)
      .map(([id]) => id);
    return el("div", { class: "triage-chip " + cssClass, onclick: () => { state.triageOpen[a.approval_id] = open ? "" : cls; render(); } }, [
      el("span", { class: "triage-chip-count" }, String(num)),
      el("span", null, cls),
      open
        ? el("div", { class: "triage-node-list", onclick: (e) => e.stopPropagation() },
            nodes.length ? nodes.map((id) => el("div", { class: "node-link", onclick: () => openNode(id) }, id)) : el("div", { class: "dim" }, "(none)"))
        : null,
    ]);
  });
  return chips.length ? el("div", { class: "triage-chips" }, chips) : null;
}

// Renders one approval record — chronological feed, pinned pending section,
// and the §6.1 takeover. Options get [n] number-key bindings; Enter picks
// the primary one.
function renderApprovalEntry(a, snap, isTakeover) {
  const isPending = a.status === "pending";
  const parts = [
    el("div", { class: "card-title" }, [
      el("span", { style: isPending ? "color:var(--accent-red); font-weight:700;" : "color:var(--accent-amber); font-weight:700;" }, `⏸ ${isPending ? "APPROVAL" : a.kind.toUpperCase()}: ${a.title}`),
      badge(a.status),
    ]),
  ];
  if (a.message) parts.push(el("div", { class: "card-text", style: isPending ? "font-size:14px; font-weight:500;" : "" }, a.message));

  const objections = isPending ? renderObjections(a.context) : null;
  if (objections) parts.push(objections);
  const triage = isPending && a.kind === "triage" ? renderTriageChips(a) : null;
  if (triage) parts.push(triage);

  if (isPending && snap.control_enabled) {
    const actionBtns = [];
    if ((a.options || []).length) {
      a.options.forEach((opt, i) => {
        actionBtns.push(
          el("button", {
            class: opt.style === "primary" ? "primary" : "",
            disabled: state.busy ? "" : null,
            onclick: () => guarded(() => apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: opt.value }).then(() => { recordCli("approve"); showToast("Approval resolved"); })),
          }, `[${i + 1}] ${opt.label}`)
        );
      });
    }
    const questions = a.questions || [];
    if (questions.length) {
      parts.push(
        el("div", { class: "approval-questions" }, questions.map((q) => {
          const row = el("div", { class: "approval-question" }, [
            el("label", { style: "font-size:12px; font-weight:600; color:var(--text-bright);" }, q.text || q.id),
          ]);
          const inputEl = el("input", { type: "text", "data-key": `approval-q-${a.approval_id}-${q.id}`, placeholder: (q.default_assumption ? `accept default: ${q.default_assumption}` : "answer…"), style: "margin-top:4px;" });
          const draftKey = `${a.approval_id}::${q.id}`;
          inputEl.value = state.approvalAnswerDrafts[draftKey] || "";
          inputEl.addEventListener("input", () => { state.approvalAnswerDrafts[draftKey] = inputEl.value; });
          row.appendChild(inputEl);
          return row;
        }))
      );
      actionBtns.push(
        el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          const answers = {};
          questions.forEach((q) => {
            answers[q.id] = (state.approvalAnswerDrafts[`${a.approval_id}::${q.id}`] || "").trim();
            delete state.approvalAnswerDrafts[`${a.approval_id}::${q.id}`];
          });
          await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "answer", answers });
          recordCli("approve");
          showToast("Answers submitted");
        }) }, "Submit Answers")
      );
    }
    if (a.allow_input) {
      const inputEl = el("input", { type: "text", "data-key": `approval-input-${a.approval_id}`, placeholder: a.input_label || "Provide response details or leave blank for default...", style: "margin-top:8px;" });
      inputEl.value = state.approvalDrafts[a.approval_id] || "";
      inputEl.addEventListener("input", () => { state.approvalDrafts[a.approval_id] = inputEl.value; });
      parts.push(inputEl);
      actionBtns.push(
        el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          const val = state.approvalDrafts[a.approval_id] || "";
          await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "answer", user_input: val });
          recordCli("approve");
          delete state.approvalDrafts[a.approval_id];
          showToast("Submitted answer");
        }) }, "Submit Input")
      );
      actionBtns.push(
        el("button", { disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "answer", user_input: "" });
          recordCli("approve");
          delete state.approvalDrafts[a.approval_id];
          showToast("Accepted default");
        }) }, "Use Default")
      );
    }
    if (a.kind === "pilot" && a.context && a.context.node_id) {
      actionBtns.push(
        el("button", { onclick: () => openNode(a.context.node_id, "overview") }, "✏️ Open pilot editor")
      );
    }
    if (isTakeover) {
      actionBtns.push(
        el("button", { class: "xs-btn", title: "read the run, come back — any key re-arms the takeover", onclick: () => dismissTakeover(a) }, "⌫ dismiss (keys)")
      );
    }
    parts.push(el("div", { class: "approval-actions" }, actionBtns));
  } else if (a.status === "resolved") {
    const answerDisplay = a.user_input ? `Answer given: "${a.user_input}"` : `Resolved via action: ${a.action || "completed"}`;
    parts.push(el("div", { style: "font-size:12px; color:var(--text-bright); font-weight:500; margin-top:6px; background:var(--bg-tertiary); padding:6px 10px; border-radius:4px;" }, answerDisplay));
  }

  const cardStyle = isPending
    ? "border:1.5px solid var(--accent-red); background:rgba(244, 63, 94, 0.07);"
    : "border:1.5px solid var(--accent-amber); background:rgba(245, 158, 11, 0.06);";
  return el("div", { class: "stream-card approval" + (isPending ? " pending" : ""), style: cardStyle }, parts);
}

function dismissTakeover(a) {
  state.approvalTakeover = false;
  state.takeoverFor = a.approval_id;
  render();
}

function liveMap() {
  const m = {};
  for (const s of state.snapshot.subagents || []) m[s.id] = s;
  return m;
}
/* ========================= PART B ========================= */

// §3 rail: left cluster = one segment per phase (glyph+VALUE), in the
// order the driver ran them; current phase gets bold+active styling.
// Right: hosted counter │ live-or-polling indicator │ clock pseudo-CNY.
function renderRail() {
  const snap = state.snapshot;
  if (!snap.attached) {
    return el("div", { class: "rail" }, [
      el("div", { class: "rail-left" }, [el("span", { class: "rail-title" }, "KUSUDAEMON")]),
      el("div", { class: "rail-right" }, [el("span", { class: "rail-a40" }, "no run attached")]),
    ]);
  }
  // §10 Stalled: a stalled run and a run mid-provider-call must never look
  // alike. When liveness says stalled, ☠ replaces the phase glyph entirely
  // and the rail's bottom border turns red (handled in CSS via .rail.stalled).
  const stalled = !!snap.stalled;
  const segClass = {
    done: "seg-passed pass", in_progress: "seg-running run", failed: "seg-fail fail",
    error: "seg-fail fail", awaiting_approval: "seg-paused paused", escalated: "seg-escalated esc",
    halted: "seg-paused paused", stalled: "seg-stalled stalled", pending: "pass", created: "pass",
  };
  let segs;
  if (stalled) {
    segs = [el("div", { class: "rail-seg stalled", title: `stalled — ${snap.stalled_reason || "driver appears dead"}` }, [
      el("span", { class: "segglyph" }, "☠"),
      el("span", { class: "segval" }, "STALLED"),
    ])];
  } else {
    segs = PHASES_ALL.filter((p) => (snap.phases || {})[p]).map((p) => {
      const st = (snap.phases || {})[p];
      const label = { in_progress: "RUN", done: "DONE", failed: "FAIL", error: "ERR", awaiting_approval: "WAIT", escalated: "ESC", halted: "HLT", stalled: "STALL", pending: "PEND", created: "" }[st] || st.toUpperCase();
      return el("div", { class: "rail-seg " + (segClass[st] || ""), title: `${p} — ${st}` }, [
        el("span", { class: "segglyph" }, PHASE_GLYPH[st] || "·"),
        el("span", { class: "segval" }, label),
      ]);
    });
  }
  const liveNow = (snap.hosted || snap.phase_status === "in_progress");
  return el("div", { class: "rail" + (stalled ? " stalled" : "") + (snap.halted ? " halted" : "") }, [
    el("div", { class: "rail-left" }, segs.length ? segs : [el("span", { class: "rail-no-phase" }, "—")]),
    el("div", { class: "rail-right" }, [
      el("span", { class: "rail-hosted", title: `${snap.hosted_count || 0} runs hosted · cap ${snap.max_concurrent_runs}` }, `${snap.hosted_count || 0}/${snap.max_concurrent_runs}`),
      el("span", { class: "rail-live" + (state.sseLive ? " on" : ""), title: state.sseLive ? "SSE live" : "SSE dropped — 2s polling" }, state.sseLive ? "🟢 LIVE" : "🔄 ⟳"),
      el("span", { class: "rail-a40" }, snap.elapsed ? fmtDur(snap.elapsed) : "—"),
    ]),
  ]);
}

// Run header row (below rail): run id + goal + phase/status/"whole run"
// provenance summary + tier badge + escalation badge + control buttons.
function renderHeaderRow() {
  const snap = state.snapshot;
  if (!snap.attached) return null;
  const tier = snap.tier ? (snap.tier_override ? `T${snap.tier_override} (floor)` : snap.tier) : null;
  const esc = snap.escalation_history || [];
  const escChip = esc.length ? el("span", {
    class: "hdr-tier-badge hdr-esc-badge" + (state.escalationFlash ? " flash" : ""),
    title: esc.map((e) => `${e.from} → ${e.to} · ${e.trigger}${e.node_id ? " · " + e.node_id : ""}`).join("\n"),
  }, `⇡${esc.length}`) : null;
  const tierChip = tier ? el("span", { class: "hdr-tier-badge", title: `measured ${snap.measured_tier}${snap.tier_override ? ` · --tier ${snap.tier_override}` : ""}` }, tier) : null;
  return el("div", { class: "hdr-run" }, [
    el("div", { class: "hdr-run-id" }, [
      el("span", { class: "runId", style: "cursor:pointer;", title: "switch run", onclick: () => { state.runSwitcherOpen = true; render(); } }, snap.run_id),
      tierChip, escChip,
      snap.halted ? el("span", { class: "hdr-tier-badge hdr-halt-badge" }, "⏸ halted") : null,
    ]),
    el("div", { class: "hdr-goal", title: snap.goal }, snap.goal || "—"),
    el("div", { class: "hdr-status" }, [
      badge(snap.phase_status || (snap.halted ? "halted" : "created")),
      el("span", { class: "dim" }, snap.phase_detail || ""),
    ]),
    el("div", { class: "hdr-buttons" }, [
      snap.control_enabled && tier !== "T3" ? el("button", { class: "btn-tiny", onclick: () => { if (confirm("Escalate tier (+1, T3 max)?") ) guarded(() => apiPost("/api/escalate", {}).then(() => { recordCli("escalate"); showToast("Tier escalated"); })); } }, "⇡ escalate") : null,
      snap.stalled ? el("button", { class: "btn-tiny", style: "color:var(--accent-red);", onclick: () => guarded(resumeRun) }, "☠ Resume") : null,
      snap.control_enabled && !snap.stalled && (snap.phase_status === "error" || snap.halted || snap.phase_status === "failed" || snap.phase_status === "escalated" || snap.phase_status === "blocked" || snap.phase_status === "paused")
        ? el("button", { class: "btn-tiny", onclick: () => guarded(() => apiPost("/api/halt", { value: false }).then(() => showToast("Resume requested"))) }, "▶ Resume")
        : null,
      snap.control_enabled ? el("button", { class: "btn-tiny", onclick: () => guarded(() => apiPost("/api/halt", { value: snap.halted ? false : true }).then(() => { recordCli("halt"); showToast(snap.halted ? "Resume requested" : "Halting after current phase"); })) }, snap.halted ? "▶" : "⏸") : null,
    ]),
  ]);
}

// Run switcher overlay — newest-first, ✅ = attached, ⏸ before count = pending.
function renderRunSwitcher() {
  if (!state.runSwitcherOpen) return null;
  const snap = state.snapshot;
  const rows = (snap.runs || []).map((r) =>
    el("div", { class: "runrow" + (r.attached ? " active" : ""), onclick: () => { attachRun(r.id); state.runSwitcherOpen = false; } }, [
      el("span", { class: "rr-glyph" }, r.attached ? "✅" : r.hosted ? "●" : "·"),
      el("span", { class: "rr-id" }, r.id),
      el("span", { class: "rr-pip" }, r.pending_approvals ? `⏸ ${r.pending_approvals}` : ""),
      el("span", { class: "rr-phase" }, PHASE_GLYPH[r.status] || "·"),
      el("span", { class: "rr-goal" }, r.goal || ""),
    ])
  );
  return el("div", { class: "overlay", onclick: (e) => { if (e.target === e.currentTarget) { state.runSwitcherOpen = false; render(); } } }, [
    el("div", { class: "panel run-switcher" }, [
      el("div", { class: "panel-hdr" }, "Switch run"),
      el("div", { class: "panel-body" }, rows),
      el("div", { class: "panel-foot" }, [
        el("button", { class: "primary", onclick: () => { state.runSwitcherOpen = false; state.newRunOpen = true; render(); } }, "＋ New Run…"),
        el("button", { onclick: () => { state.runSwitcherOpen = false; render(); } }, "Close"),
      ]),
    ]),
  ]);
}

function renderNavSection(key, title, rows) {
  const collapsed = state.navCollapsed[key];
  return el("section", { class: "nav-section", "data-section": key }, [
    el("div", { class: "nav-head", onclick: () => { state.navCollapsed[key] = !collapsed; patchNav(); } }, [
      el("span", { class: "nav-caret" }, collapsed ? "▸" : "▾"),
      el("span", { class: "nav-title" }, title),
      el("span", { class: "nav-count" }, rows.length),
    ]),
    collapsed ? null : el("div", { class: "nav-body" }, rows),
  ]);
}

// §3 nav — right column. keyboard: j/k moves, ↩ attaches the focused run.
// §10 "no run attached": Nav shows runs only — no subagents/phases chrome
// pretending to have data.
function renderNav() {
  const snap = state.snapshot;
  const runRows = (snap.runs || []).map((r) => el("div", {
    class: "nav-row run" + (r.attached ? " active" : ""),
    onclick: () => attachRun(r.id),
    oncontextmenu: (e) => { e.preventDefault(); openRunMenu(e, r.id); },
  }, [
    el("span", { class: "row-glyph" }, r.attached ? "✅" : "·"),
    el("span", { class: "row-id", title: r.id }, ltrunc(r.id, 18)),
    el("span", { class: "row-pip" }, r.pending_approvals ? `⏸${r.pending_approvals}` : (r.hosted ? "●" : "")),
    el("span", { class: "row-status" }, PHASE_GLYPH[r.status] || "·"),
  ]));
  const subs = (snap.subagents || []).slice().reverse();
  const subRows = subs.map((s) => el("div", {
    class: "nav-row sub" + (state.selectedNode === s.id ? " active" : ""),
    onclick: () => openNode(s.id, "chat"),
  }, [
    el("span", { class: "row-glyph" }, SUB_GLYPH[s.status] || "·"),
    el("span", { class: "row-id", title: s.id }, ltrunc(s.id, 18)),
    el("span", { class: "row-pip" }, s.live ? "●" : ""),
  ]));
  const phaseRows = Object.entries(snap.phases || {}).map(([p, st]) => el("div", { class: "nav-row" }, [
    el("span", { class: "row-glyph" }, PHASE_GLYPH[st] || "·"),
    el("span", { class: "row-id" }, p),
    el("span", { class: "row-status" }, st),
  ]));
  const sections = [renderNavSection("runs", "RUNS", runRows)];
  if (snap.attached) {
    sections.push(renderNavSection("subagents", "SUBAGENTS", subRows));
    sections.push(renderNavSection("phases", "PHASES", phaseRows));
  }
  return el("div", { class: "sidebar-nav" }, [
    el("div", { class: "nav-section-group" }, sections),
  ]);
}

/* ------------------------- center stream ------------------------- */

function renderCenterStream() {
  const snap = state.snapshot;
  if (!snap.attached) {
    // §10 "No run attached": the stream shows a single centered "+ new run"
    // CTA — no dashboard chrome pretending to have data.
    return el("main", { class: "chat-stream-panel" }, [
      el("div", { class: "empty-state" }, [
        el("div", { class: "dim", style: "font-size:13px; margin-bottom:14px;" }, "no run attached"),
        el("button", { class: "primary", onclick: () => { state.newRunOpen = true; render(); } }, "＋ New run…"),
        el("div", { class: "dim", style: "font-size:11px; margin-top:14px;" }, "or pick one from the runs list on the left"),
      ]),
    ]);
  }
  const feedEntries = [];
  const evList = snap.events || [];
  const lastEvents = evList.slice(-20).map((ev, i) => ({ sort: ev.ts || 0, node: renderEventEntry(ev) }));
  feedEntries.push(...lastEvents);
  const resolvedApprovals = (snap.approvals || []).filter((a) => a.status !== "pending").map((a) => ({ sort: a.updated_at || a.created_at || 0, node: renderApprovalEntry(a, snap, false) })).sort((a, b) => a.sort - b.sort);
  feedEntries.push(...resolvedApprovals);
  feedEntries.sort((a, b) => a.sort - b.sort);

  const pinnedHeader = el("div", { class: "pinned-hdr" }, [
    snap.has_contract ? el("span", { class: "hdr-pill" }, "📜 contract ✓") : null,
    snap.has_spec ? el("span", { class: "hdr-pill" }, "spec ✓") : null,
    snap.has_assembly ? el("span", { class: "hdr-pill" }, "assembly ✓") : null,
    snap.phase_status === "in_progress" && state.mainAgentThinking ? el("span", { class: "hdr-pill", style: "color:var(--accent-purple);" }, `🤖 ${state.mainAgentThinking.id}…`) : null,
    el("span", { class: "hdr-pill dim" }, `${snap.events_count || 0} events`),
  ]);

  const approvalFeed = (snap.pending_approvals || []).map((a) => renderApprovalEntry(a, snap, false));

  // §10 Stalled: never a bare "running" badge next to a dead driver — a
  // red banner with the reason and a Resume button, pinned above the feed.
  const stalledBanner = snap.stalled ? el("div", { class: "stalled-banner" }, [
    el("span", { style: "font-weight:800;" }, "☠ STALLED"),
    el("span", { class: "dim", style: "flex:1;" }, snap.stalled_reason || "the driver process appears dead (liveness check failed)"),
    snap.control_enabled ? el("button", { class: "btn-tiny", onclick: () => guarded(resumeRun) }, "▶ Resume") : null,
  ]) : null;

  return el("main", { class: "chat-stream-panel" }, [
    el("div", { class: "chat-header" }, [
      el("div", { class: "title" }, ["💬 Run Stream", snap.halted ? badge("halted") : null]),
      el("button", { class: "btn-tiny", onclick: () => { loadMainAgentThinking(); render(); } }, "refresh"),
    ]),
    stalledBanner,
    pinnedHeader,
    el("div", { class: "chat-feed", id: "chat-feed" }, feedEntries.map((e) => e.node)),
    approvalFeed.length ? el("div", { class: "pending-approvals-block" }, [
      el("div", { class: "pending-hdr" }, `⏸ PENDING APPROVALS (${approvalFeed.length})`),
      ...approvalFeed,
    ]) : null,
  ]);
}/* ========================= PART C ========================= */

// §7.2 command bar. `>` → command mode with live suggestions (Ctrl/Cmd-K also
// opens the same list as a palette). Modes: msg_agent (default)/command/
// amend/reopen. Drawn once per render, re-rendered on input via full-teardown
// §RESPONSIVE: the command bar is rebuilt only by explicit patches
// (mode-chip click, a slash-command suggestion click, a snapshot poll). On
// plain typing the textarea itself is the source of truth — its `input`
// event updates `state.promptText`/`promptMode` and refreshes only the
// rendered command suggestions in place; the cmdbar DOM is NOT rebuilt,
// so typing never loses focus or triggers a synchronous region rebuild.
function renderCommandBar() {
  const isCommand = state.promptText.trim().startsWith(">");
  const nodeId = state.selectedNode;
  const placeholder = state.promptMode === "amend"
    ? "bold_rule text with no citation numbers (e.g. Deliberately exclude historical asides)" + (nodeId ? "" : " — open a node first")
    : state.promptMode === "reopen"
      ? "reason to reopen this node (starts a repair)" + (nodeId ? "" : " — open a node first")
      : isCommand
        ? "e.g. >runs …"
        : (nodeId ? `message ${nodeId} …` : "message main agent (e.g. much more important to prioritize examples like the Friday fleet)");
  const modeChip = (mode, label, glyph, title) => el("button", {
    class: "mode-chip " + (state.promptMode === mode ? "active" : ""),
    title, onclick: () => { state.promptMode = mode; patchCmdbar(); focusCmdbar(); },
  }, glyph + label);
  const textEl = el("textarea", { class: "cmd-input" + (state.promptMode !== "msg_agent" && state.promptMode !== "command" ? " mode-" + state.promptMode : ""), rows: "2", placeholder });
  textEl.value = state.promptText;
  const suggestionsHost = el("div", { class: "cmd-suggestions" });
  const renderSuggestionsInto = (host) => {
    host.replaceChildren(...(isCommand ? commandSuggestions() : []));
  };
  let sugTimer = null;
  textEl.addEventListener("input", () => {
    state.promptText = textEl.value;
    const nowCmd = textEl.value.trim().startsWith(">");
    if (nowCmd && state.promptMode !== "command") state.promptMode = "command";
    else if (!nowCmd && state.promptMode === "command") state.promptMode = "msg_agent";
    // §Responsive: refresh only the suggestions list, debounced, never the
    // cmdbar — typing stays live.
    if (sugTimer) clearTimeout(sugTimer);
    sugTimer = setTimeout(() => renderSuggestionsInto(suggestionsHost), 80);
  });
  textEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handlePromptSubmit(e);
    }
  });
  renderSuggestionsInto(suggestionsHost);

  const subs = state.snapshot.subagents || [];
  const anyLive = subs.some((s) => s.live);
  const targetSelect = el("select", { class: "target-select", title: "message target — auto-follows the live agent unless you pick one", onchange: (e) => { state.targetAgentId = e.target.value; state.targetAgentManual = true; } }, [
    el("option", { value: "main", selected: ((!state.targetAgentManual && anyLive) || state.targetAgentId === "main") ? "selected" : null }, anyLive ? "🤖 main (live)" : "🤖 main"),
    ...subs.map((s) => el("option", { value: s.id, selected: (state.targetAgentManual && state.targetAgentId === s.id) ? "selected" : null }, `${s.live ? "● " : ""}${s.id}`)),
  ]);
  const row = el("div", { class: "cmd-buttons" }, [
    targetSelect,
    modeChip("msg_agent", "A", "💬 ", "message agent"),
    modeChip("command", "⌥", ">", "command mode"),
    modeChip("amend", "m", "✏️ ", "amend contract (whole run)"),
    modeChip("reopen", "r", "🔁 ", "reopen node (repair)"),
  ]);

  return el("div", { class: "cmdbar" }, [
    row,
    el("div", { class: "cmd-row" }, [textEl]),
    suggestionsHost,
  ]);
}

async function handlePromptSubmit(e) {
  const mode = state.promptMode;
  const raw = state.promptText;
  const text = raw.trim();
  if (!text) return;
  if (mode === "command" && text.startsWith(">")) {
    const q = text.slice(1).trim();
    const suggestions = commandSuggestions();
    const exact = suggestions.find((s) => s.key === q || s.trigger === q);
    state.promptMode = "msg_agent"; state.promptText = ""; patchCmdbar();
    if (exact) { await guarded(exact.run); return; }
    const match = suggestions.find((s) => s.pattern.test(q));
    if (match && match.fromQuery) {
      await guarded(() => match.fromQuery(q));
      return;
    }
    await guarded(cmdHelp);
    return;
  }
  if (mode === "amend") {
    const target = state.targetAgentManual ? state.targetAgentId : "main";
    const c = findCommand("amend");
    state.promptMode = "msg_agent";
    state.promptText = "";
    patchCmdbar();
    await guarded(() => c.run(text, target));
    return;
  }
  if (mode === "reopen") {
    if (!state.selectedNode) { showToast("Open a node first (click one in the tree to reopen it)", true); return; }
    const c = findCommand("reopen");
    state.promptMode = "msg_agent";
    state.promptText = "";
    patchCmdbar();
    await guarded(() => c.run(text, state.selectedNode));
    return;
  }
  // default: message a live agent
  const target = state.targetAgentManual ? state.targetAgentId : (liveSubId() || "main");
  state.targetAgentId = target;
  if (!target) { showToast("No live agent to message", true); return; }
  state.promptText = "";
  patchCmdbar();
  await guarded(async () => {
    await apiPost(`/api/node/${encodeURIComponent(target)}/interject`, { content: text });
    showToast(`message sent to ${target}`);
    loadThinkingIfNeeded(true);
  });
}

function liveSubId() {
  const subs = state.snapshot.subagents || [];
  const live = subs.find((s) => s.live);
  return live ? live.id : null;
}

/* ------------------------- commands / palette ------------------------- */
function findCommand(key) {
  return COMMANDS[key];
}

let COMMANDS = null;

function _memo(lazy) {
  if (!COMMANDS) COMMANDS = lazy();
  return COMMANDS;
}

async function cmdResume() {
  // §11: "Resume run — POST /api/runs w/ existing id". No /api/resume route
  // exists; an old version of this command posted to it and always 404'd.
  await resumeRun();
}
async function cmdHelp() {
  state.helpOpen = true;
  patchOverlays();
}
async function cmdNewRun() {
  state.newRunOpen = true;
  render();
}
async function cmdRuns() {
  state.runSwitcherOpen = true;
  render();
}
async function cmdEscalate() {
  if (confirm("Escalate tier (+1, T3 max)?")) {
    await apiPost("/api/escalate", {});
    recordCli("escalate");
    showToast("Tier escalated");
  }
}
async function cmdTaskTree() {
  state.workbenchTab = "tree";
  render();
}
async function cmdDoc() {
  state.workbenchTab = "doc";
  state.docTab = "contract";
  fetchWorkbenchData("contract");
  render();
}
async function cmdAsm() {
  state.workbenchTab = "asm";
  fetchWorkbenchData("asm");
  render();
}
async function cmdTerm() {
  state.workbenchTab = "term";
  fetchWorkbenchData("asm");
  render();
}
async function cmdToggleControl() { /* control flag is server-side */ }

async function _redispatchAction(nodeId) {
  await apiPost(`/api/node/${encodeURIComponent(nodeId)}/redispatch`, {});
  recordCli("redispatch", nodeId);
  showToast("Redispatch approval queued");
}

function buildCommands() {
  const commands = {
    resume: { key: "resume", trigger: "resume", label: "Resume", usage: "> resume", timeout: 20, run: cmdResume },
    "task-tree": { key: "task-tree", trigger: "tree", label: "Task tree", usage: "> tree", timeout: 20, run: cmdTaskTree },
    doc: { key: "doc", trigger: "doc", label: "Documents", usage: "> doc", timeout: 20, run: cmdDoc },
    asm: { key: "asm", trigger: "asm", label: "assembly", usage: "> asm", timeout: 20, run: cmdAsm },
    term: { key: "term", trigger: "term", label: "Terminal", usage: "> term", timeout: 20, run: cmdTerm },
    new: { key: "new", trigger: "new", label: "New run", usage: "> new", timeout: 20, run: cmdNewRun },
    runs: { key: "runs", trigger: "runs", label: "Switch run", usage: "> runs", timeout: 20, run: cmdRuns },
    escalate: { key: "escalate", trigger: "esc", label: "Escalate tier", usage: "> escalate", timeout: 20, run: cmdEscalate },
    help: { key: "help", trigger: "help", label: "Keyboard shortcuts", usage: "> help", timeout: 20, run: cmdHelp },
    amend: { key: "amend", trigger: "amend", label: "Amend contract", usage: "> amend <rule> [node]", timeout: 20, run: async (text) => {
      if (!state.snapshot.attached) { showToast("No run attached", true); return; }
      if (!text) { showToast("amend requires a rule text", true); return; }
      const split = text.split(/\s+/);
      const rule = split.slice(0, 3).join(" ");
      const nodeArg = split[3] ? split.slice(3).join(" ") : "";
      const target = state.targetAgentManual ? state.targetAgentId : "main";
      const resp = await apiPost("/api/amend", { text: rule + (nodeArg ? " " + nodeArg : ""), reason: "web amendment", target });
      recordCli("amend", rule + (nodeArg ? " " + nodeArg : ""));
      showToast(resp.detail || "Contract amendment queued");
    } },
    reopen: { key: "reopen", trigger: "reopen", label: "Reopen node", usage: "> reopen <reason> <node>", timeout: 20, run: async (text) => {
      if (!state.snapshot.attached) { showToast("No run attached", true); return; }
      const split = text.split(/\s+/);
      const maybeNode = split[split.length - 1] || "";
      const isTreeNode = maybeNode && (state.snapshot.tree || []).some((n) => n && n.id === maybeNode);
      const nodeArg = isTreeNode ? maybeNode : state.selectedNode;
      const reason = isTreeNode ? split.slice(0, -1).join(" ") : text;
      if (!nodeArg) { showToast("reopen needs a node id (select a node first)", true); return; }
      await apiPost("/api/reopen", { node_id: nodeArg, defect: reason, is_manual: true });
      recordCli("reopen", nodeArg);
      showToast("Node reopened");
    } },
    interject: { key: "interject", trigger: "interject", label: "Message agent", usage: "> interject <text> or just type below", timeout: 20, run: async (text) => {
      const target = state.targetAgentManual ? state.targetAgentId : (liveSubId() || "main");
      if (!target) { showToast("No live agent", true); return; }
      await apiPost(`/api/node/${encodeURIComponent(target)}/interject`, { content: text });
      recordCli("interject", target);
      showToast("Message sent");
      loadThinkingIfNeeded(true);
    } },
    redispatch: { key: "redispatch", trigger: "redispatch", label: "Restart agent", usage: "> redispatch <node>", timeout: 20, run: async (text) => {
      const nodeArg = text || state.selectedNode;
      if (!nodeArg) { showToast("redispatch needs a node", true); return; }
      await _redispatchAction(nodeArg);
    } },
  };
  // fromQuery: pattern-matching for `>` queries
  for (const [key, c] of Object.entries(commands)) {
    c.key = key;
    c.pattern = new RegExp("^" + (c.trigger || key) + "(\\s+|$)");
    c.fromQuery = c.run;
  }
  return commands;
}

async function runCommand(c) {
  await guarded(() => c.run());
}

function commandSuggestions() {
  const cmds = _memo(buildCommands);
  const q = state.promptText.replace(/^\s*>/, "").trim();
  const list = Object.values(cmds);
  if (!q) return list.map((c) => suggestionRow(c));
  const matches = list.filter((c) => c.usage.includes(q) || (c.label || "").toLowerCase().includes(q.toLowerCase()) || (c.trigger || "").startsWith(q));
  const out = matches.length ? matches : list;
  return out.slice(0, 8).map((c) => suggestionRow(c));
}

function suggestionRow(c) {
  return el("div", { class: "cmd-suggestion", onclick: () => { state.promptMode = "msg_agent"; state.promptText = c.usage.replace(/^>/, "").trim(); patchCmdbar(); focusCmdbar(); } }, [
    el("span", { class: "sug-usage" }, c.usage),
    el("span", { class: "sug-timeout" }, `timeout ${c.timeout}s`),
  ]);
}

function focusCmdbar() {
  const ta = els.cmdbar && els.cmdbar.querySelector("textarea");
  if (ta) { ta.focus(); try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) {} }
}

/* ========================= PART D ========================= */

const WORKBENCH_TABS = [
  { id: "tree", glyph: "⊞", label: "TASK TREE" },
  { id: "node", glyph: "◆", label: "NODE" },
  { id: "doc", glyph: "☰", label: "DOC" },
  { id: "asm", glyph: "▤", label: "ASM" },
  { id: "term", glyph: "⌁", label: "TERM" },
];

function attachRun(runId) {
  apiPost("/api/attach", { run_id: runId })
    .then(() => {
      state.selectedNode = null;
      state.workbenchTab = "tree";
      state.approvalTakeover = true;
      state.takeoverFor = "";
      state.treeFilter = "";
      apiGet("/api/snapshot").then(applySnapshot).catch(() => {});
    })
    .catch((err) => showToast(String(err.message || err), true));
}

function deleteRun(runId) {
  apiPost("/api/runs/delete", { run_id: runId })
    .then(() => apiGet("/api/snapshot").then(applySnapshot).catch(() => {}))
    .catch((err) => showToast(String(err.message || err), true));
}

function openRunMenu(e, runId) {
  e.preventDefault();
  state.contextMenu = { x: e.clientX, y: e.clientY, runId };
  render();
}

function renderContextMenu() {
  if (!state.contextMenu) return null;
  const m = state.contextMenu;
  const items = [];
  if (m.runId !== undefined) {
    items.push(el("div", { class: "ctx-item", onclick: () => { state.contextMenu = null; attachRun(m.runId); render(); } }, "attach"));
    items.push(el("div", { class: "ctx-item danger", onclick: () => { state.contextMenu = null; render(); if (confirm(`Delete run ${m.runId}?`)) deleteRun(m.runId); } }, "delete run"));
  } else if (m.nodeId !== undefined) {
    items.push(el("div", { class: "ctx-item", onclick: () => { state.contextMenu = null; openNode(m.nodeId, "overview"); render(); } }, "node overview"));
    items.push(el("div", { class: "ctx-item", onclick: () => { state.contextMenu = null; openReopen(m.nodeId); render(); } }, "reopen (repair)"));
    items.push(el("div", { class: "ctx-item", onclick: () => { state.contextMenu = null; render(); guarded(() => apiPost(`/api/node/${encodeURIComponent(m.nodeId)}/redispatch`, {}).then(() => { recordCli("redispatch", m.nodeId); showToast("Redispatch approval queued"); })); } }, "redispatch"));
    items.push(el("div", { class: "ctx-item", onclick: () => { state.contextMenu = null; render(); navigator.clipboard && navigator.clipboard.writeText(m.nodeId).then(() => showToast("copied id")); } }, "copy id"));
  }
  return el("div", { class: "overlay ctx-overlay", onclick: () => { state.contextMenu = null; render(); } }, [
    el("div", { class: "ctx-menu", style: `left:${m.x}px; top:${m.y}px;` }, items),
  ]);
}

function isPreviewTab(t) {
  return ["tree", "doc", "asm", "term"].includes(state.workbenchTab) && !["overview", "artifact", "gates", "diff", "versions", "chat"].includes(t);
}

function fetchWorkbenchData(id) {
  if (id === "contract") apiGet("/api/contract").then((d) => { state.contractData = d; render(); }).catch(() => {});
  if (id === "spec") apiGet("/api/spec").then((d) => { state.specText = d.text || ""; render(); }).catch(() => {});
  if (id === "spine") apiGet("/api/spine").then((d) => { state.spineText = d.text || ""; render(); }).catch(() => {});
  if (id === "manifest") apiGet("/api/manifest").then((d) => { state.manifestLines = d.lines || []; render(); }).catch(() => {});
  if (id === "asm") apiGet("/api/assembly").then((d) => { state.assembly = d; render(); }).catch(() => {});
}

function loadArtifactsIfNeeded() {
  if (state.artifactsDetail || !state.nodeDetail) return;
  const id = state.selectedNode;
  if (!id) return;
  apiGet(`/api/node/${encodeURIComponent(id)}/artifact`)
    .then((d) => { state.artifactsDetail = { tag: "current", text: d.text || "" }; render(); })
    .catch(() => {});
}

function openReopen(nodeId) {
  state.selectedNode = nodeId;
  state.workbenchTab = "node";
  state.agentTab = "overview";
  state.promptMode = "reopen";
  loadNodeDetail(nodeId);
  render();
}

function openNode(id, subTab) {
  state.selectedNode = id;
  state.workbenchTab = "node";
  if (subTab) state.agentTab = subTab;
  loadNodeDetail(id);
  if (subTab === "chat" || state.agentTab === "chat") loadThinkingIfNeeded(true);
  render();
}

function closeNode() {
  state.selectedNode = null;
  state.nodeDetail = null;
  state.agentTab = "overview";
  state.workbenchTab = "tree";
  render();
}

function loadNodeDetail(id) {
  if (!id) return;
  state.nodeDetailLoading = true;
  render();
  apiGet(`/api/node/${encodeURIComponent(id)}`)
    .then((d) => {
      state.nodeDetail = d;
      state.nodeDetailLoading = false;
      if (state.agentTab === "artifact" || state.agentTab === "versions") loadArtifactsIfNeeded();
      render();
    })
    .catch((err) => {
      state.nodeDetailLoading = false;
      showToast(String(err.message || err), true);
      render();
    });
}

function renderRightWorkbench() {
  const tab = state.workbenchTab;
  if (!state.snapshot.attached) {
    // §10 "No run attached": the inspector is empty — no chrome pretending
    // to have data.
    return el("div", { class: "workbench-panel" }, [
      el("div", { class: "workbench-tabs" }, []),
      el("div", { class: "workbench-content" }, el("div", { class: "empty-state" }, [
        el("div", { class: "dim", style: "font-size:12px;" }, "attach a run to inspect it"),
      ])),
    ]);
  }
  const tabs = WORKBENCH_TABS.map((t) =>
    el("button", {
      class: "wb-tab" + (tab === t.id ? " active" : ""),
      title: `${t.glyph} ${t.label}${t.id === "node" && state.selectedNode ? " — " + state.selectedNode : ""}`,
      onclick: () => {
        state.workbenchTab = t.id;
        if (t.id === "doc") fetchWorkbenchData(state.docTab || "contract");
        if (t.id === "asm" || t.id === "term") fetchWorkbenchData("asm");
        if (t.id === "tree") state.treeFilter = "";
        patchInspector();
      },
    }, [el("span", { class: "wb-glyph" }, t.glyph), el("span", { class: "wb-label" }, t.label)])
  );
  let body;
  const pendingPilot = (state.snapshot.pending_approvals || []).find((x) => x.kind === "pilot");
  if (state.approvalTakeover && pendingPilot && state.snapshot.control_enabled) {
    const pid = pendingPilot.context && pendingPilot.context.node_id;
    body = pid ? renderTakeoverPilot(pid) : renderApprovalEntry(pendingPilot, state.snapshot, true);
  } else if (tab === "tree") body = renderTaskTreeTab();
  else if (tab === "node") body = state.selectedNode ? renderAgentTab() : el("div", { class: "placeholder" }, "◆ select a node — tasks on the left, artifacts for repairs behind ◆ click through or press ↑/↓ + enter");
  else if (tab === "doc") body = renderDocTab();
  else if (tab === "asm") body = renderAsmTab();
  else if (tab === "term") body = renderTermTab();
  return el("div", { class: "workbench-panel" }, [
    el("div", { class: "workbench-tabs", ref: null }, tabs),
    el("div", { class: "workbench-content" }, body),
  ]);
}

/* ------------------------- node sub-tabs ------------------------- */

function renderOverview() {
  const d = state.nodeDetail;
  if (!d) return el("div", { class: "placeholder" }, "loading…");
  const snap = state.snapshot;
  const audit = d.audit || {};
  const items = audit.items || [];
  const rows = [
    ["status", el("span", null, [badge(d.status), d.attempts ? el("span", { class: "dim", style: "margin-left:6px;" }, `attempts ${d.attempts}`) : null, d.shape ? el("span", { class: "dim", style: "margin-left:6px;" }, SHAPE2[d.shape] || d.shape) : null])],
    ["inputs", d.inputs && d.inputs.length ? el("div", {}, d.inputs.map((i) => el("div", { class: "inp" + (i.exists ? "" : " missing"), title: i.ref }, [el("span", { class: "dim" }, i.exists ? "" : "⚠ "), i.ref, el("span", { class: "dim", style: "float:right;" }, `≈${i.tokens}t`)]))) : el("span", { class: "dim" }, "(none)")],
    ["budget", el("span", null, [`≈${(d.budget && d.budget.tokens) || "?"}t / ${(d.budget && d.budget.calls) || "?"} calls`])],
    ["contract rubrics", Object.values(d.rubric || {}).length ? el("div", null, Object.values(d.rubric).map((r) => el("div", { class: "rub" }, r))) : el("span", { class: "dim" }, "(none)")],
  ];
  if (d.promotion) rows.push(["promotion", el("div", { class: "promo-text" }, d.promotion)]);
  if (d.last_defect) rows.push(["last defect", el("div", { class: "dim", style: "color:var(--accent-red);" }, d.last_defect)]);
  if (d.parent) rows.push(["parent", el("span", { class: "node-link", onclick: () => openNode(d.parent, "overview") }, d.parent)]);
  const splitNote = d.status === "split" && d.split_proposal
    ? el("div", { class: "split-card" }, [
        el("div", { style: "font-weight:700; color:var(--accent-amber);" }, `⑂ split — ${d.split_proposal.reason || "no reason stated"}`),
        el("div", { class: "dim", style: "font-size:12px;" }, "children: " + ((d.split_proposal.children || []).map((c) => typeof c === "string" ? c : (c && c.id)).filter(Boolean).join(", ") || "none")),
      ])
    : null;
  return el("div", { class: "node-overview" }, [
    el("div", { class: "ov-brief" }, d.brief || ""),
    splitNote,
    ...rows.map(([k, v]) => el("div", { class: "ov-row" }, [el("span", { class: "ov-key" }, k), el("div", { class: "ov-val" }, v)])),
    el("div", { class: "ov-row" }, [
      el("span", { class: "ov-key" }, "verdict"),
      el("div", { class: "ov-val" }, [
        el("span", { class: audit.verdict === "pass" ? "gate-pass" : "gate-amber" }, audit.verdict || "(no verdict yet)"),
        audit.truncated ? el("span", { title: "artifact was over the input cap — a group was truncated for review" }, " ⚠ truncated") : null,
      ]),
    ]),
    el("div", { class: "ov-row" }, [
      el("span", { class: "ov-key" }, "artifact"),
      el("div", { class: "ov-val" }, [
        el("span", { class: "dim" }, `≈${d.artifact_tokens || 0}t`),
        el("button", { class: "btn-tiny", onclick: () => { state.agentTab = "artifact"; loadArtifactsIfNeeded(); render(); } }, "open"),
      ]),
    ]),
  ]);
}

function renderGatesTab() {
  const d = state.nodeDetail;
  if (!d) return el("div", { class: "placeholder" }, "loading…");
  const gates = d.gate_results || [];
  const audit = d.audit || {};
  const items = audit.items || [];
  const gateRows = gates.length ? gates.map((g, i) => el("div", { class: "gate-row" }, [
    el("span", { class: g.passed ? "gate-pass" : "gate-fail" }, (g.passed ? "✓" : "✕") + " " + g.gate),
    el("span", { class: "dim", style: "margin-left:auto;" }, g.detail || ""),
  ])) : el("div", { class: "dim" }, "(no cached gate results)");
  const itemRows = items.length ? items.map((it) => el("div", { class: "gate-row" + (it.pass ? "" : " fail-row") }, [
    el("span", { class: it.pass ? "gate-pass" : "gate-fail" }, it.pass ? "✓" : "✕"),
    el("span", { class: "item-id" }, it.id),
    it.node_ids && it.node_ids.length ? el("span", { class: "node-link dim", onclick: () => it.node_ids.length === 1 ? openNode(it.node_ids[0], "overview") : null }, `→ ${it.node_ids.join(", ")}`) : null,
    el("span", { class: "dim", style: "margin-left:auto; font-size:11px;" }, it.class || ""),
  ].concat(it.defect ? [el("div", { class: "defect", style: "grid-column:1/-1;" }, it.defect)] : []))) : el("div", { class: "dim" }, "(no review items)");
  // §5.2: a verdict reached over a cut artifact is a weaker verdict — the
  // truncated flag must be visible here, not only on the Overview tab.
  const truncatedChip = d.truncated ? el("span", { class: "truncated-chip", title: "the artifact was over the reviewer input cap — a section group was truncated for review" }, "⚠ truncated") : null;
  return el("div", { class: "gates-tab" }, [
    el("div", { class: "sub-hdr" }, ["GATES (machine, cached at dispatch)", truncatedChip]),
    ...gateRows,
    el("div", { class: "sub-hdr", style: "margin-top:14px;" }, "REVIEW ITEMS"),
    ...itemRows,
  ]);
}

function renderDiffTab() {
  const d = state.nodeDiff;
  if (!d) return el("div", { class: "placeholder" }, "loading diff…");
  const lines = (d.diff || d.text || "").split("\n");
  return el("pre", { class: "diff-pre" }, lines.map((l) => el("div", { class: `diff-line diff-${diffLineKind(l)}` }, l)));
}

function renderVersionsTab() {
  const d = state.nodeDetail;
  if (!d) return el("div", { class: "placeholder" }, "loading…");
  const versions = d.versions || [];
  const currentBtn = el("button", { class: (state.selectedArtifactTag === undefined ? "v-active" : ""), onclick: () => { state.selectedArtifactTag = undefined; loadArtifactsIfNeeded(); render(); } }, "current");
  const versionBtns = versions.map((v) => el("button", { class: state.selectedArtifactTag === v ? "v-active" : "", onclick: () => { state.selectedArtifactTag = v; loadArtifactsIfNeeded(); render(); } }, v));
  const body = state.artifactsDetail ? el("pre", { class: "artifact-pre" }, state.artifactsDetail.text) : el("div", { class: "placeholder" }, "select a version");
  return el("div", { class: "versions-tab" }, [
    el("div", { class: "sub-hdr" }, "VERSIONS (pre-repair snapshots)"),
    el("div", { class: "v-btns" }, [currentBtn, ...versionBtns]),
    body,
  ]);
}

function renderArtifactsTab() {
  const d = state.nodeDetail;
  if (!d) return el("div", { class: "placeholder" }, "loading…");
  if (!state.artifactsDetail) return el("div", { class: "placeholder" }, "loading artifact…");
  // §10: an empty artifact is a real, diagnostic state — render it
  // explicitly, not as an empty <pre> that reads like a rendering failure.
  if (!state.artifactsDetail.text.trim()) {
    return el("div", { class: "empty-artifact" }, [
      el("span", { style: "font-size:22px; opacity:.7;" }, "∅"),
      el("div", { class: "dim" }, "empty artifact — the episode ended without producing content"),
    ]);
  }
  return el("pre", { class: "artifact-pre" }, state.artifactsDetail.text);
}

function renderAgentTab() {
  const id = state.selectedNode;
  const d = state.nodeDetail;
  const sub = (state.snapshot.subagents || []).find((s) => s.id === id);
  if (!d && !state.nodeDetailLoading) { loadNodeDetail(id); return el("div", { class: "placeholder" }, "loading…"); }
  const tabs = [
    ["overview", "Overview"],
    ["chat", "Chat"],
    ["gates", "Gates"],
    ["artifact", "Artifact"],
    ["versions", "Versions"],
    ["diff", "Diff"],
  ];
  const subBadge = sub ? el("span", { class: "badge", "data-status": sub.status }, sub.status) : null;
  const liveBadge = sub && sub.live ? el("span", { class: "badge live-badge" }, "● live") : null;
  // §5.2 dense label-free header: `node-03.02 · attempt 2 · prose ·
  // 3.1k/24k tok · ●live` — position matters, words don't.
  const metaBits = [];
  if (d) {
    if (d.attempts) metaBits.push(`attempt ${d.attempts}`);
    if (d.shape) metaBits.push(SHAPE2[d.shape] || d.shape);
    const bud = d.budget && d.budget.tokens ? (d.budget.tokens / 1000).toFixed(1) + "K" : "?";
    metaBits.push(`${(d.artifact_tokens || 0) / 1000}K/${bud} tok`);
    if (d.parent) metaBits.push(`child of ${d.parent}`);
  }
  const metaLine = metaBits.length ? el("span", { class: "dim", style: "font-size:11px; margin-left:8px;" }, metaBits.join(" · ")) : null;
  const hdr = el("div", { class: "agent-panel-hdr" }, [
    el("span", { class: "agent-id" }, id),
    subBadge, liveBadge, metaLine,
    el("span", { style: "margin-left:auto;" }, [
      el("button", { class: "btn-tiny", title: "go back to task tree", onclick: closeNode }, "✕"),
    ]),
  ]);
  const tabBar = el("div", { class: "agent-tabs" }, tabs.map(([k, label]) =>
    el("button", { class: "agent-tab" + (state.agentTab === k ? " active" : ""), onclick: () => {
      state.agentTab = k;
      if (k === "chat") loadThinkingIfNeeded(true);
      if (k === "artifact" || k === "versions") loadArtifactsIfNeeded();
      if (k === "diff") apiGet(`/api/node/${encodeURIComponent(id)}/diff/current`).then((r) => { state.nodeDiff = r; render(); }).catch(() => {});
      render();
    } }, label)
  ));
  let body;
  if (state.agentTab === "overview") {
    const pilotA = (state.snapshot.pending_approvals || []).find((x) => x.kind === "pilot" && (x.context || {}).node_id === id);
    body = (pilotA && d && d.pilot_original) ? renderPilotEditor() : renderOverview();
  }
  else if (state.agentTab === "chat") {
    const entries = (state.nodeThinking && state.nodeThinking.entries) ? state.nodeThinking.entries : [];
    body = el("div", { class: "chat-feed node-chat" }, entries.map(renderAgentChatEntry));
  } else if (state.agentTab === "gates") body = renderGatesTab();
  else if (state.agentTab === "artifact") body = renderArtifactsTab();
  else if (state.agentTab === "versions") body = renderVersionsTab();
  else if (state.agentTab === "diff") body = renderDiffTab();
  return el("div", { class: "agent-panel" }, [hdr, tabBar, el("div", { class: "agent-body" }, body)]);
}

/* ------------------------- pilot editor + takeover ------------------------- */

function renderPilotEditor() {
  const a = (state.snapshot.pending_approvals || []).find((x) => x.kind === "pilot");
  const d = state.nodeDetail;
  if (!a || !d || !d.pilot_original) {
    return el("div", { class: "placeholder" }, "no pilot approval pending for this node");
  }
  const draftKey = a.approval_id;
  if (state.pilotDrafts[draftKey] === undefined) state.pilotDrafts[draftKey] = d.artifact || "";
  const originalLines = d.pilot_original.split("\n");
  const editedLines = (state.pilotDrafts[draftKey] || "").split("\n");
  const saveBtn = el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
    await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/pilot-save`, { text: state.pilotDrafts[draftKey] || "" });
    recordCli("pilot", a.context && a.context.node_id || d.id);
    showToast("Pilot edit saved & approval resolved");
  }) }, "Save & approve edit");
  const asIsBtn = el("button", { disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
    if (confirm("Approve this pilot as-is (accepts the Writer's output without changes)?")) {
      await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "approve" });
      recordCli("pilot", a.context && a.context.node_id || d.id);
      showToast("Approved as-is");
    }
  }) }, "Approve as-is");
  const editor = el("textarea", { class: "pilot-editor", "data-key": `pilot-${draftKey}`, rows: "10" });
  editor.value = state.pilotDrafts[draftKey] || "";
  editor.addEventListener("input", () => { state.pilotDrafts[draftKey] = editor.value; });
  return el("div", null, [
    el("div", { class: "sub-hdr" }, "✏️ PILOT EDIT — frozen original (left) vs your edit (right)"),
    el("div", { class: "pilot-editor-panes" }, [
      el("pre", { class: "pilot-original-pre" }, originalLines.map((l) => el("div", null, l))),
      editor,
    ]),
    el("div", { class: "pilot-editor-actions" }, [saveBtn, asIsBtn]),
    el("div", { class: "dim", style: "font-size:11px; margin-top:6px;" }, "The diff between your edit and the frozen original becomes the contract rules — cut historical asides, shrink examples to three lines, anything generalizable."),
  ]);
}

function renderPendingPilotNote() { return null; }

function renderTakeoverPilot(nodeId) {
  const a = (state.snapshot.pending_approvals || []).find((x) => x.kind === "pilot");
  if (!a) return el("div", { class: "placeholder" }, "no pilot pending");
  const d = state.nodeDetail;
  if (!d || d.id !== nodeId) {
    loadNodeDetail(nodeId);
    return el("div", { class: "placeholder" }, "loading pilot node…");
  }
  const dismissBtn = el("button", { class: "btn-tiny", onclick: () => dismissTakeover(a) }, "⌫ dismiss (any key re-arms)");
  return el("div", { class: "takeover-pilot" }, [
    el("div", { class: "sub-hdr" }, `⏸ PILOT · ${a.context && a.context.node_id ? a.context.node_id : nodeId} · awaiting your edit`),
    renderPilotEditor(),
    el("div", { style: "margin-top:10px;" }, dismissBtn),
  ]);
}

/* ------------------------- task tree tab ------------------------- */

function buildNodeTreeIndex() {
  const tree = state.snapshot.tree || [];
  const rows = Array.isArray(tree) ? tree : (tree.nodes || []);
  const index = {};
  const order = [];
  for (const n of rows) {
    if (!n || typeof n.id !== "string") continue;
    const parts = n.id.split(".");
    let key = "";
    for (let i = 1; i <= parts.length; i++) {
      key = parts.slice(0, i).join(".");
      if (!(key in index)) { index[key] = { id: key, children: [], node: null }; order.push(key); }
    }
    index[key].node = n;
    const parentKey = parts.slice(0, -1).join(".");
    if (parentKey && index[parentKey]) index[parentKey].children.push(key);
  }
  // top-level = no parent claim
  const tops = order.filter((k) => !k.includes(".") || !index[k.split(".").slice(0, -1).join(".")]);
  return { index, tops };
}

function treeRowClass(key, n) {
  const cls = ["tree-row"];
  if (!n) cls.push("tree-row-folder");
  return cls.join(" ");
}

// §5.1: a live subagent attached to a node (its own Writer dispatch, or a
// ~repair/~research child) renders as a clickable ● pill that opens the
// node's Chat directly on that subagent.
function liveSubFor(nodeId) {
  const subs = state.snapshot.subagents || [];
  return subs.find((s) => s.live && (s.id === nodeId || s.id.startsWith(nodeId + "~"))) || null;
}

function renderTreeBranch(key, depth, visible) {
  const { index } = buildNodeTreeIndex();
  const entry = index[key];
  if (!entry) return null;
  const n = entry.node;
  visible.push(key);
  const glyph = n ? (NODE_GLYPH[n.status] || "·") : (state.treeCollapsed[key] ? "▸" : "▾");
  const segClass = {
    passed: "pass", split: "esc", failed: "fail", blocked: "fail", stale: "paused",
    dispatched: "run", awaiting_review: "run", pending: "", ready: "",
  }[n ? n.status : ""] || "";
  const gatePips = (n && Array.isArray(n.gate_results)) ? n.gate_results.map((g) => el("span", { class: "gate-pip " + (g.passed ? "on" : "off"), title: `${g.gate}: ${g.detail || (g.passed ? "ok" : "fail")}` }, g.passed ? GATE_PIP_PASS : GATE_PIP_FAIL)) : null;
  const shape = n ? (SHAPE2[n.shape] || (n.shape ? n.shape.slice(0, 2) : "")) : "";
  const attrs = {};
  if (n && n.artifact_tokens !== undefined) attrs["data-tokens"] = n.artifact_tokens;
  const rowAttrs = Object.assign({ class: treeRowClass(key, n), style: `padding-left:${8 + depth * 14}px;` }, attrs);
  if (n) rowAttrs.onclick = () => openNode(n.id, "overview");
  else rowAttrs.onclick = () => { state.treeCollapsed[key] = !state.treeCollapsed[key]; render(); };
  if (n) rowAttrs.oncontextmenu = (e) => { e.preventDefault(); state.contextMenu = { x: e.clientX, y: e.clientY, nodeId: n.id }; render(); };
  const liveSub = n ? liveSubFor(n.id) : null;
  const attemptsSpan = n && n.attempts ? el("span", {
    class: "row-attempts" + (n.status === "blocked" || n.status === "failed" ? " warn" : (n.attempts >= 2 ? " warm" : "")),
    title: `attempts ${n.attempts}/${n.gates ? n.gates : "?"}`,
  }, `a${n.attempts}`) : null;
  const artSpan = n && n.artifact_count ? el("span", { class: "dim", style: "font-size:10px;", title: `${n.artifact_count} artifact${n.artifact_count > 1 ? "s" : ""} (current + versions)` }, `📁${n.artifact_count}`) : null;
  const livePill = liveSub ? el("span", {
    class: "tree-live",
    title: `live subagent ${liveSub.id} — open its chat`,
    onclick: (e) => { e.stopPropagation(); openNode(liveSub.id, "chat"); },
  }, "●") : null;
  const row = el("div", rowAttrs, [
    el("span", { class: "row-glyph tree-glyph" }, glyph),
    el("span", { class: "row-id" }, key),
    shape ? el("span", { class: "dim", style: "font-size:10px; margin:0 6px;" }, shape) : null,
    el("span", { class: "gate-pips" }, gatePips),
    attemptsSpan,
    el("span", { class: "dim", style: "font-size:10px; margin-left:auto;" }, n ? `${(n.artifact_tokens !== undefined ? `≈${n.artifact_tokens}t` : "")}` : ""),
    artSpan,
    livePill,
  ]);
  const kids = entry.children.filter((c) => {
    if (state.treeFilter) return c.includes(state.treeFilter);
    return !state.treeCollapsed[key];
  });
  return [row].concat(kids.map((c) => renderTreeBranch(c, depth + 1, visible)));
}

function visibleTreeRows() {
  const { tops } = buildNodeTreeIndex();
  const visible = [];
  for (const t of tops) renderTreeBranch(t, 0, visible);
  return visible;
}

function renderTaskTreeTab() {
  const { tops } = buildNodeTreeIndex();
  const counts = state.snapshot.tree_counts || {};
  const filterInput = el("input", { type: "text", placeholder: "filter node ids…", style: "width:100%;" });
  filterInput.value = state.treeFilter;
  const listHost = el("div", { class: "tree-list" });
  const refreshList = () => {
    const { tops: t2 } = buildNodeTreeIndex();
    const vis = [];
    listHost.replaceChildren(...t2.map((t) => renderTreeBranch(t, 0, vis)));
  };
  let filterTimer = null;
  filterInput.addEventListener("input", () => {
    state.treeFilter = filterInput.value;
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(refreshList, 60);  // §Responsive: typing filters the list in place; the input keeps focus
  });
  refreshList();
  return el("div", { class: "tree-tab" }, [
    el("div", { class: "tree-hdr" }, [
      el("span", { class: "sub-hdr" }, `TASK TREE — ${counts.passed || 0}/${(counts.passed || 0) + (counts.failed || 0) + (counts.blocked || 0) + (counts.pending || 0) + (counts.ready || 0) + (counts.dispatched || 0) + (counts.awaiting_review || 0) + (counts.stale || 0) + (counts.split || 0)}`),
      el("button", { class: "btn-tiny", onclick: () => { state.promptMode = "command"; state.promptText = "> redispatch "; patchCmdbar(); focusCmdbar(); } }, "redispatch"),
    ]),
    el("div", { class: "tree-filter" }, filterInput),
    listHost,
  ]);
}

/* ------------------------- doc / asm / term tabs ------------------------- */

function renderDocTab() {
  const tabs = ["contract", "spec", "spine", "manifest"];
  const labels = { contract: "📜 Contract", spec: "spec", spine: "spine", manifest: "manifest" };
  const bar = el("div", { class: "agent-tabs" }, tabs.map((t) =>
    el("button", { class: "agent-tab" + (state.docTab === t ? " active" : ""), onclick: () => { state.docTab = t; fetchWorkbenchData(t); render(); } }, labels[t])
  ));
  let body;
  if (state.docTab === "contract") {
    const c = state.contractData || { text: "", tokens: 0, ceiling: 1500 };
    const pct = c.ceiling ? Math.min(100, Math.round((c.tokens / c.ceiling) * 100)) : 0;
    // §5.3: [ amend ] sits on the contract view, not in a global menu —
    // amendment is a contract operation and its blast radius is the whole run.
    const amendBtn = state.snapshot.control_enabled ? el("button", { class: "btn-tiny", title: "append a rule to the contract (whole run)", onclick: () => { state.promptMode = "amend"; patchCmdbar(); focusCmdbar(); } }, "✏️ amend…") : null;
    body = el("div", { class: "doc-body" }, [
      el("div", { class: "contract-meter" }, [
        el("span", { class: "dim" }, `${c.tokens}t / ceiling ${c.ceiling}t`),
        el("div", { class: "meter", style: `width:${pct}%;` + (pct > 90 ? "background:var(--accent-red);" : "") }),
        amendBtn,
      ]),
      el("pre", { class: "doc-pre" }, c.text),
    ]);
  } else if (state.docTab === "spec") body = el("pre", { class: "doc-pre" }, state.specText || "");
  else if (state.docTab === "spine") body = el("pre", { class: "doc-pre" }, state.spineText || "");
  else {
    const lines = state.manifestLines || [];
    body = el("div", { class: "manifest-body" }, lines.length ? lines.map((l) => el("div", { class: "gate-row" }, [
      el("span", { class: "dim" }, l.node || ""),
      el("span", { class: "dim", style: "margin-left:auto;" }, `${l.tokens || "?"}t`),
    ])) : el("div", { class: "dim" }, "(no manifest lines yet)"));
  }
  return el("div", { class: "workbench-doc" }, [bar, body]);
}

function renderAsmTab() {
  const a = state.assembly;
  if (!a) return el("div", { class: "placeholder" }, "loading assembly…");
  const checksArr = a.checks || {};
  const checks = Array.isArray(checksArr) ? checksArr : (checksArr.checks || []);
  // §5.4: failed cross-cutting checks carry offending node ids as details
  // lines ("node-04: currently fails gates [...]") — each must be a
  // clickable chip straight through to that node, not bare text.
  const knownIds = new Set((state.snapshot.tree || []).map((n) => n && n.id).filter(Boolean));
  const rows = checks.length ? checks.map((c) => {
    const details = Array.isArray(c.details) ? c.details : (c.detail ? [c.detail] : []);
    const lines = details.map((detail) => {
      const m = /^([\w.~\-]+):\s*(.*)$/s.exec(detail || "");
      if (m && knownIds.has(m[1])) {
        return el("div", { class: "asm-detail" }, [
          el("span", { class: "node-link", onclick: () => openNode(m[1], "overview") }, m[1] + ":"),
          el("span", { class: "dim" }, m[2] || ""),
        ]);
      }
      return el("div", { class: "asm-detail" }, el("span", { class: "dim" }, detail));
    });
    return el("div", { class: "gate-row asm-check" }, [
      el("span", { class: c.passed ? "gate-pass" : "gate-fail" }, (c.passed ? "✓" : "✕") + " " + (c.name || c.id || "")),
      el("div", { class: "asm-details" }, lines),
    ]);
  }) : el("div", { class: "dim" }, "(no checks recorded)");
  return el("div", { class: "asm-body" }, [
    el("div", { class: "sub-hdr" }, "CROSS-CUTTING CHECKS"),
    ...rows,
    el("div", { class: "sub-hdr", style: "margin-top:14px;" }, "COMPILE LOG"),
    el("pre", { class: "doc-pre log-pre" }, a.compile_log || "(no compile log)"),
    el("div", { class: "sub-hdr", style: "margin-top:14px;" }, "INDEX"),
    el("pre", { class: "doc-pre" }, a.index || "(no index)"),
  ]);
}

// §5.5 Terminal: scrolling raw events.jsonl tail, filterable by type, and
// a copyable CLI equivalent of the last UI action — the escape hatch and
// teaching device in one. (The old PROGRESS/TREE tables here duplicated the
// rail and the Tree tab; dropped.)
function renderTermTab() {
  const snap = state.snapshot;
  const events = snap.events || [];
  const types = Array.from(new Set(events.map((e) => e.type))).sort();
  const filter = state.terminalFilter || "all";
  const select = el("select", { class: "term-filter", onchange: (e) => { state.terminalFilter = e.target.value; render(); } }, [
    el("option", { value: "all", selected: filter === "all" ? "selected" : null }, `all (${events.length})`),
    ...types.map((t) => el("option", { value: t, selected: filter === t ? "selected" : null }, `${t} (${events.filter((e) => e.type === t).length})`)),
  ]);
  const rows = events.filter((e) => filter === "all" || e.type === filter).slice(-200).reverse().map((ev) => {
    const textParts = [ev.type];
    if (ev.phase) textParts.push(`[${ev.phase}]`);
    if (ev.status) textParts.push(`- ${ev.status}`);
    if (ev.error) textParts.push(`ERR "${ev.error}"`);
    return el("div", { class: "gate-row term-event" }, [
      el("span", { class: "term-glyph", title: ev.type }, _EVENT_LABEL[ev.type] ? _EVENT_LABEL[ev.type].split(" ")[0] : "·"),
      el("span", { class: "dim", style: "min-width:76px;" }, fmtTime(ev.ts)),
      ev.node_id && ev.node_id !== "-" ? el("span", { class: "node-link", onclick: () => openNode(ev.node_id, "overview") }, ev.node_id) : null,
      el("span", { class: "dim", style: "margin-left:auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" }, textParts.join(" ")),
    ]);
  });
  const cliLine = state.lastCliCommand
    ? el("div", { class: "term-cli" }, [
        el("span", { class: "dim" }, "CLI equivalent of your last action:"),
        el("code", { style: "flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" }, state.lastCliCommand),
        el("button", { class: "btn-tiny", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(state.lastCliCommand).then(() => showToast("command copied")); } }, "copy"),
      ])
    : el("div", { class: "dim", style: "font-size:11px; margin-top:8px;" }, "(no UI actions yet — approve/amend/reopen/redispatch/escalate/halt record their CLI form here)");
  return el("div", { class: "term-body" }, [
    el("div", { class: "sub-hdr" }, [
      el("span", null, `TERMINAL — events.jsonl tail (${events.length} total)`),
      select,
    ]),
    el("div", { class: "term-events" }, rows.length ? rows : el("div", { class: "dim" }, "(no matching events)")),
    el("div", { class: "sub-hdr", style: "margin-top:14px;" }, "LAST UI ACTION → CLI"),
    cliLine,
  ]);
}

/* ------------------------- new run modal ------------------------- */

function renderNewRunModal() {
  if (!state.newRunOpen) return null;
  const f = (key, label, type, fieldCh) => el("label", { class: "form-field" }, [
    el("span", { class: "form-label" }, label),
    fieldCh,
  ]);
  const set = (k, v) => { state.newRun[k] = v; };  // §Responsive: typing in the modal never rebuilds — values live in state
  const input = (key, type, ph) => {
    const el2 = el("input", { type: type || "text", placeholder: ph });
    el2.value = state.newRun[key] || "";
    el2.addEventListener("input", () => set(key, el2.value));
    return el2;
  };
  const form = el("div", { class: "form-grid" }, [
    f("run_id", "Run id", input("runId", "text", "e.g. monads-01")),
    f("goal", "Goal", input("goal", "text", "the one-sentence goal")),
    f("source", "source.txt path or @path", input("source", "text", "@/path/to/corpus.txt or leave empty (workspace)")),
    f("workspace", "workspace root (optional, overrides source)", input("workspace", "text", "@/path/to/repo")),
    f("model", "model", input("model", "text", "provider model id")),
    f("compile", "compile command", input("compile", "text", "e.g. python3 -m unittest")),
    f("tier", "tier floor (0-3)", input("tier", "text", "")),
    f("dispatch", "dispatch policy",
      el("select", { onchange: (e) => set("dispatch_policy", e.target.value) },
        ["model", "deterministic"].map((v) => el("option", { value: v, selected: state.newRun.dispatch_policy === v ? "selected" : null }, v)))),
    f("survey", "survey mode",
      el("select", { onchange: (e) => set("survey_mode", e.target.value) },
        ["model", "deterministic"].map((v) => el("option", { value: v, selected: state.newRun.survey_mode === v ? "selected" : null }, v)))),
    f("max_rounds", "max rounds", input("max_rounds", "number", "100")),
    f("max_attempts", "max attempts", input("max_attempts", "number", "3")),
  ]);
  const flag = (key, label) => el("label", { class: "form-flag" }, [
    el("input", { type: "checkbox", checked: state.newRun[key] ? "checked" : null, onchange: (e) => set(key, e.target.checked) }),
    el("span", null, label),
  ]);
  return el("div", { class: "overlay", onclick: (e) => { if (e.target === e.currentTarget) { state.newRunOpen = false; render(); } } }, [
    el("div", { class: "panel newrun-panel" }, [
      el("div", { class: "panel-hdr" }, "＋ New run"),
      el("div", { class: "panel-body" }, [
        form,
        el("div", { class: "form-flags" }, [flag("document_review", "document review"), flag("inline_spans", "inline spans")]),
      ]),
      el("div", { class: "panel-foot" }, [
        el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          const r = await apiPost("/api/runs", {
            run_id: state.newRun.runId || undefined,
            goal: state.newRun.goal,
            source: state.newRun.source || undefined,
            compile_command: state.newRun.compile || undefined,
            workspace: state.newRun.workspace || undefined,
            model: state.newRun.model || undefined,
            tier_floor: state.newRun.tier || undefined,
            dispatch_policy: state.newRun.dispatch_policy,
            survey_mode: state.newRun.survey_mode,
            max_rounds: parseInt(state.newRun.max_rounds, 10) || undefined,
            max_attempts: parseInt(state.newRun.max_attempts, 10) || undefined,
            document_review: state.newRun.document_review,
            inline_spans: state.newRun.inline_spans,
          });
          state.newRun = Object.assign({}, state.newRun, { runId: "", goal: "", source: "", compile: "", workspace: "", model: "", tier: "" });
          state.newRunOpen = false;
          if (r && r.run_id) await attachRun(r.run_id);
          else apiGet("/api/snapshot").then(applySnapshot).catch(() => {});
        }) }, "Start run…"),
        el("button", { onclick: () => { state.newRunOpen = false; render(); } }, "Close"),
      ]),
    ]),
  ]);
}

/* ------------------------- auth overlay ------------------------- */

function renderAuthOverlay() {
  if (!state.authRequired) return null;
  const input = el("input", { type: "password", "data-key": "authDraft", placeholder: "dashboard auth token", style: "width:100%;" });
  input.value = state.authDraft || "";
  input.addEventListener("input", () => { state.authDraft = input.value; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitAuth(); } });
  return el("div", { class: "overlay auth-overlay" }, [
    el("div", { class: "panel auth-panel" }, [
      el("div", { class: "panel-hdr" }, "🔐 Dashboard auth required"),
      el("div", { class: "panel-body" }, [
        el("div", { class: "dim", style: "margin-bottom:10px;" }, "This dashboard is protected by an auth token (the SSE live stream needs the cookie the first authenticated request sets)."),
        input,
      ]),
      el("div", { class: "panel-foot" }, el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(submitAuth) }, "Unlock…")),
    ]),
  ]);
}

async function submitAuth() {
  const token = (state.authDraft || "").trim();
  if (!token) { showToast("enter the token", true); return; }
  state.authToken = token;
  const ok = await apiGet("/api/runs", { allowAuthPrompt: true }).catch(() => null);
  if (ok) {
    state.authRequired = false;
    state.authDraft = "";
    startLive();
    apiGet("/api/snapshot").then(applySnapshot).catch(() => {});
    showToast("unlocked");
  } else {
    state.authToken = "";
    showToast("wrong token", true);
  }
  render();
}

/* ------------------------- root render ------------------------- */
// §RESPONSIVE: there is no single ``render()`` doing a full ``#app``
// teardown any more — that was the lag the operator hit on every keystroke
// and every button click: a synchronous rebuild of the entire DOM (hundreds
// of tree rows + every subagent's chat) per input event. Now the chrome is
// built once, then each region is patched in place by rebuilding only its
// own container's children. A burst of ``schedulePatch(region)`` calls
// collapses into one ``requestAnimationFrame`` flush, so a snapshot poll
// landing mid-typing never rebuilds a region the operator isn't looking at.
//
// No keyboard shortcuts: ``onGlobalKey`` and the palette/keymap/g-prefix
// machinery are gone; commands come from the ``>`` command bar only. The old
// ``data-key`` focus-restore and ``captureScrollStates``/``restoreScrollStates``
// dance existed *only* to survive keyboard-driven full teardowns — both gone.

const els = {};

function buildChrome() {
  const appRoot = el("div", { class: "app-root" });
  els.frame = el("div", { class: "chrome-frame" });
  els.cmrail = el("div", null, null);   // placeholder; rail rebuilds its own subtree
  els.header = el("div", null, null);
  els.workspace = el("div", { class: "kd-workspace" }, [
    els.nav = el("div", null, null),
    els.center = el("div", null, null),
    els.inspector = el("div", null, null),
  ]);
  els.cmdbar = el("div", null, null);
  els.overlays = el("div", null, null);
  els.jobs = el("div", null, null);
  els.toast = el("div", null, null);
  els.frame.replaceChildren(els.cmrail, els.header, els.workspace, els.cmdbar);
  appRoot.replaceChildren(els.frame, els.jobs, els.overlays, els.toast);
  root.replaceChildren(appRoot);
}

// Region patchers — each rebuilds only its own container's subtree.
function patchRail() { if (els.cmrail) els.cmrail.replaceChildren(renderRail()); }
function patchHeader() { if (els.header) els.header.replaceChildren(renderHeaderRow()); }
function patchNav() { if (els.nav) els.nav.replaceChildren(renderNav()); }
function patchCenter() { if (els.center) els.center.replaceChildren(renderCenterStream()); }
function patchInspector() { if (els.inspector) els.inspector.replaceChildren(renderRightWorkbench()); }
function patchCmdbar() { if (els.cmdbar) els.cmdbar.replaceChildren(renderCommandBar()); }
function patchJobs() {
  if (!els.jobs) return;
  const running = (state.snapshot.jobs || []).filter((j) => j.status === "running" || j.status === "queued");
  if (!running.length) { els.jobs.replaceChildren(); return; }
  els.jobs.replaceChildren(el("div", { class: "jobs-strip" }, running.map((j) =>
    el("div", { class: "job-chip" }, [
      el("span", null, `${j.kind || "job"} ${j.job_id || ""}`),
      el("button", { class: "btn-tiny", disabled: !state.snapshot.control_enabled ? "" : null, onclick: () => guarded(() => apiPost(`/api/jobs/${encodeURIComponent(j.job_id || "")}/cancel`, {}).then(() => showToast("job cancel requested"))) }, "✕"),
    ])
  )));
}
function patchOverlays() {
  if (!els.overlays) return;
  els.overlays.replaceChildren(
    renderContextMenu(),
    renderRunSwitcher(),
    renderNewRunModal(),
    renderAuthOverlay(),
    renderHelpModal(),
  );
}
function patchToast() {
  if (!els.toast) return;
  els.toast.replaceChildren(
    state.toast ? el("div", { class: "toast" + (state.toast.isError ? " err" : ""), onclick: () => { state.toast = null; patchToast(); } }, state.toast.message) : null
  );
}

// Coalesce a burst of region patches into one rAF flush. Multiple
// ``schedulePatch(...)`` calls in the same frame run their fns exactly once,
// deduped, in registration order.
let _pending = new Set();
let _rafQueued = false;
function schedulePatch(...fns) {
  for (const f of fns) if (typeof f === "function") _pending.add(f);
  if (_rafQueued) return;
  _rafQueued = true;
  requestAnimationFrame(() => {
    _rafQueued = false;
    const run = Array.from(_pending);
    _pending = new Set();
    for (const f of run) {
      try { f(); } catch (e) { console.error(e); }
    }
  });
}

// The snapshot poll re-patches every region. Each region patcher rebuilds
// only its own container; the operator's text inputs live inside cmdbar
// (which a snapshot does NOT touch unless typing already scheduled it).
function scheduleAll() {
  schedulePatch(patchRail, patchHeader, patchNav, patchCenter, patchInspector, patchCmdbar, patchJobs, patchOverlays, patchToast);
}

// §RESPONSIVE: every button click updates `state` then schedules the
// regions that visibly depend on it — never the whole app.
function render() { scheduleAll(); }

function renderHelpModal() {
  if (!state.helpOpen) return null;
  const cmds = _memo(buildCommands);
  const groups = Object.values(cmds).map((c) =>
    el("div", { class: "key-row" }, [el("span", { class: "keycap" }, c.usage), el("span", { class: "key-desc" }, c.label)])
  );
  return el("div", { class: "overlay", onclick: (e) => { if (e.target === e.currentTarget) { state.helpOpen = false; patchOverlays(); } } }, [
    el("div", { class: "panel keymap-panel" }, [
      el("div", { class: "panel-hdr" }, "Slash commands"),
      el("div", { class: "panel-body" }, groups.length ? groups : el("div", { class: "dim" }, "(none)")),
      el("div", { class: "panel-foot" }, el("button", { onclick: () => { state.helpOpen = false; patchOverlays(); } }, "Close")),
    ]),
  ]);
}

/* ------------------------- boot ------------------------- */

const DEFAULT_FAVICON = "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="%236366f1"/></svg>');
const RED_FAVICON = "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="%23f43f5e"/></svg>');

document.addEventListener("DOMContentLoaded", () => {
  const icon = document.createElement("link");
  icon.rel = "icon";
  icon.href = DEFAULT_FAVICON;
  document.head.appendChild(icon);
  buildChrome();
  scheduleAll();
  apiGet("/api/runs", { allowAuthPrompt: true })
    .then((d) => {
      state.authRequired = false;
      const runs = d.runs || [];
      if (state.snapshot && state.snapshot.attached) {
        startLive();
      } else if (runs.length) {
        attachRun(runs[0].id);
      } else {
        startLive();
      }
    })
    .catch((err) => {
      state.authRequired = true;
      scheduleAll();
    });
  setInterval(() => {
    const now = new Date();
    const clock = document.querySelector(".rail-a40");
    if (clock && Math.abs(state.snapshot.elapsed || 0) > 0) {
      clock.textContent = fmtDur((state.snapshot.elapsed || 0) + 0.5);
    }
    if (!state.sseLive || now.getSeconds() % 10 === 0) loadMainAgentThinking();
  }, 5000);
});