import type { EventEnvelope, Snapshot } from './types';

export function eventKey(event: EventEnvelope): string {
  return (typeof event?.event_id === 'string' && event.event_id)
    || `${String(event?.run_id || '')}:${String(event?.ts || 0)}:${String(event?.type || '')}`;
}

export function dedupeEvents(events: EventEnvelope[]): EventEnvelope[] {
  const seen = new Set<string>();
  return events
    .filter((event): event is EventEnvelope => Boolean(event && typeof event === 'object'))
    .filter((event) => {
      const key = eventKey(event);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((a, b) => {
      const aTs = typeof a.ts === 'number' && Number.isFinite(a.ts) ? a.ts : 0;
      const bTs = typeof b.ts === 'number' && Number.isFinite(b.ts) ? b.ts : 0;
      return (aTs - bTs) || eventKey(a).localeCompare(eventKey(b));
    });
}

export function mergeEvent(snapshot: Snapshot, event: EventEnvelope): Snapshot {
  return {
    ...snapshot,
    events: dedupeEvents([...snapshot.events, event]),
    diagnostics: {
      ...snapshot.diagnostics,
      last_event_id: event.event_id,
      event_count: dedupeEvents([...snapshot.events, event]).length,
    },
  };
}

export function phaseLabel(type: string): string {
  const labels: Record<string, string> = {
    'run.started': 'Run started',
    'run.completed': 'Run completed',
    'run.cancelled': 'Run cancelled',
    'run.failed': 'Run failed',
    'round.manager.started': 'Manager started',
    'round.manager.completed': 'Manager completed',
    'round.executor.started': 'Executor started',
    'round.executor.completed': 'Executor completed',
    'round.auditor.started': 'Auditor started',
    'round.auditor.completed': 'Auditor completed',
    'round.final_response.started': 'Final reply started',
    'round.final_response.completed': 'Final reply completed',
    'round.final_response.discarded': 'Final reply discarded',
    'round.recorded': 'Round recorded',
    'operator.approval.pending': 'Approval requested',
    'operator.approval.resolved': 'Approval resolved',
    'operator.instruction.queued': 'Instruction queued',
    'operator.instruction.applied': 'Instruction applied',
  };
  const safeType = typeof type === 'string' ? type : 'unknown event';
  return labels[safeType] || safeType.replaceAll('.', ' ');
}
