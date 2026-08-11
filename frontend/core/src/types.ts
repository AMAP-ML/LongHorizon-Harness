export type RunStatus = 'idle' | 'running' | 'waiting_approval' | 'completed' | 'failed' | 'cancelled' | string;

/** Must stay aligned with the server-side ingress ceiling. */
export const MAX_ROUNDS = 1000;

export interface EventEnvelope {
  schema_version: number;
  event_id: string;
  type: string;
  ts: number;
  run_id: string;
  round: number | null;
  role: string | null;
  status: string | null;
  payload: Record<string, unknown>;
  legacy: Record<string, unknown>;
  offset?: number;
}

export interface ApprovalOption {
  value: string;
  label: string;
  style?: string;
}

export interface Approval {
  approval_id: string;
  title: string;
  message: string;
  options: ApprovalOption[];
  answers: string[];
  allow_input: boolean;
  input_label: string;
  context: Record<string, unknown>;
  round_index: number;
  status: 'pending' | 'resolved' | string;
  action: string;
  reason: string;
  user_input: string;
  created_at: number;
  resolved_at: number | null;
}

export interface OperatorMessage {
  id: string;
  text: string;
  created_at: number;
  status: 'queued' | 'applied' | 'rejected' | 'failed' | 'cancelled' | string;
}

export interface RoundView {
  round_index: number;
  next_step?: string;
  plan_text?: string;
  task_state?: string;
  task_contract?: string;
  executor_output?: string;
  auditor_report?: string;
  harness_feedback?: string;
  /** User-facing closing reply written after the terminal audit decision. */
  final_response?: string;
  active_role?: string | null;
  in_progress?: boolean;
  roles?: string[];
  role_sizes?: Record<string, number>;
  manager_status?: Record<string, unknown>;
  executor_status?: Record<string, unknown>;
  auditor_status?: Record<string, unknown>;
  final_response_status?: Record<string, unknown>;
}

export interface RunSummary {
  id: string;
  task: string;
  status: RunStatus;
  updated_at: number;
  log_dir: string;
  agent?: string;
  model?: string | null;
  role_configs?: Record<'manager' | 'executor' | 'auditor', { agent: string; model: string }>;
  workspace?: string;
  max_rounds?: number;
  prompt_language?: 'en' | 'zh';
}

export interface Snapshot {
  schema_version: number;
  run: {
    id: string;
    status: RunStatus;
    started_at?: number | null;
    finished_at?: number | null;
    log_dir: string;
    /** Optional supervisor result fields (introduced after protocol v1). */
    completion_satisfied?: boolean | null;
    completion_authority?: string | null;
    report_status?: string | null;
    exit_code?: number | null;
    failure_reason?: string | null;
    /** Durable user-facing reply. This is distinct from Auditor protocol text. */
    final_response?: string;
    agent?: string;
    model?: string | null;
    role_configs?: Record<'manager' | 'executor' | 'auditor', { agent: string; model: string }>;
    workspace?: string;
    max_rounds?: number;
    prompt_language?: 'en' | 'zh';
  };
  mission: { task: string; contract_path: string; plan_path: string; verified_state_path: string; report_path: string };
  rounds: RoundView[];
  active_round: number | null;
  active_role: string | null;
  events: EventEnvelope[];
  approvals: Approval[];
  /** Durable free-form instructions sent by the operator after run creation. */
  operator_messages?: OperatorMessage[];
  controls: { can_inject: boolean; can_abort: boolean; can_resume: boolean };
  diagnostics: {
    last_event_id: string | null;
    event_count: number;
    warnings: string[];
    cursor_gap?: boolean;
    resync_required?: boolean;
  };
  legacy?: Record<string, unknown>;
}

export interface ArtifactList {
  run_id: string;
  round_index: number;
  artifacts: string[];
}
