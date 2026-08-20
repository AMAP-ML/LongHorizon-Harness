import type { EventEnvelope, RoundView, Snapshot } from './types';
import { phaseLabel } from './events';

export interface PhaseItem {
  key: string;
  label: string;
  status: 'done' | 'active' | 'pending' | 'failed';
  round: number | null;
  event?: EventEnvelope;
}

export function phaseTrail(snapshot: Snapshot): PhaseItem[] {
  const items: PhaseItem[] = [];
  const terminal = snapshot.run.status === 'failed' || snapshot.run.status === 'cancelled';
  for (const round of snapshot.rounds) {
    const roles: Array<readonly [string, string]> = [
      ['manager', 'round.manager'],
      ['executor', 'round.executor'],
      ['auditor', 'round.auditor'],
      ['record', 'round.recorded'],
    ];
    const hasFinalResponse = Boolean(round.final_response || round.final_response_status)
      || snapshot.active_role === 'final_response'
      || snapshot.events.some((event) => event.round === round.round_index && event.type.startsWith('round.final_response.'));
    if (hasFinalResponse) roles.splice(3, 0, ['final_response', 'round.final_response']);
    for (const [key, prefix] of roles) {
      const event = [...snapshot.events].reverse().find((candidate) =>
        candidate.round === round.round_index && (candidate.type === prefix || candidate.type.startsWith(`${prefix}.`)));
      const active = snapshot.active_round === round.round_index &&
        (key === 'record' ? round.in_progress === false : snapshot.active_role === key);
      const failed = terminal && active;
      items.push({
        key: `${round.round_index}:${key}`,
        label: key === 'record' ? 'Recorded' : key === 'final_response' ? 'Final reply' : key[0].toUpperCase() + key.slice(1),
        status: failed ? 'failed' : active ? 'active' : event?.status === 'completed' || key === 'record' && !round.in_progress ? 'done' : 'pending',
        round: round.round_index,
        event,
      });
    }
  }
  return items;
}

export function roundSummary(round: RoundView): string {
  const parts = [round.manager_status, round.executor_status, round.auditor_status, round.final_response_status]
    .map((status) => status?.status)
    .filter(Boolean)
    .map(String);
  return parts.join(' · ') || (round.in_progress ? 'In progress' : 'Recorded');
}

export type TranscriptKind = 'plan' | 'assistant' | 'verification' | 'live' | 'user' | 'final';

export interface TranscriptOrder {
  kind: TranscriptKind;
  round?: number;
  sortRound?: number;
  sortTime?: number;
}

/** Order inside one round for entries that carry no usable timestamp. */
const TRANSCRIPT_STAGE_RANK: Record<TranscriptKind, number> = {
  plan: 1, assistant: 2, verification: 3, live: 4, user: 5, final: 6,
};

/**
 * Order a transcript strictly by time, the way a chat log reads.
 *
 * A timestamped entry is placed by its own time, so a reply the operator sent
 * after an answer can never be shown above it. Entries without a time keep the
 * durable round/stage order and are anchored next to their round's timeline.
 */
export function sortTranscript<T extends TranscriptOrder>(items: readonly T[]): T[] {
  const timeline = new Map<number, number>();
  for (const item of items) {
    const round = item.round;
    const time = item.sortTime;
    if (round === undefined || !time) continue;
    // Anchor untimed entries to the earliest time seen in their round so they
    // stay adjacent to it instead of collapsing to the top of the transcript.
    const earliest = timeline.get(round);
    if (earliest === undefined || time < earliest) timeline.set(round, time);
  }
  const effectiveTime = (item: TranscriptOrder): number => {
    // Pinned entries win over their own timestamp: a resumed run restamps
    // `started_at` to the reopen time, which would drag the original request to
    // the bottom of the transcript.
    if (item.sortRound !== undefined) {
      if (item.sortRound < 0) return Number.NEGATIVE_INFINITY;
      if (item.sortRound === Number.MAX_SAFE_INTEGER) return Number.POSITIVE_INFINITY;
    }
    if (item.sortTime) return item.sortTime;
    const round = item.sortRound ?? item.round;
    return round === undefined ? Number.NEGATIVE_INFINITY : timeline.get(round) ?? Number.NEGATIVE_INFINITY;
  };
  return items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      const leftTime = effectiveTime(left.item);
      const rightTime = effectiveTime(right.item);
      if (leftTime !== rightTime) return leftTime - rightTime;
      const leftRound = left.item.sortRound ?? left.item.round ?? 0;
      const rightRound = right.item.sortRound ?? right.item.round ?? 0;
      if (leftRound !== rightRound) return leftRound - rightRound;
      const leftRank = TRANSCRIPT_STAGE_RANK[left.item.kind];
      const rightRank = TRANSCRIPT_STAGE_RANK[right.item.kind];
      if (leftRank !== rightRank) return leftRank - rightRank;
      return left.index - right.index;
    })
    .map((entry) => entry.item);
}

/**
 * Prefer the Manager's persisted plan over its internal route token.
 *
 * `next_step` is deliberately compact (`gui`, `cli`, `done`, ...), while
 * `plan_text` contains the operator-facing contract/state summary. Rendering
 * the route whenever it exists makes a real plan look like a one-word status
 * and causes Web surfaces to disagree about what the Manager decided.
 */
export function managerPlanText(round: RoundView): string {
  const plan = String(round.plan_text || '').trim();
  if (plan) return plan;
  const next = String(round.next_step || '').trim();
  return next ? `Next step: ${next}` : '';
}

/** Return the concrete subtask a Manager routed, not its leading state boilerplate. */
export function managerPlanSummary(text: string, limit = 900): string {
  const value = String(text || '').trim();
  if (!value) return '';
  const patterns = [
    /(?:^|\n)(?:Task|任务):\s*([\s\S]*?)(?=\n(?:Acceptance criteria|验收标准|Related audit reports|相关审计报告|Related audited state|相关已审计状态|Boundaries|边界|Next|下一步):|$)/iu,
    /(?:^|\n)(?:Question|问题):\s*([\s\S]*?)(?=\n(?:Choices|选项|Boundaries|边界|Next|下一步):|$)/iu,
    /(?:^|\n)(?:Reason|原因|阻塞原因):\s*([\s\S]*?)(?=\n(?:Boundaries|边界|Next|下一步):|$)/iu,
    /(?:^|\n)(?:Completed|已完成):\s*([\s\S]*?)(?=\n(?:Incomplete|未完成):|$)/iu,
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(value);
    const section = match?.[1]?.trim();
    if (section) return section.slice(0, Math.max(1, limit));
  }
  return value.slice(0, Math.max(1, limit));
}

export { phaseLabel };
