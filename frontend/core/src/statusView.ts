import type { Approval, EventEnvelope, RoundView, Snapshot } from './types';

/** The small state vocabulary consumed by the Web status surfaces. */
export type StatusState = 'done' | 'active' | 'stopping' | 'pending' | 'waiting' | 'blocked' | 'failed' | 'cancelled' | 'skipped';

export type StatusStageKey = 'manager' | 'executor' | 'auditor' | 'final_response' | 'record';

export interface StatusStage {
  /** Stable key (`round:role`) suitable for React/Ink list keys. */
  id: string;
  key: StatusStageKey;
  label: string;
  round: number;
  status: StatusState;
  /** A short, presentation-ready sentence. Full source text stays in `detail`. */
  summary: string;
  detail: string;
  /** The raw role status (when the backend supplied one). */
  rawStatus: string | null;
  error: string | null;
}

export interface StatusRole {
  key: Exclude<StatusStageKey, 'record'>;
  label: string;
  status: StatusState;
  summary: string;
  detail: string;
  round: number | null;
  rawStatus: string | null;
  error: string | null;
}

export interface StatusApproval {
  id: string;
  title: string;
  message: string;
  status: StatusState;
  pending: boolean;
  round: number | null;
  options: string[];
  action: string;
}

export interface StatusRound {
  round: number;
  status: StatusState;
  summary: string;
  stages: StatusStage[];
  nextStep: string;
}

export interface StatusViewOptions {
  maxRounds?: number;
  maxWarnings?: number;
}

/**
 * LongHorizon's right-hand task projection.
 *
 * This intentionally models rounds and the manager/executor/auditor pipeline;
 * it does not expose Argus' mission/DAG/project concepts. The aliases are
 * useful to clients that prefer either noun (`current` or `currentStage`).
 */
export interface StatusView {
  runId: string;
  runStatus: string;
  status: StatusState;
  task: string;
  taskSummary: string;
  finalResponse: string;
  activeRound: number | null;
  currentRound: number | null;
  activeRole: string | null;
  stages: StatusStage[];
  completed: StatusStage[];
  completedStages: StatusStage[];
  current: StatusStage | null;
  currentStage: StatusStage | null;
  pending: StatusStage[];
  pendingStages: StatusStage[];
  rounds: StatusRound[];
  roles: StatusRole[];
  roleStatuses: StatusRole[];
  nextStep: string;
  nextStepKey: string | null;
  nextStepDetail: string;
  approvals: StatusApproval[];
  pendingApprovals: StatusApproval[];
  warnings: string[];
  notices: string[];
  progress: {
    completed: number;
    total: number;
    ratio: number;
  };
}

const ROLE_KEYS: readonly Exclude<StatusStageKey, 'record'>[] = ['manager', 'executor', 'auditor'];

const DONE_VALUES = new Set(['done', 'complete', 'completed', 'success', 'succeeded', 'passed', 'ok', 'finished']);
const ACTIVE_VALUES = new Set(['active', 'running', 'started', 'starting', 'in_progress', 'in-progress', 'processing']);
const WAITING_VALUES = new Set(['waiting', 'waiting_approval', 'waiting-approval', 'approval', 'paused', 'queued']);
const BLOCKED_VALUES = new Set(['blocked', 'incomplete', 'needs_revision', 'needs-revision', 'invalid']);
const FAILED_VALUES = new Set(['failed', 'failure', 'error', 'errored', 'timeout', 'timed_out', 'timed-out']);
const CANCELLED_VALUES = new Set(['cancelled', 'canceled', 'stopped', 'aborted']);
const STOPPING_VALUES = new Set(['stopping', 'stopping_requested', 'stop_requested', 'aborting', 'aborting_requested']);
const SKIPPED_VALUES = new Set(['skipped', 'not_applicable', 'not-applicable', 'not_required', 'not-required']);
const IDLE_VALUES = new Set(['', 'idle', 'unknown', 'pending', 'queued']);

function normalized(value: unknown): string {
  return String(value ?? '').trim().toLowerCase().replace(/\s+/g, '_');
}

function stageState(value: unknown): StatusState | null {
  const status = normalized(value);
  if (!status) return null;
  if (DONE_VALUES.has(status)) return 'done';
  if (ACTIVE_VALUES.has(status)) return 'active';
  if (WAITING_VALUES.has(status)) return 'waiting';
  if (BLOCKED_VALUES.has(status)) return 'blocked';
  if (FAILED_VALUES.has(status)) return 'failed';
  if (CANCELLED_VALUES.has(status)) return 'cancelled';
  if (STOPPING_VALUES.has(status)) return 'stopping';
  if (SKIPPED_VALUES.has(status)) return 'skipped';
  return null;
}

function statusObjectValue(value: unknown): { status: string | null; error: string | null; detail: string } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    const status = value == null ? null : String(value);
    return { status, error: null, detail: status || '' };
  }
  const record = value as Record<string, unknown>;
  const raw = record.status == null ? null : String(record.status);
  const error = record.error == null || !String(record.error).trim() ? null : String(record.error);
  const detail = error || raw || '';
  return { status: raw, error, detail };
}

function compact(value: unknown, max = 180): string {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(1, max - 1)).trimEnd()}…`;
}

function fullText(value: unknown): string {
  return String(value ?? '').trim();
}

function isTerminalRun(status: unknown): boolean {
  const value = normalized(status);
  return DONE_VALUES.has(value) || FAILED_VALUES.has(value) || CANCELLED_VALUES.has(value) || value === 'blocked' || value === 'incomplete';
}

function isStoppingRun(status: unknown): boolean {
  return STOPPING_VALUES.has(normalized(status));
}

function eventForStage(snapshot: Snapshot, round: number, key: StatusStageKey): EventEnvelope | undefined {
  const prefixes: Record<StatusStageKey, readonly string[]> = {
    manager: ['round.manager'],
    executor: ['round.executor'],
    auditor: ['round.auditor', 'round.audit_repair'],
    final_response: ['round.final_response'],
    record: ['round.recorded'],
  };
  let found: EventEnvelope | undefined;
  for (const [index, event] of snapshot.events.entries()) {
    if (event.round !== round) continue;
    const eventType = typeof event.type === 'string' ? event.type : '';
    if (!prefixes[key].some((prefix) => eventType === prefix || eventType.startsWith(`${prefix}.`))) continue;
    // The event list can be replayed out of order. Timestamp wins, with the
    // source order as a deterministic tie breaker.
    if (!found || event.ts > found.ts || (event.ts === found.ts && index >= snapshot.events.indexOf(found))) found = event;
  }
  return found;
}

function activeRoleMatches(activeRole: string | null, key: StatusStageKey): boolean {
  const role = normalized(activeRole).replace(/-/g, '_');
  if (!role) return false;
  if (key === 'manager') return role === 'manager';
  if (key === 'auditor') return role === 'auditor' || role.startsWith('auditor_');
  if (key === 'executor') return role === 'executor' || role === 'gui' || role === 'cli' || role.startsWith('executor_');
  if (key === 'final_response') return role === 'final_response';
  return role === 'record' || role === 'recorded';
}

function inferStatus(
  snapshot: Snapshot,
  round: RoundView,
  key: StatusStageKey,
  explicit: unknown,
  event: EventEnvelope | undefined,
): StatusState {
  const explicitState = stageState(statusObjectValue(explicit).status);
  const runTerminal = isTerminalRun(snapshot.run.status);
  // A persisted round file is itself the evidence for the record marker. Do
  // this before terminal handling; otherwise a successful round with no
  // harness_feedback was incorrectly projected as cancelled.
  if (key === 'record' && round.in_progress === false) return 'done';
  // A terminal run/round is an authoritative boundary. A stale *_start event
  // must not keep a role looking active after its persisted output was saved.
  // Preserve an explicit failure/block only when it carries real evidence;
  // otherwise map an unfinished stage to the terminal outcome.
  if (runTerminal) {
    if (explicitState === 'failed' || explicitState === 'blocked') return explicitState;
    if (explicitState === 'done') return 'done';
    const terminalEvidence = fullText(
      key === 'manager' ? round.plan_text || round.next_step
        : key === 'executor' ? round.executor_output
          : key === 'auditor' ? round.auditor_report
            : key === 'final_response' ? round.final_response || snapshot.run.final_response
              : round.harness_feedback,
    );
    if (terminalEvidence) return 'done';
    const eventState = stageState(event?.status) || stageState(event?.type?.split('.').at(-1));
    if (eventState === 'done') return 'done';
    if (eventState === 'active' || eventState === 'stopping' || activeRoleMatches(snapshot.active_role, key)) {
      if (snapshot.run.status === 'failed') return 'failed';
      if (snapshot.run.status === 'blocked' || snapshot.run.status === 'incomplete') return 'blocked';
      if (STOPPING_VALUES.has(normalized(snapshot.run.status))) return 'stopping';
      return 'cancelled';
    }
    if (snapshot.run.status === 'failed') return 'failed';
    if (snapshot.run.status === 'blocked' || snapshot.run.status === 'incomplete') return 'blocked';
    if (STOPPING_VALUES.has(normalized(snapshot.run.status))) return 'stopping';
    if (CANCELLED_VALUES.has(normalized(snapshot.run.status))) return 'cancelled';
    // A successful terminal run may legitimately stop after Manager declares
    // completion. A role that was never invoked is not cancelled or waiting.
    return 'skipped';
  }
  if (explicitState) return explicitState;
  const eventType = typeof event?.type === 'string' ? event.type : '';
  const eventState = stageState(event?.status) || stageState(eventType.split('.').at(-1));
  // A stop request is an authoritative transition even before the worker has
  // closed its current round. Preserve durable output as done, but make any
  // still-live role visibly unwind instead of calling it ordinary progress.
  if (STOPPING_VALUES.has(normalized(snapshot.run.status))) {
    const stoppingContent = fullText(
      key === 'manager' ? round.plan_text || round.next_step
        : key === 'executor' ? round.executor_output
          : key === 'auditor' ? round.auditor_report
            : key === 'final_response' ? round.final_response || snapshot.run.final_response
              : round.harness_feedback,
    );
    if (stoppingContent) return 'done';
    if (round.in_progress === false) return 'done';
    return 'stopping';
  }
  if (eventState) return eventState;

  const active = snapshot.active_round === round.round_index && activeRoleMatches(snapshot.active_role, key);
  if (active) return 'active';
  if (key === 'record') {
    if (round.in_progress === false) return 'done';
    if (STOPPING_VALUES.has(normalized(snapshot.run.status))) return 'stopping';
    if (isTerminalRun(snapshot.run.status)) return FAILED_VALUES.has(normalized(snapshot.run.status)) ? 'failed' : 'cancelled';
    return 'pending';
  }
  const content = key === 'manager'
    ? round.plan_text || round.next_step
    : key === 'executor'
      ? round.executor_output
      : key === 'auditor'
        ? round.auditor_report
        : key === 'final_response'
          ? round.final_response || snapshot.run.final_response
          : round.harness_feedback;
  if (content && fullText(content)) return 'done';
  // A completed round with no explicit role metadata still has a useful
  // durable ordering: manager -> executor -> auditor -> record.
  if (round.in_progress === false) return 'done';
  if (key === 'executor' && (round.plan_text || round.next_step) && normalized(round.next_step) !== 'ask') return 'pending';
  return 'pending';
}

function stageSummary(round: RoundView, key: StatusStageKey, status: StatusState): { summary: string; detail: string; error: string | null } {
  if (key === 'manager') {
    const detail = fullText(round.plan_text || round.next_step);
    return {
      summary: compact(round.next_step || round.plan_text || (status === 'active' ? 'Manager is preparing the next step.' : 'No manager plan recorded.')),
      detail,
      error: null,
    };
  }
  if (key === 'executor') {
    const source = fullText(round.executor_output);
    const statusInfo = statusObjectValue(round.executor_status);
    return {
      summary: compact(source || statusInfo.error || statusInfo.status || (status === 'active' ? 'Executor is working.' : status === 'skipped' ? 'Executor was not invoked in this round.' : 'Awaiting execution.')),
      detail: source || statusInfo.detail,
      error: statusInfo.error,
    };
  }
  if (key === 'auditor') {
    const source = fullText(round.auditor_report);
    const statusInfo = statusObjectValue(round.auditor_status);
    return {
      summary: compact(source || statusInfo.error || statusInfo.status || (status === 'active' ? 'Auditor is checking the result.' : status === 'skipped' ? 'Auditor was not invoked in this round.' : 'Awaiting verification.')),
      detail: source || statusInfo.detail,
      error: statusInfo.error,
    };
  }
  if (key === 'final_response') {
    const source = fullText(round.final_response);
    const statusInfo = statusObjectValue(round.final_response_status);
    return {
      summary: compact(source || statusInfo.error || statusInfo.status || (status === 'active' ? 'Writing the final reply.' : status === 'skipped' ? 'No final reply was requested.' : 'Awaiting the final reply.')),
      detail: source || statusInfo.detail,
      error: statusInfo.error,
    };
  }
  const detail = fullText(round.harness_feedback);
  return {
    summary: compact(detail || (status === 'done' ? 'Round recorded.' : status === 'active' ? 'Recording round.' : 'Round not recorded yet.')),
    detail,
    error: null,
  };
}

function stageLabel(key: StatusStageKey): string {
  return key === 'manager' ? 'Manager' : key === 'executor' ? 'Executor' : key === 'auditor' ? 'Auditor' : key === 'final_response' ? 'Final reply' : 'Recorded';
}

function roundState(stages: StatusStage[]): StatusState {
  if (stages.some((item) => item.status === 'failed')) return 'failed';
  if (stages.some((item) => item.status === 'cancelled')) return 'cancelled';
  if (stages.some((item) => item.status === 'blocked')) return 'blocked';
  if (stages.some((item) => item.status === 'stopping')) return 'stopping';
  if (stages.some((item) => item.status === 'waiting')) return 'waiting';
  if (stages.some((item) => item.status === 'active')) return 'active';
  if (stages.length > 0 && stages.every((item) => item.status === 'done' || item.status === 'skipped')) return 'done';
  return 'pending';
}

function roleListed(round: RoundView, key: Exclude<StatusStageKey, 'record'>): boolean {
  const roles = new Set((round.roles || []).map((role) => normalized(role)));
  if (key === 'manager') return roles.has('manager');
  if (key === 'auditor') return [...roles].some((role) => role === 'auditor' || role.startsWith('auditor_'));
  if (key === 'final_response') return roles.has('final_response');
  return [...roles].some((role) => role === 'executor' || role === 'gui' || role === 'cli' || role.startsWith('executor_'));
}

function hasStatusEvidence(value: unknown): boolean {
  if (value == null) return false;
  if (typeof value !== 'object' || Array.isArray(value)) return Boolean(String(value).trim());
  return Object.values(value as Record<string, unknown>).some((item) => item != null && String(item).trim());
}

/**
 * Return only the stages that this round actually scheduled or evidenced.
 *
 * Manager can terminate, block, or ask a question without invoking Executor or
 * Auditor. Treating the fixed three-role pipeline as if it always ran creates
 * fake pending work in terminal snapshots and corrupts progress percentages.
 */
function stageKeysForRound(snapshot: Snapshot, round: RoundView): StatusStageKey[] {
  const next = normalized(round.next_step);
  const executorEvent = eventForStage(snapshot, round.round_index, 'executor');
  const auditorEvent = eventForStage(snapshot, round.round_index, 'auditor');
  const finalResponseEvent = eventForStage(snapshot, round.round_index, 'final_response');
  const executorEvidence = roleListed(round, 'executor')
    || hasStatusEvidence(round.executor_status)
    || Boolean(fullText(round.executor_output))
    || Boolean(executorEvent)
    || (snapshot.active_round === round.round_index && activeRoleMatches(snapshot.active_role, 'executor'));
  const auditorEvidence = roleListed(round, 'auditor')
    || hasStatusEvidence(round.auditor_status)
    || Boolean(fullText(round.auditor_report))
    || Boolean(auditorEvent)
    || (snapshot.active_round === round.round_index && activeRoleMatches(snapshot.active_role, 'auditor'));
  const executionPlanned = next === 'gui' || next === 'cli' || executorEvidence || auditorEvidence;
  const isLatestRound = snapshot.rounds.at(-1)?.round_index === round.round_index;
  const finalResponseEvidence = roleListed(round, 'final_response')
    || hasStatusEvidence(round.final_response_status)
    || Boolean(fullText(round.final_response))
    || Boolean(finalResponseEvent)
    || (isLatestRound && Boolean(fullText(snapshot.run.final_response)))
    || (snapshot.active_round === round.round_index && activeRoleMatches(snapshot.active_role, 'final_response'));
  const keys: StatusStageKey[] = ['manager'];
  if (executionPlanned) keys.push('executor', 'auditor');
  if (finalResponseEvidence) keys.push('final_response');
  // The record marker remains useful to shared Web consumers but is kept
  // out of operator-facing execution progress.
  keys.push('record');
  return keys;
}

function nextStepInfo(snapshot: Snapshot, current: StatusStage | null, rounds: StatusRound[]): { key: string | null; text: string; detail: string } {
  const activeRound = snapshot.active_round ?? rounds.find((item) => item.status === 'active' || item.status === 'waiting')?.round ?? rounds.at(-1)?.round ?? null;
  const round = rounds.find((item) => item.round === activeRound) || rounds.at(-1);
  const source = snapshot.rounds.find((item) => item.round_index === round?.round);
  const raw = normalized(source?.next_step);
  const detail = fullText(source?.plan_text || source?.harness_feedback || '');
  const runStatus = normalized(snapshot.run.status);
  if (!isTerminalRun(runStatus) && !STOPPING_VALUES.has(runStatus) && snapshot.approvals.some((item) => item.status === 'pending')) {
    return { key: 'approval', text: 'Resolve the pending approval.', detail: snapshot.approvals.filter((item) => item.status === 'pending').map((item) => item.message).filter(Boolean).join('\n') };
  }
  if (STOPPING_VALUES.has(runStatus)) {
    return { key: 'stopping', text: 'The run is stopping.', detail };
  }
  if (DONE_VALUES.has(runStatus)) {
    return { key: 'done', text: 'No further action; the task is complete.', detail };
  }
  if (raw === 'gui') return { key: raw, text: 'Run the GUI subtask.', detail };
  if (raw === 'cli') return { key: raw, text: 'Run the CLI subtask.', detail };
  if (raw === 'ask') {
    const question = detail.split(/\n/).find((line) => /(?:question|问题)\s*[:：]/iu.test(line));
    return { key: raw, text: compact(question?.replace(/^.*?[:：]/u, '') || 'Waiting for your input.'), detail };
  }
  if (raw === 'blocked') return { key: raw, text: 'Resolve the blocker before continuing.', detail };
  if (raw === 'invalid') return { key: raw, text: 'Manager must revise the plan.', detail };
  if (raw === 'done' || raw === 'complete' || raw === 'completed') {
    return { key: 'done', text: isTerminalRun(snapshot.run.status) ? 'No further action; the task is complete.' : 'Await final verification.', detail };
  }
  // Manager may persist a concrete operator-facing next step (for example
  // "fix the failing command"). Keep that text ahead of a stale active-role
  // fallback so terminal snapshots tell the operator what remains.
  if (source?.next_step && source.next_step.trim()) {
    return { key: 'custom', text: compact(source.next_step), detail };
  }
  if (current?.key === 'manager') return { key: 'manager', text: 'Manager is preparing the next step.', detail };
  if (current?.key === 'executor') return { key: 'executor', text: 'Executor is running the planned task.', detail };
  if (current?.key === 'auditor') return { key: 'auditor', text: 'Auditor is verifying the latest result.', detail };
  if (current?.key === 'final_response') return { key: 'final_response', text: 'LongHorizon is writing the final reply.', detail };
  if (current?.key === 'record') return { key: 'record', text: 'Record the round and continue.', detail };
  if (isTerminalRun(snapshot.run.status)) return { key: null, text: 'No further action.', detail };
  return { key: null, text: 'Start manager planning.', detail };
}

function mapApproval(approval: Approval): StatusApproval {
  const pending = normalized(approval.status) === 'pending';
  return {
    id: approval.approval_id,
    title: approval.title || 'Approval required',
    message: compact(approval.message, 260),
    status: pending ? 'waiting' : stageState(approval.status) || 'done',
    pending,
    round: Number.isFinite(approval.round_index) ? approval.round_index : null,
    options: approval.options.map((option) => option.label || option.value).filter(Boolean),
    action: approval.action || '',
  };
}

/** Build the stable LongHorizon status projection for a snapshot. */
export function projectStatus(snapshot: Snapshot, options: StatusViewOptions = {}): StatusView {
  const stages: StatusStage[] = [];
  const rounds: StatusRound[] = [];
  const maxRounds = Number.isFinite(options.maxRounds) ? Math.max(1, Math.floor(options.maxRounds as number)) : snapshot.rounds.length || 1;
  const sourceRounds = snapshot.rounds.length > maxRounds ? snapshot.rounds.slice(-maxRounds) : snapshot.rounds;
  for (const round of sourceRounds) {
    const roundStages: StatusStage[] = [];
    const statusValues: Record<StatusStageKey, unknown> = {
      manager: round.manager_status,
      executor: round.executor_status,
      auditor: round.auditor_status,
      final_response: round.final_response_status,
      record: undefined,
    };
    for (const key of stageKeysForRound(snapshot, round)) {
      const event = eventForStage(snapshot, round.round_index, key);
      const statusInfo = statusObjectValue(statusValues[key]);
      const status = inferStatus(snapshot, round, key, statusValues[key], event);
      const copy = stageSummary(round, key, status);
      const stage: StatusStage = {
        id: `${round.round_index}:${key}`,
        key,
        label: stageLabel(key),
        round: round.round_index,
        status,
        summary: copy.summary,
        detail: copy.detail,
        rawStatus: statusInfo.status || event?.status || null,
        error: copy.error || (status === 'failed' ? statusInfo.error : null),
      };
      roundStages.push(stage);
      stages.push(stage);
    }
    const nextStep = compact(round.next_step || (round.in_progress ? 'in progress' : 'recorded'));
    rounds.push({
      round: round.round_index,
      status: roundState(roundStages),
      summary: compact(round.task_state || round.plan_text || nextStep),
      stages: roundStages,
      nextStep,
    });
  }

  const rawRunStatus = normalized(snapshot.run.status);
  const runState = stageState(snapshot.run.status) || (IDLE_VALUES.has(rawRunStatus) ? 'pending' : 'active');
  const activeCandidates = stages.filter((item) => item.status === 'active' || item.status === 'stopping' || item.status === 'waiting' || item.status === 'blocked' || item.status === 'failed');
  const activeRoundStages = activeCandidates.filter((item) => snapshot.active_round == null || item.round === snapshot.active_round);
  // Terminal snapshots have no active stage. Preserve failed/blocked evidence
  // in the stage list, but do not call a stale historical role "current".
  const current = isTerminalRun(snapshot.run.status)
    ? null
    : activeRoundStages[0] || activeCandidates[0] || stages.find((item) => item.status === 'pending') || null;
  const done = stages.filter((item) => item.status === 'done');
  const pending = stages.filter((item) => ['pending', 'waiting', 'stopping', 'blocked', 'failed'].includes(item.status) && item !== current);
  const roleKeys: readonly Exclude<StatusStageKey, 'record'>[] = stages.some((item) => item.key === 'final_response') || Boolean(fullText(snapshot.run.final_response))
    ? [...ROLE_KEYS, 'final_response']
    : ROLE_KEYS;
  const roleStatuses: StatusRole[] = roleKeys.map((key) => {
    const roleStages = stages.filter((item) => item.key === key);
    const latest = roleStages.at(-1);
    const currentRole = [...roleStages].reverse().find((item) =>
      (snapshot.active_round == null || item.round === snapshot.active_round)
      && (item.status === 'active' || item.status === 'stopping' || item.status === 'waiting' || item.status === 'failed' || item.status === 'blocked'));
    // A terminal run can append a manager-only round after the last executor
    // or auditor result. Prefer the most recent completed stage with evidence
    // over that empty placeholder so the rail never says "awaiting" after the
    // role has already produced a result.
    const completedWithEvidence = [...roleStages].reverse().find((item) => item.status === 'done' && Boolean(item.detail && !/^awaiting\b|^no\s+.+output\s+recorded\.?$/iu.test(item.detail.trim())));
    const chosen = currentRole || completedWithEvidence || latest;
    // The role rail is deliberately fixed to the three named LongHorizon
    // roles, but an absent role is not the same thing as pending work after a
    // terminal run.  A manager can finish a task without ever scheduling an
    // executor/auditor; showing those roles as "待开始" makes the right-hand
    // status panel contradict the authoritative terminal result.  Preserve
    // `pending` for live runs (where the role may still be scheduled), and use
    // `skipped` only when the lifecycle is already terminal.
    const fallbackStatus: StatusState = isTerminalRun(snapshot.run.status) ? 'skipped' : 'pending';
    return {
      key,
      label: stageLabel(key),
      status: chosen?.status || fallbackStatus,
      summary: chosen?.summary || (fallbackStatus === 'skipped' ? 'This role was not invoked.' : `No ${key} output recorded.`),
      detail: chosen?.detail || '',
      round: chosen?.round ?? null,
      rawStatus: chosen?.rawStatus ?? null,
      error: chosen?.error ?? null,
    };
  });
  const approvals = snapshot.approvals.map(mapApproval);
  // A pending approval can survive a worker crash/stop because the approval
  // ledger is intentionally durable.  It remains useful in the audit history,
  // but it must not be projected as an actionable request once the lifecycle
  // can no longer accept a decision.  Web consumes this field, so
  // keep the rule in the shared projection instead of duplicating it in each
  // renderer.
  const pendingApprovals = approvals.filter((item) => item.pending && !isTerminalRun(snapshot.run.status) && !isStoppingRun(snapshot.run.status));
  const inconsistentCompletion = snapshot.run.completion_satisfied === true
    && stages.some((item) => item.key !== 'record' && !['done', 'skipped'].includes(item.status));
  const primaryWarnings = [
    ...(snapshot.run.failure_reason ? [compact(snapshot.run.failure_reason, 260)] : []),
    ...snapshot.diagnostics.warnings.map((item) => compact(item, 260)).filter(Boolean),
    ...snapshot.rounds.map((round) => round.harness_feedback ? compact(round.harness_feedback, 260) : '').filter(Boolean),
  ];
  const primaryWarningBodies = new Set(primaryWarnings.map((item) => compact(item, 220)));
  const allWarnings = Array.from(new Set([
    ...primaryWarnings,
    ...stages.map((item) => {
      if (!item.error) return '';
      const error = compact(item.error, 220);
      return primaryWarningBodies.has(error) ? '' : `${item.label}: ${error}`;
    }).filter(Boolean),
    ...(inconsistentCompletion ? ['Completion report is satisfied, but one or more scheduled stages have no terminal evidence.'] : []),
  ]));
  const maxWarnings = Number.isFinite(options.maxWarnings) ? Math.max(1, Math.floor(options.maxWarnings as number)) : allWarnings.length || 1;
  const warnings = allWarnings.slice(-maxWarnings);
  const notices = Array.from(new Set([
    ...pendingApprovals.map((item) => item.message).filter(Boolean),
    ...(snapshot.controls.can_resume ? ['This run can be resumed.'] : []),
  ]));
  const next = nextStepInfo(snapshot, current, rounds);
  const progressStages = stages.filter((item) => item.key !== 'record' && item.status !== 'skipped');
  const progressDone = progressStages.filter((item) => item.status === 'done');
  const total = progressStages.length;
  const ratio = total ? progressDone.length / total : runState === 'done' ? 1 : 0;
  const task = fullText(snapshot.mission.task);
  const finalResponse = fullText(snapshot.run.final_response || [...snapshot.rounds].reverse().find((round) => fullText(round.final_response))?.final_response);
  return {
    runId: snapshot.run.id,
    runStatus: String(snapshot.run.status || 'idle'),
    status: runState,
    task,
    taskSummary: compact(task, 240),
    finalResponse,
    activeRound: snapshot.active_round,
    currentRound: current?.round ?? snapshot.active_round ?? rounds.at(-1)?.round ?? null,
    activeRole: snapshot.active_role,
    stages,
    completed: done,
    completedStages: done,
    current,
    currentStage: current,
    pending,
    pendingStages: pending,
    rounds,
    roles: roleStatuses,
    roleStatuses,
    nextStep: next.text,
    nextStepKey: next.key,
    nextStepDetail: next.detail,
    approvals,
    pendingApprovals,
    warnings,
    notices,
    progress: { completed: progressDone.length, total, ratio },
  };
}

export const buildStatusView = projectStatus;
export const statusView = projectStatus;
// Backwards-compatible verb used by early Web consumers.
export const projectStatusView = projectStatus;
