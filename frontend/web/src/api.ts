import type { ArtifactList, EventEnvelope, RunSummary, Snapshot } from '../../core/src/types';

const WEB_TOKEN_KEY = 'lh-web-token';
let volatileWebToken = '';

/** A fetch failure that keeps the HTTP status available to the UI. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function storedAuthToken(): string {
  // Prefer sessionStorage so a bearer is not left in persistent browser
  // storage.  Read the old localStorage key for backwards compatibility with
  // an operator who configured it before this setting was introduced.
  try {
    return volatileWebToken
      || window.sessionStorage.getItem(WEB_TOKEN_KEY)?.trim()
      || window.localStorage.getItem(WEB_TOKEN_KEY)?.trim()
      || '';
  } catch {
    return volatileWebToken;
  }
}

export function setStoredAuthToken(value: string): void {
  volatileWebToken = value.trim();
  try {
    if (volatileWebToken) {
      window.sessionStorage.setItem(WEB_TOKEN_KEY, volatileWebToken);
      window.localStorage.removeItem(WEB_TOKEN_KEY);
    } else {
      window.sessionStorage.removeItem(WEB_TOKEN_KEY);
      window.localStorage.removeItem(WEB_TOKEN_KEY);
    }
  } catch {
    // The module-level value still supports storage-disabled environments for
    // the lifetime of the current page.
  }
}

export function isUnauthorized(reason: unknown): boolean {
  return reason instanceof ApiError
    ? reason.status === 401
    : /(?:^|\s)401(?:\s|$)/u.test(String(reason ?? ''));
}

export function isNotFound(reason: unknown): boolean {
  return reason instanceof ApiError
    ? reason.status === 404
    : /(?:^|\s)404(?:\s|$)/u.test(String(reason ?? ''));
}

async function responseError(response: Response): Promise<ApiError> {
  let message = `${response.status} ${response.statusText}`.trim();
  try {
    const payload = await response.clone().json() as { detail?: unknown };
    if (payload && typeof payload.detail === 'string' && payload.detail.trim()) message = payload.detail.trim();
  } catch {
    try {
      const text = (await response.clone().text()).trim();
      if (text) message = text.slice(0, 500);
    } catch {
      // Keep the status-only fallback.
    }
  }
  return new ApiError(response.status, message);
}

export function idempotencyKey(prefix: string): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

export function authHeaders(): Record<string, string> {
  // Remote deployments may configure LH_HARNESS_WEB_TOKEN.  The packaged UI
  // cannot read process env, so an operator can paste the token into the
  // connection settings dialog.  Localhost deployments leave it empty and
  // preserve the zero-config experience.
  try {
    const token = storedAuthToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

const WS_AUTH_MARKER = 'lh-harness-auth.v1';

/** Encode the bearer for a WebSocket subprotocol (never put it in the URL). */
function websocketAuthProtocols(token: string): string[] {
  const bytes = new TextEncoder().encode(token);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const encoded = btoa(binary).replace(/\+/gu, '-').replace(/\//gu, '_').replace(/=+$/u, '');
  return [WS_AUTH_MARKER, `lh-harness-token.${encoded}`];
}

export interface WebMeta {
  service: string;
  api_version: string;
  protocol_version: string;
  capabilities: Record<string, boolean>;
  endpoint: string;
  /** Server-discovered agent/model catalogue. Older servers may omit these. */
  agents?: Array<{ id: string; label?: string; available?: boolean; default_model?: string; models?: ModelChoice[]; discovery?: ModelDiscovery }>;
  models?: Record<string, ModelChoice[]>;
  defaults?: { agent?: string; model?: string; effort_levels?: Record<string, string[]>; roles?: Record<string, RoleRuntimeConfig> };
  model_discovery?: Record<string, ModelDiscovery>;
}

export interface ModelChoice { id: string; label?: string; availability?: string; is_default?: boolean; reasoning_efforts?: string[] }
export interface ModelDiscovery { status?: string; source?: string; account_scoped?: boolean; refreshed_at?: string | number | null; warning?: string }
export interface RoleRuntimeConfig { agent: string; model?: string; effort?: string }

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: authHeaders() });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export async function fetchRuns(): Promise<RunSummary[]> {
  const data = await getJson<{ runs: RunSummary[] }>('/api/runs');
  return data.runs;
}

export function fetchMeta(): Promise<WebMeta> {
  return getJson<WebMeta>('/api/meta');
}

export function fetchSnapshot(runId: string): Promise<Snapshot> {
  return getJson<Snapshot>(`/api/runs/${encodeURIComponent(runId)}/snapshot`);
}

export interface EventReplay {
  run_id: string;
  events: EventEnvelope[];
  last_event_id: string | null;
  cursor_gap?: boolean;
  resync_required?: boolean;
  diagnostics?: { warnings?: string[] };
}

export function fetchEvents(runId: string, after: string | null = null, limit = 500): Promise<EventReplay> {
  const query = new URLSearchParams({ limit: String(Math.max(1, Math.min(5000, Math.floor(limit)))) });
  if (after) query.set('after', after);
  return getJson<EventReplay>(`/api/runs/${encodeURIComponent(runId)}/events?${query}`);
}

export function fetchArtifacts(runId: string, round: number): Promise<ArtifactList> {
  return getJson<ArtifactList>(`/api/runs/${encodeURIComponent(runId)}/rounds/${round}/artifacts`);
}

export interface TrajectoryView {
  round_index: number;
  role: string;
  step_count: number;
  raw_chars: number;
  steps: TrajectoryStep[];
  warning?: string;
  steps_truncated?: boolean;
}

export interface TrajectoryStep extends Record<string, unknown> {
  kind?: string;
  type?: string;
  text?: string;
  input?: Record<string, unknown>;
  images?: string[];
  image?: string;
  image_url?: string;
  imageUrl?: string;
  has_image?: boolean;
  is_error?: boolean;
  status?: string;
  exit_code?: number | string | null;
  exitCode?: number | string | null;
}

export function fetchTrajectory(runId: string, round: number, role: string): Promise<TrajectoryView> {
  return getJson<TrajectoryView>(`/api/runs/${encodeURIComponent(runId)}/rounds/${round}/trajectory/${encodeURIComponent(role)}`);
}

export async function fetchArtifact(runId: string, round: number, name: string): Promise<string> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/rounds/${round}/artifacts/${encodeURIComponent(name)}`, { headers: authHeaders() });
  if (!response.ok) throw await responseError(response);
  return response.text();
}

export function artifactRawUrl(runId: string, round: number, name: string): string {
  return `/api/runs/${encodeURIComponent(runId)}/rounds/${round}/artifacts/${encodeURIComponent(name)}/raw`;
}

/** Load a same-origin image with bearer headers without putting the token in a URL. */
export async function fetchImageSource(source: string): Promise<string> {
  if (!source.startsWith('/api/')) return source;
  const response = await fetch(source, { headers: authHeaders() });
  if (!response.ok) throw await responseError(response);
  return URL.createObjectURL(await response.blob());
}

export async function postInstruction(runId: string, instructions: string, requestKey = idempotencyKey('instruction')): Promise<void> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/instructions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders(), 'Idempotency-Key': requestKey }, body: JSON.stringify({ instructions }),
  });
  if (!response.ok) throw await responseError(response);
}

export async function resolveApproval(runId: string, approvalId: string, action: string, userInput = '', requestKey = idempotencyKey('approval')): Promise<void> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/resolve`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders(), 'Idempotency-Key': requestKey }, body: JSON.stringify({ action, user_input: userInput }),
  });
  if (!response.ok) throw await responseError(response);
}

export async function createRun(input: {
  task: string;
  agent: string;
  model?: string;
  roles?: Partial<Record<'manager' | 'executor' | 'auditor', Partial<RoleRuntimeConfig>>>;
  workspace?: string;
  max_rounds?: number;
  prompt_language?: 'en' | 'zh';
}, requestKey = idempotencyKey('create')): Promise<{ id: string }> {
  const response = await fetch('/api/runs', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders(), 'Idempotency-Key': requestKey }, body: JSON.stringify(input),
  });
  if (!response.ok) throw await responseError(response);
  const data = await response.json() as { run: { id: string } };
  return data.run;
}

export async function refreshModels(): Promise<WebMeta> {
  const response = await fetch('/api/models/refresh', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: '{}',
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<WebMeta>;
}

export async function stopRun(runId: string, requestKey = idempotencyKey('stop')): Promise<void> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/stop`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders(), 'Idempotency-Key': requestKey }, body: '{}',
  });
  if (!response.ok) throw await responseError(response);
}

export async function abortRun(runId: string, requestKey = idempotencyKey('abort')): Promise<void> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/abort`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders(), 'Idempotency-Key': requestKey }, body: '{}',
  });
  if (!response.ok) throw await responseError(response);
}

export async function resumeRun(runId: string, requestKey = idempotencyKey('resume')): Promise<{ id: string }> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders(), 'Idempotency-Key': requestKey }, body: '{}',
  });
  if (!response.ok) throw await responseError(response);
  const data = await response.json() as { run: { id: string } };
  return data.run;
}

export function streamRun(
  runId: string,
  after: string | null,
  onSnapshot: (snapshot: Snapshot) => void,
  onEvent: (event: EventEnvelope) => void,
  onStatus: (status: 'connected' | 'reconnecting' | 'closed') => void,
  options: { replay?: number; reconnect?: boolean; maxReconnectDelayMs?: number; heartbeatTimeoutMs?: number; onResync?: () => void; onAuthError?: (code: number) => void; onRunNotFound?: () => void } = {},
): () => void {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const replay = Math.max(0, Math.min(5000, Math.floor(options.replay ?? 100)));
  const reconnect = options.reconnect ?? true;
  const maxReconnectDelayMs = Math.max(250, options.maxReconnectDelayMs ?? 5000);
  let closed = false;
  let socket: WebSocket | null = null;
  let timer: number | null = null;
  let cursor = after;
  let attempts = 0;
  let watchdog: number | null = null;
  let lastFrameAt = 0;
  const heartbeatTimeoutMs = Math.max(5_000, options.heartbeatTimeoutMs ?? 30_000);

  const clearWatchdog = () => {
    if (watchdog !== null) window.clearInterval(watchdog);
    watchdog = null;
  };
  const startWatchdog = (current: WebSocket) => {
    clearWatchdog();
    lastFrameAt = Date.now();
    watchdog = window.setInterval(() => {
      if (closed || socket !== current) return;
      if (Date.now() - lastFrameAt > heartbeatTimeoutMs) {
        current.close(4008, 'heartbeat timeout');
      }
    }, Math.min(5_000, Math.max(1_000, Math.floor(heartbeatTimeoutMs / 3))));
  };

  const schedule = () => {
    if (closed || !reconnect || timer !== null) return;
    const delay = Math.min(maxReconnectDelayMs, 250 * (2 ** Math.min(attempts, 5)));
    attempts += 1;
    onStatus('reconnecting');
    timer = window.setTimeout(() => {
      timer = null;
      connect();
    }, delay);
  };

  const connect = () => {
    if (closed) return;
    onStatus('reconnecting');
    const query = new URLSearchParams({ replay: String(replay) });
    if (cursor) query.set('after', cursor);
    try {
      const token = storedAuthToken();
      const url = `${protocol}//${window.location.host}/api/runs/${encodeURIComponent(runId)}/stream?${query}`;
      socket = token
        ? new WebSocket(url, websocketAuthProtocols(token))
        : new WebSocket(url);
    } catch {
      schedule();
      return;
    }
    const current = socket;
    current.onopen = () => {
      if (closed || socket !== current) return;
      attempts = 0;
      startWatchdog(current);
      onStatus('connected');
    };
    current.onmessage = (message) => {
      if (closed || socket !== current) return;
      lastFrameAt = Date.now();
      try {
        const payload = JSON.parse(String(message.data)) as { kind: string; data?: Snapshot | EventEnvelope };
        if (payload.kind === 'resync_required') {
          cursor = null;
          options.onResync?.();
          // A gap means the server can no longer replay from this cursor.
          // Reconnect without `after` so the next snapshot establishes a new
          // epoch instead of silently continuing a partial timeline.
          current.close(4400, 'resync required');
          return;
        }
        if (payload.kind === 'snapshot' && payload.data) {
          const snapshot = payload.data as Snapshot;
          // With replay enabled the snapshot is sent before older replay
          // frames. Let those frames advance the cursor in order; otherwise a
          // reconnect can accidentally move the cursor backwards.
          if (replay === 0 && snapshot.diagnostics?.last_event_id) cursor = snapshot.diagnostics.last_event_id;
          onSnapshot(snapshot);
        }
        if (payload.kind === 'event' && payload.data) {
          const event = payload.data as EventEnvelope;
          if (event.event_id) cursor = event.event_id;
          onEvent(event);
        }
      } catch {
        // A malformed event must not bring down the workbench.
      }
    };
    current.onerror = () => {
      if (!closed && socket === current) onStatus('reconnecting');
    };
    current.onclose = (event) => {
      if (socket === current) clearWatchdog();
      if (socket === current) socket = null;
      if (closed) return;
      if (event.code === 4401 || event.code === 4403) {
        // A bad/missing token is a stable configuration error.  Retrying every
        // few hundred milliseconds only creates noise and can amplify a
        // reverse-proxy rate limit; wait for the operator to update settings.
        options.onAuthError?.(event.code);
        onStatus('closed');
      } else if (event.code === 4404) {
        options.onRunNotFound?.();
        onStatus('closed');
      } else if (reconnect) schedule();
      else onStatus('closed');
    };
  };

  connect();
  return () => {
    closed = true;
    if (timer !== null) window.clearTimeout(timer);
    clearWatchdog();
    timer = null;
    const current = socket;
    socket = null;
    if (current) current.close();
    onStatus('closed');
  };
}
