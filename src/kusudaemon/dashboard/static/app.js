// OpenCode · Kusudaemon Interactive Coder Web Interface
// Preserves all backend API hooks (/api/snapshot, /api/runs, /api/halt, /api/amend, /api/approvals)
// while rendering a 3-pane interactive coder interface.

const PHASES = ["intake", "survey", "plan", "pilot", "research", "execute", "assemble"];

const state = {
  snapshot: { attached: false, runs: [], control_enabled: true },
  sidebarTab: "sessions", // 'sessions' | 'tree' | 'phases'
  workbenchTab: "code",   // 'code' | 'contract' | 'spec' | 'spine' | 'assembly' | 'terminal'
  selectedNode: null,
  nodeDetail: null,
  nodeDetailLoading: false,
  newRunOpen: false,
  busy: false,
  toast: null,
  contractText: "",
  specText: "",
  spineText: "",
  assembly: null,
  promptText: "",
  promptMode: "auto", // 'auto' | 'amend' | 'reopen'
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
function applySnapshot(snap) {
  state.snapshot = snap;
  render();
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

// ---------------------------------------------------------------------
// Header Component
// ---------------------------------------------------------------------
function renderHeader() {
  const snap = state.snapshot;
  const brand = el("div", { class: "brand" }, [
    el("div", { class: "logo-icon" }, "⚡"),
    el("span", null, "OpenCode"),
    el("span", { class: "brand-tag" }, "Kusudaemon Coder"),
  ]);

  const children = [brand];

  if (snap.attached) {
    children.push(
      el("div", { class: "run-selector-badge" }, [
        el("span", { style: "color:var(--text-muted);" }, "Session:"),
        el("span", { style: "font-weight:600;" }, snap.run_id),
        badge(snap.phase_status || "running"),
        snap.halted ? badge("halted") : null,
      ])
    );
  } else {
    children.push(el("div", { class: "run-selector-badge" }, "No Active Session"));
  }

  children.push(el("span", { class: "spacer" }));

  if (!snap.control_enabled) {
    children.push(el("span", { class: "badge" }, "Read-Only"));
  }

  const actions = [];
  if (snap.attached && snap.control_enabled) {
    if (snap.halted) {
      actions.push(
        el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(resumeAttached) }, "▶ Resume Agent")
      );
    } else {
      actions.push(
        el("button", { class: "danger", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
          await apiPost("/api/halt", { value: true });
          showToast("Halt signal sent to agent");
        }) }, "⏸ Halt Execution")
      );
    }
  }

  actions.push(
    el("button", { class: "primary", disabled: snap.control_enabled ? null : "", onclick: () => { state.newRunOpen = true; render(); } }, "+ New Session")
  );

  children.push(el("div", { class: "hdr-actions" }, actions));
  return el("header", { class: "hdr" }, children);
}

async function resumeAttached() {
  const runId = state.snapshot.run_id;
  await apiPost("/api/halt", { value: false });
  await apiPost("/api/runs", { run_id: runId });
  showToast(`Resumed session ${runId}`);
}

// ---------------------------------------------------------------------
// Left Sidebar Component
// ---------------------------------------------------------------------
function renderSidebar() {
  const snap = state.snapshot;
  const navTabs = el("div", { class: "sidebar-nav-tabs" }, [
    el("div", { class: "nav-tab" + (state.sidebarTab === "sessions" ? " active" : ""), onclick: () => { state.sidebarTab = "sessions"; render(); } }, "Sessions"),
    el("div", { class: "nav-tab" + (state.sidebarTab === "tree" ? " active" : ""), onclick: () => { state.sidebarTab = "tree"; render(); } }, "Task Tree"),
    el("div", { class: "nav-tab" + (state.sidebarTab === "phases" ? " active" : ""), onclick: () => { state.sidebarTab = "phases"; render(); } }, "Phases"),
  ]);

  let content = null;
  if (state.sidebarTab === "sessions") {
    const runs = snap.runs || [];
    const items = runs.length
      ? runs.map((r) =>
          el("li", {
            class: "run-item" + (r.attached ? " active" : ""),
            onclick: () => guarded(() => apiPost("/api/attach", { run_id: r.id })),
          }, [
            el("div", { class: "goal" }, r.goal || r.id),
            el("div", { class: "meta" }, [badge(r.status || r.phase || "-"), el("span", null, r.id)]),
          ])
        )
      : [el("div", { class: "empty-state" }, "No coding sessions found.")];
    content = el("ul", { class: "run-list" }, items);
  } else if (state.sidebarTab === "tree") {
    const nodes = snap.tree || [];
    if (!nodes.length) {
      content = el("div", { class: "empty-state" }, "No plan tree generated yet.");
    } else {
      content = el("div", { class: "node-tree-list" }, nodes.map((n) =>
        el("div", { class: "node-card", onclick: () => openNode(n.id) }, [
          el("div", { class: "node-hdr" }, [el("span", null, `[${n.id}] ${n.shape}`), badge(n.status)]),
          el("div", { class: "brief" }, n.brief),
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
      el("h3", null, "Workspace Explorer"),
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
        el("h3", { style: "color:var(--text-bright); margin-bottom:8px;" }, "Welcome to OpenCode Coder"),
        el("p", null, "Start a new session or pick an existing task session from the sidebar to begin interactive coding."),
      ])
    );
  } else {
    // Goal prompt message
    feed.appendChild(
      el("div", { class: "stream-msg user" }, [
        el("div", { class: "msg-hdr" }, [el("span", { class: "author" }, "User Prompt"), el("span", null, "Initial Goal")]),
        el("div", { class: "msg-body" }, snap.goal || "(No prompt specified)"),
      ])
    );

    // Agent status card
    if (snap.phase) {
      feed.appendChild(
        el("div", { class: "stream-card" }, [
          el("div", { class: "card-title" }, [
            el("span", null, `🤖 Agent Phase: ${snap.phase.toUpperCase()}`),
            badge(snap.phase_status || "running"),
          ]),
          snap.phase_detail ? el("div", { style: "color:var(--accent-amber); font-size:12px; margin-top:4px;" }, snap.phase_detail) : null,
        ])
      );
    }

    // Pending Approvals in Stream
    const approvals = snap.approvals || [];
    approvals.forEach((a) => {
      const parts = [
        el("div", { class: "card-title" }, [
          el("span", null, `⚡ ${a.kind.toUpperCase()}: ${a.title}`),
          badge(a.status),
        ]),
      ];
      if (a.message) parts.push(el("div", { class: "card-text" }, a.message));

      if (a.status === "pending" && snap.control_enabled) {
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
          const inputEl = el("input", { type: "text", placeholder: a.input_label || "Provide response details...", style: "margin-top:8px;" });
          parts.push(inputEl);
          actionBtns.push(
            el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
              await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "answer", user_input: inputEl.value });
              showToast("Submitted answer");
            }) }, "Submit Input")
          );
        }
        parts.push(el("div", { class: "approval-actions" }, actionBtns));
      } else if (a.status === "resolved") {
        parts.push(el("div", { style: "font-size:11px; color:var(--text-muted); margin-top:4px;" }, `Resolved via action: ${a.action || "completed"}`));
      }

      feed.appendChild(el("div", { class: "stream-card approval" }, parts));
    });

    // Recent events stream
    const events = (snap.events || []).slice(-15);
    events.forEach((ev) => {
      feed.appendChild(
        el("div", { class: "stream-msg agent" }, [
          el("div", { class: "msg-hdr" }, [
            el("span", { class: "author" }, "Agent Event"),
            el("span", null, fmtTime(ev.ts)),
            ev.node_id && ev.node_id !== "-" ? badge(ev.node_id) : null,
          ]),
          el("div", { class: "msg-body" }, `${ev.type}${ev.phase ? ` [${ev.phase}]` : ""}${ev.status ? ` - ${ev.status}` : ""}`),
        ])
      );
    });
  }

  // Interactive Prompt Controls at Bottom
  const promptBar = renderPromptBar();

  return el("main", { class: "chat-stream-panel" }, [
    el("div", { class: "chat-header" }, [
      el("div", { class: "title" }, ["💬 Interactive Agent Stream", snap.halted ? badge("halted") : null]),
      el("span", { style: "font-size:11px; color:var(--text-muted);" }, snap.attached ? `${snap.events_count || 0} events` : ""),
    ]),
    feed,
    promptBar,
  ]);
}

function renderPromptBar() {
  const snap = state.snapshot;
  const disabled = !snap.control_enabled || state.busy;

  const modeSelector = el("div", { class: "prompt-mode-selector" }, [
    el("button", { class: "mode-btn" + (state.promptMode === "auto" ? " active" : ""), onclick: () => { state.promptMode = "auto"; render(); } }, "New Task"),
    el("button", { class: "mode-btn" + (state.promptMode === "amend" ? " active" : ""), onclick: () => { state.promptMode = "amend"; render(); } }, "Amend Contract"),
    el("button", { class: "mode-btn" + (state.promptMode === "reopen" ? " active" : ""), onclick: () => { state.promptMode = "reopen"; render(); } }, "Reopen Node"),
  ]);

  const ta = el("textarea", {
    class: "prompt-textarea",
    placeholder: state.promptMode === "amend" ? "Enter contract amendment rule to append..." : state.promptMode === "reopen" ? "Node ID and defect description..." : "Type instructions or code request for the agent...",
    rows: 1,
    disabled: disabled ? "" : null,
  });

  const sendBtn = el("button", { class: "primary", disabled: disabled ? "" : null, onclick: () => guarded(() => handlePromptSubmit(ta.value)) }, "Send ↵");

  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handlePromptSubmit(ta.value);
    }
  });

  return el("div", { class: "prompt-container" }, [
    el("div", { class: "prompt-controls" }, [
      modeSelector,
      el("span", { style: "font-size:11px; color:var(--text-muted);" }, "Press Enter to submit, Shift+Enter for newline"),
    ]),
    el("div", { class: "prompt-input-wrapper" }, [ta, sendBtn]),
  ]);
}

async function handlePromptSubmit(text) {
  text = text.trim();
  if (!text) return;

  if (state.promptMode === "amend") {
    await apiPost("/api/amend", { text, reason: "Web interaction prompt" });
    showToast("Contract amendment queued");
    fetchWorkbenchData("contract");
  } else if (state.promptMode === "reopen") {
    const parts = text.split(" ");
    const nodeId = parts[0];
    const defect = parts.slice(1).join(" ") || "Defect requested via prompt";
    await apiPost("/api/reopen", { node_id: nodeId, defect });
    showToast(`Reopen requested for node ${nodeId}`);
  } else {
    // Start or attach new task run
    const res = await apiPost("/api/runs", { goal: text });
    showToast(`Started session ${res.run_id}`);
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
    ["terminal", "🖥️ Terminal Logs"],
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
        el("span", null, state.nodeDetail ? `Node Artifact: ${state.nodeDetail.id}` : "Assembled Code Output"),
        el("button", { class: "icon-btn", onclick: () => navigator.clipboard.writeText(state.nodeDetail?.artifact || state.assembly?.output || "") }, "📋 Copy"),
      ]),
      el("pre", { class: "blob" }, state.nodeDetail?.artifact || state.assembly?.output || "(No active code artifact selected. Click a node in Task Tree to view code.)"),
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
// Node Detail Drawer
// ---------------------------------------------------------------------
function openNode(id) {
  state.selectedNode = id;
  state.nodeDetail = null;
  state.nodeDetailLoading = true;
  state.workbenchTab = "code";
  render();
  apiGet(`/api/node/${encodeURIComponent(id)}`)
    .then((d) => { state.nodeDetail = d; state.nodeDetailLoading = false; render(); })
    .catch((err) => { state.nodeDetailLoading = false; showToast(String(err.message || err), true); });
}

function closeNode() {
  state.selectedNode = null;
  state.nodeDetail = null;
  render();
}

function renderNodeDrawer() {
  if (!state.selectedNode) return null;
  const d = state.nodeDetail;
  const body = [];
  if (state.nodeDetailLoading || !d) {
    body.push(el("div", { class: "empty-state" }, "Loading node data…"));
  } else {
    body.push(el("div", { style: "display:flex; gap:12px; margin-bottom:12px;" }, [badge(d.status), el("span", null, `Shape: ${d.shape}`), el("span", null, `Attempts: ${d.attempts}`)]));
    body.push(el("h3", { style: "color:var(--text-bright);" }, "Brief"), el("p", null, d.brief));
    body.push(el("h3", { style: "color:var(--text-bright); margin-top:12px;" }, "Artifact"), el("pre", { class: "blob" }, truncate(d.artifact || "(empty)", 4000)));
  }

  const panel = el("div", { class: "panel" }, [
    el("div", { class: "panel-hdr" }, [el("h2", null, `Node Details: ${state.selectedNode}`), el("button", { onclick: closeNode }, "Close")]),
    el("div", { style: "padding:16px; overflow-y:auto;" }, body),
  ]);
  return el("div", { class: "overlay", onclick: (e) => { if (e.target.classList.contains("overlay")) closeNode(); } }, panel);
}

// ---------------------------------------------------------------------
// New Session Modal
// ---------------------------------------------------------------------
function renderNewRunModal() {
  if (!state.newRunOpen) return null;
  const goalInput = el("textarea", { placeholder: "Task Goal / Coding Instruction...", rows: 3 });
  const modelInput = el("input", { type: "text", placeholder: "Model provider default" });

  const submit = el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
    const goal = goalInput.value.trim();
    if (!goal) return;
    const res = await apiPost("/api/runs", { goal, model: modelInput.value.trim() });
    state.newRunOpen = false;
    showToast(`Started new session ${res.run_id}`);
  }) }, "Start Interactive Session");

  const cancel = el("button", { onclick: () => { state.newRunOpen = false; render(); } }, "Cancel");

  const panel = el("div", { class: "panel" }, [
    el("div", { class: "panel-hdr" }, [el("h2", null, "New Coding Session"), el("button", { onclick: () => { state.newRunOpen = false; render(); } }, "Close")]),
    el("div", { class: "form-grid" }, [
      el("label", null, "Session Goal"), goalInput,
      el("label", null, "Model"), modelInput,
    ]),
    el("div", { style: "padding:16px; display:flex; gap:8px;" }, [submit, cancel]),
  ]);

  return el("div", { class: "overlay", onclick: (e) => { if (e.target.classList.contains("overlay")) { state.newRunOpen = false; render(); } } }, panel);
}

// ---------------------------------------------------------------------
// Root Render
// ---------------------------------------------------------------------
function render() {
  root.innerHTML = "";
  root.appendChild(renderHeader());

  const workspace = el("div", { class: "opencode-workspace" }, [
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
}

render();
startLive();
