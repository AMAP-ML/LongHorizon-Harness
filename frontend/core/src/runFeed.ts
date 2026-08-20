import type { EventEnvelope, OperatorMessage, Snapshot } from './types';
import { dedupeEvents, eventKey } from './events';

/** Connection state exposed by the run feed to Web consumers. */
export type FeedConnectionStatus =
  | 'idle'
  | 'loading'
  | 'connected'
  | 'reconnecting'
  | 'closed'
  | 'error';

export interface RunFeedState {
  /** The run this feed belongs to. A null id means no run is selected. */
  runId: string | null;
  snapshot: Snapshot | null;
  events: EventEnvelope[];
  connection: FeedConnectionStatus;
  lastEventId: string | null;
  maxEvents: number;
}

export type RunFeedAction =
  | { type: 'reset'; runId: string | null }
  | { type: 'seed'; snapshot: Snapshot }
  | { type: 'replay'; events: EventEnvelope[] }
  | { type: 'snapshot'; snapshot: Snapshot }
  | { type: 'event'; event: EventEnvelope }
  | { type: 'operator_message'; runId: string; message: OperatorMessage }
  | { type: 'approval_response'; runId: string; approvalId: string; action: string; userInput: string; resolvedAt: number }
  | { type: 'connection'; runId?: string | null; status: FeedConnectionStatus };

export const DEFAULT_RUN_FEED_MAX_EVENTS = 400;

function normalizedLimit(maxEvents: number | undefined): number {
  if (!Number.isFinite(maxEvents)) return DEFAULT_RUN_FEED_MAX_EVENTS;
  return Math.max(1, Math.floor(maxEvents as number));
}

function boundedEvents(events: EventEnvelope[], maxEvents: number): EventEnvelope[] {
  return dedupeEvents(events).slice(-maxEvents);
}

function eventsForRun(events: EventEnvelope[], runId: string): EventEnvelope[] {
  return events.filter((event) => event.run_id === runId);
}

function cursorFor(events: EventEnvelope[], fallback: string | null = null): string | null {
  return events.at(-1)?.event_id || fallback || null;
}

function snapshotWithEvents(snapshot: Snapshot, events: EventEnvelope[], cursor: string | null): Snapshot {
  return {
    ...snapshot,
    events,
    diagnostics: {
      ...snapshot.diagnostics,
      last_event_id: cursor || snapshot.diagnostics.last_event_id || null,
      event_count: events.length,
    },
  };
}

function sameRun(runId: string | null, snapshot: Snapshot): boolean {
  return runId !== null && snapshot.run.id === runId;
}

function terminal(status: string): boolean {
  return ['completed', 'complete', 'success', 'succeeded', 'done', 'finished', 'failed', 'failure', 'blocked', 'incomplete', 'cancelled', 'canceled', 'stopped', 'aborted'].includes(String(status || '').trim().toLowerCase());
}

/** Resume generation of a snapshot; 0 for a run that was never resumed. */
function epochOf(snapshot: Snapshot): number {
  const value = snapshot.run?.resume_epoch;
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

function lifecycleRank(status: unknown): number {
  const normalized = String(status || '').trim().toLowerCase();
  if (terminal(normalized)) return 4;
  if (['stopping', 'aborting', 'stop_requested', 'abort_requested'].includes(normalized)) return 3;
  // `waiting_approval` and `running` are the same phase seen at two moments: a
  // run toggles between them every gate. Ranking the gate higher made the
  // return to `running` look stale, freezing the UI on the answered approval
  // until a reload.
  if (['running', 'active', 'executing', 'waiting_approval', 'waiting', 'blocked_waiting'].includes(normalized)) return 2;
  if (['starting', 'creating'].includes(normalized)) return 1;
  return 0;
}

function snapshotEventTime(snapshot: Snapshot): number {
  return snapshot.events.reduce((latest, event) => Math.max(latest, event.ts), 0);
}

function snapshotCursor(snapshot: Snapshot): string | null {
  return snapshot.diagnostics?.last_event_id || snapshot.events.at(-1)?.event_id || null;
}

function meaningful(value: unknown): boolean {
  if (typeof value === 'string') return value.trim().length > 0;
  return value !== null && value !== undefined;
}

function mergeRecordPreserving(current: Record<string, unknown> | undefined, incoming: Record<string, unknown> | undefined): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...(current || {}), ...(incoming || {}) };
  for (const [key, value] of Object.entries(current || {})) {
    if (!meaningful((incoming || {})[key]) && meaningful(value)) merged[key] = value;
  }
  return merged;
}

function mergeStatusObject(current: unknown, incoming: unknown): unknown {
  if (!current || typeof current !== 'object') return incoming;
  if (!incoming || typeof incoming !== 'object') return current;
  const left = current as Record<string, unknown>;
  const right = incoming as Record<string, unknown>;
  const leftStatus = String(left.status || '').trim().toLowerCase();
  const rightStatus = String(right.status || '').trim().toLowerCase();
  const terminalStatus = (status: string) => ['completed', 'complete', 'success', 'succeeded', 'done', 'finished', 'failed', 'failure', 'blocked', 'incomplete', 'cancelled', 'canceled', 'stopped', 'aborted'].includes(status);
  // A durable terminal role result must not be replaced by a late REST
  // placeholder that only carries `running` or no status at all.
  if (terminalStatus(leftStatus) && !terminalStatus(rightStatus)) {
    return { ...right, ...left };
  }
  const merged: Record<string, unknown> = { ...left, ...right };
  for (const [key, value] of Object.entries(left)) {
    if (!meaningful(right[key]) && meaningful(value)) merged[key] = value;
  }
  return merged;
}

function mergeRound(current: Snapshot['rounds'][number] | undefined, incoming: Snapshot['rounds'][number]): Snapshot['rounds'][number] {
  if (!current) return incoming;
  const merged = { ...current, ...incoming };
  for (const key of ['next_step', 'plan_text', 'task_state', 'task_contract', 'executor_output', 'auditor_report', 'harness_feedback', 'final_response', 'active_role'] as const) {
    if (!meaningful(incoming[key]) && meaningful(current[key])) (merged as Record<string, unknown>)[key] = current[key];
  }
  if (current.in_progress === false && incoming.in_progress !== false) merged.in_progress = false;
  merged.roles = Array.from(new Set([...(current.roles || []), ...(incoming.roles || [])]));
  merged.role_sizes = { ...(current.role_sizes || {}) };
  for (const [role, size] of Object.entries(incoming.role_sizes || {})) {
    const previous = merged.role_sizes[role];
    merged.role_sizes[role] = typeof previous === 'number' && typeof size === 'number' ? Math.max(previous, size) : size;
  }
  for (const key of ['manager_status', 'executor_status', 'auditor_status', 'final_response_status'] as const) {
    merged[key] = mergeStatusObject(current[key], incoming[key]) as typeof merged[typeof key];
  }
  return merged;
}

function mergeApprovals(current: Snapshot['approvals'], incoming: Snapshot['approvals']): Snapshot['approvals'] {
  const byId = new Map(current.map((item) => [item.approval_id, item]));
  for (const item of incoming) {
    const previous = byId.get(item.approval_id);
    if (!previous) {
      byId.set(item.approval_id, item);
      continue;
    }
    const merged = { ...previous, ...item };
    for (const key of ['title', 'message', 'action', 'reason', 'user_input', 'input_label'] as const) {
      if (!meaningful(item[key]) && meaningful(previous[key])) merged[key] = previous[key];
    }
    if (!meaningful(item.resolved_at) && meaningful(previous.resolved_at)) merged.resolved_at = previous.resolved_at;
    // A resolved record carries the operator's grant; a later pending frame
    // (or an older server that omits the field) must not erase it.  0 is the
    // "not chosen" sentinel here, so it counts as absent.
    if (!item.extra_rounds && previous.extra_rounds) merged.extra_rounds = previous.extra_rounds;
    if (item.allow_extra_rounds === undefined && previous.allow_extra_rounds !== undefined) {
      merged.allow_extra_rounds = previous.allow_extra_rounds;
    }
    if (previous.status === 'resolved' && item.status === 'pending') merged.status = previous.status;
    merged.options = item.options?.length ? item.options : previous.options;
    merged.answers = item.answers?.length ? item.answers : previous.answers;
    byId.set(item.approval_id, merged);
  }
  return [...byId.values()].sort((left, right) => left.created_at - right.created_at);
}

function mergeOperatorMessages(
  current: NonNullable<Snapshot['operator_messages']>,
  incoming: NonNullable<Snapshot['operator_messages']>,
): NonNullable<Snapshot['operator_messages']> {
  const byId = new Map(current.map((item) => [item.id, item]));
  const deliveryRank = (status: string) => status === 'queued' ? 0 : 1;
  for (const item of incoming) {
    const previous = byId.get(item.id);
    const merged = { ...previous, ...item };
    // Delivery receipts are independent of the role-event cursor. A stale
    // snapshot must not turn an applied/failed command back into `queued`.
    if (previous && deliveryRank(previous.status) > deliveryRank(item.status)) merged.status = previous.status;
    byId.set(item.id, merged);
  }
  return [...byId.values()].sort((left, right) => left.created_at - right.created_at);
}

function withDurableInteractions(preferred: Snapshot, other: Snapshot): Snapshot {
  return {
    ...preferred,
    approvals: mergeApprovals(other.approvals || [], preferred.approvals || []),
    operator_messages: mergeOperatorMessages(other.operator_messages || [], preferred.operator_messages || []),
  };
}

/** Merge same-cursor snapshots without allowing REST placeholders to erase
 * durable role output, completion state, or approval decisions. */
function mergeEqualCursorSnapshot(current: Snapshot, incoming: Snapshot): Snapshot {
  const run = { ...incoming.run };
  // A new generation supersedes the previous one outright: keeping its rank or
  // back-filling its outcome would leave a resumed run looking finished.
  const resumed = epochOf(incoming) > epochOf(current);
  if (!resumed && lifecycleRank(current.run.status) > lifecycleRank(incoming.run.status)) run.status = current.run.status;
  const carried = resumed
    ? (['agent', 'model', 'workspace', 'max_rounds', 'prompt_language'] as const)
    : (['started_at', 'finished_at', 'completion_satisfied', 'completion_authority', 'report_status', 'exit_code', 'failure_reason', 'final_response', 'agent', 'model', 'workspace', 'max_rounds', 'prompt_language'] as const);
  for (const key of carried) {
    if (!meaningful(incoming.run[key]) && meaningful(current.run[key])) (run as Record<string, unknown>)[key] = current.run[key];
  }
  const roundsByIndex = new Map(current.rounds.map((round) => [round.round_index, round]));
  const rounds = incoming.rounds.map((round) => mergeRound(roundsByIndex.get(round.round_index), round));
  const incomingRoundIds = new Set(incoming.rounds.map((round) => round.round_index));
  for (const round of current.rounds) if (!incomingRoundIds.has(round.round_index)) rounds.push(round);
  rounds.sort((left, right) => left.round_index - right.round_index);
  const activeRound = incoming.active_round ?? (terminal(incoming.run.status) ? null : current.active_round);
  const activeRole = incoming.active_role || (terminal(incoming.run.status) ? null : current.active_role);
  const currentMission = current.mission as unknown as Record<string, unknown>;
  const incomingMission = incoming.mission as unknown as Record<string, unknown>;
  const currentLegacy = current.legacy as Record<string, unknown> | undefined;
  const incomingLegacy = incoming.legacy as Record<string, unknown> | undefined;
  const mergedLegacy = mergeRecordPreserving(currentLegacy, incomingLegacy);
  if (currentLegacy?.report && typeof currentLegacy.report === 'object' && incomingLegacy?.report && typeof incomingLegacy.report === 'object') {
    mergedLegacy.report = mergeRecordPreserving(currentLegacy.report as Record<string, unknown>, incomingLegacy.report as Record<string, unknown>);
  }
  return {
    ...current,
    ...incoming,
    run,
    mission: mergeRecordPreserving(currentMission, incomingMission) as Snapshot['mission'],
    rounds,
    active_round: activeRound,
    active_role: activeRole,
    approvals: mergeApprovals(current.approvals, incoming.approvals),
    operator_messages: mergeOperatorMessages(current.operator_messages || [], incoming.operator_messages || []),
    diagnostics: {
      ...current.diagnostics,
      ...incoming.diagnostics,
      warnings: Array.from(new Set([...(current.diagnostics.warnings || []), ...(incoming.diagnostics.warnings || [])])),
    },
    legacy: mergedLegacy,
  };
}

/**
 * Compare cursors when both snapshots retain evidence for the same event
 * epoch.  Event ids are opaque, so membership (and the optional byte offset)
 * is safer than lexical comparison.  The common case is a server snapshot
 * whose last id is equal to the client's last id but whose retained event
 * count is smaller (server=200, client=400); that snapshot is still newer for
 * durable fields and must be accepted.
 */
function cursorRelation(current: Snapshot, incoming: Snapshot): -1 | 0 | 1 | null {
  const currentCursor = snapshotCursor(current);
  const incomingCursor = snapshotCursor(incoming);
  if (!currentCursor || !incomingCursor) return null;
  if (currentCursor === incomingCursor) return 0;
  const currentEvents = current.events || [];
  const incomingEvents = incoming.events || [];
  if (incomingEvents.some((event) => event.event_id === currentCursor)) return 1;
  if (currentEvents.some((event) => event.event_id === incomingCursor)) return -1;

  const currentLast = currentEvents.find((event) => event.event_id === currentCursor) || currentEvents.at(-1);
  const incomingLast = incomingEvents.find((event) => event.event_id === incomingCursor) || incomingEvents.at(-1);
  const currentOffset = currentLast?.offset;
  const incomingOffset = incomingLast?.offset;
  if (typeof currentOffset === 'number' && typeof incomingOffset === 'number' && incomingOffset !== currentOffset) {
    return incomingOffset > currentOffset ? 1 : -1;
  }
  return null;
}

/** Keep durable state monotonic when REST and WS snapshots race. */
function preferSnapshot(current: Snapshot | null, incoming: Snapshot): Snapshot {
  if (!current) return incoming;
  // A resume reopens a terminal run, so it is the one case where a non-terminal
  // frame legitimately follows a terminal one. Every guard below assumes the
  // opposite, which left a resumed run showing its old ended state until the
  // page was reloaded. A higher generation is newer by definition.
  const currentEpoch = epochOf(current);
  const incomingEpoch = epochOf(incoming);
  if (incomingEpoch !== currentEpoch) {
    return incomingEpoch > currentEpoch
      ? withDurableInteractions(incoming, current)
      : withDurableInteractions(current, incoming);
  }
  // Lifecycle terminality is stronger than event timestamps. A report/status
  // overlay can arrive without a new event (or with a zero timestamp), and a
  // timestamp-only comparison would otherwise keep a stale "running" rail
  // forever after the worker has completed or failed.
  if (terminal(incoming.run.status) && !terminal(current.run.status)) return withDurableInteractions(incoming, current);
  if (terminal(current.run.status) && !terminal(incoming.run.status)) return withDurableInteractions(current, incoming);
  const relation = cursorRelation(current, incoming);
  if (relation === -1) return withDurableInteractions(current, incoming);
  if (relation === 1) return withDurableInteractions(incoming, current);
  if (relation === 0) return mergeEqualCursorSnapshot(current, incoming);
  const currentTime = snapshotEventTime(current);
  const incomingTime = snapshotEventTime(incoming);
  if (incomingTime < currentTime) return withDurableInteractions(current, incoming);
  // ``event_count`` is not a freshness counter: the server intentionally
  // retains only its last 200 records while a client may buffer 400.  Equal
  // timestamps therefore remain eligible to update durable fields such as
  // rounds, approvals, and lifecycle overlays.
  return withDurableInteractions(incoming, current);
}

/** Create an empty, immutable feed state for one selected run. */
export function createRunFeedState(
  runId: string | null = null,
  maxEvents = DEFAULT_RUN_FEED_MAX_EVENTS,
): RunFeedState {
  return {
    runId,
    snapshot: null,
    events: [],
    connection: runId ? 'idle' : 'closed',
    lastEventId: null,
    maxEvents: normalizedLimit(maxEvents),
  };
}

/**
 * Merge REST snapshots and WebSocket events for a single run.
 *
 * Actions for another run are ignored, which makes switching runs safe even
 * when an old socket delivers a late message after it has been disposed.
 */
export function reduceRunFeed(state: RunFeedState, action: RunFeedAction): RunFeedState {
  if (action.type === 'reset') {
    return createRunFeedState(action.runId, state.maxEvents);
  }

  if (action.type === 'connection') {
    if (action.runId !== undefined && action.runId !== state.runId) return state;
    if (action.status === state.connection) return state;
    return { ...state, connection: action.status };
  }

  if (action.type === 'event') {
    const { event } = action;
    if (state.runId === null || event.run_id !== state.runId) return state;
    const events = boundedEvents([...state.events, event], state.maxEvents);
    if (events.length === state.events.length && events.every((item, i) => eventKey(item) === eventKey(state.events[i]))) {
      return state;
    }
    const cursor = cursorFor(events, state.lastEventId);
    const snapshot = state.snapshot ? snapshotWithEvents(state.snapshot, events, cursor) : null;
    return {
      ...state,
      snapshot,
      events,
      lastEventId: cursor,
    };
  }

  if (action.type === 'replay') {
    if (state.runId === null) return state;
    const accepted = action.events.filter((event) => event.run_id === state.runId);
    if (!accepted.length) return state;
    const events = boundedEvents([...state.events, ...accepted], state.maxEvents);
    const cursor = cursorFor(events, state.lastEventId);
    const snapshot = state.snapshot ? snapshotWithEvents(state.snapshot, events, cursor) : null;
    return { ...state, snapshot, events, lastEventId: cursor };
  }

  if (action.type === 'operator_message') {
    if (state.runId !== action.runId || !state.snapshot) return state;
    return {
      ...state,
      snapshot: {
        ...state.snapshot,
        operator_messages: mergeOperatorMessages(state.snapshot.operator_messages || [], [action.message]),
      },
    };
  }

  if (action.type === 'approval_response') {
    if (state.runId !== action.runId || !state.snapshot) return state;
    const approval = state.snapshot.approvals.find((item) => item.approval_id === action.approvalId);
    if (!approval) return state;
    return {
      ...state,
      snapshot: {
        ...state.snapshot,
        approvals: state.snapshot.approvals.map((item) => item.approval_id === action.approvalId ? {
          ...item,
          status: 'resolved',
          action: action.action,
          user_input: action.userInput,
          resolved_at: action.resolvedAt,
        } : item),
      },
    };
  }

  const { snapshot } = action;
  if (!sameRun(state.runId, snapshot)) return state;

  if (action.type === 'snapshot') {
    const resetHistory = snapshot.diagnostics?.resync_required === true || snapshot.diagnostics?.cursor_gap === true;
    const events = boundedEvents([...(resetHistory ? [] : state.events), ...eventsForRun(snapshot.events, state.runId as string)], state.maxEvents);
    const cursor = cursorFor(events, state.lastEventId || snapshot.diagnostics.last_event_id);
    const merged = snapshotWithEvents(preferSnapshot(state.snapshot, snapshot), events, cursor);
    return {
      ...state,
      snapshot: merged,
      events,
      lastEventId: cursor,
    };
  }

  // A seed is intentionally the same merge operation as a snapshot update:
  // a REST response can race with replayed WebSocket events.
  const resetHistory = snapshot.diagnostics?.resync_required === true || snapshot.diagnostics?.cursor_gap === true;
  const events = boundedEvents([...(resetHistory ? [] : state.events), ...eventsForRun(snapshot.events, state.runId as string)], state.maxEvents);
  const cursor = cursorFor(events, state.lastEventId || snapshot.diagnostics.last_event_id);
  const merged = snapshotWithEvents(preferSnapshot(state.snapshot, snapshot), events, cursor);
  return {
    ...state,
    snapshot: merged,
    events,
    lastEventId: cursor,
  };
}
