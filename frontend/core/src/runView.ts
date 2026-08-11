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
