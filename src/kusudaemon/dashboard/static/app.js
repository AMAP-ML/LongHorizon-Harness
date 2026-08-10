// Kusudaemon web dashboard.
// Preserves every backend API hook the old 3-pane view had (/api/snapshot,
// /api/runs, /api/halt, /api/amend, /api/approvals) and adds the surface
// the (now-deleted) Textual TUI had on top of it: a Subagents view over
// every dispatched Writer/repair/research episode, a node drawer with
// Overview/Artifact/Diff/Thinking tabs, live mid-episode interject, and
// per-node reopen.

const PHASES = ["intake", "survey", "plan", "pilot", "research", "execute", "assemble"];

const state = {
  snapshot: { attached: false, runs: [], control_enabled: true },
  sidebarTab: "sessions", // 'sessions' | 'tree' | 'subagents' | 'phases'
  workbenchTab: "code",   // 'code' | 'contract' | 'spec' | 'spine' | 'assembly' | 'terminal'
  selectedNode: null,
  nodeDetail: null,
  nodeSubagent: null,
  nodeDetailLoading: false,
  drawerTab: "overview", // 'overview' | 'artifact' | 'diff' | 'thinking'
  nodeDiff: null,        // [{tag, lines}] once loaded, keyed to selectedNode
  nodeThinking: null,    // [{role, text}] once loaded, keyed to selectedNode
  newRunOpen: false,
  busy: false,
  toast: null,
  contractText: "",
  specText: "",
  spineText: "",
  assembly: null,
  promptText: "",
  promptMode: "auto", // 'auto' | 'amend' | 'reopen'
  // Draft text for every other free-text field, keyed so a periodic
  // snapshot re-render (SSE/poll) can safely rebuild the field's DOM node
  // from scratch without losing what's typed in it — see render()'s
  // comment for why that rebuild happens on every live update.
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

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

function showToast(message, isError) {
  state.toast = { message, isError };
  render();
  setTimeout(() => {
    if (state.toast && state.toast.message === message) {
      state.toast = null;
      render();
    }
  }, 4500);
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

function applySnapshot(snap) {
  if (snap && snap.attached && !snap.goal && state.snapshot && state.snapshot.run_id === snap.run_id && state.snapshot.goal) {
    snap.goal = state.snapshot.goal;
  }
  const unchanged = snapshotFingerprint(snap) === snapshotFingerprint(state.snapshot);
  state.snapshot = snap;
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
  const tabs = [
    ["sessions", "Runs"],
    ["tree", "Task Tree"],
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
            onclick: () => guarded(() => apiPost("/api/attach", { run_id: r.id })),
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
  } else if (state.sidebarTab === "tree") {
    const nodes = snap.tree || [];
    if (!nodes.length) {
      content = el("div", { class: "empty-state" }, "No plan tree generated yet.");
    } else {
      content = el("div", { class: "node-tree-list" }, nodes.map((n) =>
        el("div", { class: "node-card" + (n.id === state.selectedNode ? " active" : ""), onclick: () => openNode(n.id) }, [
          el("div", { class: "node-hdr" }, [el("span", null, `[${n.id}] ${n.shape}`), badge(n.status)]),
          el("div", { class: "brief" }, n.brief),
        ])
      ));
    }
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

  // Resume status banner if halted, error, or paused
  const isStoppedOrHalted = snap.halted || snap.phase_status === "error" || snap.phase_status === "paused" || snap.phase_status === "escalated";
  if (isStoppedOrHalted && snap.control_enabled) {
    feed.appendChild(
      el("div", { class: "stream-card resume-banner" }, [
        el("div", { class: "card-title" }, [
          el("span", { style: "color:var(--accent-amber);" }, `⚠️ Run status: ${snap.phase_status || (snap.halted ? "halted" : "stopped")}`),
          el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(resumeAttached) }, "▶ Resume / Continue Run"),
        ]),
        el("div", { style: "font-size:12px; color:var(--text-muted);" }, "Click Resume to continue execution automatically."),
      ])
    );
  }

  // Resolved Intake / Survey Answers Section
  const approvals = snap.approvals || [];
  const intakeApprovals = approvals.filter((a) => a.kind === "intake_question" || (a.user_input && a.status === "resolved"));
  if (intakeApprovals.length > 0) {
    feed.appendChild(
      el("div", { class: "stream-card intake-summary-card" }, [
        el("div", { class: "card-title" }, [
          el("span", null, "📋 Intake & Survey Answers Provided"),
          el("span", { class: "badge", "data-status": "passed" }, `${intakeApprovals.length} answered`),
        ]),
        el("div", { class: "intake-answers-list" }, intakeApprovals.map((a) =>
          el("div", { class: "intake-qa-item" }, [
            el("div", { class: "intake-q" }, `Q: ${a.message || a.title}`),
            el("div", { class: "intake-a" }, `A: ${a.user_input || "(default accepted)"}`),
          ])
        )),
      ])
    );
  }

  // Live Thinking Stream Widget for active subagents
  const subagents = snap.subagents || [];
  const liveSub = subagents.find((s) => s.live);
  if (liveSub) {
    feed.appendChild(
      el("div", { class: "stream-card thinking-live-card" }, [
        el("div", { class: "card-title" }, [
          el("span", { style: "color:var(--accent-purple); font-weight:600;" }, `🧠 Bot Thinking Stream [Subagent: ${liveSub.id}]`),
          el("button", { class: "xs-btn", onclick: () => openNode(liveSub.id) }, "Open Details"),
        ]),
        el("div", { class: "thinking-live-body", id: `thinking-live-${liveSub.id}` }, [
          el("span", { class: "dim" }, `Subagent ${liveSub.id} is active (${liveSub.role}). View full thinking trace in Subagents drawer.`),
        ]),
      ])
    );
  }

  // Render Approvals (Pending & Resolved)
  approvals.forEach((a) => {
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
    feed.appendChild(el("div", { class: "stream-card approval" + (isPending ? " pending" : ""), style: cardStyle }, parts));
  });

  // Events (Newest at bottom)
  const events = (snap.events || []).slice(-20);
  events.forEach((ev) => {
    feed.appendChild(
      el("div", { class: "stream-msg agent" }, [
        el("div", { class: "msg-hdr" }, [
          el("span", { class: "author" }, "Event"),
          el("span", null, fmtTime(ev.ts)),
          ev.node_id && ev.node_id !== "-" ? el("span", { class: "node-link", onclick: () => openNode(ev.node_id) }, ev.node_id) : null,
        ]),
        el("div", { class: "msg-body" }, `${ev.type}${ev.phase ? ` [${ev.phase}]` : ""}${ev.status ? ` - ${ev.status}` : ""}`),
      ])
    );
  });

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
    el("button", { class: "mode-btn" + (state.promptMode === "auto" ? " active" : ""), onclick: () => { state.promptMode = "auto"; render(); } }, "New Run"),
    el("button", { class: "mode-btn" + (state.promptMode === "amend" ? " active" : ""), onclick: () => { state.promptMode = "amend"; render(); } }, "Amend Contract"),
    el("button", { class: "mode-btn" + (state.promptMode === "reopen" ? " active" : ""), onclick: () => { state.promptMode = "reopen"; render(); } }, "Reopen Node"),
    el("button", { class: "mode-btn" + (state.promptMode === "msg_agent" ? " active" : ""), onclick: () => { state.promptMode = "msg_agent"; render(); } }, "💬 Message Agent"),
  ]);

  let targetSelector = null;
  if (state.promptMode === "msg_agent") {
    const subagents = snap.subagents || [];
    const options = subagents.length
      ? subagents.map((s) => el("option", { value: s.id, selected: (state.targetAgentId || subagents[0].id) === s.id ? "selected" : null }, `[${s.id}] ${s.kind} (${s.status})`))
      : [el("option", { value: "" }, "(No subagents found)")];

    const selectEl = el("select", {
      class: "agent-target-select",
      onchange: (e) => { state.targetAgentId = e.target.value; }
    }, options);

    if (!state.targetAgentId && subagents.length) {
      state.targetAgentId = subagents[0].id;
    }

    targetSelector = el("div", { class: "agent-target-picker" }, [
      el("label", { style: "font-size:11px; font-weight:600; color:var(--accent-purple);" }, "Target:"),
      selectEl,
    ]);
  }

  const ta = el("textarea", {
    class: "prompt-textarea",
    "data-key": "prompt-textarea",
    placeholder:
      state.promptMode === "amend" ? "Enter contract amendment rule to append..." :
      state.promptMode === "reopen" ? "Node ID and defect description..." :
      state.promptMode === "msg_agent" ? "Type a direct message to send to the subagent mid-episode..." :
      "Type a run goal (or @path/to/file)...",
    rows: 1,
    disabled: disabled ? "" : null,
  });
  ta.value = state.promptText;
  ta.addEventListener("input", () => { state.promptText = ta.value; });

  const sendBtn = el("button", { class: "primary", disabled: disabled ? "" : null, onclick: () => guarded(() => handlePromptSubmit()) }, "Send ↵");

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
    const targetId = state.targetAgentId || (state.snapshot.subagents && state.snapshot.subagents[0] && state.snapshot.subagents[0].id);
    if (!targetId) {
      showToast("No target agent selected", true);
      return;
    }
    await apiPost(`/api/node/${encodeURIComponent(targetId)}/interject`, { text });
    state.promptText = "";
    showToast(`Message queued for agent ${targetId}`);
  } else {
    const res = await apiPost("/api/runs", { goal: text });
    state.promptText = "";
    showToast(`Started run ${res.run_id}`);
  }
}

// ---------------------------------------------------------------------
// Right Workbench Component (Code, Spec, Logs, Assembly)
// ---------------------------------------------------------------------
function renderRightWorkbench() {
  const wbTabs = [
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
      el("pre", { class: "blob" }, (state.nodeDetail && state.nodeDetail.artifact) || (state.assembly && state.assembly.output) || "(No active artifact selected. Click a node in Task Tree to view it.)"),
    ]);
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
function openNode(id) {
  state.selectedNode = id;
  state.nodeDetail = null;
  state.nodeSubagent = null;
  state.nodeDiff = null;
  state.nodeThinking = null;
  state.nodeDetailLoading = true;
  state.drawerTab = "overview";
  state.workbenchTab = "code";
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
  render();
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
  if (s.live) parts.push(el("div", { style: "color:var(--accent-amber); margin-top:8px; font-weight:600;" }, "● live — send it a message below"));
  if (s.error) parts.push(el("div", { style: "color:var(--accent-red); margin-top:8px;" }, `error: ${s.error}`));
  parts.push(el("div", { class: "dim", style: "margin-top:12px;" }, "Not a tree node — see its parent tree node for artifact/diff history."));
  return el("div", null, parts);
}

function renderArtifactTab() {
  const d = state.nodeDetail;
  if (!d) return el("div", { class: "empty-state" }, "(not a tree node — no standalone artifact)");
  return el("pre", { class: "blob" }, d.artifact || "(empty)");
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

function renderThinkingTab() {
  loadThinkingIfNeeded();
  if (state.nodeThinking === "loading" || state.nodeThinking === null) return el("div", { class: "empty-state" }, "Loading trace…");
  if (!state.nodeThinking.length) return el("div", { class: "empty-state" }, "(no trace yet)");
  return el("div", { class: "trace-log" }, state.nodeThinking.map((e) =>
    el("div", { class: `trace-entry trace-${e.role}` }, [el("span", { class: "trace-role" }, `[${e.role}] `), e.text])
  ));
}

function renderNodeDrawer() {
  if (!state.selectedNode) return null;
  const id = state.selectedNode;
  const tabs = [
    ["overview", "Overview"],
    ["artifact", "Artifact"],
    ["diff", "Diff"],
    ["thinking", "Thinking"],
  ];

  const tabBar = el("div", { class: "drawer-tabs" }, tabs.map(([tid, label]) =>
    el("div", { class: "drawer-tab" + (state.drawerTab === tid ? " active" : ""), onclick: () => { state.drawerTab = tid; render(); } }, label)
  ));

  let body;
  if (state.nodeDetailLoading) {
    body = el("div", { class: "empty-state" }, "Loading…");
  } else if (state.drawerTab === "overview") body = renderOverview();
  else if (state.drawerTab === "artifact") body = renderArtifactTab();
  else if (state.drawerTab === "diff") body = renderDiffTab();
  else body = renderThinkingTab();

  const live = isLive(id);
  const interjectInput = el("input", { type: "text", "data-key": `interject-${id}`, placeholder: live ? "Message the running agent…" : "(not currently running)", disabled: live ? null : "" });
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

  const panel = el("div", { class: "panel drawer-panel" }, [
    el("div", { class: "panel-hdr" }, [el("h2", null, `${id}`), el("button", { onclick: closeNode }, "Close")]),
    tabBar,
    el("div", { class: "drawer-body" }, body),
    el("div", { class: "drawer-bar" }, [
      el("label", { class: "bar-label" }, "Message the running agent (mid-episode)"),
      el("div", { class: "bar-row" }, [interjectInput, interjectBtn]),
    ]),
    el("div", { class: "drawer-bar" }, [
      el("label", { class: "bar-label" }, "Reopen (passed nodes only): defect to fix"),
      el("div", { class: "bar-row" }, [reopenInput, reopenBtn]),
    ]),
  ]);
  return el("div", { class: "overlay", onclick: (e) => { if (e.target.classList.contains("overlay")) closeNode(); } }, panel);
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
  field.focus();
  if (typeof saved.selectionStart === "number") {
    try { field.setSelectionRange(saved.selectionStart, saved.selectionEnd); } catch (e) {}
  }
}

function render() {
  const focusState = captureFocusState();
  const feedBefore = document.getElementById("chat-feed-scroll");
  const feedScrollTop = feedBefore ? feedBefore.scrollTop : null;

  root.innerHTML = "";
  root.appendChild(renderHeader());

  const workspace = el("div", { class: "kd-workspace" }, [
    renderSidebar(),
    renderCenterStream(),
    renderRightWorkbench(),
  ]);
  root.appendChild(workspace);

  const nodeDrawer = renderNodeDrawer();
  if (nodeDrawer) root.appendChild(nodeDrawer);

  const newRunModal = renderNewRunModal();
  if (newRunModal) root.appendChild(newRunModal);

  if (state.toast) {
    root.appendChild(el("div", { class: "toast" + (state.toast.isError ? " err" : "") }, state.toast.message));
  }

  restoreFocusState(focusState);
  const feedAfter = document.getElementById("chat-feed-scroll");
  if (feedAfter) {
    if (feedScrollTop === null || (feedBefore && feedBefore.scrollHeight - feedScrollTop - feedBefore.clientHeight < 120)) {
      feedAfter.scrollTop = feedAfter.scrollHeight;
    } else {
      feedAfter.scrollTop = feedScrollTop;
    }
  }
}

render();
startLive();
