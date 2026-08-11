// Kusudaemon web dashboard.
// Preserves every backend API hook the old 3-pane view had (/api/snapshot,
// /api/runs, /api/halt, /api/amend, /api/approvals) and adds the surface
// the (now-deleted) Textual TUI had on top of it: a Subagents view over
// every dispatched Writer/repair/research episode, a right-workbench Agent
// tab (Overview/Artifact/Diff/Chat) for a specific node or subagent's own
// detail, live mid-episode interject, and per-node reopen. Left sidebar is
// navigation only (Runs/Subagents/Phases); every detail view -- including
// the Task Tree -- opens on the right, never a floating overlay.

const PHASES = ["intake", "survey", "plan", "pilot", "research", "execute", "assemble"];

const state = {
  snapshot: { attached: false, runs: [], control_enabled: true },
  sidebarTab: "sessions", // 'sessions' | 'subagents' | 'phases' -- navigation only; all detail views open in the right-side workbench (see workbenchTab)
  workbenchTab: "tree",   // 'tree' | 'code' | 'agent' | 'contract' | 'spec' | 'spine' | 'assembly' | 'terminal' -- Task Tree is the default view
  selectedNode: null,
  nodeDetail: null,
  nodeSubagent: null,
  nodeDetailLoading: false,
  agentTab: "overview", // 'overview' | 'artifact' | 'artifacts' | 'diff' | 'chat' -- sub-tabs within the right workbench's Agent tab
  nodeDiff: null,        // [{tag, lines}] once loaded, keyed to selectedNode
  nodeThinking: null,    // [{role, text}] once loaded, keyed to selectedNode -- also the live chat feed for whichever agent is open, kept fresh by applySnapshot's isLive(selectedNode) refresh
  artifactsDetail: null,      // owner node's /api/node/<id> detail (artifact + versions), keyed to artifactsOwnerId(selectedNode)
  selectedArtifactTag: undefined, // undefined = not yet loaded/picked; null = "current" artifact; a version tag string otherwise
  selectedArtifactText: null,
  mainAgentThinking: null, // {id, label, entries, live} -- "what's running right now," shown livestreamed in the main chat; see loadMainAgentThinking(). Independent of a specific subagent opened in the right panel's Agent tab, which keeps its own history regardless of what's currently live.
  newRunOpen: false,
  busy: false,
  toast: null,
  contractText: "",
  specText: "",
  spineText: "",
  assembly: null,
  promptText: "",
  promptMode: "msg_agent", // 'msg_agent' | 'auto' | 'amend' | 'reopen'
  targetAgentId: "main",
  targetAgentManual: false, // true once the operator picks a target by hand; see renderPromptBar
  newRun: { runId: "", goal: "", source: "", model: "", compile: "" },
  interjectDrafts: {}, // nodeId -> text
  reopenDrafts: {},    // nodeId -> text
  approvalDrafts: {},  // approvalId -> text
};

const root = document.getElementById("app");

// ---------------------------------------------------------------------
// API Transport
// ---------------------------------------------------------------------
async function apiGet(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

async function apiPost(path, body = {}) {
  state.busy = true;
  render();
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
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

// ---------------------------------------------------------------------
// Live SSE Stream / Polling
// ---------------------------------------------------------------------
function snapshotFingerprint(snap) {
  // server_time is stamped fresh on every /api/snapshot call and /api/stream
  // tick regardless of whether anything actually changed; comparing it along
  // with the rest of the payload would make every tick look "changed" and
  // defeat the whole point of this comparison.
  const { server_time, ...rest } = snap || {};
  return JSON.stringify(rest);
}

function loadMainAgentThinking() {
  const snap = state.snapshot;
  const subagents = (snap && snap.subagents) || [];
  const liveSub = subagents.find((s) => s.live);
  // Follow whichever episode is actually live, or -- once it's no longer
  // live -- the most recently dispatched one (RunState.subagents() returns
  // dispatch order): gptme runs each turn synchronously, no token-level
  // streaming (_gptme_worker.py's `stream=False`), so a fast episode can
  // dispatch, produce its one message, and complete inside a single ~1.5s
  // polling gap, and `live` may never be observed true for it. Falls back
  // to "main" itself only once nothing has ever been dispatched.
  const targetId = liveSub ? liveSub.id : (subagents.length ? subagents[subagents.length - 1].id : "main");
  apiGet(`/api/node/${encodeURIComponent(targetId)}/thinking`)
    .then((d) => {
      const sub = subagents.find((s) => s.id === targetId);
      const next = { id: targetId, label: sub ? `${targetId} (${sub.role || sub.kind})` : targetId, entries: d.entries || [], live: sub ? sub.live : false };
      if (JSON.stringify(next) !== JSON.stringify(state.mainAgentThinking)) {
        state.mainAgentThinking = next;
        render();
      }
    })
    .catch(() => {});
}

function applySnapshot(snap) {
  if (snap && snap.attached && !snap.goal && state.snapshot && state.snapshot.run_id === snap.run_id && state.snapshot.goal) {
    snap.goal = state.snapshot.goal;
  }
  const unchanged = snapshotFingerprint(snap) === snapshotFingerprint(state.snapshot);
  state.snapshot = snap;
  if (snap && snap.attached) loadMainAgentThinking();
  // Keeps whichever agent is currently open (right workbench's Agent tab)
  // streaming live while it's actually running, independent of whatever
  // loadMainAgentThinking() above is following for the main chat -- an
  // explicitly-opened subagent keeps showing *its own* chat even after
  // something else becomes "live."
  if (state.selectedNode && isLive(state.selectedNode)) {
    loadThinkingIfNeeded(true);
  }
  if (!unchanged) render();
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
  const tick = () => apiGet("/api/snapshot").then(applySnapshot).catch(() => {});
  tick();
  setInterval(tick, 2000);
}

// ---------------------------------------------------------------------
// DOM Helpers
// ---------------------------------------------------------------------
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

function truncate(text, n) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "\n…[truncated]" : text;
}

// A "diff" entry's text is a plain unified diff (rendering.py's
// format_diff_plain) -- classify each line by its leading character so it
// gets the same add/remove/hunk/header coloring as the dedicated Diff tab,
// rather than dumping raw +/- text.
function diffLineKind(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "header";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

// Turns one trace entry (rendering.parse_trace on the backend, served by
// every /api/node/<id>/thinking call) into a normal item in a chat feed --
// no bounding box of its own, no separate scroll region. Shared by the
// main chat's live "what's running right now" stream and the right
// workbench's per-agent Chat tab, so both read as the same chat interface:
// a thought, then the tool call it led to, then the next thought below
// it, each one just another entry in the history instead of everything
// dumped into a fixed-height side widget.
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

// Renders one events.jsonl record for the chat feed. `phase_failed` (and
// its "escalated" cousins) used to get a second, separately-styled copy
// pinned to a fixed position derived from the *current* snapshot status
// rather than from when the failure actually happened -- see
// renderCenterStream's history-merge comment for the ordering bug that
// caused. Styling it here instead means it's still visually distinct, but
// sits at its real place in history.
// Friendlier author label for the run's lifecycle events -- "updates like
// phase completed, phase started, subagent spawned" per the operator's ask,
// rather than the raw events.jsonl `type` string.
const _EVENT_LABEL = {
  phase_started: "▶️ Phase started",
  phase_done: "✅ Phase completed",
  node_dispatched: "🚀 Subagent spawned",
  node_redispatched: "🔁 Subagent re-dispatched",
  session_captured: "🔗 Subagent session attached",
  episode_completed: "🏁 Subagent finished",
};

function renderEventEntry(ev) {
  const isAutoResume = ev.type === "phase_auto_resuming";
  const isFailure = ev.type === "phase_failed" || ev.type === "run_escalated" || (ev.type === "phase_done" && ev.status === "escalated");
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

// Renders one approval record (pending question or resolved answer) for
// the chat feed. Split out of renderCenterStream so the same markup can
// be used both for resolved approvals (interleaved into chronological
// history) and pending ones (pinned at the bottom) -- see that function.
function renderApprovalEntry(a, snap) {
  const isPending = a.status === "pending";
  const parts = [
    el("div", { class: "card-title" }, [
      el("span", { style: isPending ? "color:var(--accent-amber); font-weight:700;" : "" }, `⚡ ${isPending ? "ACTION REQUIRED" : a.kind.toUpperCase()}: ${a.title}`),
      badge(a.status),
    ]),
  ];
  if (a.message) parts.push(el("div", { class: "card-text", style: isPending ? "font-size:14px; font-weight:500;" : "" }, a.message));

  if (isPending && snap.control_enabled) {
    const actionBtns = [];
    if ((a.options || []).length) {
      a.options.forEach((opt) => {
        actionBtns.push(
          el("button", {
            class: opt.style === "primary" ? "primary" : "",
            disabled: state.busy ? "" : null,
            onclick: () => guarded(() => apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: opt.value }).then(() => showToast("Approval resolved"))),
          }, opt.label)
        );
      });
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
          delete state.approvalDrafts[a.approval_id];
          showToast("Submitted answer");
        }) }, "Submit Input")
      );
      actionBtns.push(
        el("button", { disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "answer", user_input: "" });
          delete state.approvalDrafts[a.approval_id];
          showToast("Accepted default");
        }) }, "Use Default")
      );
    }
    parts.push(el("div", { class: "approval-actions" }, actionBtns));
  } else if (a.status === "resolved") {
    const answerDisplay = a.user_input ? `Answer given: "${a.user_input}"` : `Resolved via action: ${a.action || "completed"}`;
    parts.push(el("div", { style: "font-size:12px; color:var(--text-bright); font-weight:500; margin-top:6px; background:var(--bg-tertiary); padding:6px 10px; border-radius:4px;" }, answerDisplay));
  }

  const cardStyle = isPending ? "border:1.5px solid var(--accent-amber); background:rgba(245, 158, 11, 0.08);" : "";
  return el("div", { class: "stream-card approval" + (isPending ? " pending" : ""), style: cardStyle }, parts);
}

function liveMap() {
  const m = {};
  for (const s of state.snapshot.subagents || []) m[s.id] = s;
  return m;
}

// ---------------------------------------------------------------------
// Header Component
// ---------------------------------------------------------------------
function renderHeader() {
  const snap = state.snapshot;
  const brand = el("div", { class: "brand" }, [
    el("div", { class: "logo-icon" }, "⚡"),
    el("span", null, "Kusudaemon"),
    el("span", { class: "brand-tag" }, "Recursive Harness"),
  ]);

  const children = [brand];

  if (snap.attached) {
    children.push(
      el("div", { class: "run-selector-badge" }, [
        el("span", { style: "color:var(--text-muted);" }, "Run:"),
        el("span", { style: "font-weight:600;" }, snap.run_id),
        badge(snap.phase_status || "running"),
        (snap.pending_approvals || []).length ? el("span", { class: "badge", "data-status": "waiting_for_approval" }, "⚡ ACTION REQUIRED") : null,
        snap.halted ? badge("halted") : null,
        // §D0c: "in_progress" forever is indistinguishable from mid-call
        // unless something says so -- this is that something.
        snap.stalled
          ? el("span", { class: "badge", "data-status": "error", title: snap.stalled_reason || "" }, "☠ STALLED")
          : null,
      ])
    );
  } else {
    children.push(el("div", { class: "run-selector-badge" }, "No Attached Run"));
  }

  children.push(el("span", { class: "spacer" }));

  if (!snap.control_enabled) {
    children.push(el("span", { class: "badge" }, "Read-Only"));
  }

  const actions = [];
  if (snap.attached && snap.control_enabled) {
    const isStoppedOrHalted = snap.halted || snap.phase_status === "error" || snap.phase_status === "paused" || snap.phase_status === "escalated" || snap.phase_status === "halted";
    if (isStoppedOrHalted) {
      actions.push(
        el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(resumeAttached) }, "▶ Resume / Continue")
      );
    } else {
      actions.push(
        el("button", { class: "danger", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          await apiPost("/api/halt", { value: true });
          showToast("Halt signal sent");
        }) }, "⏸ Halt")
      );
    }
  }

  actions.push(
    el("button", { class: "primary", disabled: snap.control_enabled ? null : "", onclick: () => { state.newRunOpen = true; render(); } }, "+ New Run")
  );

  children.push(el("div", { class: "hdr-actions" }, actions));
  return el("header", { class: "hdr" }, children);
}

async function resumeAttached() {
  const runId = state.snapshot.run_id;
  await apiPost("/api/halt", { value: false });
  await apiPost("/api/runs", { run_id: runId });
  showToast(`Resumed run ${runId}`);
}

// ---------------------------------------------------------------------
// Left Sidebar Component
// ---------------------------------------------------------------------
function renderSidebar() {
  const snap = state.snapshot;
  // Navigation only -- every detail view (task tree, a specific node or
  // subagent's chat, artifacts, ...) opens in the right-side workbench
  // instead (renderRightWorkbench), never here.
  const tabs = [
    ["sessions", "Runs"],
    ["subagents", "Subagents"],
    ["phases", "Phases"],
  ];
  const navTabs = el("div", { class: "sidebar-nav-tabs" }, tabs.map(([id, label]) =>
    el("div", { class: "nav-tab" + (state.sidebarTab === id ? " active" : ""), onclick: () => { state.sidebarTab = id; render(); } }, label)
  ));

  let content = null;
  if (state.sidebarTab === "sessions") {
    const runs = snap.runs || [];
    const items = runs.length
      ? runs.map((r) =>
          el("li", {
            class: "run-item" + (r.attached ? " active" : ""),
            onclick: () => guarded(() => {
              state.targetAgentManual = false; // resume auto-following the live subagent in the new run
              state.selectedNode = null;
              state.workbenchTab = "tree"; // Task Tree is the default view for whichever run is attached
              return apiPost("/api/attach", { run_id: r.id });
            }),
          }, [
            el("div", { style: "display:flex; justify-content:space-between; align-items:flex-start; gap:8px;" }, [
              el("div", { class: "goal" }, r.goal || r.id),
              snap.control_enabled ? el("button", {
                class: "icon-btn delete-btn",
                title: "Delete Run",
                onclick: (e) => {
                  e.stopPropagation();
                  guarded(async () => {
                    if (confirm(`Delete run "${r.id}"? This action cannot be undone.`)) {
                      await apiPost("/api/runs/delete", { run_id: r.id });
                      showToast(`Deleted run ${r.id}`);
                    }
                  });
                }
              }, "🗑️") : null,
            ]),
            el("div", { class: "meta" }, [badge(r.status || r.phase || "-"), el("span", null, r.id)]),
          ])
        )
      : [el("div", { class: "empty-state" }, "No runs found.")];
    content = el("ul", { class: "run-list" }, items);
  } else if (state.sidebarTab === "subagents") {
    const subs = snap.subagents || [];
    if (!subs.length) {
      content = el("div", { class: "empty-state" }, "No subagents dispatched yet.");
    } else {
      content = el("div", { class: "node-tree-list" }, subs.map((s) =>
        el("div", { class: "node-card" + (s.id === state.selectedNode ? " active" : ""), onclick: () => openNode(s.id) }, [
          el("div", { class: "node-hdr" }, [
            el("span", null, `${s.live ? "● " : ""}[${s.id}]`),
            badge(s.status),
          ]),
          el("div", { class: "brief" }, `${s.kind} · ${s.role} · attempts=${s.attempts}${s.duration_ms ? ` · ${s.duration_ms}ms` : ""}`),
          snap.control_enabled ? el("div", { style: "margin-top:6px; display:flex; justify-content:flex-end;" }, [
            el("button", {
              class: "xs-btn",
              onclick: (e) => {
                e.stopPropagation();
                state.promptMode = "msg_agent";
                state.targetAgentId = s.id;
                state.targetAgentManual = true;
                render();
              }
            }, "💬 Message Agent")
          ]) : null,
        ])
      ));
    }
  } else if (state.sidebarTab === "phases") {
    content = el("div", { class: "phase-pipeline" }, PHASES.map((p) => {
      const status = (snap.phases || {})[p] || "pending";
      const isCurrent = snap.phase === p;
      return el("div", { class: "phase-step", "data-status": status }, [
        el("span", { class: "indicator" }),
        el("span", { style: isCurrent ? "font-weight:600; color:var(--text-bright);" : "" }, p.toUpperCase() + (isCurrent ? " (Active)" : "")),
        el("span", { class: "spacer" }),
        badge(status),
      ]);
    }));
  }

  return el("aside", { class: "sidebar-nav" }, [
    el("div", { class: "sidebar-header" }, [
      el("h3", null, "Workspace"),
      el("button", { class: "icon-btn", onclick: () => { state.newRunOpen = true; render(); } }, "+ New"),
    ]),
    navTabs,
    el("div", { class: "sidebar-content" }, content),
  ]);
}

// ---------------------------------------------------------------------
// Center Chat & Interactive Stream Component
// ---------------------------------------------------------------------
function renderCenterStream() {
  const snap = state.snapshot;
  const feed = el("div", { class: "chat-feed", id: "chat-feed-scroll" });

  if (!snap.attached) {
    feed.appendChild(
      el("div", { class: "empty-state" }, [
        el("h3", { style: "color:var(--text-bright); margin-bottom:8px;" }, "Welcome to Kusudaemon"),
        el("p", null, "Start a new run or pick an existing one from the sidebar to begin."),
      ])
    );
    return el("main", { class: "chat-stream-panel" }, [
      el("div", { class: "chat-header" }, [
        el("div", { class: "title" }, ["💬 Run Stream"]),
      ]),
      feed,
      renderPromptBar(),
    ]);
  }

  // Pinned Header Section at top (Goal & Phase stay fixed at top when scrolling)
  const pinnedHeader = el("div", { class: "pinned-run-header" }, [
    el("div", { class: "stream-msg user pinned-goal" }, [
      el("div", { class: "msg-hdr" }, [el("span", { class: "author" }, "Goal"), el("span", null, "Run Spec")]),
      el("div", { class: "msg-body" }, snap.goal || "(no goal recorded)"),
    ]),
    snap.phase ? el("div", { class: "stream-card pinned-phase" }, [
      el("div", { class: "card-title" }, [
        el("span", null, `🤖 Phase: ${snap.phase.toUpperCase()}`),
        badge(snap.phase_status || "running"),
      ]),
      snap.phase_detail ? el("div", { style: "color:var(--accent-amber); font-size:12px; margin-top:4px;" }, snap.phase_detail) : null,
    ]) : null,
  ]);

  // 1. Chronological history: events and already-resolved approvals,
  // strictly interleaved by when they actually happened (oldest first,
  // newest at the bottom). This used to be split into fixed sections --
  // events, then a "phase error" card pinned by *current* snapshot
  // status, then approvals last -- so a phase failure that happened
  // after the operator had already answered some intake questions
  // rendered above those already-answered questions instead of below
  // them. Still-pending approvals are held out and rendered separately at
  // the very bottom (see the end of this function) regardless of when
  // they were created, so an operator scrolled to the bottom still never
  // has to scroll back up to find one.
  const approvals = snap.approvals || [];
  const pendingApprovals = approvals.filter((a) => a.status === "pending");
  const resolvedApprovals = approvals.filter((a) => a.status !== "pending");
  const historyItems = [
    ...(snap.events || []).slice(-20).map((ev) => ({ ts: ev.ts || 0, node: renderEventEntry(ev) })),
    ...resolvedApprovals.map((a) => ({ ts: a.created_at || 0, node: renderApprovalEntry(a, snap) })),
  ];
  historyItems.sort((x, y) => x.ts - y.ts);
  historyItems.forEach((item) => feed.appendChild(item.node));

  // 2. Live "what's running right now" stream, following whichever
  // episode is actually live (or most-recently dispatched -- see
  // loadMainAgentThinking()). Each trace entry renders as a plain item
  // directly in this same feed -- a thought, then the tool call it led
  // to, then the next thought below it -- growing as the episode
  // progresses, not boxed into a separate fixed-height widget. A
  // subagent the operator explicitly opens gets its *own* full history in
  // the right workbench's Agent tab (renderAgentTab), independent of
  // whatever this section is currently following.
  const mainAgent = state.mainAgentThinking;
  if (snap.attached && mainAgent) {
    feed.appendChild(
      el("div", { class: "live-agent-divider" }, [
        el("span", null, `🤖 ${mainAgent.label}`),
        mainAgent.live ? el("span", { class: "badge", "data-status": "running" }, "live") : null,
        el("span", { class: "divider-line" }),
        el("span", { class: "node-link", onclick: () => openNode(mainAgent.id, "chat") }, "open in Agent tab"),
        el("span", { class: "node-link", onclick: () => openNode(mainAgent.id, "artifacts") }, "📁 Artifacts"),
      ])
    );
    const entries = mainAgent.entries || [];
    if (entries.length) {
      entries.forEach((e) => feed.appendChild(renderAgentChatEntry(e)));
    } else {
      feed.appendChild(el("div", { class: "empty-state" }, `Waiting for trace from ${mainAgent.label}...`));
    }
  }

  // 3. Resume Status Card -- persistent call-to-action tied to the
  // *current* run status, not a point-in-time history entry, so it stays
  // pinned here rather than joining the chronological merge above.
  const isStoppedOrHalted = snap.halted || snap.phase_status === "error" || snap.phase_status === "paused" || snap.phase_status === "escalated";
  if (isStoppedOrHalted && snap.control_enabled) {
    feed.appendChild(
      el("div", { class: "stream-card resume-banner" }, [
        el("div", { class: "card-title" }, [
          el("span", { style: "color:var(--accent-amber); font-weight:600;" }, `▶ Resume Run (Status: ${snap.phase_status || (snap.halted ? "halted" : "stopped")})`),
          el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(resumeAttached) }, "▶ Resume / Continue Run"),
        ]),
        el("div", { style: "font-size:12px; color:var(--text-muted); margin-top:4px;" }, "Click Resume / Continue to re-trigger execution from the last saved checkpoint."),
      ])
    );
  }

  // 4. Pending approvals -- always last, right above the prompt bar; see
  // this function's opening comment for why they're held out of the
  // chronological merge above.
  pendingApprovals.forEach((a) => feed.appendChild(renderApprovalEntry(a, snap)));

  const promptBar = renderPromptBar();

  return el("main", { class: "chat-stream-panel" }, [
    el("div", { class: "chat-header" }, [
      el("div", { class: "title" }, ["💬 Run Stream", snap.halted ? badge("halted") : null]),
      el("span", { style: "font-size:11px; color:var(--text-muted);" }, snap.attached ? `${snap.events_count || 0} events` : ""),
    ]),
    pinnedHeader,
    feed,
    promptBar,
  ]);
}

function renderPromptBar() {
  const snap = state.snapshot;
  const disabled = !snap.control_enabled || state.busy;

  const modeSelector = el("div", { class: "prompt-mode-selector" }, [
    el("button", { class: "mode-btn" + (state.promptMode === "msg_agent" ? " active" : ""), onclick: () => { state.promptMode = "msg_agent"; render(); } }, "💬 Message Agent"),
    el("button", { class: "mode-btn" + (state.promptMode === "auto" ? " active" : ""), onclick: () => { state.promptMode = "auto"; render(); } }, "New Run"),
    el("button", { class: "mode-btn" + (state.promptMode === "amend" ? " active" : ""), onclick: () => { state.promptMode = "amend"; render(); } }, "Amend Contract"),
    el("button", { class: "mode-btn" + (state.promptMode === "reopen" ? " active" : ""), onclick: () => { state.promptMode = "reopen"; render(); } }, "Reopen Node"),
  ]);

  let targetSelector = null;
  let msgTargetIsLive = true; // only meaningful in msg_agent mode; see below
  if (state.promptMode === "msg_agent") {
    const subagents = snap.subagents || [];
    const anyLive = subagents.some((s) => s.live);

    // Auto-follow whichever subagent is actually live instead of leaving
    // the selection pinned to "main" (which -- outside a lucky fallback
    // scan on the backend -- almost never has a live gptme session of its
    // own) or to a previous target whose episode has since finished.
    // Interjecting into a dead session always 409s ("no live session
    // found for this node"); repeatedly hitting Send against a stale
    // selection is exactly that. Once the operator manually picks a
    // target, respect it until they attach a new run.
    if (!state.targetAgentManual) {
      const liveSub = subagents.find((s) => s.live);
      state.targetAgentId = liveSub ? liveSub.id : "main";
    }

    const mainOpt = el("option", { value: "main", selected: (!state.targetAgentId || state.targetAgentId === "main") ? "selected" : null }, "🤖 Main Agent (Current Phase)");
    const subOpts = subagents.map((s) => el("option", { value: s.id, selected: state.targetAgentId === s.id ? "selected" : null }, `[${s.id}] ${s.kind} (${s.status}${s.live ? ", live" : ""})`));
    const options = [mainOpt, ...subOpts];

    const selectEl = el("select", {
      class: "agent-target-select",
      onchange: (e) => { state.targetAgentManual = true; state.targetAgentId = e.target.value; render(); },
    }, options);

    // "main" can still route to a live subagent via the backend's own
    // fallback scan (RunState.node_gptme_logdir), so only treat it as
    // definitely-dead when nothing at all is live.
    msgTargetIsLive = state.targetAgentId === "main" ? anyLive : !!subagents.find((s) => s.id === state.targetAgentId && s.live);

    targetSelector = el("div", { class: "agent-target-picker" }, [
      el("label", { style: "font-size:11px; font-weight:600; color:var(--accent-purple);" }, "Target:"),
      selectEl,
      !msgTargetIsLive
        ? el("span", { style: "font-size:11px; color:var(--text-muted); margin-left:8px;" }, "not currently running -- nothing to message")
        : null,
    ]);
  }

  const msgBlocked = state.promptMode === "msg_agent" && !msgTargetIsLive;
  const ta = el("textarea", {
    class: "prompt-textarea",
    "data-key": "prompt-textarea",
    placeholder:
      state.promptMode === "amend" ? "Enter contract amendment rule to append..." :
      state.promptMode === "reopen" ? "Node ID and defect description..." :
      msgBlocked ? "No live agent to message right now -- pick a different target or wait for one to start." :
      state.promptMode === "msg_agent" ? "Type a direct message to send to the main agent or subagent mid-episode..." :
      "Type a run goal (or @path/to/file)...",
    rows: 1,
    disabled: (disabled || msgBlocked) ? "" : null,
  });
  ta.value = state.promptText;
  ta.addEventListener("input", () => { state.promptText = ta.value; });

  const sendBtn = el("button", { class: "primary", disabled: (disabled || msgBlocked) ? "" : null, onclick: () => guarded(() => handlePromptSubmit()) }, "Send ↵");

  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handlePromptSubmit();
    }
  });

  return el("div", { class: "prompt-container" }, [
    el("div", { class: "prompt-controls" }, [
      modeSelector,
      targetSelector,
      el("span", { style: "font-size:11px; color:var(--text-muted);" }, "Press Enter to submit"),
    ]),
    el("div", { class: "prompt-input-wrapper" }, [ta, sendBtn]),
  ]);
}

async function handlePromptSubmit() {
  const text = state.promptText.trim();
  if (!text) return;

  if (state.promptMode === "amend") {
    await apiPost("/api/amend", { text, reason: "web amendment" });
    state.promptText = "";
    showToast("Contract amendment queued");
    fetchWorkbenchData("contract");
  } else if (state.promptMode === "reopen") {
    const parts = text.split(" ");
    const nodeId = parts[0];
    const defect = parts.slice(1).join(" ") || "Defect requested via prompt";
    await apiPost("/api/reopen", { node_id: nodeId, defect });
    state.promptText = "";
    showToast(`Reopen requested for node ${nodeId}`);
  } else if (state.promptMode === "msg_agent") {
    const targetId = state.targetAgentId || "main";
    const subagents = state.snapshot.subagents || [];
    const targetIsLive = targetId === "main" ? subagents.some((s) => s.live) : !!subagents.find((s) => s.id === targetId && s.live);
    if (!targetIsLive) {
      // The Send button/textarea are already disabled for this case, but
      // Enter-to-submit bypasses that (its keydown handler doesn't check
      // `disabled`) -- catch it here too instead of firing a POST that
      // can only 409 with "no live session found for this node".
      showToast(`${targetId === "main" ? "No agent" : targetId} is not currently running -- nothing to message`, true);
      return;
    }
    await apiPost(`/api/node/${encodeURIComponent(targetId)}/interject`, { text });
    state.promptText = "";
    showToast(`Message queued for agent ${targetId}`);
  } else {
    const res = await apiPost("/api/runs", { goal: text });
    state.promptText = "";
    state.selectedNode = null;
    state.workbenchTab = "tree";
    showToast(`Started run ${res.run_id}`);
  }
}

// ---------------------------------------------------------------------
// Right Workbench Component (Code, Spec, Logs, Assembly)
// ---------------------------------------------------------------------
function renderRightWorkbench() {
  const wbTabs = [
    ["tree", "🌳 Task Tree"],
    ["agent", "🤖 Agent"],
    ["code", "💻 Code & Artifacts"],
    ["contract", "📜 Contract"],
    ["spec", "📋 Spec"],
    ["spine", "🦴 Spine"],
    ["assembly", "🧩 Assembly"],
    ["terminal", "🖥️ Events Log"],
  ];

  const header = el("div", { class: "workbench-tabs" }, wbTabs.map(([id, label]) =>
    el("div", {
      class: "wb-tab" + (state.workbenchTab === id ? " active" : ""),
      onclick: () => { state.workbenchTab = id; fetchWorkbenchData(id); render(); },
    }, label)
  ));

  let body = null;
  const snap = state.snapshot;

  if (state.workbenchTab === "code") {
    body = el("div", { class: "code-container" }, [
      el("div", { class: "code-header" }, [
        el("span", null, state.nodeDetail ? `Node Artifact: ${state.nodeDetail.id}` : "Assembled Output"),
        el("button", { class: "icon-btn", onclick: () => navigator.clipboard.writeText((state.nodeDetail && state.nodeDetail.artifact) || (state.assembly && state.assembly.output) || "") }, "📋 Copy"),
      ]),
      el("pre", { class: "blob" }, (state.nodeDetail && state.nodeDetail.artifact) || (state.assembly && state.assembly.output) || "(No active artifact selected. Click a node in the Task Tree tab to view it.)"),
    ]);
  } else if (state.workbenchTab === "tree") {
    body = renderTaskTreeTab();
  } else if (state.workbenchTab === "agent") {
    body = renderAgentTab();
  } else if (state.workbenchTab === "contract") {
    body = el("pre", { class: "blob" }, state.contractText || "(No contract frozen yet)");
  } else if (state.workbenchTab === "spec") {
    body = el("pre", { class: "blob" }, state.specText || "(No spec generated yet)");
  } else if (state.workbenchTab === "spine") {
    body = el("pre", { class: "blob" }, state.spineText || "(No spine generated yet)");
  } else if (state.workbenchTab === "assembly") {
    const a = state.assembly || {};
    body = el("div", null, [
      el("h3", { style: "color:var(--text-bright); margin-bottom:8px;" }, "Compilation & Cross-Checks"),
      a.checks && Object.keys(a.checks).length ? el("pre", { class: "blob" }, JSON.stringify(a.checks, null, 2)) : null,
      el("pre", { class: "blob" }, a.compile_log || "(No compile logs available)"),
      el("h3", { style: "color:var(--text-bright); margin-top:16px; margin-bottom:8px;" }, "Assembly Output"),
      el("pre", { class: "blob" }, a.output || "(Not assembled yet)"),
    ]);
  } else if (state.workbenchTab === "terminal") {
    const events = (snap.events || []).slice().reverse();
    body = el("table", { class: "tree" }, [
      el("thead", null, el("tr", null, ["Time", "Node", "Type / Description"].map((h) => el("th", null, h)))),
      el("tbody", null, events.map((e) => el("tr", null, [
        el("td", { style: "font-family:var(--font-mono); font-size:11px;" }, fmtTime(e.ts)),
        el("td", null, e.node_id || "-"),
        el("td", null, `${e.type} ${e.phase ? `[${e.phase}]` : ""} ${e.status || ""}`),
      ]))),
    ]);
  }

  return el("section", { class: "workbench-panel" }, [header, el("div", { class: "workbench-content" }, body)]);
}

function fetchWorkbenchData(id) {
  if (!state.snapshot.attached) return;
  if (id === "contract") apiGet("/api/contract").then((d) => { state.contractText = d.text; render(); }).catch(() => {});
  if (id === "spec") apiGet("/api/spec").then((d) => { state.specText = d.text; render(); }).catch(() => {});
  if (id === "spine") apiGet("/api/spine").then((d) => { state.spineText = d.text; render(); }).catch(() => {});
  if (id === "assembly") apiGet("/api/assembly").then((d) => { state.assembly = d; render(); }).catch(() => {});
}

// ---------------------------------------------------------------------
// Node / Subagent Detail Drawer
// ---------------------------------------------------------------------
function openNode(id, tab) {
  state.selectedNode = id;
  state.nodeDetail = null;
  state.nodeSubagent = null;
  state.nodeDiff = null;
  state.nodeThinking = null;
  state.nodeDetailLoading = true;
  state.agentTab = tab || "overview";
  state.artifactsDetail = null;
  state.selectedArtifactTag = undefined;
  state.selectedArtifactText = null;
  state.workbenchTab = "agent"; // every detail view opens on the right, never a floating overlay
  render();
  apiGet(`/api/node/${encodeURIComponent(id)}`)
    .then((d) => { state.nodeDetail = d; state.nodeDetailLoading = false; render(); })
    .catch(() => {
      state.nodeSubagent = (state.snapshot.subagents || []).find((s) => s.id === id) || null;
      state.nodeDetailLoading = false;
      render();
    });
}

function closeNode() {
  state.selectedNode = null;
  state.nodeDetail = null;
  state.nodeSubagent = null;
  state.workbenchTab = "tree"; // Task Tree is the default/resting view of the right column
  render();
}

// A repair/research subagent's derived id ("<node>~repair1", "<node>~research~slug")
// doesn't own its own artifact -- repairs land on the same real out/<node>.md
// as their parent tree node once they pass, and pre-repair snapshots live
// under out/.versions/<node>/ keyed by that same parent id. So "this agent's
// artifacts" always resolves through the owning tree node's id.
function artifactsOwnerId(id) {
  const idx = (id || "").indexOf("~");
  return idx === -1 ? id : id.slice(0, idx);
}

function isLive(id) {
  const sub = liveMap()[id];
  return !!(sub && sub.live);
}

function loadDiffIfNeeded() {
  if (state.nodeDiff !== null) return;
  const id = state.selectedNode;
  const versions = (state.nodeDetail && state.nodeDetail.versions) || [];
  if (!versions.length) {
    state.nodeDiff = [];
    render();
    return;
  }
  state.nodeDiff = "loading";
  render();
  Promise.all(
    versions.slice().reverse().map((tag) =>
      apiGet(`/api/node/${encodeURIComponent(id)}/diff/${encodeURIComponent(tag)}`).then((d) => ({ tag, lines: d.lines }))
    )
  ).then((results) => { state.nodeDiff = results; render(); }).catch(() => { state.nodeDiff = []; render(); });
}

function loadThinkingIfNeeded(force = false) {
  if (state.nodeThinking !== null && !force && state.nodeThinking !== "loading") return;
  const id = state.selectedNode;
  if (!id) return;
  if (state.nodeThinking === null) state.nodeThinking = "loading";
  apiGet(`/api/node/${encodeURIComponent(id)}/thinking`)
    .then((d) => { state.nodeThinking = d.entries || []; render(); })
    .catch(() => { if (state.nodeThinking === "loading") state.nodeThinking = []; });
}

function renderOverview() {
  const d = state.nodeDetail;
  if (d) {
    const parts = [
      el("div", { style: "display:flex; gap:12px; align-items:center; margin-bottom:12px;" }, [
        badge(d.status),
        el("span", null, `shape: ${d.shape || "-"}`),
        el("span", null, `attempts: ${d.attempts}`),
      ]),
    ];
    if (d.brief) parts.push(el("p", { style: "margin-bottom:12px;" }, d.brief));

    parts.push(el("h4", { class: "detail-h" }, "Gates"));
    const gateResults = d.gate_results || [];
    if (gateResults.length) {
      parts.push(el("ul", { class: "gate-list" }, gateResults.map((g) =>
        el("li", { class: g.passed ? "gate-pass" : "gate-fail" }, `${g.passed ? "✓" : "✗"} ${g.gate}${g.detail ? " — " + g.detail : ""}`)
      )));
    } else {
      parts.push(el("div", { class: "dim" }, "(none)"));
    }

    if (d.judgment && d.judgment.length) {
      parts.push(el("h4", { class: "detail-h" }, "Judgment"));
      parts.push(el("ul", { class: "gate-list" }, d.judgment.map((jid) =>
        el("li", null, `${jid}: ${(d.rubric || {})[jid] || ""}`)
      )));
    }

    if (d.manifest) {
      parts.push(el("h4", { class: "detail-h" }, "Reviewer verdict"));
      parts.push(el("div", null, String(d.manifest.verdict || d.manifest.gates || "")));
      if (d.manifest.promotion) {
        parts.push(el("div", { style: "margin-top:6px;" }, [el("b", null, "promotion: "), d.manifest.promotion]));
      }
    }

    if (d.inputs && d.inputs.length) {
      parts.push(el("h4", { class: "detail-h" }, "Inputs"));
      parts.push(el("ul", { class: "gate-list" }, d.inputs.map((item) =>
        el("li", { class: item.exists ? "gate-pass" : "gate-fail" }, `${item.exists ? "✓" : "✗"} ${item.ref} (${item.tokens || 0} tok)`)
      )));
    }

    if (d.depends_on && d.depends_on.length) {
      parts.push(el("div", { style: "margin-top:8px;" }, [el("b", null, "depends on: "), d.depends_on.join(", ")]));
    }
    parts.push(el("div", { style: "margin-top:8px; color:var(--text-muted); font-size:11px;" }, [
      `budget: ${(d.budget || {}).tokens} tokens, ${(d.budget || {}).calls} calls · artifact tokens: ${d.artifact_tokens || 0}`,
      d.versions && d.versions.length ? ` · versions: ${d.versions.join(", ")}` : "",
    ].join("")));
    return el("div", null, parts);
  }

  const s = state.nodeSubagent;
  if (!s) return el("div", { class: "empty-state" }, "(no record yet for this id)");
  const parts = [
    el("div", { style: "display:flex; gap:12px; align-items:center; margin-bottom:8px;" }, [badge(s.status), el("span", null, `kind: ${s.kind}`), el("span", null, `role: ${s.role}`)]),
    el("div", { style: "color:var(--text-muted); font-size:12px;" }, `attempts: ${s.attempts}   duration: ${s.duration_ms || 0}ms`),
  ];
  if (s.live) {
    parts.push(el("div", { style: "color:var(--accent-amber); margin-top:8px; font-weight:600;" }, "● live — send it a message below"));
  } else if (s.role === "explorer") {
    // survey's optional large-corpus explorer subagent (driver.py's
    // _phase_survey) wraps a plain, non-agentic model call: its status can
    // say "running" while that call is in flight, but it never has a
    // gptme session behind it, so it can never be live/messageable --
    // spell that out instead of leaving "RUNNING" up top contradict a
    // generic "not currently running" on the message box below.
    parts.push(el("div", { class: "dim", style: "margin-top:8px;" }, "This step wraps a plain model call, not an interactive session — there's nothing to message, even while its status says \"running.\""));
  }
  if (s.error) parts.push(el("div", { style: "color:var(--accent-red); margin-top:8px;" }, `error: ${s.error}`));
  parts.push(el("div", { class: "dim", style: "margin-top:12px;" }, "Not a tree node — see its parent tree node for artifact/diff history."));
  return el("div", null, parts);
}

function renderArtifactTab() {
  const d = state.nodeDetail;
  if (!d) return el("div", { class: "empty-state" }, "(not a tree node — no standalone artifact)");
  return el("pre", { class: "blob" }, d.artifact || "(empty)");
}

// "Artifacts" — every artifact file this agent's owning tree node has ever
// produced: the current out/<node>.md plus each pre-repair snapshot,
// picked from a list and viewed right here in the right column.
function loadArtifactsIfNeeded() {
  if (state.artifactsDetail !== null) return;
  const ownerId = artifactsOwnerId(state.selectedNode);
  state.artifactsDetail = "loading";
  apiGet(`/api/node/${encodeURIComponent(ownerId)}`)
    .then((d) => { state.artifactsDetail = d; render(); })
    .catch(() => { state.artifactsDetail = {}; render(); });
}

function loadArtifactText(ownerId, tag) {
  state.selectedArtifactTag = tag;
  state.selectedArtifactText = "loading";
  render();
  const req = tag
    ? apiGet(`/api/node/${encodeURIComponent(ownerId)}/version/${encodeURIComponent(tag)}`)
    : apiGet(`/api/node/${encodeURIComponent(ownerId)}/artifact`);
  req
    .then((d) => { state.selectedArtifactText = d.text || "(empty)"; render(); })
    .catch(() => { state.selectedArtifactText = "(failed to load)"; render(); });
}

function renderArtifactsTab() {
  loadArtifactsIfNeeded();
  const d = state.artifactsDetail;
  if (d === "loading" || d === null) return el("div", { class: "empty-state" }, "Loading artifacts…");
  const ownerId = artifactsOwnerId(state.selectedNode);
  const items = [];
  if (d.artifact) items.push({ tag: null, label: "current", meta: `${d.artifact_tokens || 0} tok` });
  (d.versions || []).slice().reverse().forEach((v) => items.push({ tag: v, label: v, meta: "pre-repair snapshot" }));
  if (!items.length) return el("div", { class: "empty-state" }, "(no artifacts generated yet)");

  const list = el("div", { class: "artifacts-list" }, items.map((it) =>
    el("div", {
      class: "artifact-item" + (state.selectedArtifactTag === it.tag ? " active" : ""),
      onclick: () => loadArtifactText(ownerId, it.tag),
    }, [
      el("span", null, it.label),
      el("span", { class: "dim" }, it.meta),
    ])
  ));
  const viewer = el(
    "div",
    { class: "artifact-viewer" },
    state.selectedArtifactTag === undefined
      ? el("div", { class: "empty-state" }, "Select an artifact on the left to view its content.")
      : el("pre", { class: "blob" }, state.selectedArtifactText === "loading" ? "Loading…" : state.selectedArtifactText)
  );
  return el("div", { class: "artifacts-pane" }, [list, viewer]);
}

function renderDiffTab() {
  loadDiffIfNeeded();
  if (state.nodeDiff === "loading" || state.nodeDiff === null) return el("div", { class: "empty-state" }, "Loading diff…");
  if (!state.nodeDiff.length) return el("div", { class: "empty-state" }, "(no prior versions — nothing to diff yet)");
  const blocks = state.nodeDiff.map(({ tag, lines }) =>
    el("div", { class: "diff-block" }, [
      el("div", { class: "diff-block-title" }, `${tag} → current`),
      el("pre", { class: "diff-pre" }, lines.map((line) => el("div", { class: `diff-line diff-${line.kind}` }, line.text))),
    ])
  );
  return el("div", null, blocks);
}

// Chat sub-view within the Agent tab -- the same renderAgentChatEntry used
// by the main chat's live "what's running right now" stream, so an
// explicitly-opened subagent reads as the same interface as the main
// agent, just scoped to its own history (loadThinkingIfNeeded keeps this
// fresh while the opened node is live -- see applySnapshot).
function renderAgentChatTab() {
  loadThinkingIfNeeded();
  const artifactsBar = el("div", { class: "chat-artifacts-bar" }, [
    el("button", { class: "xs-btn", onclick: () => { state.agentTab = "artifacts"; render(); } }, "📁 Artifacts"),
  ]);
  if (state.nodeThinking === "loading" || state.nodeThinking === null) return el("div", null, [artifactsBar, el("div", { class: "empty-state" }, "Loading chat…")]);
  if (!state.nodeThinking.length) return el("div", null, [artifactsBar, el("div", { class: "empty-state" }, "(no messages yet)")]);
  return el("div", null, [artifactsBar, el("div", { class: "agent-chat-log" }, state.nodeThinking.map(renderAgentChatEntry))]);
}

// The right workbench's Agent tab: a specific node or subagent's full
// detail (Overview/Artifact/Diff/Chat) plus interject/reopen -- embedded
// in the workbench instead of a floating overlay, so it opens on the
// right side alongside Code/Tree/Contract/etc rather than covering the
// whole screen. Replaces the old renderNodeDrawer.
function renderAgentTab() {
  if (!state.selectedNode) {
    return el("div", { class: "empty-state" }, "Select a node from the Task Tree tab, or a subagent from the left sidebar, to open its chat here.");
  }
  const id = state.selectedNode;
  const tabs = [
    ["overview", "Overview"],
    ["artifact", "Artifact"],
    ["artifacts", "📁 Artifacts"],
    ["diff", "Diff"],
    ["chat", "Chat"],
  ];

  const tabBar = el("div", { class: "agent-tabs" }, tabs.map(([tid, label]) =>
    el("div", { class: "agent-tab" + (state.agentTab === tid ? " active" : ""), onclick: () => { state.agentTab = tid; render(); } }, label)
  ));

  let body;
  if (state.nodeDetailLoading) {
    body = el("div", { class: "empty-state" }, "Loading…");
  } else if (state.agentTab === "overview") body = renderOverview();
  else if (state.agentTab === "artifact") body = renderArtifactTab();
  else if (state.agentTab === "artifacts") body = renderArtifactsTab();
  else if (state.agentTab === "diff") body = renderDiffTab();
  else body = renderAgentChatTab();

  const live = isLive(id);
  const isExplorer = !!(state.nodeSubagent && state.nodeSubagent.role === "explorer");
  const interjectPlaceholder = live
    ? "Message the running agent…"
    : isExplorer
      ? "(this step wraps a plain model call — no interactive session to message)"
      : "(not currently running)";
  const interjectInput = el("input", { type: "text", "data-key": `interject-${id}`, placeholder: interjectPlaceholder, disabled: live ? null : "" });
  interjectInput.value = state.interjectDrafts[id] || "";
  interjectInput.addEventListener("input", () => { state.interjectDrafts[id] = interjectInput.value; });
  const interjectBtn = el("button", { class: "primary", disabled: live && !state.busy ? null : "", onclick: () => guarded(async () => {
    const text = (state.interjectDrafts[id] || "").trim();
    if (!text) return;
    const ok = await apiPost(`/api/node/${encodeURIComponent(id)}/interject`, { text }).then(() => true).catch((e) => { showToast(String(e.message || e), true); return false; });
    if (ok) { state.interjectDrafts[id] = ""; showToast(`queued for ${id}`); }
  }) }, "Send");
  interjectInput.addEventListener("keydown", (e) => { if (e.key === "Enter") interjectBtn.click(); });

  const reopenable = !!(state.nodeDetail && state.nodeDetail.status === "passed");
  const reopenInput = el("input", { type: "text", "data-key": `reopen-${id}`, placeholder: reopenable ? "describe what's wrong…" : "(only for passed nodes)", disabled: reopenable ? null : "" });
  reopenInput.value = state.reopenDrafts[id] || "";
  reopenInput.addEventListener("input", () => { state.reopenDrafts[id] = reopenInput.value; });
  const reopenBtn = el("button", { disabled: reopenable && !state.busy ? null : "", onclick: () => guarded(async () => {
    const text = (state.reopenDrafts[id] || "").trim();
    if (!text) return;
    await apiPost("/api/reopen", { node_id: id, defect: text });
    state.reopenDrafts[id] = "";
    showToast("reopen queued — approve it in the run stream");
  }) }, "Reopen");
  reopenInput.addEventListener("keydown", (e) => { if (e.key === "Enter") reopenBtn.click(); });

  return el("div", { class: "agent-panel" }, [
    el("div", { class: "agent-panel-hdr" }, [el("h3", null, id), el("button", { class: "xs-btn", onclick: closeNode }, "✕ Close")]),
    tabBar,
    el("div", { class: "agent-body" }, body),
    el("div", { class: "agent-bar" }, [
      el("label", { class: "bar-label" }, "Message the running agent (mid-episode)"),
      el("div", { class: "bar-row" }, [interjectInput, interjectBtn]),
    ]),
    el("div", { class: "agent-bar" }, [
      el("label", { class: "bar-label" }, "Reopen (passed nodes only): defect to fix"),
      el("div", { class: "bar-row" }, [reopenInput, reopenBtn]),
    ]),
  ]);
}

// Task Tree tab: node ids are dot-hierarchical (planner.py's
// `f"{path}.{candidate.id}"` when recursing into a slice), so grouping by
// that path builds a real parent/child tree instead of the flat list the
// left sidebar used to show. Intermediate path segments that aren't
// themselves a dispatched leaf (a planning-level grouping, not a node in
// tree.json) render as plain folders -- only real leaves get a status
// badge and are clickable.
function buildNodeTreeIndex(nodes) {
  const root = { children: new Map(), node: null };
  nodes.forEach((n) => {
    let cur = root;
    String(n.id).split(".").forEach((part) => {
      if (!cur.children.has(part)) cur.children.set(part, { children: new Map(), node: null });
      cur = cur.children.get(part);
    });
    cur.node = n;
  });
  return root;
}

// Every subagent episode dispatched under a tree node's id -- the node
// itself (a Writer dispatch, id === node.id) plus any repair/research
// children (derived ids "<node>~repair1", "<node>~research~slug").
function findAttachedSubagents(nodeId) {
  const subs = state.snapshot.subagents || [];
  return subs.filter((s) => s.id === nodeId || s.id.startsWith(nodeId + "~"));
}

function liveBadge() {
  // Reuses the "running" badge color (see style.css's data-status rules)
  // under a more literal "live" label for the tree's attached-subagent dot.
  return el("span", { class: "badge", "data-status": "running" }, "● live");
}

function renderTreeBranch(branch, depth) {
  const rows = [];
  for (const [part, child] of branch.children) {
    const n = child.node;
    const hasKids = child.children.size > 0;
    const attached = n ? findAttachedSubagents(n.id) : [];
    const liveSub = attached.find((s) => s.live);
    const lastSub = attached.length ? attached[attached.length - 1] : null;
    rows.push(
      el(
        "div",
        {
          class: "tree-row" + (n && n.id === state.selectedNode ? " active" : "") + (n ? "" : " tree-row-folder"),
          style: `padding-left:${depth * 18 + 10}px;`,
          onclick: n ? () => openNode(n.id) : null,
        },
        [
          el("span", { class: "tree-glyph" }, hasKids ? "▾" : n ? "·" : " "),
          el("span", { class: "tree-label" }, part),
          n ? badge(n.status) : null,
          n && n.shape ? el("span", { class: "dim", style: "font-size:11px; margin-left:6px;" }, n.shape) : null,
          liveSub
            ? el("span", {
                onclick: (e) => { e.stopPropagation(); openNode(liveSub.id, "chat"); },
                title: `subagent ${liveSub.id} is running -- click to open its chat`,
              }, liveBadge())
            : lastSub
              ? el("span", { title: `subagent ${lastSub.id}: ${lastSub.status}` }, badge(lastSub.status))
              : null,
          n && n.artifact_count
            ? el("span", { class: "dim tree-artifact-count", title: "artifacts generated" }, `📄 ${n.artifact_count}`)
            : null,
        ]
      )
    );
    if (hasKids) rows.push(...renderTreeBranch(child, depth + 1));
  }
  return rows;
}

function renderTaskTreeTab() {
  const nodes = state.snapshot.tree || [];
  if (!nodes.length) return el("div", { class: "empty-state" }, "No plan tree generated yet.");
  return el("div", { class: "task-tree-view" }, renderTreeBranch(buildNodeTreeIndex(nodes), 0));
}

// ---------------------------------------------------------------------
// New Run Modal
// ---------------------------------------------------------------------
function resetNewRunDraft() {
  state.newRun = { runId: "", goal: "", source: "", model: "", compile: "" };
}

function renderNewRunModal() {
  if (!state.newRunOpen) return null;
  const nr = state.newRun;

  const runIdInput = el("input", { type: "text", "data-key": "new-run-run-id", placeholder: "leave blank to generate" });
  runIdInput.value = nr.runId;
  runIdInput.addEventListener("input", () => { nr.runId = runIdInput.value; });

  const goalInput = el("textarea", { "data-key": "new-run-goal", placeholder: "Goal (or @path/to/file)...", rows: 3 });
  goalInput.value = nr.goal;
  goalInput.addEventListener("input", () => { nr.goal = goalInput.value; });

  const sourceInput = el("textarea", { "data-key": "new-run-source", placeholder: "Source: text, @path, or blank", rows: 2 });
  sourceInput.value = nr.source;
  sourceInput.addEventListener("input", () => { nr.source = sourceInput.value; });

  const modelInput = el("input", { type: "text", "data-key": "new-run-model", placeholder: "Model (blank = provider default)" });
  modelInput.value = nr.model;
  modelInput.addEventListener("input", () => { nr.model = modelInput.value; });

  const compileInput = el("input", { type: "text", "data-key": "new-run-compile", placeholder: "Compile command (optional)" });
  compileInput.value = nr.compile;
  compileInput.addEventListener("input", () => { nr.compile = compileInput.value; });

  const submit = el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
    const goal = nr.goal.trim();
    if (!goal) return;
    const res = await apiPost("/api/runs", {
      run_id: nr.runId.trim(),
      goal,
      source: nr.source.trim(),
      model: nr.model.trim() || null,
      compile_command: nr.compile.trim() || null,
    });
    state.newRunOpen = false;
    resetNewRunDraft();
    state.selectedNode = null;
    state.workbenchTab = "tree";
    showToast(`Started run ${res.run_id}`);
  }) }, "Start Run");

  const cancel = el("button", { onclick: () => { state.newRunOpen = false; resetNewRunDraft(); render(); } }, "Cancel");

  const panel = el("div", { class: "panel" }, [
    el("div", { class: "panel-hdr" }, [el("h2", null, "New run (or resume: reuse an existing run id)"), el("button", { onclick: () => { state.newRunOpen = false; resetNewRunDraft(); render(); } }, "Close")]),
    el("div", { class: "form-grid" }, [
      el("label", null, "Run id"), runIdInput,
      el("label", null, "Goal"), goalInput,
      el("label", null, "Source"), sourceInput,
      el("label", null, "Model"), modelInput,
      el("label", null, "Compile command"), compileInput,
    ]),
    el("div", { style: "padding:16px; display:flex; gap:8px;" }, [submit, cancel]),
  ]);

  return el("div", { class: "overlay", onclick: (e) => { if (e.target.classList.contains("overlay")) { state.newRunOpen = false; resetNewRunDraft(); render(); } } }, panel);
}

// ---------------------------------------------------------------------
// Root Render
// ---------------------------------------------------------------------
// render() rebuilds the whole DOM tree from scratch on every call (no
// diffing), and it's called on every live snapshot tick (SSE ~1.5s /
// polling 2s). Plain <input>/<textarea> elements hold their in-progress
// value only in the DOM node itself, so a naive rebuild would wipe
// whatever the operator is mid-typing on every tick. Capture the focused
// field's value/selection (keyed by its stable data-key) and the feed's
// scroll position before tearing down, then restore both afterward.
function captureFocusState() {
  const active = document.activeElement;
  if (!active || !root.contains(active)) return null;
  const key = active.getAttribute("data-key");
  if (!key) return null;
  return {
    key,
    value: active.value,
    selectionStart: active.selectionStart,
    selectionEnd: active.selectionEnd,
  };
}

function restoreFocusState(saved) {
  if (!saved) return;
  const field = root.querySelector(`[data-key="${CSS.escape(saved.key)}"]`);
  if (!field) return;
  field.value = saved.value;
  try {
    field.focus({ preventScroll: true });
  } catch (e) {
    field.focus();
  }
  if (typeof saved.selectionStart === "number") {
    try { field.setSelectionRange(saved.selectionStart, saved.selectionEnd); } catch (e) {}
  }
}

function captureScrollStates() {
  const map = new Map();
  const selectors = ["#chat-feed-scroll", ".sidebar-content", ".workbench-content"];
  selectors.forEach((sel) => {
    const el = root.querySelector(sel);
    if (el) {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      map.set(sel, { scrollTop: el.scrollTop, atBottom });
    }
  });
  return map;
}

function restoreScrollStates(savedMap) {
  if (!savedMap) return;
  savedMap.forEach((pos, sel) => {
    const el = root.querySelector(sel);
    if (el) {
      if (sel === "#chat-feed-scroll" && pos.atBottom) {
        el.scrollTop = el.scrollHeight;
      } else {
        el.scrollTop = pos.scrollTop;
      }
    }
  });
}

function render() {
  const focusState = captureFocusState();
  const scrollStates = captureScrollStates();

  const frag = document.createDocumentFragment();
  frag.appendChild(renderHeader());

  const workspace = el("div", { class: "kd-workspace" }, [
    renderSidebar(),
    renderCenterStream(),
    renderRightWorkbench(),
  ]);
  frag.appendChild(workspace);

  const newRunModal = renderNewRunModal();
  if (newRunModal) frag.appendChild(newRunModal);

  if (state.toast) {
    frag.appendChild(el("div", { class: "toast" + (state.toast.isError ? " err" : "") }, state.toast.message));
  }

  root.innerHTML = "";
  root.appendChild(frag);

  restoreScrollStates(scrollStates);
  restoreFocusState(focusState);
}

render();
startLive();
