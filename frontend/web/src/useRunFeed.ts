import { useCallback, useEffect, useReducer } from 'react';
import {
  createRunFeedState,
  reduceRunFeed,
  type FeedConnectionStatus,
} from '../../core/src/runFeed';
import type { OperatorMessage, Snapshot } from '../../core/src/types';
import { fetchEvents, fetchSnapshot, isNotFound, isUnauthorized, streamRun } from './api';
import { useUiLanguage } from './i18n';

export const EMPTY_SNAPSHOT: Snapshot = {
  schema_version: 1,
  run: { id: '', status: 'idle', log_dir: '' },
  mission: { task: '', contract_path: '', plan_path: '', verified_state_path: '', report_path: '' },
  rounds: [],
  active_round: null,
  active_role: null,
  events: [],
  approvals: [],
  operator_messages: [],
  controls: { can_inject: false, can_abort: false, can_resume: false },
  diagnostics: { last_event_id: null, event_count: 0, warnings: [] },
};

export interface RunFeedHandle {
  snapshot: Snapshot;
  events: Snapshot['events'];
  connection: FeedConnectionStatus;
  error: string;
  refresh: () => Promise<void>;
  appendOperatorMessage: (message: OperatorMessage) => void;
  recordApprovalResponse: (approvalId: string, action: string, userInput: string, resolvedAt: number) => void;
}

/**
 * LongHorizon's run-scoped live feed.
 *
 * REST data is loaded in parallel with the WebSocket. Both paths can replay
 * the same records, so the shared reducer performs the dedupe and run guard.
 */
export function useRunFeed(runId: string, authRevision = 0): RunFeedHandle {
  const { text } = useUiLanguage();
  const [feed, dispatch] = useReducer(
    reduceRunFeed,
    runId || null,
    (initialRunId) => createRunFeedState(initialRunId),
  );
  const [error, setError] = useReducer((_: string, next: string) => next, '');
  const refresh = useCallback(async () => {
    if (!runId) return;
    const snapshot = await fetchSnapshot(runId);
    dispatch({ type: 'snapshot', snapshot });
  }, [runId]);
  const appendOperatorMessage = useCallback((message: OperatorMessage) => {
    if (!runId) return;
    dispatch({ type: 'operator_message', runId, message });
  }, [runId]);
  const recordApprovalResponse = useCallback((approvalId: string, action: string, userInput: string, resolvedAt: number) => {
    if (!runId) return;
    dispatch({ type: 'approval_response', runId, approvalId, action, userInput, resolvedAt });
  }, [runId]);

  useEffect(() => {
    dispatch({ type: 'reset', runId: runId || null });
    setError('');
    if (!runId) return undefined;

    let cancelled = false;
    let feedEpoch = 0;
    const accept = <T,>(promise: Promise<T>, onValue: (value: T) => void, epoch = feedEpoch) => {
      void promise.then((value) => {
        if (!cancelled && epoch === feedEpoch) onValue(value);
      }).catch((reason: unknown) => {
        if (cancelled || epoch !== feedEpoch) return;
        if (isNotFound(reason)) {
          // REST often reports a stale persisted selection before the
          // WebSocket upgrade can return its 4404 close code.  Clear the run
          // immediately so a fresh server opens on the welcome screen instead
          // of reconnecting forever to an id from another runs root.
          window.dispatchEvent(new CustomEvent('lh-run-not-found'));
          return;
        }
        setError(isUnauthorized(reason) ? '401 invalid or missing bearer token' : String(reason));
      });
    };

    const initialEpoch = feedEpoch;
    accept(fetchSnapshot(runId), (snapshot) => dispatch({ type: 'snapshot', snapshot }), initialEpoch);
    accept(fetchEvents(runId, null, 500), (replay) => {
      if (replay.resync_required || replay.cursor_gap) {
        setError(text('事件游标已轮转，正在从最新快照重新同步。', 'The event cursor has rotated. Resyncing from the latest snapshot.'));
        // A gap starts a new event epoch. Drop the old buffer and do not merge
        // this non-contiguous replay before the authoritative snapshot arrives.
        feedEpoch += 1;
        const resyncEpoch = feedEpoch;
        dispatch({ type: 'reset', runId });
        accept(fetchSnapshot(runId), (snapshot) => dispatch({ type: 'snapshot', snapshot }), resyncEpoch);
        return;
      }
      dispatch({ type: 'replay', events: replay.events });
    }, initialEpoch);

    const cleanup = streamRun(
      runId,
      null,
      (snapshot) => dispatch({ type: 'snapshot', snapshot }),
      (event) => dispatch({ type: 'event', event }),
      (status) => {
        dispatch({ type: 'connection', runId, status });
        if (status === 'connected') setError('');
      },
      {
        replay: 100,
        reconnect: true,
        onResync: () => {
          // A WebSocket gap invalidates the parallel REST snapshot/replay as
          // well. Advance the same epoch used by `accept()` before clearing
          // the reducer so a slow initial response cannot restore old data.
          feedEpoch += 1;
          dispatch({ type: 'reset', runId });
        },
        onAuthError: () => setError('401 invalid or missing bearer token'),
        onRunNotFound: () => {
          setError(text('会话已不存在或已被移除，请从会话列表重新选择。', 'This task no longer exists or was removed. Select another task from the list.'));
          // Clear the selection so the next render leaves the stale feed and
          // its composer behind instead of reconnecting to a missing run.
          window.dispatchEvent(new CustomEvent('lh-run-not-found'));
        },
      },
    );
    return () => {
      cancelled = true;
      cleanup();
    };
  }, [runId, authRevision, text]);

  const current = feed.runId === (runId || null) ? feed : null;
  return {
    snapshot: current?.snapshot || EMPTY_SNAPSHOT,
    events: current?.events || [],
    connection: current?.connection || (runId ? 'loading' : 'closed'),
    error,
    refresh,
    appendOperatorMessage,
    recordApprovalResponse,
  };
}
