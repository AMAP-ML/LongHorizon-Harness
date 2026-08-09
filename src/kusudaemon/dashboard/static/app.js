// Kusudaemon recursive-decomposition dashboard (PLAN.md §11 view surface).
// Vanilla JS, no build step, no framework: fetch + a plain re-render on
// every snapshot tick. The run directory on disk is the only authority —
// this file never keeps state the server snapshot doesn't already have,
// except which tab/node is currently open in the UI.

const PHASES = ["intake", "survey", "plan", "pilot", "research", "execute", "assemble"];
const TABS = [
  ["overview", "Overview"],
  ["tree", "Tree"],
  ["approvals", "Approvals"],
  ["contract", "Contract"],
  ["spec", "Spec"],
  ["spine", "Spine"],
  ["assembly", "Assembly"],
  ["events", "Events"],
];

const state = {
  snapshot: { attached: false, runs: [], control_enabled: true },
  tab: "overview",
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
  amendText: "",
};

const root = document.getElementById("app");

// ---------------------------------------------------------------------
// API
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
// Live updates: SSE with a polling fallback
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
// Helpers
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
  return new Date(ts * 1000).toLocaleString();
}

function truncate(text, n) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "\n…[truncated]" : text;
}

// ---------------------------------------------------------------------
// Render: header
// ---------------------------------------------------------------------
function renderHeader() {
  const snap = state.snapshot;
  const children = [
    el("span", { class: "brand" }, "🪷 Kusudaemon"),
  ];
  if (snap.attached) {
    children.push(el("span", { class: "run-id" }, snap.run_id));
    children.push(badge(snap.phase_status || "pending"));
    if (snap.halted) children.push(badge("halted"));
  }
  children.push(el("span", { class: "spacer" }));
  if (!snap.control_enabled) {
    children.push(el("span", { class: "badge" }, "read-only view"));
  }
  if (snap.attached && snap.control_enabled) {
    if (snap.halted) {
      children.push(
        el("button", {
          class: "primary",
          disabled: state.busy ? "" : null,
          onclick: () => guarded(resumeAttached),
        }, "Resume")
      );
    } else {
      children.push(
        el("button", {
          class: "danger",
          disabled: state.busy ? "" : null,
          onclick: () => guarded(async () => {
            await apiPost("/api/halt", { value: true });
            showToast("halt requested — stops at the next phase boundary");
          }),
        }, "Halt")
      );
    }
  }
  return el("div", { class: "hdr" }, children);
}

async function resumeAttached() {
  const runId = state.snapshot.run_id;
  await apiPost("/api/halt", { value: false });
  await apiPost("/api/runs", { run_id: runId });
  showToast(`resuming ${runId}`);
}

// ---------------------------------------------------------------------
// Render: sidebar
// ---------------------------------------------------------------------
function renderSidebar() {
  const snap = state.snapshot;
  const runs = snap.runs || [];
  const items = runs.length
    ? runs.map((r) =>
        el(
          "li",
          { class: "run-item" + (r.attached ? " active" : ""), onclick: () => guarded(() => apiPost("/api/attach", { run_id: r.id }).then(() => { state.tab = "overview"; })) },
          [
            el("div", { class: "goal" }, r.goal || r.id),
            el("div", { class: "meta" }, [badge(r.status || r.phase || "-"), el("span", null, r.phase || "")]),
          ]
        )
      )
    : [el("div", { class: "run-empty" }, "No runs yet.")];

  const newRunBtn = el(
    "button",
    { class: "primary", style: "width:100%", disabled: snap.control_enabled ? null : "", onclick: () => { state.newRunOpen = true; render(); } },
    "+ New run"
  );

  return el("div", { class: "sidebar" }, [
    el("div", { class: "sidebar-section" }, [newRunBtn]),
    el("div", { class: "sidebar-section", style: "flex:1; overflow-y:auto;" }, [
      el("h3", null, "Runs"),
      el("ul", { class: "run-list" }, items),
    ]),
  ]);
}

// ---------------------------------------------------------------------
// Render: tabs
// ---------------------------------------------------------------------
function renderTabs() {
  return el(
    "div",
    { class: "tabs" },
    TABS.map(([id, label]) =>
      el(
        "div",
        { class: "tab" + (state.tab === id ? " active" : ""), onclick: () => { state.tab = id; onTabOpen(id); render(); } },
        label
      )
    )
  );
}

function onTabOpen(id) {
  if (!state.snapshot.attached) return;
  if (id === "contract") apiGet("/api/contract").then((d) => { state.contractText = d.text; render(); }).catch(() => {});
  if (id === "spec") apiGet("/api/spec").then((d) => { state.specText = d.text; render(); }).catch(() => {});
  if (id === "spine") apiGet("/api/spine").then((d) => { state.spineText = d.text; render(); }).catch(() => {});
  if (id === "assembly") apiGet("/api/assembly").then((d) => { state.assembly = d; render(); }).catch(() => {});
}

// ---------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------
function renderOverview() {
  const snap = state.snapshot;
  const counts = snap.tree_counts || {};
  const statOrder = ["passed", "dispatched", "awaiting_review", "pending", "stale", "blocked", "failed"];
  const stats = statOrder
    .filter((k) => counts[k])
    .map((k) => el("div", { class: "stat-card" }, [el("div", { class: "n" }, String(counts[k])), el("div", { class: "l" }, k)]));
  stats.push(el("div", { class: "stat-card" }, [el("div", { class: "n" }, String(snap.events_count || 0)), el("div", { class: "l" }, "events")]));
  stats.push(el("div", { class: "stat-card" }, [el("div", { class: "n" }, String((snap.pending_approvals || []).length)), el("div", { class: "l" }, "pending approvals")]));

  const phaseStrip = el(
    "div",
    { class: "phase-strip" },
    PHASES.map((p) => {
      const status = (snap.phases || {})[p] || "pending";
      const isCurrent = snap.phase === p;
      return el("div", { class: "phase-chip", "data-status": status }, [
        el("span", { class: "dot" }),
        el("span", null, p + (isCurrent ? " ●" : "")),
      ]);
    })
  );

  const parts = [
    el("h2", { class: "section-title" }, "Goal"),
    el("pre", { class: "blob" }, snap.goal || "(none)"),
    el("h2", { class: "section-title" }, "Phases"),
    phaseStrip,
    snap.phase_detail ? el("div", { class: "error-banner" }, snap.phase_detail) : null,
    el("h2", { class: "section-title" }, "Status"),
    el("div", { class: "stat-row" }, stats),
  ];

  if ((snap.jobs || []).length) {
    parts.push(el("h2", { class: "section-title" }, "Background jobs"));
    parts.push(
      el(
        "table",
        { class: "tree" },
        el("tbody", null, snap.jobs.map((j) => el("tr", null, [
          el("td", null, j.kind),
          el("td", null, badge(j.status)),
          el("td", null, j.detail || ""),
          el("td", { class: "deps" }, fmtTime(j.ts)),
        ])))
      )
    );
  }

  return el("div", null, parts);
}

// ---------------------------------------------------------------------
// Tree tab
// ---------------------------------------------------------------------
function renderTree() {
  const nodes = state.snapshot.tree || [];
  if (!nodes.length) return el("div", { class: "empty-state" }, "No nodes yet — the plan phase hasn't produced a tree.");
  const rows = nodes.map((n) =>
    el("tr", { class: "node-row", onclick: () => openNode(n.id) }, [
      el("td", { class: "id" }, n.id),
      el("td", null, badge(n.status)),
      el("td", { class: "brief" }, n.brief),
      el("td", null, n.shape),
      el("td", { class: "deps" }, (n.depends_on || []).join(", ") || "-"),
      el("td", null, String(n.attempts || 0)),
      el("td", null, `${n.gates} gate${n.gates === 1 ? "" : "s"}${n.judgment ? `, ${n.judgment} judgment` : ""}`),
    ])
  );
  return el("table", { class: "tree" }, [
    el("thead", null, el("tr", null, ["id", "status", "brief", "shape", "depends on", "attempts", "checks"].map((h) => el("th", null, h)))),
    el("tbody", null, rows),
  ]);
}

function openNode(id) {
  state.selectedNode = id;
  state.nodeDetail = null;
  state.nodeDetailLoading = true;
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

function renderNodePanel() {
  if (!state.selectedNode) return null;
  const d = state.nodeDetail;
  const body = [];
  if (state.nodeDetailLoading || !d) {
    body.push(el("div", { class: "empty-state" }, "Loading…"));
  } else {
    body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "status"), badge(d.status)]));
    body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "shape"), el("span", null, d.shape)]));
    body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "attempts"), el("span", null, String(d.attempts))]));
    body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "budget"), el("span", null, `${d.budget.tokens} tokens / ${d.budget.calls} calls`)]));
    body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "depends on"), el("span", null, (d.depends_on || []).join(", ") || "-")]));
    body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "brief"), el("span", null, d.brief)]));

    body.push(el("h2", { class: "section-title" }, "Gates"));
    body.push(
      el(
        "ul",
        { class: "gate-list" },
        (d.gate_results || []).map((g) => el("li", null, [el("span", { class: g.passed ? "ok" : "bad" }, g.passed ? "✓" : "✗"), el("span", null, g.gate), el("span", { class: "meta" }, g.detail || "")]))
      )
    );

    if ((d.judgment || []).length) {
      body.push(el("h2", { class: "section-title" }, "Judgment rubric"));
      body.push(
        el(
          "ul",
          { class: "gate-list" },
          d.judgment.map((id) => el("li", null, [el("span", null, id + ":"), el("span", null, d.rubric[id] || "")]))
        )
      );
      if (d.audit && d.audit.verdict) {
        body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "verdict"), badge(d.audit.verdict)]));
        (d.audit.items || []).forEach((item) => {
          body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, item.id || "-"), el("span", null, `${item.pass ? "pass" : "fail"}${item.defect ? " — " + item.defect : ""}`)]));
        });
      }
    }

    if ((d.inputs || []).length) {
      body.push(el("h2", { class: "section-title" }, "Inputs"));
      body.push(
        el(
          "ul",
          { class: "input-list" },
          d.inputs.map((i) => el("li", null, [el("span", { class: i.exists ? "ok" : "bad" }, i.exists ? "✓" : "✗"), el("span", null, i.ref), el("span", { class: "meta" }, `${i.tokens} tok`)]))
        )
      );
    }

    if (d.promotion) {
      body.push(el("h2", { class: "section-title" }, "Promotion (handoff summary)"));
      body.push(el("pre", { class: "blob" }, d.promotion));
    }

    if (d.manifest) {
      body.push(el("h2", { class: "section-title" }, "Manifest"));
      body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "gates"), badge(d.manifest.gates)]));
      body.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "tokens"), el("span", null, String(d.manifest.tokens))]));
    }

    if ((d.versions || []).length) {
      body.push(el("h2", { class: "section-title" }, "Versions (pre-repair snapshots)"));
      body.push(el("div", null, d.versions.map((v) => el("span", { style: "margin-right:8px; font-family:var(--mono); color:var(--text-dim);" }, v))));
    }

    body.push(el("h2", { class: "section-title" }, `Artifact (${d.artifact_tokens} tokens)`));
    body.push(el("pre", { class: "blob" }, truncate(d.artifact || "(empty)", 6000)));

    if (state.snapshot.control_enabled && d.status === "passed") {
      body.push(el("h2", { class: "section-title" }, "Reopen"));
      const ta = el("textarea", { placeholder: "Describe the defect to fix…", style: "width:100%; min-height:60px;" });
      body.push(ta);
      body.push(
        el("button", { onclick: () => guarded(async () => {
          const defect = ta.value.trim();
          if (!defect) return;
          await apiPost("/api/reopen", { node_id: d.id, defect });
          showToast("reopen requested — see Approvals");
        }) }, "Request reopen")
      );
    }
  }

  const panel = el("div", { class: "panel" }, [
    el("div", { class: "panel-hdr" }, [el("h2", null, state.selectedNode), el("button", { class: "close-btn", onclick: closeNode }, "Close")]),
    ...body,
  ]);
  return el("div", { class: "overlay", onclick: (e) => { if (e.target.classList.contains("overlay")) closeNode(); } }, panel);
}

// ---------------------------------------------------------------------
// Approvals tab
// ---------------------------------------------------------------------
function renderApprovals() {
  const approvals = (state.snapshot.approvals || []).slice().reverse();
  if (!approvals.length) return el("div", { class: "empty-state" }, "No approvals recorded for this run yet.");
  return el("div", null, approvals.map(renderApprovalCard));
}

function renderApprovalCard(a) {
  const control = state.snapshot.control_enabled;
  const parts = [
    el("div", { class: "kind" }, `${a.kind}${a.status === "resolved" ? " · resolved" : ""}`),
    el("div", { class: "title" }, a.title),
  ];
  if (a.message) parts.push(el("pre", { class: "message" }, a.message));

  if (a.status === "pending" && control) {
    if ((a.options || []).length) {
      parts.push(
        el(
          "div",
          { class: "approval-actions" },
          a.options.map((opt) =>
            el(
              "button",
              { class: opt.style === "primary" ? "primary" : "", disabled: state.busy ? "" : null, onclick: () => guarded(() => apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: opt.value }).then(() => showToast("resolved"))) },
              opt.label
            )
          )
        )
      );
    }
    if (a.allow_input) {
      const ta = el("textarea", { placeholder: a.input_label || "Your answer" });
      parts.push(ta);
      parts.push(
        el("div", { class: "approval-actions" }, [
          el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
            await apiPost(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, { action: "answer", user_input: ta.value });
            showToast("submitted");
          }) }, "Submit"),
        ])
      );
    }
  } else if (a.status === "resolved") {
    parts.push(el("div", { class: "kv-row" }, [el("span", { class: "k" }, "action"), el("span", null, a.action || "-")]));
    if (a.user_input) parts.push(el("pre", { class: "message" }, truncate(a.user_input, 800)));
  }
  return el("div", { class: "approval-card" }, parts);
}

// ---------------------------------------------------------------------
// Contract / spec / spine / assembly / events tabs
// ---------------------------------------------------------------------
function renderContract() {
  const control = state.snapshot.control_enabled;
  const parts = [
    el("pre", { class: "blob" }, state.contractText || "(no contract frozen yet — runs the pilot phase first)"),
  ];
  if (control) {
    parts.push(el("h2", { class: "section-title" }, "Amend"));
    const ta = el("textarea", { placeholder: "Rule text to append to the contract…", style: "width:100%; min-height:70px;" });
    parts.push(ta);
    parts.push(
      el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
        const text = ta.value.trim();
        if (!text) return;
        await apiPost("/api/amend", { text, reason: "web amendment" });
        ta.value = "";
        showToast("amendment queued — see Approvals for the re-validation counts");
        state.tab = "approvals";
      }) }, "Request amendment")
    );
  }
  return el("div", null, parts);
}

function renderSpec() {
  return el("pre", { class: "blob" }, state.specText || "(no spec yet)");
}

function renderSpine() {
  return el("pre", { class: "blob" }, state.spineText || "(no spine yet — runs the survey phase first)");
}

function renderAssembly() {
  const a = state.assembly || {};
  const parts = [];
  if (a.checks && Object.keys(a.checks).length) {
    parts.push(el("h2", { class: "section-title" }, "Cross-cutting checks"));
    parts.push(el("pre", { class: "blob" }, JSON.stringify(a.checks, null, 2)));
  }
  if (a.compile_log) {
    parts.push(el("h2", { class: "section-title" }, "Compile log"));
    parts.push(el("pre", { class: "blob" }, a.compile_log));
  }
  parts.push(el("h2", { class: "section-title" }, "Assembled output"));
  parts.push(el("pre", { class: "blob" }, a.output || "(not assembled yet)"));
  return el("div", null, parts);
}

function renderEvents() {
  const events = state.snapshot.events || [];
  if (!events.length) return el("div", { class: "empty-state" }, "No events yet.");
  return el(
    "div",
    { class: "events-list" },
    events
      .slice()
      .reverse()
      .map((e) =>
        el("div", { class: "ev" }, [
          el("span", { class: "t" }, fmtTime(e.ts)),
          el("span", { class: "node" }, e.node_id && e.node_id !== "-" ? e.node_id : ""),
          el("span", null, `${e.type}${e.phase ? ` (${e.phase})` : ""}${e.status ? ` — ${e.status}` : ""}`),
        ])
      )
  );
}

// ---------------------------------------------------------------------
// New run modal
// ---------------------------------------------------------------------
function renderNewRunModal() {
  if (!state.newRunOpen) return null;
  const f = {
    run_id: el("input", { type: "text", placeholder: "auto-generated if empty" }),
    goal: el("textarea", { placeholder: "Task goal (e.g. 'Write a 5-chapter primer on X')" }),
    source: el("textarea", { placeholder: "Source document text (optional)" }),
    model: el("input", { type: "text", placeholder: "provider default" }),
    compile_command: el("input", { type: "text", placeholder: "e.g. latexmk -pdf (optional)" }),
    research_plan: el("textarea", { placeholder: '[{"node_id": "2.1", "kind": "web_search", "question": "..."}]' }),
    max_rounds: el("input", { type: "number", value: "100" }),
    max_attempts: el("input", { type: "number", value: "3" }),
  };
  const rows = [
    ["Run id", f.run_id],
    ["Goal", f.goal],
    ["Source", f.source],
    ["Model", f.model],
    ["Compile command", f.compile_command],
    ["Research plan", f.research_plan],
    ["Max rounds", f.max_rounds],
    ["Max attempts", f.max_attempts],
  ];
  const form = el(
    "div",
    { class: "form-grid" },
    rows.flatMap(([label, input]) => [el("label", null, label), input])
  );
  const note = el("div", { class: "footer-note" }, "Reusing an existing run id resumes that run from wherever it left off — goal/source/etc. are then read from the run's own saved spec, not this form.");
  const submit = el("button", { class: "primary", disabled: state.busy ? "" : null, onclick: () => guarded(async () => {
    const body = {
      run_id: f.run_id.value.trim(),
      goal: f.goal.value,
      source: f.source.value,
      model: f.model.value.trim(),
      compile_command: f.compile_command.value.trim(),
      research_plan: f.research_plan.value.trim(),
      max_rounds: parseInt(f.max_rounds.value || "100", 10),
      max_attempts: parseInt(f.max_attempts.value || "3", 10),
    };
    const res = await apiPost("/api/runs", body);
    state.newRunOpen = false;
    state.tab = "overview";
    showToast(`started ${res.run_id}`);
  }) }, "Start");
  const cancel = el("button", { onclick: () => { state.newRunOpen = false; render(); } }, "Cancel");
  const panel = el("div", { class: "panel" }, [
    el("div", { class: "panel-hdr" }, [el("h2", null, "New / resume run"), el("button", { class: "close-btn", onclick: () => { state.newRunOpen = false; render(); } }, "Close")]),
    form,
    el("div", { style: "margin-top:14px; display:flex; gap:8px;" }, [submit, cancel]),
    note,
  ]);
  return el("div", { class: "overlay", onclick: (e) => { if (e.target.classList.contains("overlay")) { state.newRunOpen = false; render(); } } }, panel);
}

// ---------------------------------------------------------------------
// Root render
// ---------------------------------------------------------------------
function renderTabContent() {
  if (!state.snapshot.attached) {
    return el("div", { class: "empty-state" }, "No run attached. Pick one from the sidebar, or start a new one.");
  }
  switch (state.tab) {
    case "overview": return renderOverview();
    case "tree": return renderTree();
    case "approvals": return renderApprovals();
    case "contract": return renderContract();
    case "spec": return renderSpec();
    case "spine": return renderSpine();
    case "assembly": return renderAssembly();
    case "events": return renderEvents();
    default: return null;
  }
}

function render() {
  root.innerHTML = "";
  root.appendChild(renderHeader());
  const layout = el("div", { class: "layout" }, [renderSidebar(), el("div", { class: "main" }, [renderTabs(), el("div", { class: "tab-content" }, renderTabContent())])]);
  root.appendChild(layout);
  const nodePanel = renderNodePanel();
  if (nodePanel) root.appendChild(nodePanel);
  const newRunPanel = renderNewRunModal();
  if (newRunPanel) root.appendChild(newRunPanel);
  if (state.toast) {
    root.appendChild(el("div", { class: "toast" + (state.toast.isError ? " err" : "") }, state.toast.message));
  }
}

render();
startLive();
