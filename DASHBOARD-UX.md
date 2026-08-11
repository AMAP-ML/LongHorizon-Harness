# DASHBOARD-UX.md — control surface design

Design spec for the Kusudaemon web dashboard (`src/kusudaemon/dashboard/`).
Dark, dense, monospace. **Every capability reachable in ≤2 keystrokes or 1
click.** Text is the last resort, not the first — but nothing is ever hidden
behind a state you can't see.

This is a design document, not an implementation plan. It describes the
target; §11 lists what does not exist yet behind it.

---

## §1 What the operator is actually doing

The dashboard is not a viewer. A run lasts hours, is unattended most of that
time, and `wait_for_resolution(timeout=None)` **waits forever** — so the
single most expensive failure mode in this whole system is *the operator not
noticing the run is blocked on them.* Everything below is ordered by that.

Four jobs, in priority order:

| # | Job | Answered in |
|---|---|---|
| 1 | Is it moving, stuck, or waiting on me? | Rail (§3) — always visible, zero clicks |
| 2 | Answer the thing it's waiting on | Takeover (§6) — steals the inspector, keys `1`–`4` |
| 3 | Steer it mid-flight | Command bar + palette (§7) — one keystroke from anywhere |
| 4 | Read what happened | Inspector (§5) — tabs, never a modal |

Job 2 deserves emphasis: per CLAUDE.md §4.4, the pilot approval diff is
"the highest-signal input in the system." Today answering it means leaving
the browser, editing a file on disk, and running `approve`. §6.2 fixes that.

---

## §2 Layout

Five regions. Rail and command bar are fixed; the three columns scroll
independently. No floating overlays except the command palette and the
keymap — a detail view that covers the run is a detail view you close before
you're done reading.

```
┌─ RAIL ── 34px, fixed ─────────────────────────────────────────────────────────┐
│ ⬢ run-2026-08-11-a4f  T2↑T3  ▶execute  ▓▓▓▓▓▓▓░░ 23/31  4m12s  ●2  ⚠1  ⏸  ⌘K │
├─ NAV ────┬─ STREAM ────────────────────────┬─ INSPECTOR ───────────────────────┤
│ 220px    │ flex, min 440px                 │ 480px, drag-resize 380–840        │
│ fixed    │                                 │                                   │
│          │  chronological, oldest → newest │  ⌗tree  ⬡node  ⧉doc  ⊞asm  ⌸term │
│ ▸ runs   │  events · thinking · tool calls │  ─────────────────────────────────│
│ ▸ tree   │  · diffs · resolved approvals   │                                   │
│ ▸ agents │                                 │  (default: tree)                  │
│ ▸ phases │                                 │                                   │
│          │  ── pinned bottom ──            │                                   │
│          │  ⏸ PENDING APPROVAL             │                                   │
├──────────┴─────────────────────────────────┴───────────────────────────────────┤
│ ▌ →node-03.02 ▾ │ message or >command                                    ⏎     │
└─ COMMAND BAR ── 38px, fixed ───────────────────────────────────────────────────┘
```

**Why the inspector is a column and not a drawer.** You interject into a node
*while reading its artifact*. You reopen a node *while reading the verdict
that made you want to*. A modal forces you to memorize one thing to act on
another. Everything in the inspector coexists with the stream and the
command bar.

**Below 1200px** the inspector slides over the stream as a panel with a
scrim, rather than the three columns compressing. Columns never shrink into
each other — that is the overlap rule (§9) applied at the layout level.

---

## §3 The rail — one row, no labels

Left to right, every element is a glyph or a number. Full words appear only
on hover.

| Element | Renders | Click | Source |
|---|---|---|---|
| Run chip | `⬢ run-id` | run switcher dropdown | `snap.run_id`, `snap.runs` |
| Tier | `T2` or `T2↑T3` | escalation trail popover | `tier`, `measured_tier`, `escalation_history` |
| Phase | `▶execute` | jump to phases in nav | `phase`, `phase_status` |
| Progress | `▓▓▓▓▓▓▓░░ 23/31` | jump to tree | `tree_counts` |
| Elapsed | `4m12s` | — | first event ts → `server_time` |
| Live agents | `●2` | nav → agents, filtered live | `subagents[].live` |
| Blocked | `⚠1` | jump to first blocked node | `tree_counts.blocked` |
| Halt | `⏸` / `▶` | toggle halt | `POST /api/halt` |
| Palette | `⌘K` | open palette | — |

**Progress bar is segmented by status, not a single fill:**
`green passed · purple split · cyan dispatched · amber awaiting_review/stale
· red blocked · dim pending`. One 9-character bar carries the entire tree
state. That is the density trade this design keeps making: a bar you can
read in 200ms instead of a table you read in 20s.

**Exactly three things may animate**, and nothing else, ever:

1. `▶` phase glyph — slow pulse while `phase_status == "in_progress"`
2. `●` live-agent dot — pulse while any subagent is live
3. `⏸ PENDING` badge — 1.2s pulse, **red**, until resolved

**☠ STALLED** (from `liveness.run_liveness`) replaces the phase glyph
entirely and turns the whole rail's bottom border red. A stalled run and a
run mid-provider-call must never look alike — that's the exact bug §D0c was
filed for, and the rail is where it gets fixed visually.

**Multi-run.** Other hosted runs appear as bare chips right of the run chip:
`⬢a4f ⬡9c2 ⬡7e1  3/4`. `⬢` attached, `⬡` hosted-not-attached, a red chip if
that run has a pending approval. At the `--max-concurrent-runs` cap the
counter turns amber and "new run" disables with the 429 reason on hover.

---

## §4 Nav — 220px, navigation only

Four sections, always all four, no tabs — tabs hide state, and one of these
sections is where a pending approval lives. Collapsible headers, counts on
the right.

```
RUNS                 4
 ⬢ run-…a4f    ▶ T2  ← attached
 ⬡ run-…9c2    ⏸ T3  ← red dot: needs you
 · run-…7e1    ✓ T1
 · run-…22b    ✕ T2
                  + new
TREE            23/31
 (mirrors inspector tree, collapsed to
  glyph + last id segment)
AGENTS             7
 ● node-03.02   1.2s
 ◐ node-04      12s
 ✓ node-02      4.1s
 ✕ node-01~r1
PHASES          5/7
 ✓ classify  ✓ intake  ✓ explore
 ✓ plan  ▶ execute  · review  · assemble
```

Nav rows are 22px, one line, hard-truncated left-side (`run-…a4f`) so the
*distinguishing* end of an id stays visible. Ids in this system share long
prefixes; truncating the right end would make every row read identically.

Phases render as a compact wrap of glyph+name chips, not a list — the phase
list is tier-dependent (`phases_for(tier)`), so it grows mid-run when a tier
escalates, and a wrapping chip row absorbs that without relayout.

---

## §5 Inspector — five tabs, tree is home

Tab strip is glyph+word, 26px, no icons-only (five ambiguous glyphs is worse
than five short words). `[` and `]` cycle.

### 5.1 ⌗ Tree — the default resting view

**One line per node, fixed columns, no cards.** Cards waste ~4× the vertical
space per node and stop you from scanning a column. At 31 nodes a card list
is three screens; this is one.

```
     ID                          S  SH  GATES     A  TOK   ART
 ▾ node-03                       ⑂  pr  ▪▪▪▪▪     1  8.2k   3   ●
   ├ node-03.01                  ●  pr  ▪▪▪▪▪     1  3.1k   1
   ├ node-03.02                  ◐  pr  ▪▪▫▫▫     2  2.8k   0   ●
   └ node-03.03                  ·  pr  ▫▫▫▫▫     0  2.4k   0
 ▸ node-04                       ◑  de  ▪▪▪▪▫     1  6.0k   1
   node-05                       ⊘  ps  ▪▫▪▪▪     3  5.5k   2
```

Column contract — this is what guarantees no overlap:

| Col | Width | Content | Overflow |
|---|---|---|---|
| ID | flex, min 160px | indented dot-path leaf segment | CSS ellipsis + `title` |
| S | 16px | status glyph (§8) | never overflows |
| SH | 24px | shape, 2 chars (`pr de ps re`) | fixed |
| GATES | 60px | one pip per gate, ▪ pass ▫ fail/unrun | caps at 8, `+n` after |
| A | 20px | attempts, amber ≥2, red at max | fixed |
| TOK | 48px | artifact tokens, `tabular-nums`, right | fixed |
| ART | 28px | artifact count (`_artifact_count`) | fixed |
| ● | 12px | live subagent attached | fixed |

The **gate pip strip is the whole point**: five squares tell you *which* gate
failed without opening anything, and gates never enter model context anyway
(§7 of CLAUDE.md), so the dashboard is the only place they're legible at all.
Hover a pip → gate spec + detail.

Interactions:

- click row → Node tab, `openNode(id)`
- click `●` → Node tab, Chat sub-tab, directly on that live subagent
- `j`/`k` move, `Enter` open, `Space` expand/collapse
- `/` filters (matches id, brief, shape, status)
- right-click → context menu: reopen · redispatch · artifact · versions · copy id
- **folder rows** (an intermediate dot-path segment that isn't a dispatched
  node) render dim, unclickable, no badges — same rule as today

**Split parents** (`status: "split"`) render `⑂` and their gate pips are
replaced with `─────`: they have no artifact of their own until every child
passes, at which point the derived concatenation appears and pips return.

### 5.2 ⬡ Node — six sub-tabs

Header line is dense and label-free:

```
node-03.02  ◐ dispatched  ·  attempt 2/3  ·  prose  ·  8.2k/24k tok  ·  ●live
[ interject ] [ reopen ] [ redispatch ] [ open artifact ] [ versions ▾ ]
```

| Sub-tab | Shows | Route |
|---|---|---|
| **Chat** | thinking · tool calls · diffs, as chat entries | `/thinking`, `/trace` |
| **Overview** | brief, judgment items, rubric, inputs (path·tokens·exists), depends_on, budget, promotion | `/api/node/<id>` |
| **Gates** | per-gate pass/detail table + reviewer verdict items with `class` and `node_ids`, + `truncated` flag | `audit/<id>.json` via node detail |
| **Artifact** | rendered text | `/artifact` |
| **Versions** | `out/.versions/<id>/` list, newest first | `/version/<tag>` |
| **Diff** | version ↔ current | `/diff/<tag>` |

Two things this design insists on that the current one blurs:

- **Node status and subagent status are separate columns, never merged.**
  `node.status` is tree-gate state; a subagent's status is episode state.
  A node can be `passed` while a repair subagent under `node~repair1` is
  live. Show both, side by side, always.
- **`verdict.truncated`** gets a visible amber `⚠ truncated` chip on the
  Gates tab. A verdict reached over a cut artifact is a weaker verdict and
  the operator is the only one who can act on that.

### 5.3 ⧉ Doc — the frozen texts

`spec.md` · `contract.md` · `spine.json` · `manifest.jsonl`, as a segmented
control, monospace, read-only. Contract gets a token-count bar against its
ceiling (`ContractCeilingExceeded` is a hard failure — show the headroom
*before* an amendment hits it). `[ amend ]` sits on the contract view, not in
a global menu, because amendment is a contract operation and its blast
radius is the whole run.

### 5.4 ⊞ Assembly

`checks.json` as a pass/fail list (each check one line, glyph + name +
offending node ids as clickable chips), `compile.log` tail, `index.md`,
`main.md`. Failed check → click a node id → straight to §5.2. This is the
only screen where a *cross-node* defect is actionable, so attribution chips
must be links, not text.

### 5.5 ⌸ Terminal

Scrolling raw `events.jsonl` tail, filterable by type, and a copyable
equivalent CLI command for whatever you last did in the UI
(`kusudaemon amend <run-id> --file …`). Escape hatch and teaching device
in one — nobody reads raw JSONL by choice, but you want it when the UI is
wrong.

---

## §6 Approvals — the takeover

An approval is not a feed item. It is the run's critical path.

### 6.1 Standard approval

When `pending_approvals` is non-empty:

1. Rail badge `⏸` goes red and pulses.
2. The inspector **switches to the approval and locks the tab strip dim**
   (still clickable — you may need to read an artifact to decide — but it
   snaps back on any keypress that isn't navigation).
3. The stream pins a compact `⏸` marker at the bottom, below the resume
   banner, so a scrolled-to-bottom operator cannot miss it.
4. Browser tab title becomes `⏸ kusudaemon`, and favicon flips red.

```
⏸  APPROVAL · amend                                        2m ago
────────────────────────────────────────────────────────────────
context
  35 nodes · ~412k tok estimated
  "cut every historical aside"

[1] Apply amendment          [2] Cancel
                                    ⌫ notes ▸
```

Options come from `Approval.options` and are bound to number keys in order.
`Enter` picks the `style: "primary"` one. Free-text answers live in
`state.approvalDrafts` keyed by id (the render-teardown rule — §9.4).

### 6.2 Pilot approval — the editor (new capability)

The pilot diff is the highest-signal input in the system and currently
requires leaving the browser. It gets a purpose-built view:

```
⏸  PILOT · node-07 · derivation-dominant
┌── original (frozen) ──────────┬── your edit ────────────────────┐
│ ## Worked Examples            │ ## Worked Examples              │
│                               │                                 │
│ Historically, Gauss first…    │ ⌫⌫⌫ (deleted)                   │
│                               │                                 │
│ Let x = …                     │ Let x = …                       │
└───────────────────────────────┴─────────────────────────────────┘
  −412 / +38 tokens              [ save & approve ]  [ approve as-is ]
```

Left pane read-only, right pane an editable textarea seeded with the current
file, live diff gutter between them. `save & approve` writes the edited text
back to disk and resolves the approval in one action — exactly what
`approve_pilot` already expects, just without the round trip through an
editor. `approve as-is` is the zero-model-call path (an unedited pilot spends
nothing on contract derivation) and is labeled to make that cost visible.

### 6.3 Intake — the batched question form

§B3 intake is now **one approval per round carrying up to 4 questions**. It
renders as a form, all questions at once, each with its `default_assumption`
as ghost placeholder text — so leaving a field blank visibly means "accept
this assumption," not "I skipped it."

Objections render above the questions in amber with their `{claim, why,
options[]}` structure intact. An objection is the model pushing back; it
should look different from a question, because you answer them differently.

### 6.4 Amend triage

The re-validation triage approval renders as three stacked count chips —
`clean 18 · patchable 9 · regenerate 4` — each expanding to the node list,
each node clickable through to §5.2 **before** you approve. Approving a
regenerate-heavy triage without seeing which nodes is how you lose four
chapters; the cost estimate alone is not enough information.

---

## §7 Command bar and palette — "control everything quickly"

### 7.1 The bar (always present, bottom)

Two modes in one input, switched by the first character:

- **default → interject.** Target selector on the left auto-follows the live
  subagent (`targetAgentManual` overrides). Send is **disabled with a
  reason** when the target has no live session — never let the operator fire
  a request that can only 409.
- **`>` → command.** Same input, fuzzy-matched against every action.

### 7.2 Palette (`⌘K`)

Fuzzy list over the *complete* action inventory, each row `glyph · name ·
keybinding · scope`. Node-scoped commands (`reopen`, `redispatch`,
`interject`) prefill with the currently selected node. This is the promise
that "everything is controllable quickly" is kept literally: if it exists as
an API route, it is in the palette.

### 7.3 Keymap

Vim-ish, single-key where unambiguous, no modifiers except the palette.
`?` shows the map — the only screen in this design that is mostly text.

| Key | Action |
|---|---|
| `⌘K` `Ctrl+K` | palette |
| `g` `r`/`t`/`a`/`p` | nav to runs / tree / agents / phases |
| `j` `k` | move in the focused list |
| `Enter` | open selected |
| `Space` | expand/collapse tree branch |
| `/` | filter tree |
| `[` `]` | cycle inspector tabs |
| `1`–`4` | answer pending approval option |
| `a` | jump to pending approval |
| `i` | interject into selected node |
| `r` | reopen selected node |
| `d` | redispatch selected node |
| `m` | amend contract |
| `e` | escalate tier |
| `h` | toggle halt |
| `x` | resume run |
| `n` | new run |
| `Esc` | back to tree / close palette |
| `?` | keymap |

---

## §8 Status vocabulary — learn once, applies everywhere

One glyph per state, same glyph in the rail, nav, tree, and agent list. A
status that renders as a word in one place and a glyph in another has to be
learned twice.

**Node status** — all eight of `NodeStatus`, plus one derived state

| Glyph | Status | Color | |
|---|---|---|---|
| `·` | pending | dim | |
| `○` | *ready* | cyan | derived (`is_ready()`: pending + deps passed), not stored |
| `◐` | dispatched | cyan, pulsing | |
| `◑` | awaiting_review | amber | |
| `●` | passed | green | |
| `✕` | failed | red | non-terminal-ish: retried up to `max_attempts` |
| `⊘` | blocked | red, filled | attempts exhausted — the one that escalates |
| `◌` | stale | amber, hollow | |
| `⑂` | split | purple | |

`failed` and `blocked` must not share a glyph. `failed` means "it'll try
again"; `blocked` means "it stopped and is waiting for you." Collapsing them
is how you sit watching a dead run.

**Phase** `▶` in progress · `✓` done · `✕` failed · `⏸` awaiting approval ·
`·` not started · `☠` stalled

**Subagent** (`_summarize_subagent`) `·` pending · `◐` running · `✓` done ·
`✕` error · `⏱` timeout, with a separate `●` live dot (`live` is
`has_logdir && !completed` — orthogonal to `status`, so it is its own column,
never merged into the glyph) · `◇` explorer, the non-interactive pseudo-agent
(`explore-01`), which must never show a bare RUNNING badge next to "not
currently running"

**Shape** two chars, from `_SHAPES`: `pr` prose · `de` derivation ·
`ps` problem-set · `re` reference.

**Tier** plain `T0`–`T3`; `↑` between measured and effective when overridden
or escalated. Escalation trail on hover: `T1 → T2 (size_defect_retry,
node-04)`.

Color is never the only signal — every state has a distinct glyph shape too.

---

## §9 Density rules — how "lots of information" stays readable

The user's constraint is literal: throw information at them, but no text may
overlap. That is a typographic contract, enforced by construction:

1. **One grid.** Base 13px `Fira Code`, line-height 22px, all row heights a
   multiple of 22. Nothing is vertically centered by eye.
2. **Numbers are `font-variant-numeric: tabular-nums` and right-aligned.**
   Columns of proportional digits shimmer and misread.
3. **Every flexible text cell is `min-width: 0; overflow: hidden;
   text-overflow: ellipsis; white-space: nowrap` with a `title`.** Truncation
   is the *only* permitted response to overflow. Never wrap inside a table
   row; never shrink a fixed column below its declared width.
4. **No text is absolutely positioned over other text.** Badges are inline
   flex children with `flex-shrink: 0`. Tooltips are the single exception and
   they are transient, dismissible, and offset from the cursor.
5. **Prose only in four places:** brief, contract/spec body, artifact body,
   keymap. Those get `--font-sans`, 14px/1.6, and a 78ch measure. Everywhere
   else is mono and one line.
6. **Contrast floor:** dim text is `--text-dim` on `--bg-primary` only, never
   on `--bg-card`, and never below 14px.
7. **Two nesting depths of border, maximum.** The existing palette's glass
   and double-bezel treatment is decoration; at this density it costs
   vertical rhythm. Keep the color variables, drop the multi-layer shadows on
   anything that repeats per row.

**Density math, 1440px:** nav 220 + stream ~700 + inspector 480. Tree row at
480px: ID 160 (min) + 16 + 24 + 60 + 20 + 48 + 28 + 12 = 368px of content,
8 gaps × 8px = 64, total 432 < 480. Headroom goes to ID. At 380px (min
inspector) ID falls to its 160px floor and the row still fits at 432px —
the inspector cannot be dragged narrower than the row, which is why 380 is
the floor and not a round number.

**Vertical:** 34 rail + 38 bar = 72px chrome. At 900px viewport that leaves
828px ≈ 37 tree rows visible without scrolling. A 31-node plan fits on one
screen. That is the design target.

### 9.4 The render-teardown constraint (carried forward)

`render()` has no diffing; every tick rebuilds `#app`. Consequences that this
design must honor and that any new component inherits:

- **Every free-text value lives in `state`**, never only in the DOM. New
  fields this design adds: `paletteQuery`, `treeFilter`, `pilotEdit[nodeId]`,
  `intakeAnswers[approvalId][questionId]`, `inspectorWidth`.
- Focus and selection are restored after every render.
- Every independently-scrolling region carries `data-scroll-key`; scroll
  positions are captured and restored per key. `scroll-behavior: smooth` is
  forbidden on any restored region — the correction plays as a visible jump.
- `snapshotFingerprint()` strips `server_time` before comparing.

---

## §10 States that aren't the happy path

| State | Design |
|---|---|
| No run attached | Nav shows runs only; stream shows a single centered `+ new run`; inspector empty. No dashboard chrome pretending to have data. |
| Run created, nothing yet | Phase `·`, progress bar empty-dim, stream shows the goal as the first entry. |
| Stalled | Rail bottom border red, `☠` replaces phase glyph, banner in stream with `stalled_reason` and a `resume` button. |
| Halted | Rail `⏸` filled, whole rail desaturates, `▶ resume` in the bar. |
| Blocked node(s) | Rail `⚠n` red; clicking jumps to the first blocked node's Gates tab, not the tree — you want the reason, not the row. |
| Phase failed | Feed entry styled red **at its own timestamp** (never re-pinned to current state), with the traceback collapsed. |
| Escalation fired | Inline feed marker `T2 → T3 · split_accepted · node-04`, and the rail tier chip flashes once. |
| Auth required | Full-screen token prompt before any `/api` call; on success the cookie is planted and the SSE stream authenticates itself. |
| Concurrency cap | New-run disabled, counter amber, 429 payload (`hosted`, `max_concurrent_runs`) shown on hover. |
| SSE dropped | Rail gains a small `⟳` and falls back to 2s polling; no modal, no toast. |
| Empty artifact | Artifact tab shows `∅ empty` explicitly — an empty artifact is a real, diagnostic state (§D0), not a rendering failure. |

---

## §11 Control inventory — and what's missing

Everything the operator can do, where it lives, and whether a route exists.

| Control | UI location | Key | Route | Status |
|---|---|---|---|---|
| Attach run | nav → runs | `g r` | `POST /api/attach` | ✅ |
| New run | nav → runs `+` | `n` | `POST /api/runs` | ✅ |
| Resume run | rail / stalled banner | `x` | `POST /api/runs` w/ existing id | ✅ |
| Delete run | run row context menu | — | `DELETE /api/runs/<id>` | ✅ |
| Halt / unhalt | rail | `h` | `POST /api/halt` | ✅ |
| Resolve approval | takeover | `1`–`4` | `POST /api/approvals/<id>/resolve` | ✅ |
| Amend contract | Doc → contract | `m` | `POST /api/amend` | ✅ |
| Reopen node | node header / tree menu | `r` | `POST /api/reopen` | ✅ |
| Interject | command bar | `i` | `POST /api/node/<id>/interject` | ✅ |
| Read artifact / versions / diff | Node tab | — | `/artifact`, `/version/<tag>`, `/diff/<tag>` | ✅ |
| Read trace / thinking | Node → Chat | — | `/trace`, `/thinking` | ✅ |
| Read spec / contract / spine / manifest | Doc tab | — | `/api/spec` etc. | ✅ |
| Read assembly + checks + compile log | Assembly tab | — | `/api/assembly` | ✅ |
| **Escalate tier** | rail tier chip / palette | `e` | `POST /api/escalate` | ✅ 2026-08-11 — confirm in-browser; `escalate_run`'s own read-modify-write, 409 without tier.json |
| **Tier floor on new run** | new-run form | — | `tier_override` | ✅ 2026-08-11 — validated T0–T3 or blank; a floor, never a ceiling |
| **Workspace mode** | new-run form | — | `workspace` | ✅ 2026-08-11 — `measure_workspace` at launch; bad path 400s with the message |
| **Run options** (`document_review`, `survey_mode`, `inline_spans`, `dispatch_policy`, `auto_probe_plan`, `max_rounds`) | new-run form | — | — | ✅ 2026-08-11 — `_options_from_body` is now the full `RunOptions` surface |
| **Pilot edit + approve in-browser** | §6.2 | — | `POST /api/approvals/<id>/pilot-save` | ✅ 2026-08-11 — writes the artifact, resolves with the edit as `user_input`; `approve as-is` stays the blank-input zero-call path |
| **Redispatch a single node** | node header | `d` | `POST /api/node/<id>/redispatch` | ✅ 2026-08-11 — confirm approval; apply resets failed/blocked/stale to `pending`, attempts 0 |
| **View a split proposal** | Node tab on `⑂` | — | `GET /api/node/<id>/split` | ✅ 2026-08-11 — proposal + per-child status from `snap.tree`'s `parent` rows |
| **Cancel a running job** | jobs strip | — | `POST /api/jobs/<id>/cancel` | ✅ 2026-08-11 — `jobs.jsonl` record is the authority; thread honours the event after its provider call lands |
| **Intake answers in one approval** | §6.3 | — | `answers` passthrough on resolve | ✅ 2026-08-11 — per-question inputs, one Submit; driver reads them off the resolved record |
| **Hosted-runs counter** | rail | — | `max_concurrent_runs` on snapshot | ✅ 2026-08-11 — `hosted n/max` chip; the §C4 cap known only at `make_server` time |

All of §11's gaps are closed. The keyboard affordances (`e`/`d`/`n`…) and the
palette remain unbuilt — that is the command-bar workstream, still §12-adjacent
and unstarted.

---

## §13 Shipped 2026-08-11 — command bar, palette, keys, and the new grid

The command-bar + palette workstream landed this session, on top of §11's
already-closed control surface. This section records what exists now and
where the implementation deliberately differs from the spec above. It is
written after the fact, so it states facts, not intentions.

**New chrome (top to bottom):** rail (34px) → run header row → three-pane
workspace (nav / stream / inspector) → command bar. The design doc's §2
diagram holds structurally; the rail itself does not (§3's 9-element rail
was re-scoped — see deviations below).

- **Command bar** (§7.1): always present. Left: message-target select
  (auto-follows the live subagent until the operator picks one — the old
  prompt-bar dropdown bug, gone by construction), then four mode chips:
  `💬 A` message (default), `> ⌥` command, `✏️ m` amend, `🔁 r` reopen.
  Text starting with `>` switches to command mode automatically. Reopen
  mode target is the inspector's selected node.
- **Palette** (`⌘K` / `ctrl+K`, §7.2): fuzzy filter over the command list,
  ↑/↓ + Enter to run, Esc to close. Commands: `resume`, `tree`, `doc`,
  `asm`, `term`, `new`, `runs`, `escalate`, `help`, `amend`, `reopen`,
  `interject`, `redispatch`.
- **Keymap** (⌘K when palette closed, or `?`): groups Global / Focus move /
  Run — `g r` reopen selected node, `g t` task tree, `g p` cycle doc tabs,
  `g a` resolve the top pending approval (first option), `esc` closes
  palette/menu/takeover/prompt-mode, `ctrl+]`/`ctrl+[` cycle inspector
  tabs, `j/k` move in the task tree, `Enter` opens the focused row
  (folders expand/collapse).
- **Task tree is the inspector's default home** (§5.1): dot-hierarchical
  grouping, per-row status glyph + shape tag + gate pips (from
  `gate_results`) + token count + versions count, `● live` subagent pill
  opens the node's Chat, right-click context menu (node overview / reopen /
  redispatch / copy id; runs: attach / delete). Keyboard seams (`data-key`)
  on filter input preserve focus under the full-teardown render.
- **Pilot editor** (§6.2): a pending pilot approval takes the inspector
  over — frozen original (left) vs editable textarea (right), `Save &
  approve edit` (POST `/api/approvals/<id>/pilot-save`, resolved with the
  edit as `user_input`) and `Approve as-is` (blank-input zero-call path).
  The same editor renders in the Node tab's Overview whenever the node has
  a pending pilot approval with a `pilot_original` snapshot. Legacy
  approvals without `context.node_id` fall back to the plain approval card.
- **Intake questions + objections** (§6.3/§6.4): one approval per round
  with one input per question (`default_assumption` as placeholder),
  `Submit Answers` resolves once with the `answers` map; objections render
  as amber `{claim, why, options[]}` blocks. Amend triage renders as three
  expandable count chips, each expanding to its node list, each node clickable.
- **Jobs strip** (§8.4): running/queued jobs from `snapshot.jobs` render as
  a strip above the toast with per-job cancel (`POST /api/jobs/<id>/cancel`).
- **Run switcher**: clicking the run id in the header row opens the runs
  sheet (newest first, ✅ attached, ⏸ pending count, phase glyph, goal
  ellipsized). Right-click a nav run row for attach/delete.
- **New-run modal** = the full `RunOptions` surface (`workspace`, tier
  floor, dispatch policy, survey mode, max rounds/attempts, document
  review, inline spans), matching §11's rows.
- **Auth overlay**: on any 401 the whole UI is covered by a token prompt;
  validating plants the cookie via the Bearer handshake (§C4) and the SSE
  stream then authenticates.
- **Polling fallback parity**: `applySnapshot` defaults
  `control_enabled`/`max_concurrent_runs` for the non-SSE snapshot path
  (the header row's hosted counter and escalator button work without SSE).

**Deliberate deviations from the spec above** (scope cuts, all documented
here so nobody re-reads the spec and "fixes" them):

1. **The rail is a phase strip, not §3's 9-element rail.** Rail-left is one
   segmented bar per phase the driver has touched, in run order
   (DONE/RUN/WAIT/ESC/HLT/STALL/FAIL via `PHASE_GLYPH`); rail-right is
   hosted counter, live/polling indicator, elapsed. Run chip → header-row
   run id (click = switcher), tier/escalation badges → `hdr-tier-badges`,
   halt/resume/escalate buttons → header-row buttons. The 9-element rail's
   per-element density is still the design target; this pass bet on the
   header row because it is where the operator's eyes already are when a
   tier escalates or an approval lands.
2. **No drag-resize on the inspector** (fixed column; CSS flex keeps it
   ≥480px). The `380–840` drag handle is future work.
3. **No ⌘1..9 workbench-tab keys and no single-letter `e/d/n/x/h/i`
   shortcuts.** The g-prefix set (`g r/t/p/a`) plus the palette cover the
   same jobs; the keymap documents exactly what exists.
4. **Standard/intake approvals do not steal the inspector** — §6.1's
   takeover visual is implemented for the pilot only; standard and intake
   approvals stay pinned at the stream's bottom (plus working `1..9`/Enter
   quick-resolve keys while a takeover is armed).
5. **Nav has three sections** (runs/subagents/phases), tree lives in the
   inspector per §5.1. No collapsible tree section in nav.
6. **No segmented progress bar** (§3's `▓▓▓░░ 23/31`): density moved into
   per-phase rail segments and the tree tab's count line.

**Verification:** `node --check` on `app.js` is the syntax gate
(§9.4's no-build-step rule); the dashboard JS assertions run in
`test_dashboard_server.py`. Full suite: 683 tests, all passing.

---

## §12 Non-goals

- **No graph/DAG visualization.** `depends_on` is empty on every planner leaf
  today (§4.3 freezes the contract precisely so leaves are independent). A
  force-directed graph of 31 unconnected nodes is decoration.
- **No charts.** Token counts and call counts are numbers; a sparkline of
  them is bigger and less precise. §C5's eval harness is where measurement
  belongs, not here.
- **No mobile layout.** Minimum useful width is 1200px.
- **No inline artifact editing** except the pilot (§6.2). CLAUDE.md §4.6's
  read-only-assembler discipline exists because "helpfully" editing content
  to make something green is how you ship a passing compile over corrupted
  content. The dashboard obeys the same rule the assembler does; a repair
  goes through review.
- **No build step.** Vanilla JS, no framework, no bundler. The dashboard
  crashing must never touch the run, and a zero-dependency view surface is
  how that stays true.
