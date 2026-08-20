import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  createRunFeedState,
  reduceRunFeed,
  type RunFeedState,
} from '../src/runFeed';
import type { EventEnvelope, Snapshot } from '../src/types';
import { availableCommands, commandHelp, normaliseMaxRounds, parseCommand, parseNewRunArgs } from '../src/commands';
import { managerPlanSummary, managerPlanText, sortTranscript } from '../src/runView';
import { DEFAULT_PANEL_STATE, reducePanelState } from '../src/panels';
import { projectStatus } from '../src/statusView';
import { isTrajectoryNoise, projectTrajectoryView } from '../src/trajectoryView';
import { projectArtifactView } from '../src/artifactView';

function event(
  eventId: string,
  ts: number,
  runId = 'run-a',
  type = 'round.executor.started',
): EventEnvelope {
  return {
    schema_version: 1,
    event_id: eventId,
    type,
    ts,
    run_id: runId,
    round: 1,
    role: 'executor',
    status: 'running',
    payload: {},
    legacy: {},
  };
}

function snapshot(runId = 'run-a', events: EventEnvelope[] = []): Snapshot {
  return {
    schema_version: 1,
    run: { id: runId, status: 'running', log_dir: `/tmp/${runId}` },
    mission: {
      task: `task for ${runId}`,
      contract_path: '',
      plan_path: '',
      verified_state_path: '',
      report_path: '',
    },
    rounds: [],
    active_round: null,
    active_role: null,
    events,
    approvals: [],
    controls: { can_inject: true, can_abort: true, can_resume: false },
    diagnostics: {
      last_event_id: events.at(-1)?.event_id || null,
      event_count: events.length,
      warnings: [],
    },
  };
}

function reduce(state: RunFeedState, ...actions: Parameters<typeof reduceRunFeed> extends [RunFeedState, infer A] ? A[] : never): RunFeedState {
  return actions.reduce(reduceRunFeed, state);
}

test('creates an isolated feed with a normalized bounded size', () => {
  assert.equal(createRunFeedState().connection, 'closed');
  assert.equal(createRunFeedState('run-a', 2.9).maxEvents, 2);
  assert.equal(createRunFeedState('run-a', 0).maxEvents, 1);
  assert.equal(createRunFeedState('run-a', Number.NaN).maxEvents, 400);
});

test('same-cursor snapshots retain durable operator messages and delivery updates', () => {
  const base = snapshot('run-a');
  const queued: Snapshot = {
    ...base,
    operator_messages: [{ id: 'msg-1', text: 'Use the Chinese title.', created_at: 10, status: 'queued' }],
  };
  const applied: Snapshot = {
    ...base,
    operator_messages: [
      { id: 'msg-1', text: 'Use the Chinese title.', created_at: 10, status: 'applied' },
      { id: 'msg-2', text: 'Add sources.', created_at: 20, status: 'queued' },
    ],
  };
  const state = reduce(createRunFeedState('run-a'), { type: 'snapshot', snapshot: queued }, { type: 'snapshot', snapshot: applied });

  assert.deepEqual(state.snapshot?.operator_messages, applied.operator_messages);
});

test('operator messages render optimistically and reconcile without duplication', () => {
  const message = { id: 'instruction-live', text: 'Use the new title.', created_at: 10, status: 'queued' as const };
  let state = reduce(createRunFeedState('run-a'), { type: 'seed', snapshot: snapshot('run-a') });
  state = reduceRunFeed(state, { type: 'operator_message', runId: 'run-a', message });
  assert.deepEqual(state.snapshot?.operator_messages, [message]);

  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a'),
      operator_messages: [{ ...message, created_at: 11, status: 'applied' }],
    },
  });
  assert.equal(state.snapshot?.operator_messages?.length, 1);
  assert.equal(state.snapshot?.operator_messages?.[0]?.status, 'applied');
  assert.equal(state.snapshot?.operator_messages?.[0]?.created_at, 11);
});

test('operator message actions ignore late updates from another run', () => {
  const state = reduce(createRunFeedState('run-a'), { type: 'seed', snapshot: snapshot('run-a') });
  const unchanged = reduceRunFeed(state, {
    type: 'operator_message',
    runId: 'run-b',
    message: { id: 'foreign', text: 'Wrong run', created_at: 10, status: 'queued' },
  });
  assert.equal(unchanged, state);
});

test('a newer role-event snapshot cannot erase an optimistic operator message', () => {
  const first = event('event-1', 10);
  const second = event('event-2', 20);
  let state = reduce(createRunFeedState('run-a'), { type: 'seed', snapshot: snapshot('run-a', [first]) });
  state = reduceRunFeed(state, {
    type: 'operator_message',
    runId: 'run-a',
    message: { id: 'instruction-race', text: 'Keep this visible.', created_at: 15, status: 'queued' },
  });
  state = reduceRunFeed(state, { type: 'snapshot', snapshot: snapshot('run-a', [first, second]) });
  assert.equal(state.snapshot?.operator_messages?.[0]?.id, 'instruction-race');
});

test('approval responses render optimistically and survive stale snapshots', () => {
  const approval = {
    approval_id: 'approval-1', title: 'Manager needs input', message: 'Choose.', options: [], answers: [],
    allow_input: true, input_label: '', context: {}, round_index: 1, status: 'pending', action: '', reason: '',
    user_input: '', created_at: 10, resolved_at: null,
  };
  const first = event('approval-event-1', 10);
  const second = event('approval-event-2', 20);
  let state = reduce(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: { ...snapshot('run-a', [first]), approvals: [approval] },
  });
  state = reduceRunFeed(state, {
    type: 'approval_response',
    runId: 'run-a',
    approvalId: 'approval-1',
    action: 'continue',
    userInput: 'Use account B.',
    resolvedAt: 15,
  });
  assert.equal(state.snapshot?.approvals[0]?.status, 'resolved');
  assert.equal(state.snapshot?.approvals[0]?.user_input, 'Use account B.');

  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: { ...snapshot('run-a', [first, second]), approvals: [approval] },
  });
  assert.equal(state.snapshot?.approvals[0]?.status, 'resolved');
  assert.equal(state.snapshot?.approvals[0]?.user_input, 'Use account B.');
  assert.equal(state.snapshot?.approvals[0]?.resolved_at, 15);
});

test('a resumed run replaces its own terminal state without a reload', () => {
  // Every monotonic guard assumes a terminal frame is final. Resuming in place
  // is the one case where it is not, so without the generation counter the UI
  // kept showing the ended run (and an empty conversation) until a reload.
  const ended = event('resume-ev-1', 10);
  let state = reduce(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: {
      ...snapshot('run-a', [ended]),
      run: { id: 'run-a', status: 'incomplete', log_dir: '/tmp/run-a', finished_at: 20, exit_code: 1, failure_reason: 'stopped' },
    },
  });
  assert.equal(state.snapshot?.run.status, 'incomplete');

  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a', [ended]),
      run: { id: 'run-a', status: 'starting', log_dir: '/tmp/run-a', resume_epoch: 1 },
    },
  });
  assert.equal(state.snapshot?.run.status, 'starting');
  assert.equal(state.snapshot?.run.resume_epoch, 1);
  assert.equal(state.snapshot?.run.finished_at, undefined, 'the previous outcome must not be carried over');
  assert.equal(state.snapshot?.run.exit_code, undefined);
  assert.equal(state.snapshot?.run.failure_reason, undefined);
});

test('a stale frame from the previous generation is ignored', () => {
  const ev = event('resume-ev-2', 10);
  let state = reduce(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: { ...snapshot('run-a', [ev]), run: { id: 'run-a', status: 'running', log_dir: '/tmp/run-a', resume_epoch: 2 } },
  });

  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: { ...snapshot('run-a', [ev]), run: { id: 'run-a', status: 'cancelled', log_dir: '/tmp/run-a', finished_at: 5, resume_epoch: 1 } },
  });

  assert.equal(state.snapshot?.run.status, 'running', 'an older generation must not end the current one');
  assert.equal(state.snapshot?.run.resume_epoch, 2);
});

test('terminality still wins inside one generation', () => {
  const ev = event('resume-ev-3', 10);
  let state = reduce(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: { ...snapshot('run-a', [ev]), run: { id: 'run-a', status: 'completed', log_dir: '/tmp/run-a', resume_epoch: 1 } },
  });

  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: { ...snapshot('run-a', [ev]), run: { id: 'run-a', status: 'running', log_dir: '/tmp/run-a', resume_epoch: 1 } },
  });

  assert.equal(state.snapshot?.run.status, 'completed');
});

test('keeps an operator round grant when a stale pending frame arrives', () => {
  const base = {
    approval_id: 'approval-rounds', title: 'Round limit reached', message: '', options: [], answers: [],
    allow_input: true, input_label: '', allow_extra_rounds: true, context: { trigger: 'max_rounds' },
    round_index: 5, status: 'pending', action: '', reason: '', user_input: '', created_at: 10, resolved_at: null,
  };
  const first = event('rounds-event-1', 10);
  const second = event('rounds-event-2', 20);
  let state = reduce(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: { ...snapshot('run-a', [first]), approvals: [{ ...base, status: 'resolved', action: 'continue', extra_rounds: 7, resolved_at: 15 }] },
  });
  assert.equal(state.snapshot?.approvals[0]?.extra_rounds, 7);

  // A stale frame (or an older server that omits the field) must not erase it.
  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: { ...snapshot('run-a', [first, second]), approvals: [base] },
  });
  assert.equal(state.snapshot?.approvals[0]?.extra_rounds, 7);
  assert.equal(state.snapshot?.approvals[0]?.allow_extra_rounds, true);
  assert.equal(state.snapshot?.approvals[0]?.status, 'resolved');
});

test('keeps the Manager plan body when a compact route is present', () => {
  assert.equal(
    managerPlanText({ round_index: 1, next_step: 'gui', plan_text: 'Task contract:\nOpen the page and verify the screenshot.' }),
    'Task contract:\nOpen the page and verify the screenshot.',
  );
  assert.equal(managerPlanText({ round_index: 1, next_step: 'cli' }), 'Next step: cli');
});

test('shows the routed Manager task instead of the empty completed-state preamble', () => {
  const plan = `Current task state:

Completed: None; no auditor reports exist.

Incomplete: Research ten articles.

Next: cli

Task: Inspect the repository first and return exact file evidence.

Acceptance criteria: Cite the relevant files.

Boundaries: Read only.`;

  assert.equal(
    managerPlanSummary(plan),
    'Inspect the repository first and return exact file evidence.',
  );
  assert.equal(managerPlanSummary('Question: Which account should be used?\n\nChoices: Work | Personal'), 'Which account should be used?');
});

test('orders a transcript strictly by time', () => {
  const ordered = sortTranscript([
    { kind: 'plan', round: 1, sortTime: 20, id: 'plan-1' },
    { kind: 'verification', round: 1, sortTime: 40, id: 'audit-1' },
    { kind: 'final', sortTime: 60, id: 'reply-round-1' },
    { kind: 'plan', round: 2, sortTime: 120, id: 'plan-2' },
    { kind: 'user', sortRound: -1, sortTime: 1, id: 'task' },
    // Sent after reading the round-1 answer, so it must land below it.
    { kind: 'user', sortTime: 80, id: 'follow-up' },
  ] as const);

  assert.deepEqual(ordered.map((item) => item.id), [
    'task', 'plan-1', 'audit-1', 'reply-round-1', 'follow-up', 'plan-2',
  ]);
});

test('an answer never floats below a reply the operator sent earlier', () => {
  // The regression: a closing answer written at t=60 was pinned last, so a
  // follow-up typed at t=80 appeared above the text it responded to.
  const ordered = sortTranscript([
    { kind: 'user', sortTime: 80, id: 'follow-up' },
    { kind: 'final', sortTime: 60, id: 'answer' },
  ] as const);

  assert.deepEqual(ordered.map((item) => item.id), ['answer', 'follow-up']);
});

test('untimed entries stay next to their own round', () => {
  // Only the plan carries an event time; the rest of round 2 has none yet.
  const ordered = sortTranscript([
    { kind: 'user', sortTime: 10, id: 'earlier-reply' },
    { kind: 'live', sortRound: Number.MAX_SAFE_INTEGER, id: 'live' },
    { kind: 'verification', round: 2, id: 'audit' },
    { kind: 'assistant', round: 2, id: 'exec' },
    { kind: 'plan', round: 2, sortTime: 50, id: 'plan' },
  ] as const);

  assert.deepEqual(ordered.map((item) => item.id), ['earlier-reply', 'plan', 'exec', 'audit', 'live']);
});

test('the original request stays first after a resume restamps the run', () => {
  // Resuming sets `started_at` to the reopen time, so the task card's own
  // timestamp is newer than every round it started.
  const ordered = sortTranscript([
    { kind: 'plan', round: 1, sortTime: 100, id: 'plan' },
    { kind: 'final', sortTime: 200, id: 'answer' },
    { kind: 'user', sortRound: -1, sortTime: 9_000, id: 'task' },
  ] as const);

  assert.deepEqual(ordered.map((item) => item.id), ['task', 'plan', 'answer']);
});

test('a stable sort keeps equal entries in their projected order', () => {
  const ordered = sortTranscript([
    { kind: 'user', round: 1, id: 'first' },
    { kind: 'user', round: 1, id: 'second' },
    { kind: 'user', round: 1, id: 'third' },
  ] as const);

  assert.deepEqual(ordered.map((item) => item.id), ['first', 'second', 'third']);
});

test('normalizes the Web round limit without changing a typed ten to one', () => {
  assert.equal(normaliseMaxRounds('10'), 10);
  assert.equal(normaliseMaxRounds('0010'), 10);
  assert.equal(normaliseMaxRounds(''), 25);
  assert.equal(normaliseMaxRounds('not-a-number'), 25);
  assert.equal(normaliseMaxRounds('0'), 1);
  assert.equal(normaliseMaxRounds('1001'), 1000);
});

test('seeds a snapshot, filters foreign events, and keeps diagnostics coherent', () => {
  const state = createRunFeedState('run-a', 10);
  const seeded = reduceRunFeed(state, {
    type: 'seed',
    snapshot: snapshot('run-a', [event('a-1', 1), event('foreign', 2, 'run-b'), event('a-2', 3)]),
  });

  assert.deepEqual(seeded.events.map((item) => item.event_id), ['a-1', 'a-2']);
  assert.equal(seeded.snapshot?.mission.task, 'task for run-a');
  assert.equal(seeded.snapshot?.diagnostics.event_count, 2);
  assert.equal(seeded.snapshot?.diagnostics.last_event_id, 'a-2');
  assert.equal(seeded.lastEventId, 'a-2');
});

test('merges REST replay and WebSocket replay without duplicate rows', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: snapshot('run-a', [event('a-1', 1), event('a-2', 2)]),
  });

  state = reduceRunFeed(state, {
    type: 'replay',
    events: [event('a-2', 2), event('a-3', 3), event('other', 4, 'run-b')],
  });
  state = reduceRunFeed(state, { type: 'event', event: event('a-3', 3) });

  assert.deepEqual(state.events.map((item) => item.event_id), ['a-1', 'a-2', 'a-3']);
  assert.equal(state.lastEventId, 'a-3');
  assert.equal(state.snapshot?.diagnostics.event_count, 3);
});

test('keeps the cursor monotonic when an old event arrives after reconnect', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'replay',
    events: [event('a-2', 20), event('a-3', 30)],
  });
  state = reduceRunFeed(state, { type: 'event', event: event('a-1', 10) });

  assert.deepEqual(state.events.map((item) => item.event_id), ['a-1', 'a-2', 'a-3']);
  assert.equal(state.lastEventId, 'a-3');
});

test('enforces a bounded event buffer while retaining the newest events', () => {
  let state = createRunFeedState('run-a', 3);
  for (let index = 1; index <= 5; index += 1) {
    state = reduceRunFeed(state, { type: 'event', event: event(`a-${index}`, index) });
  }

  assert.deepEqual(state.events.map((item) => item.event_id), ['a-3', 'a-4', 'a-5']);
  assert.equal(state.snapshot, null);
  assert.equal(state.lastEventId, 'a-5');
});

test('ignores stale events and snapshots after switching runs', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: snapshot('run-a', [event('a-1', 1)]),
  });
  state = reduceRunFeed(state, { type: 'reset', runId: 'run-b' });
  const unchanged = reduce(state,
    { type: 'event', event: event('a-late', 9, 'run-a') },
    { type: 'replay', events: [event('a-late-2', 10, 'run-a')] },
    { type: 'snapshot', snapshot: snapshot('run-a', [event('a-late-3', 11, 'run-a')]) },
  );

  assert.equal(unchanged.runId, 'run-b');
  assert.equal(unchanged.snapshot, null);
  assert.deepEqual(unchanged.events, []);
});

test('guards connection updates from an old run', () => {
  const state = createRunFeedState('run-b');
  const unchanged = reduceRunFeed(state, { type: 'connection', runId: 'run-a', status: 'connected' });
  assert.equal(unchanged.connection, 'idle');
  const connected = reduceRunFeed(state, { type: 'connection', runId: 'run-b', status: 'connected' });
  assert.equal(connected.connection, 'connected');
});

test('snapshot updates replace durable state but preserve already received events', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'event',
    event: event('a-live', 5),
  });
  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a', [event('a-1', 1)]),
      run: { id: 'run-a', status: 'completed', log_dir: '/tmp/run-a' },
    },
  });

  assert.equal(state.snapshot?.run.status, 'completed');
  assert.deepEqual(state.events.map((item) => item.event_id), ['a-1', 'a-live']);
  assert.equal(state.snapshot?.diagnostics.last_event_id, 'a-live');
});

test('terminal lifecycle wins over a newer-looking running snapshot without events', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'snapshot',
    snapshot: snapshot('run-a', [event('a-1', 100)]),
  });
  const terminalSnapshot = {
    ...snapshot('run-a', []),
    run: { ...snapshot('run-a').run, status: 'completed', completion_satisfied: true },
    diagnostics: { ...snapshot('run-a').diagnostics, last_event_id: null, event_count: 0 },
  };
  state = reduceRunFeed(state, { type: 'snapshot', snapshot: terminalSnapshot });
  assert.equal(state.snapshot?.run.status, 'completed');
  assert.equal(state.snapshot?.run.completion_satisfied, true);
});

test('accepts a fresh server snapshot when its retained tail is shorter than the client buffer', () => {
  const retained = Array.from({ length: 400 }, (_, index) => event(`a-${index + 1}`, index + 1));
  let state = reduceRunFeed(createRunFeedState('run-a', 400), {
    type: 'seed',
    snapshot: snapshot('run-a', retained),
  });
  const serverTail = retained.slice(-200);
  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a', serverTail),
      run: { ...snapshot('run-a', serverTail).run, status: 'waiting_approval' },
      rounds: [{ round_index: 7, in_progress: true, next_step: 'approve' }],
      approvals: [{
        approval_id: 'approval-7', title: 'Review', message: '', options: [], answers: [],
        allow_input: false, input_label: '', context: {}, round_index: 7, status: 'pending',
        action: '', reason: '', user_input: '', created_at: 1, resolved_at: null,
      }],
    },
  });

  assert.equal(state.snapshot?.run.status, 'waiting_approval');
  assert.equal(state.snapshot?.rounds[0]?.round_index, 7);
  assert.equal(state.snapshot?.approvals[0]?.approval_id, 'approval-7');
  assert.equal(state.events.length, 400);
  assert.equal(state.lastEventId, 'a-400');
});

test('same cursor snapshots merge durable round output instead of regressing it', () => {
  const cursor = event('same-cursor', 10);
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: {
      ...snapshot('run-a', [cursor]),
      run: { ...snapshot('run-a').run, status: 'running', agent: 'codex', model: 'gpt-5.6-sol', final_response: 'Durable final reply' },
      active_round: 1,
      active_role: 'executor',
      rounds: [{
        round_index: 1,
        in_progress: true,
        roles: ['manager', 'executor'],
        plan_text: 'Inspect the workspace',
        executor_output: 'Changed src/app.ts',
        final_response: 'Durable final reply',
        role_sizes: { executor: 120 },
        executor_status: { status: 'completed', output: 'Changed src/app.ts' },
        final_response_status: { status: 'completed' },
      }],
    },
  });
  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a', [cursor]),
      run: { ...snapshot('run-a').run, status: 'running', agent: 'codex', model: null },
      active_round: null,
      active_role: null,
      rounds: [{ round_index: 1, in_progress: true, roles: ['executor'], role_sizes: { executor: 4 }, executor_status: {} }],
    },
  });
  const round = state.snapshot?.rounds[0];
  assert.equal(round?.plan_text, 'Inspect the workspace');
  assert.equal(round?.executor_output, 'Changed src/app.ts');
  assert.equal(round?.executor_status?.status, 'completed');
  assert.equal(round?.final_response, 'Durable final reply');
  assert.equal(round?.final_response_status?.status, 'completed');
  assert.equal(round?.role_sizes?.executor, 120);
  assert.equal(state.snapshot?.active_round, 1);
  assert.equal(state.snapshot?.run.model, 'gpt-5.6-sol');
  assert.equal(state.snapshot?.run.final_response, 'Durable final reply');
});

test('a resolved gate lets the run go back to running without a reload', () => {
  // A run toggles waiting_approval -> running at every gate, so this is normal
  // progress, not a stale frame. Treating it as a regression froze the UI on the
  // answered approval until the page was reloaded.
  const cursor = event('waiting-cursor', 11);
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: {
      ...snapshot('run-a', [cursor]),
      run: { ...snapshot('run-a').run, status: 'waiting_approval' },
    },
  });
  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a', [cursor]),
      run: { ...snapshot('run-a').run, status: 'running' },
    },
  });
  assert.equal(state.snapshot?.run.status, 'running');
});

test('a terminal status still outranks an active one on the same cursor', () => {
  const cursor = event('terminal-cursor', 12);
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: {
      ...snapshot('run-a', [cursor]),
      run: { ...snapshot('run-a').run, status: 'completed' },
    },
  });
  state = reduceRunFeed(state, {
    type: 'snapshot',
    snapshot: {
      ...snapshot('run-a', [cursor]),
      run: { ...snapshot('run-a').run, status: 'waiting_approval' },
    },
  });
  assert.equal(state.snapshot?.run.status, 'completed');
});

test('resync snapshots start a new event epoch and discard the old buffer', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: snapshot('run-a', [event('old-1', 1), event('old-2', 2)]),
  });

  const resyncSnapshot = {
    ...snapshot('run-a', [event('new-1', 10)]),
    diagnostics: {
      ...snapshot('run-a', [event('new-1', 10)]).diagnostics,
      cursor_gap: true,
      resync_required: true,
    },
  };
  state = reduceRunFeed(state, { type: 'snapshot', snapshot: resyncSnapshot });

  assert.deepEqual(state.events.map((item) => item.event_id), ['new-1']);
  assert.equal(state.lastEventId, 'new-1');
  assert.equal(state.snapshot?.diagnostics.cursor_gap, true);
  assert.equal(state.snapshot?.diagnostics.event_count, 1);
});

test('a gap replay is never merged by the reducer after the feed is reset', () => {
  let state = reduceRunFeed(createRunFeedState('run-a'), {
    type: 'seed',
    snapshot: snapshot('run-a', [event('old-1', 1)]),
  });

  // This models the Web hook's gap branch: reset first, then wait for the
  // authoritative snapshot instead of dispatching the non-contiguous replay.
  state = reduceRunFeed(state, { type: 'reset', runId: 'run-a' });
  state = reduceRunFeed(state, { type: 'snapshot', snapshot: snapshot('run-a', [event('new-1', 10)]) });

  assert.deepEqual(state.events.map((item) => item.event_id), ['new-1']);
  assert.equal(state.events.some((item) => item.event_id === 'old-1'), false);
});

test('parses the shared command catalog and gates only explicit capabilities', () => {
  assert.deepEqual(parseCommand('/attach run-a extra'), { name: 'attach', args: ['run-a', 'extra'], raw: '/attach run-a extra' });
  assert.deepEqual(parseCommand('/new inspect screenshots --agent codex --model gpt-5.6-sol --rounds 4'), {
    name: 'new',
    args: ['inspect', 'screenshots', '--agent', 'codex', '--model', 'gpt-5.6-sol', '--rounds', '4'],
    raw: '/new inspect screenshots --agent codex --model gpt-5.6-sol --rounds 4',
  });
  assert.equal(parseCommand('plain text'), null);
  const commands = availableCommands({}, snapshot('run-a'), true);
  assert.equal(commands.some((item) => item.name === 'inject'), true);
  assert.equal(availableCommands({ injections: false }, snapshot('run-a'), true).some((item) => item.name === 'inject'), false);
  assert.match(commandHelp({}, snapshot('run-a'), true), /\/trajectory/);
});

test('keeps /abort typable while a stop is in flight', () => {
  const base = snapshot('run-a');
  // `can_abort` is false once the run is stopping, but escalating an ignored
  // SIGTERM is exactly what the operator needs at that point.
  const stopping = {
    ...base,
    run: { ...base.run, status: 'stopping' },
    controls: { can_inject: false, can_abort: false, can_resume: false },
  };
  assert.equal(availableCommands({}, stopping, true).some((item) => item.name === 'abort'), true);
  assert.equal(availableCommands({}, stopping, true).some((item) => item.name === 'stop'), false);

  const terminal = {
    ...base,
    run: { ...base.run, status: 'cancelled' },
    controls: { can_inject: false, can_abort: false, can_resume: true },
  };
  assert.equal(availableCommands({}, terminal, true).some((item) => item.name === 'abort'), false);
  assert.equal(availableCommands({ abort: false }, stopping, true).some((item) => item.name === 'abort'), false);
});

test('parses /new options consistently and rejects malformed flags', () => {
  assert.deepEqual(parseNewRunArgs([
    'inspect', 'screenshots', '--agent', 'claude_code', '--model=gpt-test',
    '--workspace', '/tmp/work', '--rounds=4', '--language', 'zh',
  ]), {
    task: 'inspect screenshots',
    agent: 'claude_code',
    model: 'gpt-test',
    workspace: '/tmp/work',
    maxRounds: 4,
    promptLanguage: 'zh',
  });
  assert.deepEqual(parseNewRunArgs(['do', '--', '--model', 'as task text']), {
    task: 'do --model as task text',
  });
  assert.deepEqual(parseNewRunArgs([
    'mixed', '--manager-agent', 'codex', '--manager-model=gpt-manager',
    '--executor-agent=claude_code', '--executor-model', 'sonnet',
    '--auditor-agent', 'codex', '--auditor-model', 'gpt-auditor',
  ]), {
    task: 'mixed',
    roles: {
      manager: { agent: 'codex', model: 'gpt-manager' },
      executor: { agent: 'claude_code', model: 'sonnet' },
      auditor: { agent: 'codex', model: 'gpt-auditor' },
    },
  });
  assert.match(parseNewRunArgs(['task', '--model']).error || '', /需要一个值/);
  assert.match(parseNewRunArgs(['task', '--rounds', '0']).error || '', /1–1000/);
  assert.match(parseNewRunArgs(['task', '--rounds=1.5']).error || '', /整数/);
  assert.match(parseNewRunArgs(['task', '--language', 'fr']).error || '', /zh 或 en/);
});

test('/new applies a global reasoning effort to every role and allows per-role overrides', () => {
  assert.deepEqual(parseNewRunArgs(['task', '--effort', 'high']), {
    task: 'task',
    roles: {
      manager: { reasoning_effort: 'high' },
      executor: { reasoning_effort: 'high' },
      auditor: { reasoning_effort: 'high' },
    },
  });
  assert.deepEqual(parseNewRunArgs([
    'task', '--effort=medium', '--manager-effort', 'ultra',
  ]), {
    task: 'task',
    roles: {
      manager: { reasoning_effort: 'ultra' },
      executor: { reasoning_effort: 'medium' },
      auditor: { reasoning_effort: 'medium' },
    },
  });
});

test('/new rejects effort values that could break out of a TOML string or argv', () => {
  // The value reaches Codex as inline TOML and the other backends as argv.
  for (const bad of ['a"b', "a'b", 'high low', 'x'.repeat(65), '$(id)']) {
    assert.match(parseNewRunArgs(['task', `--effort=${bad}`]).error || '', /只能包含/, bad);
  }
  // An unknown tier is not rejected: the accepted set differs per backend and
  // per model, and a newer tier must not need a harness release.
  assert.equal(parseNewRunArgs(['task', '--effort=ultra']).error, undefined);
  assert.equal(parseNewRunArgs(['task', '--effort=a.b_c:d-1']).error, undefined);
});

test('keeps Web panel transitions deterministic', () => {
  let state = DEFAULT_PANEL_STATE;
  state = reducePanelState(state, { type: 'open', panel: 'events' });
  assert.deepEqual(state, { open: true, panel: 'events' });
  state = reducePanelState(state, { type: 'toggle', panel: 'events' });
  assert.equal(state.open, false);
  state = reducePanelState(state, { type: 'toggle', panel: 'trajectory' });
  assert.deepEqual(state, { open: true, panel: 'trajectory' });
});

test('projects bounded run status with role and approval summaries', () => {
  const base = snapshot('run-a', [
    { ...event('r1-start', 1, 'run-a', 'round.manager.started'), role: 'manager' },
    { ...event('r1-done', 2, 'run-a', 'round.recorded'), role: null },
  ]);
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'waiting_approval' },
    active_round: 2,
    active_role: 'executor',
    rounds: [
      { round_index: 1, in_progress: false, roles: ['manager', 'executor'], manager_status: { status: 'completed' } },
      { round_index: 2, in_progress: true, roles: ['manager', 'executor'], active_role: 'executor', executor_status: { status: 'running' } },
    ],
    approvals: [{ approval_id: 'a-1', title: '', message: '', options: [], answers: [], allow_input: false, input_label: '', context: {}, round_index: 2, status: 'pending', action: '', reason: '', user_input: '', created_at: 0, resolved_at: null }],
    diagnostics: { ...base.diagnostics, warnings: ['old', 'new'] },
  });

  assert.deepEqual(view.rounds.map((item) => item.round), [1, 2]);
  assert.equal(view.activeRole, 'executor');
  assert.equal(view.pendingApprovals.length, 1);
  assert.equal(view.roles.find((item) => item.key === 'executor')?.status, 'active');
  assert.equal(view.progress.total, view.stages.filter((item) => item.key !== 'record' && item.status !== 'skipped').length);
  assert.deepEqual(view.warnings.slice(0, 2), ['old', 'new']);
});

test('does not project an idle run as active work', () => {
  const view = projectStatus({
    ...snapshot('run-idle'),
    run: { ...snapshot('run-idle').run, status: 'idle' },
  });
  assert.equal(view.status, 'pending');
  assert.equal(view.nextStepKey, null);
});

test('deduplicates a provider failure repeated by the role status', () => {
  const base = snapshot('run-provider-failed');
  const message = "模型不可用：The 'bad-model' model is not supported.";
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'failed', failure_reason: message },
    rounds: [{
      round_index: 1,
      in_progress: false,
      next_step: 'blocked',
      harness_feedback: message,
      manager_status: { status: 'error', error: message },
    }],
  });

  assert.deepEqual(view.warnings, [message]);
});

test('terminal rounds do not regress to active from stale start events', () => {
  const base = snapshot('run-failed', [
    { ...event('manager-start', 1, 'run-failed', 'round.manager.started'), role: 'manager' },
  ]);
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'failed' },
    active_round: 1,
    active_role: 'manager',
    rounds: [{
      round_index: 1,
      in_progress: false,
      next_step: '修复失败命令',
      plan_text: '执行截图检查',
      executor_output: '执行完成',
      auditor_report: '发现命令失败',
    }],
  });
  assert.equal(view.roles.find((item) => item.key === 'manager')?.status, 'done');
  assert.equal(view.nextStep, '修复失败命令');
  assert.equal(view.nextStepKey, 'custom');
});

test('successful manager-only terminal round is not inflated with fake executor/auditor work', () => {
  const base = snapshot('run-complete', [
    { ...event('r1-manager', 1, 'run-complete', 'round.manager.started'), role: 'manager' },
    { ...event('r1-executor', 2, 'run-complete', 'round.executor.completed'), role: 'executor', status: 'completed' },
    { ...event('r1-auditor', 3, 'run-complete', 'round.auditor.completed'), role: 'auditor', status: 'completed' },
  ]);
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'completed', completion_satisfied: true, completion_authority: 'manager_with_role_auditors' },
    active_round: null,
    active_role: null,
    rounds: [
      { round_index: 1, in_progress: false, next_step: 'done', plan_text: '执行并验证', executor_output: '执行完成', auditor_report: 'Status: complete' },
      { round_index: 2, in_progress: false, next_step: 'done', plan_text: '任务已完成' },
    ],
  });

  const latest = view.rounds.find((round) => round.round === 2);
  assert.deepEqual(latest?.stages.map((stage) => stage.key), ['manager', 'record']);
  assert.equal(view.activeRound, null);
  assert.equal(view.currentRound, 2);
  assert.equal(view.current, null);
  assert.equal(view.progress.ratio, 1);
  assert.equal(view.progress.completed, view.progress.total);
  assert.equal(view.nextStepKey, 'done');
  assert.equal(view.roles.find((role) => role.key === 'executor')?.status, 'done');
  assert.equal(view.roles.find((role) => role.key === 'auditor')?.status, 'done');
});

test('projects the final user reply as a distinct completed role', () => {
  const base = snapshot('run-complete', [
    { ...event('reply-done', 4, 'run-complete', 'round.final_response.completed'), role: 'final_response', status: 'completed' },
  ]);
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'completed', completion_satisfied: true, final_response: 'Everything requested is now implemented.' },
    rounds: [{
      round_index: 1,
      in_progress: false,
      next_step: 'done',
      plan_text: 'Finish the task',
      final_response: 'Everything requested is now implemented.',
      final_response_status: { status: 'done' },
      roles: ['manager', 'final_response'],
    }],
  });

  assert.equal(view.finalResponse, 'Everything requested is now implemented.');
  assert.equal(view.stages.find((stage) => stage.key === 'final_response')?.status, 'done');
  assert.equal(view.roles.find((role) => role.key === 'final_response')?.status, 'done');
});

test('planned but never-started terminal stages are explicit skipped evidence', () => {
  const base = snapshot('run-stop');
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'completed' },
    rounds: [{ round_index: 1, in_progress: false, next_step: 'cli', plan_text: '准备执行' }],
  });
  const stages = view.rounds[0].stages.filter((stage) => stage.key !== 'record');
  assert.deepEqual(stages.map((stage) => stage.status), ['done', 'skipped', 'skipped']);
  assert.equal(view.progress.total, 1);
  assert.equal(view.progress.ratio, 1);
});

test('a stopping lifecycle does not leave stale roles looking active', () => {
  const base = snapshot('run-stop', [
    { ...event('manager-start', 10, 'run-stop', 'round.manager.started'), role: 'manager' },
  ]);
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'stopping' },
    active_round: 1,
    active_role: 'manager',
    rounds: [{ round_index: 1, in_progress: true, next_step: 'cli', plan_text: '准备执行' }],
  });
  assert.equal(view.status, 'stopping');
  assert.equal(view.current?.status, 'stopping');
  assert.equal(view.nextStepKey, 'stopping');
});

test('terminal lifecycle hides stale pending approvals from the actionable projection', () => {
  const base = snapshot('run-terminal');
  const approval = {
    approval_id: 'stale-approval', title: 'Continue?', message: 'old checkpoint',
    options: [], answers: [], allow_input: false, input_label: '', context: {},
    round_index: 1, status: 'pending', action: '', reason: '', user_input: '',
    created_at: 1, resolved_at: null,
  } as const;
  const terminal = projectStatus({
    ...base,
    run: { ...base.run, status: 'completed' },
    approvals: [approval],
    rounds: [{ round_index: 1, in_progress: false, plan_text: '任务已完成', next_step: 'done' }],
  });
  const stopping = projectStatus({
    ...base,
    run: { ...base.run, status: 'stopping' },
    approvals: [approval],
    rounds: [{ round_index: 1, in_progress: true, plan_text: '正在收尾', next_step: 'cli' }],
  });
  assert.equal(terminal.approvals.length, 1);
  assert.equal(terminal.pendingApprovals.length, 0);
  assert.equal(stopping.pendingApprovals.length, 0);
  // A stale record carries no operator decision, so it must not be mistaken for
  // a fresh answer still travelling to the worker.
  assert.equal(stopping.awaitingHandoff, false);
});

test('a submitted decision is projected as awaiting the worker handoff', () => {
  const base = snapshot('run-handoff');
  const approval = {
    approval_id: 'a-1', title: 'Continue?', message: 'checkpoint',
    options: [], answers: [], allow_input: false, input_label: '', context: {},
    round_index: 1, action: '', reason: '', user_input: '',
    created_at: 1,
  };
  // The lifecycle still reports `waiting_approval` because only the worker can
  // clear it, so the answered state is the client's own optimistic record.
  const answered = projectStatus({
    ...base,
    run: { ...base.run, status: 'waiting_approval' },
    approvals: [{ ...approval, status: 'resolved', action: 'continue', resolved_at: 2 }],
  });
  assert.equal(answered.pendingApprovals.length, 0);
  assert.equal(answered.awaitingHandoff, true);

  const unanswered = projectStatus({
    ...base,
    run: { ...base.run, status: 'waiting_approval' },
    approvals: [{ ...approval, status: 'pending', resolved_at: null }],
  });
  assert.equal(unanswered.pendingApprovals.length, 1);
  assert.equal(unanswered.awaitingHandoff, false);

  // Once the worker picks the decision up the run reports its own progress, so
  // this placeholder must disappear instead of stacking with "Manager is working".
  const running = projectStatus({
    ...base,
    run: { ...base.run, status: 'running' },
    approvals: [{ ...approval, status: 'resolved', action: 'continue', resolved_at: 2 }],
  });
  assert.equal(running.awaitingHandoff, false);
});

test('terminal manager-only runs mark never-invoked roles as skipped', () => {
  const base = snapshot('manager-only');
  const view = projectStatus({
    ...base,
    run: { ...base.run, status: 'completed' },
    rounds: [{ round_index: 1, in_progress: false, plan_text: '已确认任务无需执行', next_step: 'done' }],
  });
  assert.deepEqual(view.roles.map((role) => [role.key, role.status]), [
    ['manager', 'done'], ['executor', 'skipped'], ['auditor', 'skipped'],
  ]);
});

test('projects trajectory text, images, errors, filters, and bounds', () => {
  const view = projectTrajectoryView({
    run_id: 'run-a', round_index: 3, role: 'executor', steps: [
      { kind: 'session', text: 'ignore me' },
      { kind: 'tool_use', input: { command: 'ls -la' } },
      { kind: 'tool_result', text: 'x'.repeat(20), images: ['a.png', 'a.png'] },
      { kind: 'error', text: 'failed' },
    ],
  }, { maxSteps: 2, maxTextChars: 8, includeKinds: ['tool_result', 'error'] });

  assert.equal(view.truncated, false);
  assert.equal(view.totalSteps, 2);
  assert.deepEqual(view.items.map((item) => item.kind), ['tool_result', 'error']);
  assert.equal(view.items[0].text, 'xxxxxxx…');
  assert.deepEqual(view.items[0].images, ['a.png']);
  assert.equal(view.items[1].isError, true);
  assert.equal(view.items[0].searchText.includes('tool_result'), true);
});

test('classifies provider reconnect/deprecation chatter as non-work noise', () => {
  assert.equal(isTrajectoryNoise('[features].codex_hooks is deprecated. Use [features].hooks instead.'), true);
  assert.equal(isTrajectoryNoise('Reconnecting...'), true);
  assert.equal(isTrajectoryNoise('Reconnecting... 1/5 (stream disconnected before completion)'), true);
  assert.equal(isTrajectoryNoise('RECONNECTING… retrying transport'), true);
  assert.equal(isTrajectoryNoise('`[features].codex_hooks` is deprecated. Use `[features].hooks` instead.'), true);
  assert.equal(isTrajectoryNoise('pytest: 3 passed'), false);
});

test('normalizes all supported single-image trajectory fields', () => {
  const view = projectTrajectoryView({
    round_index: 1,
    role: 'executor',
    steps: [
      { kind: 'tool_result', image: 'data:image/png;base64,a' },
      { kind: 'tool_result', image_url: 'https://example.test/shot.png' },
      { kind: 'tool_result', imageUrl: '/api/shot.png' },
    ],
  });

  assert.deepEqual(view.items.map((item) => item.images), [
    ['data:image/png;base64,a'],
    ['https://example.test/shot.png'],
    ['/api/shot.png'],
  ]);
  assert.equal(view.items.every((item) => item.hasImage), true);
});

test('marks non-zero command exits as failed even when the adapter omitted is_error', () => {
  const view = projectTrajectoryView({
    round_index: 1,
    role: 'executor',
    steps: [
      { kind: 'tool_result', text: 'command output\n[exit_code=2]' },
      { kind: 'tool_result', exit_code: 1, text: 'failed command' },
      { kind: 'tool_result', exit_code: 0, text: 'ok' },
    ],
  });
  assert.equal(view.items[0].isError, true);
  assert.equal(view.items[1].isError, true);
  assert.equal(view.items[2].isError, false);
});

test('projects structured agent file changes and enriches matching paths with exact numstat', () => {
  const view = projectArtifactView({
    run_id: 'run-a', round_index: 1, role: 'executor', steps: [
      {
        kind: 'tool_use', id: 'patch-1', name: 'apply_patch', input: {
          changes: [
            { path: 'frontend/core/src/artifactView.ts', kind: 'add' },
            { path: 'frontend/core/src/index.ts', kind: 'update' },
          ],
        },
      },
      { kind: 'tool_result', tool_use_id: 'patch-1', text: 'Done', is_error: false },
      { kind: 'tool_use', id: 'diff-1', name: 'shell', input: { command: 'git diff --numstat' } },
      {
        kind: 'tool_result', tool_use_id: 'diff-1', is_error: false,
        text: '140\t0\tfrontend/core/src/artifactView.ts\n1\t0\tfrontend/core/src/index.ts\n99\t2\tunrelated-user-file.ts\n[exit_code=0]',
      },
    ],
  });

  assert.equal(view.files.scope, 'agent');
  assert.equal(view.files.totalFiles, 2);
  assert.equal(view.files.additions, 141);
  assert.equal(view.files.deletions, 0);
  assert.equal(view.files.lineCountCoverage, 'complete');
  assert.deepEqual(view.files.items.map((item) => [item.path, item.kind, item.extension]), [
    ['frontend/core/src/artifactView.ts', 'added', 'ts'],
    ['frontend/core/src/index.ts', 'modified', 'ts'],
  ]);
});

test('does not present an unmatched workspace diff as exact agent line totals', () => {
  const view = projectArtifactView({
    round_index: 1, role: 'executor', steps: [
      { kind: 'tool_use', id: 'edit-1', name: 'Edit', input: { file_path: 'src/main.py' } },
      { kind: 'tool_result', tool_use_id: 'edit-1', text: 'updated' },
      { kind: 'tool_use', id: 'stat-1', name: 'Bash', input: { command: 'git diff --stat' } },
      {
        kind: 'tool_result', tool_use_id: 'stat-1',
        text: ' src/main.py | 4 ++++\n old-user-change.txt | 2 ++\n 2 files changed, 6 insertions(+)',
      },
    ],
  });

  assert.equal(view.files.scope, 'agent');
  assert.equal(view.files.totalFiles, 1);
  assert.equal(view.files.additions, null);
  assert.equal(view.files.lineCountCoverage, 'partial');
});

test('falls back to a bounded workspace diff when no structured file event exists', () => {
  const view = projectArtifactView({
    round_index: 2, role: 'executor', steps: [
      { kind: 'tool_use', id: 'stat-1', name: 'shell', input: { command: "/bin/zsh -lc 'git diff --stat'" } },
      {
        kind: 'tool_result', tool_use_id: 'stat-1',
        text: ' frontend/core/src/a.ts | 3 ++-\n frontend/web/src/App.tsx | 9 ++++++---\n 2 files changed, 8 insertions(+), 4 deletions(-)',
      },
    ],
  }, { maxFiles: 1 });

  assert.equal(view.files.scope, 'workspace');
  assert.equal(view.files.totalFiles, 2);
  assert.equal(view.files.shownFiles, 1);
  assert.equal(view.files.hiddenFiles, 1);
  assert.equal(view.files.additions, 8);
  assert.equal(view.files.deletions, 4);
});

test('projects paired tests, builds, compound checks, failures, and live validation state', () => {
  const view = projectArtifactView({
    run_id: 'run-a', round_index: 3, role: 'executor', steps: [
      { kind: 'tool_use', id: 'py', name: 'shell', input: { command: 'python -m pytest -q' } },
      { kind: 'tool_result', tool_use_id: 'py', text: '............ [100%]\n12 passed, 1 skipped in 0.62s\n[exit_code=0]' },
      { kind: 'tool_use', id: 'web', name: 'shell', input: { command: 'npm --prefix frontend/web run build' } },
      { kind: 'tool_result', tool_use_id: 'web', text: '✓ 40 modules transformed.\n[exit_code=0]' },
      { kind: 'tool_use', id: 'checks', name: 'Bash', input: { command: 'cd frontend/web && npm run typecheck && npm run build' } },
      { kind: 'tool_result', tool_use_id: 'checks', text: '{"output":"built successfully","exit_code":0,"error":null}' },
      { kind: 'tool_use', id: 'bad', name: 'shell', input: { command: 'npm --prefix frontend/core test' } },
      { kind: 'tool_result', tool_use_id: 'bad', text: '# tests 10\n# pass 8\n# fail 2\n[exit_code=1]', is_error: true },
      { kind: 'tool_use', id: 'live', name: 'shell', input: { command: 'git diff --check' } },
      { kind: 'tool_use', id: 'search', name: 'shell', input: { command: "rg -n 'pytest|passed|typecheck' src" } },
      { kind: 'tool_result', tool_use_id: 'search', text: 'src/example.py:1: pytest' },
    ],
  });

  assert.equal(view.validations.total, 5);
  assert.deepEqual(view.validations.items.map((item) => [item.label, item.operations, item.status]), [
    ['Python', ['test'], 'passed'],
    ['Web', ['build'], 'passed'],
    ['Web', ['typecheck', 'build'], 'passed'],
    ['Core', ['test'], 'failed'],
    ['Git', ['diff-check'], 'running'],
  ]);
  assert.equal(view.validations.items[0].passedCount, 12);
  assert.equal(view.validations.items[0].skippedCount, 1);
  assert.equal(view.validations.items[1].moduleCount, 40);
  assert.equal(view.validations.items[3].failedCount, 2);
  assert.deepEqual(
    { passed: view.validations.passed, failed: view.validations.failed, running: view.validations.running },
    { passed: 3, failed: 1, running: 1 },
  );
});

test('uses the final aggregate test line instead of summing nested runner summaries', () => {
  const view = projectArtifactView({
    round_index: 4,
    role: 'executor',
    steps: [
      { kind: 'tool_use', id: 'pytest', name: 'shell', input: { command: 'python -m pytest -q' } },
      {
        kind: 'tool_result',
        tool_use_id: 'pytest',
        text: 'tests/unit/test_a.py ........ 2 passed\ntests/unit/test_b.py .. 2 passed\n4 passed, 1 skipped in 0.31s\n[exit_code=0]',
      },
    ],
  });
  const result = view.validations.items[0];
  assert.equal(result.status, 'passed');
  assert.equal(result.passedCount, 4);
  assert.equal(result.skippedCount, 1);
});

test('does not claim success from stdout counts when the exit status is unavailable', () => {
  const view = projectArtifactView({
    round_index: 5,
    role: 'executor',
    steps: [
      { kind: 'tool_use', id: 'pytest', name: 'shell', input: { command: 'python -m pytest -q' } },
      { kind: 'tool_result', tool_use_id: 'pytest', text: '12 passed, 1 skipped in 0.62s' },
    ],
  });
  const result = view.validations.items[0];
  assert.equal(result.status, 'unknown');
  assert.equal(result.passedCount, 12);
  assert.equal(view.validations.unknown, 1);
});

test('accepts an explicit protocol success status even without an exit code', () => {
  const view = projectArtifactView({
    round_index: 6,
    role: 'executor',
    steps: [
      { kind: 'tool_use', id: 'build', name: 'shell', input: { command: 'npm run build' } },
      { kind: 'tool_result', tool_use_id: 'build', status: 'completed', text: 'build finished' },
    ],
  });
  assert.equal(view.validations.items[0].status, 'passed');
});

test('coalesces labelled TAP/Bun counters without dropping pass counts', () => {
  const view = projectArtifactView({
    round_index: 7,
    role: 'executor',
    steps: [
      { kind: 'tool_use', id: 'node', name: 'shell', input: { command: 'npm test' } },
      {
        kind: 'tool_result',
        tool_use_id: 'node',
        text: 'ℹ tests 33\nℹ suites 0\nℹ pass 31\nℹ fail 2\nℹ skipped 0\n[exit_code=1]',
      },
    ],
  });
  const result = view.validations.items[0];
  assert.equal(result.status, 'failed');
  assert.equal(result.passedCount, 31);
  assert.equal(result.failedCount, 2);
});
