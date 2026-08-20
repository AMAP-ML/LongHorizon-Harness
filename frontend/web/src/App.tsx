import { useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Braces,
  Camera,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  CircleCheck,
  CircleDotDashed,
  CircleX,
  Copy,
  Ellipsis,
  ExternalLink,
  FileCode2,
  FileJson2,
  FilePenLine,
  FileText,
  Files,
  FlaskConical,
  FolderOpen,
  ListChecks,
  LoaderCircle,
  KeyRound,
  PanelRight,
  PanelTop,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  X,
  XCircle,
  ZoomIn,
  ZoomOut,
  type LucideIcon,
} from 'lucide-react';
import { MAX_ROUNDS, type ArtifactList, type EventEnvelope, type RunSummary, type Snapshot } from '../../core/src/types';
import { availableCommands, isTrajectoryNoise, managerPlanSummary, managerPlanText, normaliseMaxRounds, projectArtifactView, projectStatus, dedupeEvents, parseCommand, parseNewRunArgs, phaseLabel, projectTrajectoryView, reducePanelState, DEFAULT_PANEL_STATE, type ArtifactProjection, type FileChangeItem, type PanelName, type StatusView, type TrajectoryItem, type ValidationResultSummary } from '../../core/src';
import {
  abortRun,
  createRun,
  fetchArtifact,
  fetchArtifacts,
  fetchMeta,
  refreshModels,
  fetchRuns,
  fetchTrajectory,
  postInstruction,
  resolveApproval,
  resumeRun,
  stopRun,
  artifactRawUrl,
  fetchImageSource,
  idempotencyKey,
  isUnauthorized,
  setStoredAuthToken,
  storedAuthToken,
  type WebMeta,
  type RoleRuntimeConfig,
  type TrajectoryView,
} from './api';
import { useRunFeed } from './useRunFeed';
import { uiText, useUiLanguage, type UiLanguage } from './i18n';

type DetailsTab = 'artifacts' | 'trajectory' | 'events';
type MessageKind = 'user' | 'plan' | 'assistant' | 'verification' | 'final' | 'live';

function readRunId(): string {
  try {
    return window.localStorage.getItem('lh-run-id') || '';
  } catch {
    return '';
  }
}

function rememberRunId(value: string): void {
  try {
    if (value) window.localStorage.setItem('lh-run-id', value);
    else window.localStorage.removeItem('lh-run-id');
  } catch {
    // A disabled storage backend should not make the workbench unusable.
  }
}

function useful(text?: string | null): text is string {
  return Boolean(text && text.trim() && !text.includes('produced no readable natural-language output'));
}

function compactText(text: string, limit = 1600): string {
  const value = text.trim();
  return value.length > limit ? `${value.slice(0, limit).trimEnd()}…` : value;
}

function cleanAgentOutput(text: string): string {
  // The persisted report starts with machine status fields. They are useful
  // in Details, but they make the conversation feel like a raw log stream.
  return text.trim().replace(/^Status:\s*\S+\s+Integrity:\s*\S+\s+Contract audit:\s*\S+\s*/u, '').trim();
}

function roleTitle(role: string | null | undefined): string {
  if (!role) return 'LongHorizon';
  if (role === 'executor') return 'Executor';
  if (role === 'auditor') return 'Auditor';
  if (role === 'manager') return 'Manager';
  if (role === 'final_response') return 'Final reply';
  return role.replaceAll('_', ' ');
}

function statusLabel(status: string, hasFinalResponse = false, language: UiLanguage = 'zh'): string {
  if (status === 'waiting_approval' && hasFinalResponse) return uiText(language, '结果待确认', 'Result awaiting confirmation');
  const labels = language === 'zh' ? {
    starting: '正在启动', creating: '正在创建', running: '正在处理', stopping: '正在停止', aborting: '正在中止',
    waiting_approval: '需要输入', completed: '已完成', complete: '已完成', failed: '失败', blocked: '已阻塞',
    incomplete: '未完成', cancelled: '已停止', canceled: '已停止', stopped: '已停止', aborted: '已中止', idle: '空闲',
  } : {
    starting: 'Starting', creating: 'Creating', running: 'Running', stopping: 'Stopping', aborting: 'Aborting',
    waiting_approval: 'Input required', completed: 'Completed', complete: 'Completed', failed: 'Failed', blocked: 'Blocked',
    incomplete: 'Incomplete', cancelled: 'Stopped', canceled: 'Stopped', stopped: 'Stopped', aborted: 'Aborted', idle: 'Idle',
  };
  return (labels as Record<string, string>)[status] || (status ? uiText(language, `状态：${status}`, `Status: ${status}`) : uiText(language, '未知', 'Unknown'));
}

function statusClass(status: string): string {
  return `status-dot status-${status.replaceAll('_', '-')}`;
}

function terminalLifecycle(status: unknown): boolean {
  return new Set(['completed', 'complete', 'success', 'succeeded', 'done', 'finished', 'failed', 'failure', 'blocked', 'incomplete', 'cancelled', 'canceled', 'stopped', 'aborted']).has(String(status ?? '').trim().toLowerCase());
}

function stoppingLifecycle(status: unknown): boolean {
  return new Set(['stopping', 'aborting', 'stop_requested', 'abort_requested']).has(String(status ?? '').trim().toLowerCase());
}

// SVG is intentionally treated as text/attachment by the API: rendering an
// untrusted agent-produced SVG in the dashboard origin would allow script and
// external-resource execution. Raster formats remain safe image previews.
const IMAGE_ARTIFACT_RE = /\.(?:png|jpe?g|gif|webp|avif|bmp|ico)$/iu;

function isImageArtifact(name: string): boolean {
  return IMAGE_ARTIFACT_RE.test(name);
}

function formatTime(ts?: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
}

function eventSummary(event: EventEnvelope, language: UiLanguage): string {
  const role = event.role ? ` · ${roleTitle(event.role)}` : '';
  const phase = phaseLabel(event.type);
  const localizedPhase = language === 'en' ? phase : ({
    'Run started': '任务开始', 'Run finished': '任务结束', 'Manager started': 'Manager 开始', 'Manager completed': 'Manager 完成',
    'Executor started': 'Executor 开始', 'Executor completed': 'Executor 完成', 'Auditor started': 'Auditor 开始', 'Auditor completed': 'Auditor 完成',
  } as Record<string, string>)[phase] || phase;
  return `${localizedPhase}${role}`;
}

function commandHelpText(capabilities: Record<string, boolean>, snapshot: Snapshot, hasRun: boolean, language: UiLanguage): string {
  const descriptions: Record<string, [string, string]> = {
    help: ['显示可用命令', 'Show available commands'], runs: ['列出任务', 'List tasks'], new: ['开始新任务', 'Start a new task'], attach: ['切换到任务', 'Switch to a task'],
    inject: ['追加指令', 'Queue an instruction'], approve: ['处理待确认请求', 'Resolve an approval'], stop: ['请求安全停止', 'Request a graceful stop'], abort: ['中止当前任务', 'Abort the active task'],
    resume: ['重新开始已结束任务', 'Restart a finished task'], details: ['打开详情', 'Open task details'], events: ['打开事件面板', 'Open the event panel'], artifacts: ['打开运行记录', 'Open run records'], trajectory: ['打开执行轨迹', 'Open the trajectory panel'],
  };
  return availableCommands(capabilities, snapshot, hasRun).map((command) => {
    const description = descriptions[command.name] || [command.description, command.description];
    return `/${command.name}${command.args ? ` ${command.args}` : ''}  ${uiText(language, description[0], description[1])}`;
  }).join('\n');
}

function commandErrorText(message: string, language: UiLanguage): string {
  if (language === 'zh') return message;
  return message
    .replace(/ 需要一个值$/u, ' requires a value')
    .replace('--language 必须是 zh 或 en', '--language must be zh or en')
    .replace(/--rounds 必须是 1–1000 的整数/u, '--rounds must be an integer from 1 to 1000')
    .replace(/--rounds 必须在 1–(\d+) 之间/u, '--rounds must be between 1 and $1');
}

interface ConversationMessage {
  id: string;
  kind: MessageKind;
  role: string;
  round?: number;
  title: string;
  text: string;
  input?: string;
  activity?: ActivityStep[];
  artifacts?: ArtifactProjection;
  time?: number;
  /** Internal timestamp used to interleave durable operator follow-ups. */
  sortTime?: number;
  /** Whether the text is backed by the harness completion authority. */
  authority?: 'final_response' | 'auditor' | 'report' | 'none';
}

type ActivityAction = 'read' | 'edit' | 'validate' | 'search' | 'task' | 'screenshot' | 'command' | 'result' | 'note';

interface ActivityStep {
  id?: string;
  kind?: string;
  status?: string;
  title?: string;
  action?: ActivityAction;
  summary?: string;
  result?: string;
  detail?: string;
  links?: string[];
  text?: string;
  images?: string[];
  imageWarning?: string;
}

const MODEL_PRESETS: Record<string, { id: string; label: string }[]> = {
  codex: [{ id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol · default' }],
  claude_code: [{ id: 'claude-opus-5', label: 'Claude Opus 5 · default' }],
  deepseek_harness: [{ id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash · default' }],
  opencode: [{ id: 'opencode/deepseek-v4-flash-free', label: 'DeepSeek V4 Flash Free · default' }],
};

type PublicRole = 'manager' | 'executor' | 'auditor';
type RoleSelection = RoleRuntimeConfig & { custom: boolean };
const EFFORT_LEVEL_FALLBACK = ['min', 'low', 'med', 'high', 'xhigh', 'max'];
const PUBLIC_ROLES: Array<{ id: PublicRole; label: string; description: string }> = [
  { id: 'manager', label: 'Manager', description: '规划、路由与完成判定' },
  { id: 'executor', label: 'Executor', description: '执行 GUI / CLI 子任务' },
  { id: 'auditor', label: 'Auditor', description: '独立验证与验收' },
];

// A long-horizon run can contain hundreds/thousands of rounds.  The
// conversation needs recent evidence and the active role; historical detail
// remains available through the Details drawer's on-demand request.
const MAX_TRAJECTORY_ROUNDS = 8;
const MAX_TRAJECTORY_STEPS = 600;

function normalizedModelChoices(value: unknown): { id: string; label: string; availability?: string }[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === 'string' && item.trim()) return [{ id: item.trim(), label: item.trim() }];
    if (!item || typeof item !== 'object') return [];
    const record = item as Record<string, unknown>;
    const id = typeof record.id === 'string' ? record.id.trim() : '';
    return id ? [{ id, label: typeof record.label === 'string' && record.label.trim() ? record.label.trim() : id, availability: typeof record.availability === 'string' ? record.availability : undefined }] : [];
  });
}

function defaultAgent(meta: WebMeta | null): string {
  const preferred = meta?.defaults?.agent;
  if (preferred && meta?.agents?.some((item) => item.id === preferred && item.available !== false)) return preferred;
  return meta?.agents?.find((item) => item.available !== false)?.id || 'codex';
}

function defaultModel(meta: WebMeta | null, agent: string): string {
  return meta?.agents?.find((item) => item.id === agent)?.default_model
    || (meta?.defaults?.agent === agent ? meta.defaults.model : '')
    || normalizedModelChoices(meta?.models?.[agent])[0]?.id
    || MODEL_PRESETS[agent]?.[0]?.id
    || '';
}

interface ImageSourceProjection {
  images: string[];
  omittedLargeDataUrls: number;
}

function projectImageSources(value: unknown): ImageSourceProjection {
  const values = Array.isArray(value) ? value : [value];
  const images: string[] = [];
  let omittedLargeDataUrls = 0;
  for (const source of values) {
    if (typeof source !== 'string') continue;
    const candidate = source.trim();
    // Trajectory data is agent-controlled.  Auto-loading arbitrary http(s)
    // URLs would turn screenshots into a browser-side SSRF/tracking primitive
    // and would also bypass the Web API bearer.  External URLs remain useful
    // as explicit text links; only same-origin API artifacts and bounded
    // raster data URLs are rendered in the gallery.
    const dataImage = /^data:image\/(?:png|jpe?g|gif|webp|avif|bmp|x-icon);(?:base64,)?/iu.test(candidate);
    if (dataImage) {
      if (candidate.length <= 2_000_000) images.push(candidate);
      else omittedLargeDataUrls += 1;
    } else if (candidate.startsWith('/api/') && !candidate.startsWith('//')) {
      images.push(candidate);
    }
  }
  return { images: [...new Set(images)], omittedLargeDataUrls };
}

function trajectoryStepText(step: Record<string, unknown>): string {
  if (typeof step.text === 'string' && step.text.trim()) return step.text;
  const { images: _images, image: _image, image_url: _imageUrl, imageUrl: _imageUrlCamel, has_image: _hasImage, ...withoutImages } = step;
  return Object.keys(withoutImages).length ? JSON.stringify(withoutImages, null, 2) : '';
}

function trajectoryResultFailed(step: Record<string, unknown>): boolean {
  if (step.is_error === true || /^(?:error|failed|failure)$/iu.test(String(step.status || ''))) return true;
  const exitCode = step.exit_code ?? step.exitCode;
  if (typeof exitCode === 'number' && exitCode !== 0) return true;
  if (typeof exitCode === 'string' && /^-?\d+$/u.test(exitCode.trim()) && Number(exitCode) !== 0) return true;
  const text = typeof step.text === 'string' ? step.text : '';
  const match = text.match(/\[exit_code=(-?\d+)\]/iu) || text.match(/"exit_code"\s*:\s*(-?\d+)/iu);
  return Boolean(match && Number(match[1]) !== 0);
}

// Keep links useful in prose and code output without turning template strings
// such as `http://127.0.0.1:{args.port}` into clickable destinations.
const URL_RE = /https?:\/\/[^\s<>"'`]+/giu;

function normalizeLink(value: string): string | null {
  // Markdown and quoted shell output commonly leave closing punctuation on
  // the URL. Strip it, while rejecting unresolved `{placeholder}` values.
  const candidate = value.trim().replace(/[.,;:!?]+$/u, '').replace(/[)\]}]+$/u, '');
  if (!candidate || /[{}]/u.test(candidate)) return null;
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? candidate : null;
  } catch {
    return null;
  }
}

function extractLinks(text: string): string[] {
  const links = (text.match(URL_RE) || []).map(normalizeLink).filter((value): value is string => Boolean(value));
  return [...new Set(links)];
}

function stepImageProjection(step: Record<string, unknown>): ImageSourceProjection {
  const projections = [step.images, step.image, step.image_url, step.imageUrl].map(projectImageSources);
  return {
    images: [...new Set(projections.flatMap((item) => item.images))],
    omittedLargeDataUrls: projections.reduce((total, item) => total + item.omittedLargeDataUrls, 0),
  };
}

function trajectoryAction(step: Record<string, unknown>, kind: string, images: string[]): ActivityAction {
  const name = String(step.name || '').toLowerCase();
  const input = step.input && typeof step.input === 'object' ? step.input as Record<string, unknown> : {};
  const command = String(input.command || input.cmd || '').trim();
  if (images.length || /(?:screen_?shot|capture|snapshot)/iu.test(name)) return 'screenshot';
  if (/^(?:apply_patch|file_change|edit|multiedit|write|notebookedit)$/iu.test(name)) return 'edit';
  if (name === 'web_search') return 'search';
  if (name === 'todo_list') return 'task';
  if (command && /(?:\bpytest\b|\bunittest\b|\b(?:npm|pnpm|yarn|bun)\b[^\n]*(?:\btest\b|\btypecheck\b|\bbuild\b|\blint\b)|\btsc\b|\bvitest\b|\bjest\b|\bcargo\s+test\b|\bgo\s+test\b|\bgit\s+diff\b[^\n]*--check)/iu.test(command)) return 'validate';
  if (/^(?:read|glob|grep)$/iu.test(name)) return name === 'read' ? 'read' : 'search';
  if (command && /(?:^|[;&|]\s*|\s)(?:cat|sed|head|tail|less|rg|grep|find|fd|ls|tree|pwd|wc)\b|\bgit\s+(?:status|diff|show|log)\b/iu.test(command)) return 'read';
  if (kind === 'tool_use') return 'command';
  if (kind === 'result' || kind === 'tool_result') return 'result';
  return 'note';
}

function trajectoryTitle(action: ActivityAction, language: UiLanguage): string {
  const zh = ({
    read: '读取与检查',
    edit: '更新文件',
    validate: '运行验证',
    search: '检索资料',
    task: '更新任务清单',
    screenshot: '获取截图',
    command: '运行命令',
    result: '执行结果',
    note: '工作摘要',
  } as Record<ActivityAction, string>)[action];
  const en = ({
    read: 'Read and inspect', edit: 'Update files', validate: 'Run validation', search: 'Search sources', task: 'Update task list',
    screenshot: 'Capture screenshot', command: 'Run command', result: 'Execution result', note: 'Work summary',
  } as Record<ActivityAction, string>)[action];
  return uiText(language, zh, en);
}

function trajectorySummary(item: TrajectoryItem, language: UiLanguage): string {
  const raw = item.raw as Record<string, unknown>;
  const input = raw.input && typeof raw.input === 'object' ? raw.input as Record<string, unknown> : {};
  if (item.kind === 'tool_use') {
    if (typeof input.command === 'string') return compactText(input.command.replace(/\s+/gu, ' '), 180);
    if (Array.isArray(input.changes)) {
      const paths = input.changes.map((change) => change && typeof change === 'object' ? String((change as Record<string, unknown>).path || '') : '').filter(Boolean);
      return paths.length ? uiText(language, `${paths.length} 个文件 · ${paths.slice(0, 2).join(', ')}`, `${paths.length} files · ${paths.slice(0, 2).join(', ')}`) : uiText(language, '准备文件变更', 'Preparing file changes');
    }
    if (typeof input.query === 'string') return compactText(input.query, 180);
    return compactText(item.text || uiText(language, '准备调用工具', 'Preparing tool call'), 180);
  }
  const value = (item.text || '').replace(/\[exit_code=0\]/giu, '').trim();
  if (item.kind === 'tool_result') {
    const visible = value.replace(/(?:^|\n)\[image\](?=\n|$)/giu, '').trim();
    if (visible) return compactText(visible.split(/\n+/u).find(Boolean) || visible, 180);
    if (item.images.length) return uiText(language, `${item.images.length} 张截图已返回`, `${item.images.length} screenshots returned`);
    return uiText(language, '已返回结果', 'Result returned');
  }
  if (item.kind === 'result') return value ? compactText(value.split(/\n+/u).find(Boolean) || value, 220) : uiText(language, '阶段完成', 'Stage completed');
  return compactText(value, 220);
}

function trajectoryActivity(trajectory: TrajectoryView | undefined, includeResult = true, language: UiLanguage = 'zh'): ActivityStep[] {
  if (!trajectory) return [];
  const toolResults = new Map<string, { failed: boolean; raw: Record<string, unknown> }>();
  for (const step of trajectory.steps) {
    if (step.kind !== 'tool_result') continue;
    const raw = step as Record<string, unknown>;
    const id = String(raw.tool_use_id || raw.id || '').trim();
    if (id) toolResults.set(id, { failed: trajectoryResultFailed(raw), raw });
  }
  const projection = projectTrajectoryView(trajectory, {
    maxSteps: 80,
    maxTextChars: 240,
    includeKinds: includeResult
      ? ['text', 'tool_use', 'tool_result', 'result', 'error']
      : ['text', 'tool_use', 'error'],
  });
  const visibleToolIds = new Set(projection.items
    .filter((item) => item.kind === 'tool_use')
    .map((item) => String((item.raw as Record<string, unknown>).id || '').trim())
    .filter(Boolean));
  return projection.items
    .filter((item) => !isTrajectoryNoise(item.text))
    .filter((item) => Boolean(item.text?.trim() || item.images.length || item.hasImage))
    .filter((item) => {
      if (item.kind !== 'tool_result') return true;
      const resultId = String((item.raw as Record<string, unknown>).tool_use_id || '').trim();
      return !resultId || !visibleToolIds.has(resultId);
    })
    .map((item): ActivityStep => {
      const raw = item.raw as Record<string, unknown>;
      const summary = trajectorySummary(item, language);
      const toolId = String(raw.id || raw.tool_use_id || '').trim();
      const matchedResult = item.kind === 'tool_use' && toolId ? toolResults.get(toolId) : undefined;
      const paired = includeResult ? matchedResult : undefined;
      const ownImageProjection = stepImageProjection(raw);
      const resultImageProjection = paired ? stepImageProjection(paired.raw) : { images: [], omittedLargeDataUrls: 0 };
      const ownImages = ownImageProjection.images;
      const resultImages = resultImageProjection.images;
      const images = [...new Set([...ownImages, ...resultImages])];
      const omittedLargeDataUrls = ownImageProjection.omittedLargeDataUrls + resultImageProjection.omittedLargeDataUrls;
      const action = trajectoryAction(raw, item.kind, images);
      const pairedText = paired ? trajectoryStepText(paired.raw) : '';
      const resultText = paired && !isTrajectoryNoise(pairedText)
        ? pairedText.replace(/(?:^|\n)\[image\](?=\n|$)/giu, '').replace(/\[exit_code=0\]/giu, '').trim()
        : '';
      const resultSummary = resultText ? compactText(resultText.split(/\n+/u).find(Boolean) || resultText, 180) : resultImages.length ? uiText(language, `${resultImages.length} 张截图已返回`, `${resultImages.length} screenshots returned`) : paired ? uiText(language, '已完成，无额外输出', 'Completed with no additional output') : '';
      const ownDetail = trajectoryStepText(raw);
      const pairedDetail = paired ? trajectoryStepText(paired.raw) : '';
      const detail = [ownDetail, pairedDetail && !isTrajectoryNoise(pairedDetail) && pairedDetail !== ownDetail ? `${uiText(language, '结果', 'Result')}\n${pairedDetail}` : ''].filter(Boolean).join('\n\n');
      const status = item.isError || matchedResult?.failed
        ? 'failed'
        : item.kind === 'tool_use'
          ? matchedResult ? 'done' : 'running'
          : 'done';
      return {
        id: `${projection.roundIndex}-${projection.role}-${item.index}`,
        kind: images.length > 0 && item.kind === 'tool_result' ? 'image' : item.kind,
        status,
        title: trajectoryTitle(action, language),
        action,
        summary,
        result: resultSummary,
        detail,
        text: item.text,
        links: extractLinks(`${summary}\n${resultText}\n${detail}`),
        images,
        imageWarning: omittedLargeDataUrls > 0 ? uiText(language, `${omittedLargeDataUrls} 张截图超过 2MB，已隐藏`, `${omittedLargeDataUrls} screenshots over 2 MB were hidden`) : undefined,
      };
    });
}

function hasExecutionArtifacts(projection: ArtifactProjection): boolean {
  return projection.files.totalFiles > 0 || projection.validations.total > 0;
}

interface CompletionEvidence {
  satisfied: boolean;
  finalResponse: string;
  reportText: string;
}

/** Read the durable completion report without trusting arbitrary role output. */
function completionEvidence(snapshot: Snapshot): CompletionEvidence {
  const run = snapshot.run as Snapshot['run'] & { completion_satisfied?: unknown; completion_authority?: unknown };
  const legacy = snapshot.legacy && typeof snapshot.legacy === 'object' ? snapshot.legacy as Record<string, unknown> : {};
  const nested = legacy.report && typeof legacy.report === 'object' ? legacy.report as Record<string, unknown> : legacy;
  const satisfied = run.completion_satisfied === true || nested.completion_satisfied === true;
  const finalResponse = typeof run.final_response === 'string' && run.final_response.trim()
    ? run.final_response.trim()
    : typeof nested.final_response === 'string'
      ? nested.final_response.trim()
      : [...snapshot.rounds].reverse().find((round) => useful(round.final_response))?.final_response?.trim() || '';
  const reportText = typeof nested.latest_auditor_report === 'string'
    ? nested.latest_auditor_report.trim()
    : typeof nested.auditor_report === 'string'
      ? nested.auditor_report.trim()
      : '';
  return { satisfied, finalResponse, reportText };
}

function terminalResultLabel(status: string, language: UiLanguage): string {
  if (status === 'failed') return uiText(language, '运行失败', 'Run failed');
  if (status === 'cancelled' || status === 'canceled') return uiText(language, '运行已停止', 'Run stopped');
  if (status === 'blocked' || status === 'incomplete') return uiText(language, '任务未完成', 'Task incomplete');
  return uiText(language, '运行结果', 'Run result');
}

function conversationStageTime(snapshot: Snapshot, round: number, stage: 'manager' | 'executor' | 'auditor' | 'final_response'): number | undefined {
  const prefix = stage === 'final_response' ? 'round.final_response.' : `round.${stage}.`;
  const times = snapshot.events
    .filter((event) => event.round === round && event.type.startsWith(prefix))
    .map((event) => event.ts)
    .filter((value) => Number.isFinite(value) && value > 0);
  return times.length ? Math.max(...times) : undefined;
}

function approvalResponseText(approval: Snapshot['approvals'][number], language: UiLanguage): string {
  const userInput = approval.user_input.trim();
  if (userInput) return userInput;
  const option = approval.options.find((item) => item.value === approval.action);
  const label = option?.label || approval.action;
  if (/continue/iu.test(label) || approval.action === 'continue') return uiText(language, '继续运行', 'Continue run');
  if (/end/iu.test(label)) return uiText(language, '结束任务', 'End task');
  if (/stop|abort|cancel/iu.test(label) || ['stop', 'abort', 'cancel'].includes(approval.action)) return uiText(language, '停止任务', 'Stop task');
  return label;
}

function insertConversationInteraction(messages: ConversationMessage[], message: ConversationMessage): void {
  const insertion = message.sortTime
    ? messages.findIndex((candidate, index) => index > 0 && Boolean(candidate.sortTime && candidate.sortTime > message.sortTime!))
    : -1;
  if (insertion >= 0) messages.splice(insertion, 0, message);
  else messages.push(message);
}

function conversationFor(snapshot: Snapshot, trajectoryMap: Record<string, TrajectoryView>, liveTrajectory: TrajectoryView | null, artifacts: ArtifactProjection, language: UiLanguage): ConversationMessage[] {
  const messages: ConversationMessage[] = [];
  const task = snapshot.mission.task.trim();
  const taskTime = snapshot.run.started_at || snapshot.events.find((event) => event.type === 'run.started')?.ts;
  if (task) messages.push({ id: 'task', kind: 'user', role: 'You', title: 'You', text: task, sortTime: taskTime || undefined });

  const hiddenRounds = Math.max(0, snapshot.rounds.length - MAX_TRAJECTORY_ROUNDS);
  if (hiddenRounds > 0) {
    messages.push({
      id: 'history-collapsed',
      kind: 'plan',
      role: 'LongHorizon',
      title: uiText(language, '历史轮次已折叠', 'Earlier rounds collapsed'),
      text: uiText(language, `为保持长任务流畅，已折叠更早 ${hiddenRounds} 轮；可在详情 → 执行轨迹 / 事件中按轮次查看。`, `${hiddenRounds} earlier rounds were collapsed to keep long tasks responsive. Open Details → Trajectory / Events to inspect them.`),
    });
  }
  for (const round of snapshot.rounds.slice(-MAX_TRAJECTORY_ROUNDS)) {
    const managerText = managerPlanText(round);
    if (useful(managerText)) {
      messages.push({
        id: `plan-${round.round_index}`,
        kind: 'plan',
        role: 'Manager',
        round: round.round_index,
        title: uiText(language, `计划 · 第 ${round.round_index} 轮`, `Plan · Round ${round.round_index}`),
        input: task,
        text: managerPlanSummary(managerText),
        sortTime: conversationStageTime(snapshot, round.round_index, 'manager'),
        activity: trajectoryActivity(trajectoryMap[`${round.round_index}:manager`], false, language),
      });
    }
    if (useful(round.executor_output)) {
      messages.push({
        id: `executor-${round.round_index}`,
        kind: 'assistant',
        role: 'Executor',
        round: round.round_index,
        title: uiText(language, `执行器 · 第 ${round.round_index} 轮`, `Executor · Round ${round.round_index}`),
        input: managerPlanSummary(round.plan_text || '') || round.next_step,
        text: cleanAgentOutput(round.executor_output),
        sortTime: conversationStageTime(snapshot, round.round_index, 'executor'),
        activity: trajectoryActivity(trajectoryMap[`${round.round_index}:executor`], true, language),
      });
    }
    if (useful(round.auditor_report)) {
      messages.push({
        id: `auditor-${round.round_index}`,
        kind: 'verification',
        role: 'Auditor',
        round: round.round_index,
        title: uiText(language, `验证 · 第 ${round.round_index} 轮`, `Verification · Round ${round.round_index}`),
        input: cleanAgentOutput(round.executor_output || round.task_state || ''),
        text: cleanAgentOutput(round.auditor_report),
        sortTime: conversationStageTime(snapshot, round.round_index, 'auditor'),
        activity: trajectoryActivity(trajectoryMap[`${round.round_index}:auditor`], true, language),
      });
    }
  }

  if (liveTrajectory && liveTrajectory.steps.length && ['running', 'starting', 'waiting_approval', 'stopping', 'aborting'].includes(snapshot.run.status)) {
    const activity = trajectoryActivity(liveTrajectory, true, language);
    const visible = liveTrajectory.steps
      .filter((step) => step.kind === 'text' || step.kind === 'result')
      .map((step) => String(step.text || '').trim())
      .filter(Boolean);
    const latest = visible.at(-1);
    if (latest || activity.length > 0 || hasExecutionArtifacts(artifacts)) {
      messages.push({
        id: `live-${liveTrajectory.round_index}-${liveTrajectory.role}`,
        kind: 'live',
        role: roleTitle(liveTrajectory.role),
        round: liveTrajectory.round_index,
        title: uiText(language, `${roleTitle(liveTrajectory.role)} 正在处理`, `${roleTitle(liveTrajectory.role)} is working`),
        text: compactText(latest || uiText(language, '中间截图', 'Intermediate screenshot')),
        activity,
        artifacts: hasExecutionArtifacts(artifacts) ? artifacts : undefined,
      });
    }
  }

  const terminal = terminalLifecycle(snapshot.run.status);
  const evidence = completionEvidence(snapshot);
  const latestAuditor = [...messages].reverse().find((message) => message.kind === 'verification');
  // The Executor's output is an implementation result, not the final answer.
  // A final card is sourced from the persisted completion report or Auditor;
  // if neither exists, show an explicit missing-evidence result instead of
  // presenting an unverified claim as authoritative.
  // The backend deliberately writes the closing reply before raising the
  // completion approval, so the operator can decide whether to accept it or
  // continue the run. Show that durable reply while waiting for the decision;
  // otherwise the approval asks about an answer the operator cannot inspect.
  if (terminal || evidence.finalResponse) {
    const nonSuccessTerminal = ['failed', 'blocked', 'incomplete', 'cancelled', 'canceled'].includes(snapshot.run.status);
    const authoritativeText = evidence.finalResponse || snapshot.run.failure_reason || evidence.reportText || (evidence.satisfied || nonSuccessTerminal ? latestAuditor?.text || '' : '');
    const text = authoritativeText || uiText(language, `${terminalResultLabel(snapshot.run.status, language)}，但尚未收到 Auditor 的最终报告。`, `${terminalResultLabel(snapshot.run.status, language)}, but the Auditor's final report has not been received.`);
    messages.push({
      id: 'final-answer',
      kind: 'final',
      role: 'LongHorizon',
      title: evidence.finalResponse || evidence.satisfied ? uiText(language, '最终回答', 'Final answer') : terminalResultLabel(snapshot.run.status, language),
      text,
      authority: evidence.finalResponse
        ? 'final_response'
        : evidence.reportText && (evidence.satisfied || nonSuccessTerminal)
          ? 'report'
        : authoritativeText && latestAuditor && (evidence.satisfied || nonSuccessTerminal)
          ? 'auditor'
          : 'none',
      sortTime: snapshot.rounds.at(-1)?.round_index
        ? conversationStageTime(snapshot, snapshot.rounds.at(-1)!.round_index, 'final_response')
        : snapshot.run.finished_at || undefined,
      artifacts: hasExecutionArtifacts(artifacts) ? artifacts : undefined,
    });
  }

  for (const item of [...(snapshot.operator_messages || [])].sort((left, right) => left.created_at - right.created_at)) {
    const time = Number.isFinite(item.created_at) && item.created_at > 0 ? item.created_at : undefined;
    const message: ConversationMessage = {
      id: `operator-${item.id}`,
      kind: 'user',
      role: uiText(language, '你', 'You'),
      title: uiText(language, '你', 'You'),
      text: item.text,
      time,
      sortTime: time,
    };
    insertConversationInteraction(messages, message);
  }
  for (const approval of snapshot.approvals) {
    if (approval.status === 'pending' || (!approval.user_input.trim() && !approval.action.trim())) continue;
    const time = approval.resolved_at && Number.isFinite(approval.resolved_at) ? approval.resolved_at : undefined;
    insertConversationInteraction(messages, {
      id: `approval-response-${approval.approval_id}`,
      kind: 'user',
      role: uiText(language, '你', 'You'),
      title: uiText(language, '你', 'You'),
      text: approvalResponseText(approval, language),
      time,
      sortTime: time,
    });
  }
  return messages;
}

export default function App() {
  const { language, setLanguage, text } = useUiLanguage();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState(readRunId);
  const [error, setError] = useState('');
  const [dismissedFeedError, setDismissedFeedError] = useState('');
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [approvalInputs, setApprovalInputs] = useState<Record<string, string>>({});
  const [capabilities, setCapabilities] = useState<Record<string, boolean>>({});
  const [meta, setMeta] = useState<WebMeta | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const [search, setSearch] = useState('');
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [creatingNew, setCreatingNew] = useState(false);
  const [detailsTab, setDetailsTab] = useState<DetailsTab>('artifacts');
  const [selectedRound, setSelectedRound] = useState<number | null>(null);
  const [artifactList, setArtifactList] = useState<ArtifactList | null>(null);
  const [artifactError, setArtifactError] = useState('');
  const [artifactName, setArtifactName] = useState('');
  const [artifactText, setArtifactText] = useState('');
  const [artifactReload, setArtifactReload] = useState(0);
  const [trajectoryRole, setTrajectoryRole] = useState('executor');
  const [trajectoryData, setTrajectoryData] = useState<TrajectoryView | null>(null);
  const [trajectoryError, setTrajectoryError] = useState('');
  const [trajectoryReload, setTrajectoryReload] = useState(0);
  const [trajectories, setTrajectories] = useState<Record<string, TrajectoryView>>({});
  const [followLatest, setFollowLatest] = useState(true);
  const [mobileStatusOpen, setMobileStatusOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [authRevision, setAuthRevision] = useState(0);
  const [panelState, dispatchPanel] = useReducer(reducePanelState, DEFAULT_PANEL_STATE);
  const conversationRef = useRef<HTMLElement>(null);
  const operationKeys = useRef(new Map<string, string>());
  const trajectoryInFlight = useRef(new Set<string>());
  const trajectoryLoadedSizes = useRef(new Map<string, number>());
  const trajectoryRequestSeq = useRef(new Map<string, number>());
  const liveTrajectoryInFlight = useRef(new Set<string>());
  const runListGeneration = useRef(0);
  const artifactRequest = useRef(0);
  const trajectoryRequest = useRef(0);

  const feed = useRunFeed(runId, authRevision);
  const snapshot = feed.snapshot;
  const connection = feed.connection;

  const currentRun = runs.find((run) => run.id === runId);
  const statusView = useMemo(() => projectStatus(snapshot), [snapshot]);
  const activeRound = snapshot.active_round ?? statusView.current?.round ?? snapshot.rounds.at(-1)?.round_index ?? null;
  // During a stop/abort transition the backend may clear ``active_role``
  // before the last trajectory flush.  Use the shared status projection as a
  // conservative fallback so the final visible step is not lost.
  const activeRole = snapshot.active_role
    || (statusView.current && statusView.current.key !== 'record' ? statusView.current.key : '');
  const orderedTrajectories = useMemo(() => Object.entries(trajectories)
    .sort(([left], [right]) => {
      const [leftRound, leftRole] = left.split(':');
      const [rightRound, rightRole] = right.split(':');
      const roundDifference = Number(leftRound) - Number(rightRound);
      if (roundDifference) return roundDifference;
      const roleOrder: Record<string, number> = { manager: 0, executor: 1, auditor: 2 };
      return (roleOrder[leftRole] ?? 9) - (roleOrder[rightRole] ?? 9);
    })
    .map(([, trajectory]) => trajectory)
    .slice(-MAX_TRAJECTORY_ROUNDS * 4), [trajectories]);
  const executionArtifacts = useMemo(() => projectArtifactView(orderedTrajectories, { maxFiles: 200, maxValidations: 24 }), [orderedTrajectories]);
  const messages = useMemo(() => conversationFor(snapshot, trajectories, activeRole ? trajectories[`${activeRound}:${activeRole}`] || null : null, executionArtifacts, language), [snapshot, activeRole, activeRound, trajectories, executionArtifacts, language]);
  // A stale approval record can survive a stop/crash. Keep it in Details for
  // audit history, but never render an actionable card after lifecycle has
  // become terminal; doing so suggests that clicking it can resume a dead run.
  const pendingApprovalIds = useMemo(() => new Set(statusView.pendingApprovals.map((approval) => approval.id)), [statusView.pendingApprovals]);
  const pendingApprovals = snapshot.approvals.filter((approval) => pendingApprovalIds.has(approval.approval_id));
  const filteredRuns = runs.filter((run) => `${run.id} ${run.task}`.toLowerCase().includes(search.toLowerCase()));
  const commandOptions = availableCommands(capabilities, snapshot, Boolean(runId));
  const visibleError = error || (feed.error && feed.error !== dismissedFeedError ? feed.error : '');
  const typedCommand = parseCommand(instruction);
  const typedCommandName = typedCommand?.name;
  const canCreateRun = meta !== null && capabilities.create_run !== false;
  const canStopRun = capabilities.stop !== false && snapshot.controls.can_abort
    && !terminalLifecycle(snapshot.run.status) && !stoppingLifecycle(snapshot.run.status);
  const canAbortRun = capabilities.abort !== false && snapshot.controls.can_abort
    && !terminalLifecycle(snapshot.run.status) && !stoppingLifecycle(snapshot.run.status);
  const canResumeRun = capabilities.resume !== false && snapshot.controls.can_resume;
  const composerInteractive = Boolean(runId && snapshot.controls.can_inject);
  const composerCanSend = composerInteractive && Boolean(instruction.trim()) && Boolean(
    typedCommand ? commandOptions.some((item) => item.name === typedCommandName) : true,
  );
  const composerPlaceholder = composerInteractive
    ? text('输入后续指令', 'Enter a follow-up instruction')
    : !runId
      ? text('点击左侧“新任务”开始', 'Click “New task” in the sidebar to begin')
      : pendingApprovals.length > 0
        ? text('请先处理上方的待确认请求', 'Resolve the approval request above first')
        : stoppingLifecycle(snapshot.run.status)
          ? text('正在停止任务…', 'Stopping the task…')
          : canResumeRun
            ? text('任务已结束，可点击顶部“重新开始”继续', 'Task ended. Click “Restart” above to continue')
            : terminalLifecycle(snapshot.run.status)
              ? text('任务已结束', 'Task ended')
              : text('当前阶段暂不可输入', 'Input is unavailable during the current stage');
  const composerFooter = composerInteractive
    ? text('操作指令', 'Command')
    : canResumeRun
      ? text('可重新开始或创建新任务', 'Restart or create a new task')
      : pendingApprovals.length > 0
        ? text('等待你的确认', 'Waiting for your approval')
        : text('输入已停用', 'Input disabled');

  function operationKey(scope: string, fingerprint: string): { id: string; key: string } {
    const id = `${scope}\u0000${fingerprint}`;
    const existing = operationKeys.current.get(id);
    if (existing) return { id, key: existing };
    const key = idempotencyKey(scope);
    operationKeys.current.set(id, key);
    return { id, key };
  }

  function clearOperationKey(id: string): void {
    operationKeys.current.delete(id);
  }

  function trajectoryIdentity(round: number, role: string): string {
    return `${runId}:${round}:${role}`;
  }

  function beginTrajectoryRequest(requestKey: string): number {
    const next = (trajectoryRequestSeq.current.get(requestKey) || 0) + 1;
    trajectoryRequestSeq.current.set(requestKey, next);
    return next;
  }

  function commitTrajectory(storageKey: string, requestKey: string, requestId: number, next: TrajectoryView, size?: number): void {
    if (trajectoryRequestSeq.current.get(requestKey) !== requestId) return;
    if (typeof size === 'number' && Number.isFinite(size)) trajectoryLoadedSizes.current.set(storageKey, size);
    // The request sequence above rejects responses that started before a newer
    // poll.  Do not use payload length as a freshness signal: when a role ends,
    // the backend rebuilds its live trajectory into a deduplicated normalized
    // file, which can legitimately be shorter than the preceding live view.
    setTrajectories((current) => ({ ...current, [storageKey]: next }));
  }

  useEffect(() => {
    const onRunNotFound = () => setRunId('');
    window.addEventListener('lh-run-not-found', onRunNotFound);
    return () => window.removeEventListener('lh-run-not-found', onRunNotFound);
  }, []);

  useEffect(() => {
    const onFailure = (reason: unknown) => {
      if (isUnauthorized(reason)) setAuthOpen(true);
      setError(isUnauthorized(reason) ? text('此 Web 服务需要访问令牌，请在连接设置中填写。', 'This Web service requires an access token. Enter it in Connection settings.') : String(reason));
    };
    void fetchMeta().then((next) => {
      setMeta(next);
      setCapabilities(next.capabilities);
      setAuthOpen(false);
    }).catch(onFailure);
    runListGeneration.current += 1;
    const refresh = () => {
      const requestGeneration = ++runListGeneration.current;
      return fetchRuns().then((items) => {
      if (requestGeneration !== runListGeneration.current) return;
      setRuns(items);
      // A selected run is stronger than a potentially stale list response.
      // Immediately after /new (or /attach), the supervisor can have created
      // the run while this poll still sees the previous list.  Switching to
      // the first row here loses the newly selected run and makes the UI look
      // as if creation/attach failed.  Keep an explicit selection until the
      // run-scoped feed receives its authoritative 404/4404 signal; only
      // choose a default when there is no selection at all.
      if (!runId && items.length) {
        setRunId(items[0].id);
        setError('');
      }
      }).catch(onFailure);
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => {
      window.clearInterval(timer);
      runListGeneration.current += 1;
    };
  }, [runId, authRevision, text]);

  useEffect(() => {
    rememberRunId(runId);
    setTrajectories({});
    trajectoryInFlight.current.clear();
    trajectoryLoadedSizes.current.clear();
    trajectoryRequestSeq.current.clear();
    liveTrajectoryInFlight.current.clear();
    artifactRequest.current += 1;
    trajectoryRequest.current += 1;
    setArtifactList(null);
    setArtifactError('');
    setArtifactName('');
    setArtifactText('');
    setTrajectoryData(null);
    setTrajectoryError('');
  }, [runId]);

  useEffect(() => {
    if (!feed.error || !/\b401\b/u.test(feed.error)) return;
    setAuthOpen(true);
    setError(text('此 Web 服务需要访问令牌，请在连接设置中填写。', 'This Web service requires an access token. Enter it in Connection settings.'));
  }, [feed.error, text]);

  const recentRounds = useMemo(() => {
    const recent = snapshot.rounds.slice(-MAX_TRAJECTORY_ROUNDS);
    if (activeRound !== null && !recent.some((round) => round.round_index === activeRound)) {
      const active = snapshot.rounds.find((round) => round.round_index === activeRound);
      if (active) return [...recent.slice(-(MAX_TRAJECTORY_ROUNDS - 1)), active];
    }
    return recent;
  }, [snapshot.rounds, activeRound]);
  const trajectoryTargets = recentRounds.flatMap((round) => (round.roles || []).map((role) => ({
    key: `${round.round_index}:${role}`,
    requestKey: trajectoryIdentity(round.round_index, role),
    round: round.round_index,
    role,
    size: Number.isFinite(round.role_sizes?.[role]) ? Number(round.role_sizes?.[role]) : -1,
  })));
  const trajectoryKeys = trajectoryTargets.map((target) => `${target.key}@${target.size}`).join('|');
  useEffect(() => {
    if (!runId || !trajectoryKeys) return;
    let cancelled = false;
    const loadAll = async () => {
      const pending = trajectoryTargets.filter((target) => {
        const loadedSize = trajectoryLoadedSizes.current.get(target.key);
        return !Object.hasOwn(trajectories, target.key)
          || (target.size >= 0 && loadedSize !== target.size);
      }).filter((target) => !trajectoryInFlight.current.has(target.requestKey));
      if (!pending.length) return;
      // Four concurrent reads keep a large historical run from opening one
      // request per role/round and still drain the queue in the same effect.
      const queue = [...pending];
      const worker = async () => {
        while (!cancelled) {
          const target = queue.shift();
          if (!target) return;
          trajectoryInFlight.current.add(target.requestKey);
          const requestId = beginTrajectoryRequest(target.requestKey);
          try {
            const trajectory = await fetchTrajectory(runId, target.round, target.role);
            if (!cancelled) commitTrajectory(target.key, target.requestKey, requestId, trajectory, target.size >= 0 ? target.size : undefined);
          } catch {
            // A role file is created lazily; the live poll/snapshot will retry.
          } finally {
            trajectoryInFlight.current.delete(target.requestKey);
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(4, pending.length) }, () => worker()));
    };
    void loadAll();
    return () => { cancelled = true; };
  // ``trajectoryKeys`` is a compact version vector (round/role/byte size).
  // It changes when a live JSONL file grows, so completed roles also refresh.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, trajectoryKeys, trajectories]);

  // Trajectory files are the live assistant response. Polling them fills the
  // gap between lifecycle events, so the user sees intermediate text while an
  // agent is still thinking/working instead of only seeing the final report.
  useEffect(() => {
    if (!runId || activeRound === null || !activeRole) return;
    let cancelled = false;
    const key = `${activeRound}:${activeRole}`;
    const requestKey = trajectoryIdentity(activeRound, activeRole);
    const load = async () => {
      if (liveTrajectoryInFlight.current.has(requestKey)) return;
      liveTrajectoryInFlight.current.add(requestKey);
      const requestId = beginTrajectoryRequest(requestKey);
      try {
        const next = await fetchTrajectory(runId, activeRound, activeRole);
        if (!cancelled) commitTrajectory(key, requestKey, requestId, next);
      } catch {
        // The role file is created lazily; the next poll will retry.
      } finally {
        liveTrajectoryInFlight.current.delete(requestKey);
      }
    };
    void load();
    const timer = window.setInterval(load, 800);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [runId, activeRound, activeRole]);

  useEffect(() => {
    if (!detailsOpen || !runId) return;
    const requestId = ++artifactRequest.current;
    setArtifactList(null);
    setArtifactError('');
    setArtifactName('');
    setArtifactText('');
    if (selectedRound === null) return () => { artifactRequest.current += 1; };
    void fetchArtifacts(runId, selectedRound)
      .then((next) => { if (requestId === artifactRequest.current) setArtifactList(next); })
      .catch((reason) => { if (requestId === artifactRequest.current) { setArtifactList(null); setArtifactError(String(reason)); } });
    return () => { artifactRequest.current += 1; };
  }, [detailsOpen, runId, selectedRound, artifactReload]);

  useEffect(() => {
    if (!detailsOpen || detailsTab !== 'trajectory' || !runId) return;
    const requestId = ++trajectoryRequest.current;
    setTrajectoryData(null);
    setTrajectoryError('');
    if (selectedRound === null || !trajectoryRole) return () => { trajectoryRequest.current += 1; };
    void fetchTrajectory(runId, selectedRound, trajectoryRole)
      .then((next) => { if (requestId === trajectoryRequest.current) setTrajectoryData(next); })
      .catch((reason) => { if (requestId === trajectoryRequest.current) { setTrajectoryData(null); setTrajectoryError(String(reason)); } });
    return () => { trajectoryRequest.current += 1; };
  }, [detailsOpen, detailsTab, runId, selectedRound, trajectoryRole, trajectoryReload]);

  useEffect(() => {
    if (selectedRound === null) return;
    const roles = snapshot.rounds.find((round) => round.round_index === selectedRound)?.roles || [];
    if (roles.length && !roles.includes(trajectoryRole)) setTrajectoryRole(roles[0]);
  }, [selectedRound, snapshot.rounds, trajectoryRole]);

  useEffect(() => {
    if (followLatest && conversationRef.current) conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
  }, [messages, pendingApprovals.length, snapshot.run.status, followLatest]);

  useEffect(() => {
    if (!mobileStatusOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setMobileStatusOpen(false); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [mobileStatusOpen]);

  useEffect(() => {
    if (!detailsOpen && !authOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      if (authOpen) setAuthOpen(false);
      else {
        dispatchPanel({ type: 'close' });
        setDetailsOpen(false);
        setCreatingNew(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [detailsOpen, authOpen]);

  async function submitOperatorInstruction(messageText: string) {
    const requestKey = idempotencyKey('instruction');
    const createdAt = Date.now() / 1000;
    setBusy(true);
    setError('');
    setInstruction('');
    feed.appendOperatorMessage({ id: requestKey, text: messageText, created_at: createdAt, status: 'queued' });
    try {
      await postInstruction(runId, messageText, requestKey);
    } catch (reason) {
      feed.appendOperatorMessage({ id: requestKey, text: messageText, created_at: createdAt, status: 'failed' });
      setError(String(reason));
      setBusy(false);
      return;
    }
    try {
      await feed.refresh();
    } catch (reason) {
      // The command was accepted, so keep the optimistic queued message. The
      // live feed will reconcile it even if this one REST refresh failed.
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function sendInstruction() {
    const inputText = instruction.trim();
    if (!inputText) return;
    const command = parseCommand(inputText);
    if (command) {
      if (!commandOptions.some((item) => item.name === command.name)) { setError(text(`命令不可用：/${command.name}`, `Command unavailable: /${command.name}`)); setInstruction(''); return; }
      if (command.name === 'help') { setError(commandHelpText(capabilities, snapshot, Boolean(runId), language)); setInstruction(''); return; }
      if (command.name === 'details' || command.name === 'events' || command.name === 'artifacts' || command.name === 'trajectory') {
        const panel = command.name as PanelName;
        dispatchPanel({ type: 'open', panel });
        setDetailsTab(panel === 'details' ? 'artifacts' : panel);
        setSelectedRound(activeRound);
        setDetailsOpen(true);
        setInstruction('');
        return;
      }
      if (command.name === 'runs') { setError(runs.map((run) => `${run.id} · ${run.task || 'Untitled task'}`).join('\n') || text('暂无会话', 'No tasks yet')); setInstruction(''); return; }
      if (command.name === 'attach') {
        if (!command.args[0]) setError(text('用法：/attach <run_id>', 'Usage: /attach <run_id>'));
        else setRunId(command.args[0]);
        setInstruction('');
        return;
      }
      if (command.name === 'new') {
        const parsed = parseNewRunArgs(command.args);
        if (parsed.error || !parsed.task) {
          setError(parsed.error ? commandErrorText(parsed.error, language) : text('用法：/new <task> [--language zh|en] [--manager-agent id] [--manager-model id] [--executor-agent id] [--executor-model id] [--auditor-agent id] [--auditor-model id] [--workspace path] [--rounds n]', 'Usage: /new <task> [--language zh|en] [--manager-agent id] [--manager-model id] [--executor-agent id] [--executor-model id] [--auditor-agent id] [--auditor-model id] [--workspace path] [--rounds n]'));
          setInstruction('');
          return;
        }
        setControlBusy(true); setError('');
        const selectedAgent = parsed.agent || meta?.defaults?.agent || meta?.agents?.find((item) => item.available !== false)?.id || 'codex';
        const selectedModel = parsed.model || (parsed.agent ? undefined : meta?.defaults?.model);
        const payload = {
          task: parsed.task,
          agent: selectedAgent,
          model: selectedModel,
          roles: parsed.roles,
          workspace: parsed.workspace,
          max_rounds: parsed.maxRounds || 25,
          prompt_language: parsed.promptLanguage || 'zh' as const,
        };
        const request = operationKey('create', JSON.stringify(payload));
        try {
          const created = await createRun(payload, request.key);
          setRunId(created.id);
          clearOperationKey(request.id);
          setInstruction('');
        } catch (reason) { setError(String(reason)); }
        finally { setControlBusy(false); }
        return;
      }
      if (command.name === 'approve') {
        if (command.args.length < 2) setError(text('用法：/approve <approval_id> <action>', 'Usage: /approve <approval_id> <action>'));
        else void approve(command.args[0], command.args[1]);
        setInstruction('');
        return;
      }
      if (command.name === 'inject') {
        const injected = command.args.join(' ').trim();
        if (!runId || !injected) { setError(text('用法：/inject <text>', 'Usage: /inject <text>')); setInstruction(''); return; }
        await submitOperatorInstruction(injected);
        return;
      }
      if (command.name === 'stop' || command.name === 'abort' || command.name === 'resume') { void lifecycle(command.name); setInstruction(''); return; }
    }
    if (!runId) return;
    if (!snapshot.controls.can_inject) {
      setError(text('当前任务已结束或不可控；可使用 /new、/attach、/resume 或打开详情。', 'This task has ended or cannot accept input. Use /new, /attach, /resume, or open Details.'));
      setInstruction('');
      return;
    }
    await submitOperatorInstruction(inputText);
  }

  async function approve(approvalId: string, action: string, inputOverride?: string) {
    const submittedInput = inputOverride ?? (approvalInputs[approvalId] || '');
    setBusy(true); setError('');
    try {
      await resolveApproval(runId, approvalId, action, submittedInput);
      feed.recordApprovalResponse(approvalId, action, submittedInput.trim(), Date.now() / 1000);
      setApprovalInputs((current) => {
        const next = { ...current };
        delete next[approvalId];
        return next;
      });
      await feed.refresh();
    }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function lifecycle(action: 'stop' | 'abort' | 'resume') {
    setControlBusy(true); setError('');
    const request = operationKey(action, runId);
    try {
      if (action === 'stop') await stopRun(runId, request.key);
      if (action === 'abort') await abortRun(runId, request.key);
      if (action === 'resume') {
        const created = await resumeRun(runId, request.key);
        setRunId(created.id);
      } else {
        // Stop/Abort changes supervisor control state without appending a role
        // event. Refresh immediately so the header and rail switch to
        // "stopping" instead of waiting for the next WebSocket lifecycle poll.
        await feed.refresh();
      }
      clearOperationKey(request.id);
    } catch (reason) { setError(String(reason)); }
    finally { setControlBusy(false); }
  }

  async function openArtifact(round: number, name: string) {
    const requestId = ++artifactRequest.current;
    setArtifactName(name);
    if (isImageArtifact(name)) {
      setArtifactText('');
      return;
    }
    setArtifactText(text('正在加载…', 'Loading…'));
    try {
      const text = await fetchArtifact(runId, round, name);
      if (requestId === artifactRequest.current) setArtifactText(text);
    }
    catch (reason) { if (requestId === artifactRequest.current) setArtifactText(String(reason)); }
  }

  async function submitNewRun(task: string, roles: Record<PublicRole, RoleRuntimeConfig>, workspace: string, maxRounds: string, promptLanguage: 'en' | 'zh') {
    setControlBusy(true); setError('');
    const manager = roles.manager;
    const payload = { task, agent: manager.agent, model: manager.model || undefined, roles, workspace: workspace || undefined, max_rounds: normaliseMaxRounds(maxRounds), prompt_language: promptLanguage };
    const request = operationKey('create', JSON.stringify(payload));
    try {
      const created = await createRun(payload, request.key);
      setRunId(created.id); setCreatingNew(false); setDetailsOpen(false);
      clearOperationKey(request.id);
    } catch (reason) { setError(String(reason)); }
    finally { setControlBusy(false); }
  }

  async function refreshModelCatalogue() {
    const next = await refreshModels();
    setMeta(next);
    setCapabilities(next.capabilities);
  }

  return (
    <div className="codex-shell codex-workbench">
      <aside className="codex-sidebar">
        <div className="codex-brand"><span className="codex-mark"><Sparkles size={14} /></span><span>LongHorizon</span><div className="ui-language-switch" role="group" aria-label={text('界面语言', 'Interface language')}><button type="button" className={language === 'zh' ? 'active' : ''} aria-pressed={language === 'zh'} onClick={() => setLanguage('zh')} title="中文">中</button><button type="button" className={language === 'en' ? 'active' : ''} aria-pressed={language === 'en'} onClick={() => setLanguage('en')} title="English">EN</button></div></div>
        <button className="new-task-button" onClick={() => { setCreatingNew(true); setDetailsOpen(true); }} disabled={!canCreateRun}><Plus size={14} />{text('新任务', 'New task')}</button>
        <label className="session-search"><span><Search size={13} /></span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={text('搜索会话', 'Search tasks')} /></label>
        <div className="session-label">{text('会话', 'Tasks')} <span>{runs.length}</span></div>
        <div className="session-list">
          {filteredRuns.map((run) => <button key={run.id} className={`session-item ${run.id === runId ? 'selected' : ''}`} onClick={() => setRunId(run.id)}><span className={statusClass(run.status)} /><span className="session-copy"><strong>{run.task || 'Untitled task'}</strong><small>{run.id}</small></span></button>)}
          {!filteredRuns.length && <div className="sidebar-empty">{text('暂无会话', 'No tasks yet')}</div>}
        </div>
        <div className="sidebar-footer"><span className={`connection-dot connection-${runId ? connection : 'idle'}`} />{!runId ? text('空闲', 'Idle') : connection === 'connected' ? text('已连接', 'Connected') : connection === 'loading' ? text('连接中', 'Connecting') : connection === 'reconnecting' ? text('重连中', 'Reconnecting') : connection === 'closed' ? text('已断开', 'Disconnected') : text('连接异常', 'Connection error')}<span>·</span> {text('本地工作区', 'Local workspace')}<button type="button" className="connection-settings" onClick={() => setAuthOpen(true)} title={text('连接设置', 'Connection settings')} aria-label={text('连接设置', 'Connection settings')}><KeyRound size={13} /></button></div>
      </aside>

      <main className="codex-main">
        <header className="codex-header"><div className="header-context"><span className="header-folder"><PanelTop size={16} /></span><strong className="header-title">{currentRun?.task || runId || text('新会话', 'New task')}</strong><span className="header-more"><Ellipsis size={16} /></span></div><div className="header-actions"><span className={statusClass(snapshot.run.status)} /><span className="header-status">{statusLabel(snapshot.run.status, Boolean(statusView.finalResponse), language)}</span><button className="mobile-status-button" onClick={() => setMobileStatusOpen((open) => !open)} aria-label={text('打开任务状态', 'Open task status')} title={text('任务状态', 'Task status')}><PanelRight size={14} /><span>{text('状态', 'Status')}</span></button>{canStopRun && <button onClick={() => void lifecycle('stop')} disabled={controlBusy}>{text('停止', 'Stop')}</button>}{canAbortRun && <button className="danger-text" onClick={() => void lifecycle('abort')} disabled={controlBusy}>{text('中止', 'Abort')}</button>}{canResumeRun && <button onClick={() => void lifecycle('resume')} disabled={controlBusy}>{text('重新开始', 'Restart')}</button>}<button className="details-button" disabled={!runId} onClick={() => { if (!runId) return; setCreatingNew(false); setSelectedRound(activeRound); dispatchPanel({ type: 'open', panel: 'details' }); setDetailsOpen(true); }} aria-label={text('查看详情', 'View details')} title={runId ? text('详情', 'Details') : text('选择任务后查看详情', 'Select a task to view details')}><FolderOpen size={14} /><span>{text('详情', 'Details')}</span></button></div></header>

        <section className="conversation" aria-label="LongHorizon conversation" ref={conversationRef} onScroll={(event) => { const node = event.currentTarget; setFollowLatest(node.scrollHeight - node.scrollTop - node.clientHeight < 48); }}>
          {!runId && <div className="welcome"><div className="welcome-mark"><Sparkles size={20} /></div><h1>{text('你想完成什么？', 'What do you want to accomplish?')}</h1><p>{text('开始一个真实的 LongHorizon 任务。计划、中间过程、验证和最终回答会像 Codex 一样显示在这里。', 'Start a real LongHorizon task. Plans, intermediate work, verification, and the final answer will appear here as they happen.')}</p>{meta && !canCreateRun && <p className="welcome-note">{text('当前连接为只读模式；启动 ', 'This connection is read-only. Start ')}<code>lh-harness web</code>{text(' 后即可创建任务。', ' to create tasks.')}</p>}{!meta && <p className="welcome-note">{text('正在连接 Web 服务…', 'Connecting to the Web service…')}</p>}<button className="welcome-button" disabled={!canCreateRun} onClick={() => { setCreatingNew(true); setDetailsOpen(true); }}><Plus size={15} />{text('开始任务', 'Start task')}</button></div>}
          {runId && messages.map((message) => <ConversationMessage key={message.id} message={message} />)}
          {runId && !messages.length && <div className="empty-conversation"><span className="thinking-dot" />{text('等待 LongHorizon 启动…', 'Waiting for LongHorizon to start…')}</div>}
          {['running', 'starting', 'stopping', 'aborting'].includes(snapshot.run.status) && <div className="working-line" role="status" aria-live="polite"><span className="thinking-dot" /><span>{snapshot.run.status === 'stopping' || snapshot.run.status === 'aborting' ? text('正在收尾并停止 worker', 'Finishing up and stopping the worker') : activeRole ? text(`${roleTitle(activeRole)} 正在处理`, `${roleTitle(activeRole)} is working`) : text('LongHorizon 正在处理', 'LongHorizon is working')}</span></div>}
          {pendingApprovals.map((approval) => <ApprovalCard key={approval.approval_id} approval={approval} busy={busy} userInput={approvalInputs[approval.approval_id] || ''} onUserInput={(value) => setApprovalInputs((current) => ({ ...current, [approval.approval_id]: value }))} onApprove={approve} />)}
        </section>

        {visibleError && <div className="error-line" role="alert" aria-live="assertive"><span><AlertTriangle size={14} /></span>{visibleError}<button onClick={() => { setError(''); setDismissedFeedError(feed.error); }}>{text('关闭', 'Dismiss')}</button></div>}
        <div className={`composer-wrap ${composerInteractive ? '' : 'composer-disabled'}`}><textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); void sendInstruction(); } }} placeholder={composerPlaceholder} disabled={busy || !composerInteractive} /><div className="composer-footer"><span>{composerFooter}{composerInteractive && <> · <kbd>⌘</kbd><kbd>↵</kbd> {text('发送', 'Send')}</>}</span><button onClick={() => void sendInstruction()} disabled={busy || !composerCanSend}>{text('发送', 'Send')}</button></div></div>
      </main>

      {mobileStatusOpen && <button type="button" className="status-mobile-backdrop" onClick={() => setMobileStatusOpen(false)} aria-label={text('关闭任务状态', 'Close task status')} />}
      <StatusPanel
        view={statusView}
        snapshot={snapshot}
        connection={connection}
        mobileOpen={mobileStatusOpen}
        onMobileClose={() => setMobileStatusOpen(false)}
        onDetails={() => {
          setCreatingNew(false);
          setSelectedRound(activeRound);
          dispatchPanel({ type: 'open', panel: 'details' });
          setDetailsOpen(true);
        }}
      />

      {detailsOpen && <DetailsDrawer creating={creatingNew} runId={runId} snapshot={snapshot} meta={meta} selectedRound={selectedRound} setSelectedRound={setSelectedRound} tab={panelState.panel === 'details' ? detailsTab : panelState.panel} setTab={(tab) => { setDetailsTab(tab); dispatchPanel({ type: 'open', panel: tab === 'artifacts' && panelState.panel === 'details' ? 'details' : tab }); }} artifactList={artifactList} artifactError={artifactError} artifactName={artifactName} artifactText={artifactText} openArtifact={openArtifact} retryArtifacts={() => setArtifactReload((value) => value + 1)} trajectoryRole={trajectoryRole} setTrajectoryRole={setTrajectoryRole} trajectoryData={trajectoryData} trajectoryError={trajectoryError} retryTrajectory={() => setTrajectoryReload((value) => value + 1)} onClose={() => { dispatchPanel({ type: 'close' }); setDetailsOpen(false); setCreatingNew(false); }} onCreate={submitNewRun} onRefreshModels={refreshModelCatalogue} controlBusy={controlBusy} />}
      {authOpen && <AuthDialog onClose={() => setAuthOpen(false)} onSaved={() => { setAuthOpen(false); setAuthRevision((value) => value + 1); setError(''); setDismissedFeedError(''); }} />}
    </div>
  );
}

function localizedNextStep(view: StatusView, language: UiLanguage): string {
  if (view.nextStepKey === 'approval') return uiText(language, '处理待确认请求后继续。', 'Resolve the pending approval to continue.');
  if (view.nextStepKey === 'gui') return uiText(language, '执行 GUI 子任务。', 'Execute the GUI subtask.');
  if (view.nextStepKey === 'cli') return uiText(language, '执行 CLI 子任务。', 'Execute the CLI subtask.');
  if (view.nextStepKey === 'ask') return /waiting for your input/iu.test(view.nextStep) ? uiText(language, '等待你的输入。', 'Waiting for your input.') : view.nextStep;
  if (view.nextStepKey === 'blocked') return uiText(language, '先解决阻塞项，再继续任务。', 'Resolve the blocker before continuing.');
  if (view.nextStepKey === 'invalid') return uiText(language, 'Manager 需要修订当前计划。', 'The Manager needs to revise the current plan.');
  if (view.nextStepKey === 'done') return view.status === 'done' ? uiText(language, '任务已完成，无需后续操作。', 'The task is complete. No further action is needed.') : uiText(language, '等待最终验证。', 'Waiting for final verification.');
  if (view.nextStepKey === 'stopping') return uiText(language, '正在停止运行，等待 worker 收尾。', 'Stopping the run and waiting for the worker to finish.');
  if (view.nextStepKey === 'manager') return uiText(language, 'Manager 正在准备下一步。', 'The Manager is preparing the next step.');
  if (view.nextStepKey === 'executor') return uiText(language, 'Executor 正在执行计划。', 'The Executor is carrying out the plan.');
  if (view.nextStepKey === 'auditor') return uiText(language, 'Auditor 正在验证最新结果。', 'The Auditor is verifying the latest result.');
  if (view.nextStepKey === 'record') return uiText(language, '记录本轮结果并继续。', 'Record this round and continue.');
  if (view.nextStepKey === 'custom') return view.nextStep;
  if (/^start manager planning\.?$/iu.test(view.nextStep)) return uiText(language, '等待 Manager 生成计划。', 'Waiting for the Manager to create a plan.');
  if (/^no further action\.?$/iu.test(view.nextStep)) return uiText(language, '任务已结束，无需后续操作。', 'The task has ended. No further action is needed.');
  return view.nextStep;
}

function localizedRoleSummary(role: StatusView['roleStatuses'][number], language: UiLanguage): string {
  if (role.status === 'skipped') return uiText(language, '本轮未触发。', 'Not triggered in this round.');
  const summary = role.summary.trim();
  if (!summary || /^No .+ output recorded\.?$/iu.test(summary)) return uiText(language, '尚未记录输出。', 'No output recorded yet.');
  if (/^done$/iu.test(summary)) return uiText(language, '已完成。', 'Completed.');
  if (/^Status:\s*complete\b/iu.test(summary)) return uiText(language, '审计通过，完整性正常。', 'Audit passed; the result is complete.');
  if (/^Status:\s*(?:incomplete|blocked)\b/iu.test(summary)) return uiText(language, '审计发现问题，需要处理。', 'The audit found an issue that needs attention.');
  return summary;
}

function localizedStageStatus(status: string, language: UiLanguage): string {
  const zh = status === 'active' ? '当前阶段' : status === 'stopping' ? '正在停止' : status === 'waiting' ? '等待确认' : status === 'done' ? '已记录结果' : status === 'failed' ? '需要处理' : status === 'blocked' ? '已阻塞' : status === 'cancelled' ? '已停止' : status === 'skipped' ? '本轮未触发' : '等待前置阶段';
  const en = status === 'active' ? 'Current stage' : status === 'stopping' ? 'Stopping' : status === 'waiting' ? 'Awaiting approval' : status === 'done' ? 'Result recorded' : status === 'failed' ? 'Needs attention' : status === 'blocked' ? 'Blocked' : status === 'cancelled' ? 'Stopped' : status === 'skipped' ? 'Not triggered' : 'Waiting for prior stage';
  return uiText(language, zh, en);
}

function StatusPanel({ view, snapshot, connection, mobileOpen, onMobileClose, onDetails }: { view: StatusView; snapshot: Snapshot; connection: string; mobileOpen: boolean; onMobileClose: () => void; onDetails: () => void }) {
  const { language, text } = useUiLanguage();
  const hasRun = Boolean(view.runId && view.task.trim());
  const round = view.rounds.find((item) => item.round === (view.currentRound ?? view.activeRound)) || view.rounds.at(-1);
  const stages = round?.stages.filter((stage) => stage.key !== 'record') || view.stages.filter((stage) => stage.key !== 'record').slice(-3);
  const roleItems = hasRun ? view.roleStatuses : [];
  // `record` is a persistence marker, not an operator-visible stage. Use the
  // same execution-stage denominator for the chain counter and progress bar.
  const executionStages = view.stages.filter((stage) => stage.key !== 'record' && stage.status !== 'skipped');
  const roundExecutionStages = stages.filter((stage) => stage.key !== 'record' && stage.status !== 'skipped');
  const doneCount = roundExecutionStages.filter((stage) => stage.status === 'done').length;
  const totalCount = roundExecutionStages.length;
  const percent = Math.round(view.progress.ratio * 100);
  const nextStep = hasRun ? localizedNextStep(view, language) : text('创建或选择一个任务。', 'Create or select a task.');
  const routedTask = view.nextStepDetail ? managerPlanSummary(view.nextStepDetail, 180) : '';
  const nextDetail = !hasRun
    ? text('任务开始后，这里会显示 Manager / Executor / Auditor 的真实结果。', 'The actual Manager, Executor, and Auditor results will appear here after the task starts.')
    : routedTask
    ? routedTask
    : view.nextStepDetail
    ? compactText(view.nextStepDetail.replace(/^Current task state:\s*/iu, '').replace(/^Completed:\s*/iu, text('已完成：', 'Completed: ')), 180)
    : text('状态会随着 Manager / Executor / Auditor 的结果自动更新。', 'Status updates automatically as Manager, Executor, and Auditor results arrive.');
  const connectionLabel = !hasRun ? text('未选择任务', 'No task selected') : connection === 'connected' ? (['done', 'failed', 'blocked', 'cancelled'].includes(view.status) ? text('已同步', 'Synced') : text('实时', 'Live')) : connection === 'loading' ? text('连接中', 'Connecting') : connection === 'reconnecting' ? text('重连中', 'Reconnecting') : connection === 'closed' ? text('已断开', 'Disconnected') : text('连接异常', 'Connection error');
  const localized = statusLabel(view.runStatus || view.status, Boolean(view.finalResponse), language);
  const resultLabel = view.status === 'failed'
    ? text('结果失败', 'Failed')
    : view.status === 'blocked'
      ? text('结果未完成', 'Incomplete')
        : view.status === 'cancelled'
          ? text('结果已停止', 'Stopped')
          : view.runStatus === 'aborting' || view.runStatus === 'aborted'
            ? text('正在中止', 'Aborting')
            : view.status === 'stopping'
              ? text('正在停止', 'Stopping')
              : view.status === 'done'
                ? text('任务完成', 'Complete')
                : text('尚未结束', 'In progress');
  const roundBudget = Number(snapshot.run.max_rounds || 0);
  const budgetSuffix = roundBudget > 0 ? ` / ${roundBudget}` : '';
  const roundLabel = view.activeRound !== null ? text(`第 ${view.activeRound}${budgetSuffix} 轮`, `Round ${view.activeRound}${budgetSuffix}`) : view.currentRound !== null ? text(`最近 R${view.currentRound}${budgetSuffix}`, `Latest R${view.currentRound}${budgetSuffix}`) : text('未开始', 'Not started');
  const roleState = (status: string) => status === 'active' ? text('进行中', 'In progress') : status === 'stopping' ? text('正在停止', 'Stopping') : status === 'waiting' ? text('等待确认', 'Awaiting approval') : status === 'done' ? text('已完成', 'Completed') : status === 'failed' ? text('失败', 'Failed') : status === 'blocked' ? text('阻塞', 'Blocked') : status === 'skipped' ? text('未触发', 'Not triggered') : text('待开始', 'Pending');
  return <aside className={`status-panel ${mobileOpen ? 'mobile-open' : ''}`} aria-label={text('任务状态面板', 'Task status panel')} aria-live="polite">
    <div className="status-panel-head"><div><span className="status-eyebrow">RUN STATUS</span><h2>{text('任务状态', 'Task status')}</h2></div><div className="status-panel-head-actions"><span className={`status-panel-dot phase-${view.status} connection-${hasRun ? connection : 'idle'}`} title={`${localized} · ${connectionLabel}`} /><button className="status-mobile-close" onClick={onMobileClose} aria-label={text('关闭任务状态', 'Close task status')}><X size={16} /></button></div></div>
    <div className="status-overview"><div className="status-overview-line"><strong>{localized}</strong><span>{connectionLabel}</span></div><p>{view.taskSummary || text('尚未选择任务', 'No task selected')}</p><div className={`status-progress status-progress-${view.status}`}><i style={{ width: `${percent}%` }} /></div><div className="status-progress-meta"><span>{executionStages.length ? text(`${percent}% 全局 · ${resultLabel}${totalCount ? ` · 本轮 ${doneCount}/${totalCount}` : ''}`, `${percent}% overall · ${resultLabel}${totalCount ? ` · This round ${doneCount}/${totalCount}` : ''}`) : hasRun ? text('等待第一轮', 'Waiting for the first round') : text('尚未开始', 'Not started')}</span><span>{roundLabel}</span></div></div>
    <section className="status-section"><div className="status-section-title"><span>{text('本轮执行链', 'Execution chain')}</span><small>{totalCount ? `${doneCount}/${totalCount}` : '—'}</small></div>{stages.length ? <ol className="status-stage-list">{stages.filter((stage) => stage.key !== 'record').map((stage) => <li className={`status-stage stage-${stage.status}`} key={stage.id}><span className="stage-marker">{stage.status === 'done' ? <Check size={10} /> : stage.status === 'failed' ? <X size={10} /> : stage.status === 'active' || stage.status === 'stopping' ? <LoaderCircle className="trajectory-spinner" size={10} /> : stage.status === 'waiting' ? <CircleDotDashed size={10} /> : stage.status === 'blocked' ? <AlertTriangle size={10} /> : <Circle size={8} />}</span><div><strong>{stage.label}</strong><small>{localizedStageStatus(stage.status, language)}</small></div></li>)}</ol> : <p className="status-empty-copy">{text('暂无轮次结果；任务开始后会在这里显示每个阶段。', 'No round results yet. Each stage will appear here after the task starts.')}</p>}</section>
    <section className="status-section"><div className="status-section-title"><span>{text('下一步', 'Next step')}</span><small>{round ? (view.activeRound !== null ? `R${round.round}` : text(`最近 R${round.round}`, `Latest R${round.round}`)) : '—'}</small></div><div className="status-next-step"><span className="status-next-icon"><ArrowRight size={13} /></span><div><strong>{nextStep}</strong><p>{nextDetail}</p></div></div></section>
    {hasRun && <section className="status-section"><div className="status-section-title"><span>{text('角色输出', 'Role output')}</span><small>{roleItems.length}</small></div><div className="status-role-list">{roleItems.map((role) => <div className="status-role-row" key={role.key}><span className={`role-marker phase-${role.status}`}>{role.key === 'manager' ? 'M' : role.key === 'executor' ? 'E' : 'A'}</span><div className="status-role-copy"><div><strong>{role.label}</strong><span className={`role-status-text phase-${role.status}`}>{roleState(role.status)}</span></div><small>{localizedRoleSummary(role, language)}</small></div></div>)}</div></section>}
    {(view.pendingApprovals.length > 0 || view.warnings.length > 0) && <section className="status-section status-notices"><div className="status-section-title"><span>{text('需要关注', 'Needs attention')}</span><small>{view.pendingApprovals.length + view.warnings.length}</small></div>{view.pendingApprovals.length > 0 && <div className="status-notice notice-approval"><span><AlertTriangle size={11} /></span><div><strong>{text('等待你的确认', 'Waiting for your approval')}</strong><small>{text(`${view.pendingApprovals.length} 个审批请求暂停了任务`, `${view.pendingApprovals.length} approval request${view.pendingApprovals.length === 1 ? '' : 's'} paused the task`)}</small></div></div>}{view.warnings.map((warning, index) => <div className="status-notice notice-warning" key={`${warning}-${index}`}><span><AlertTriangle size={11} /></span><div><strong>{text('运行提示', 'Run notice')}</strong><small>{compactText(warning, 180)}</small></div></div>)}</section>}
    {hasRun && <button className="status-details-link" onClick={onDetails}>{text('查看 artifacts、轨迹与事件', 'View artifacts, trajectory, and events')} <ExternalLink size={13} /></button>}
  </aside>;
}

const ACTIVITY_ICONS: Record<ActivityAction, LucideIcon> = {
  read: BookOpen,
  edit: FilePenLine,
  validate: FlaskConical,
  search: Search,
  task: ListChecks,
  screenshot: Camera,
  command: SquareTerminal,
  result: CheckCircle2,
  note: CircleDotDashed,
};

function TrajectoryIcon({ action, status }: { action: ActivityAction; status: string }) {
  const Icon = status === 'failed' ? XCircle : status === 'running' ? LoaderCircle : ACTIVITY_ICONS[action];
  return <Icon className={status === 'running' ? 'trajectory-spinner' : ''} size={15} strokeWidth={1.8} />;
}

function TrajectorySteps({ steps }: { steps: ActivityStep[] }) {
  const { text } = useUiLanguage();
  const [expanded, setExpanded] = useState(false);
  if (!steps.length) return null;
  const hidden = Math.max(0, steps.length - 10);
  const visibleSteps = expanded || !hidden ? steps : steps.slice(-10);
  return <div className="trajectory-steps" aria-label={text('中间步骤结果', 'Intermediate steps')}>
    {hidden > 0 && <button type="button" className="trajectory-history-toggle" onClick={() => setExpanded((current) => !current)}>{expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}<span>{expanded ? text('收起更早步骤', 'Hide earlier steps') : text(`显示更早 ${hidden} 步`, `Show ${hidden} earlier steps`)}</span></button>}
    {visibleSteps.map((step, index) => {
    const summary = step.summary || step.text || '';
    const detail = step.detail && step.detail !== summary ? step.detail : '';
    const links = step.links || [];
    const images = step.images || [];
    const status = step.status || 'done';
    const action = step.action || 'note';
    return <article className={`trajectory-step-row trajectory-${status} action-${action}`} key={step.id || `${step.kind}-${index}`}>
      <span className="trajectory-step-icon" aria-hidden="true"><TrajectoryIcon action={action} status={status} /></span>
      <div className="trajectory-step-main"><div className="trajectory-step-head"><strong>{step.title || (step.kind === 'tool_use' ? text('工具调用', 'Tool call') : text('步骤结果', 'Step result'))}</strong><span>{status === 'running' ? text('进行中', 'In progress') : status === 'failed' ? text('失败', 'Failed') : text('已完成', 'Completed')}</span></div>{summary && <p>{summary}</p>}{step.result && <div className={`trajectory-result trajectory-result-${status}`}>{status === 'failed' ? <CircleX size={13} /> : status === 'running' ? <LoaderCircle className="trajectory-spinner" size={13} /> : <CircleCheck size={13} />}<span>{step.result}</span></div>}{links.length > 0 && <div className="trajectory-links">{links.slice(0, 3).map((href) => <a href={href} target="_blank" rel="noreferrer" key={href}>{href.replace(/^https?:\/\//u, '').slice(0, 58)} <ExternalLink size={11} /></a>)}</div>}{images.length > 0 && <ImageGallery images={images} label={text('中间截图', 'intermediate screenshot')} />}{step.imageWarning && <div className="trajectory-image-warning"><AlertTriangle size={12} /><span>{step.imageWarning}</span></div>}{detail && <details className="trajectory-detail"><summary>{text('查看原始详情', 'View raw details')}</summary><pre>{detail}</pre></details>}</div>
    </article>;
  })}</div>;
}

function fileVisual(file: FileChangeItem): { Icon: LucideIcon; tone: string } {
  const extension = file.extension || '';
  if (['ts', 'tsx', 'js', 'jsx'].includes(extension)) return { Icon: Braces, tone: 'file-tone-script' };
  if (extension === 'json') return { Icon: FileJson2, tone: 'file-tone-data' };
  if (['md', 'txt', 'rst'].includes(extension)) return { Icon: FileText, tone: 'file-tone-text' };
  if (['py', 'go', 'rs', 'java', 'css', 'scss', 'html', 'sh', 'zsh', 'yaml', 'yml', 'toml'].includes(extension)) return { Icon: FileCode2, tone: 'file-tone-code' };
  return { Icon: FileText, tone: 'file-tone-default' };
}

function FilePathRow({ file }: { file: FileChangeItem }) {
  const { text } = useUiLanguage();
  const [copied, setCopied] = useState(false);
  const { Icon, tone } = fileVisual(file);
  const copyPath = async () => {
    try {
      await navigator.clipboard.writeText(file.path);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  };
  return <li className="edited-file-row">
    <span className={`edited-file-icon ${tone}`} aria-hidden="true"><Icon size={15} strokeWidth={1.8} /></span>
    <span className="edited-file-path" title={file.path}>{file.path}</span>
    {file.additions !== null && file.deletions !== null && <span className="edited-file-lines"><span>+{file.additions}</span><span>-{file.deletions}</span></span>}
    <button type="button" className="copy-path-button" onClick={() => void copyPath()} title={copied ? text('已复制', 'Copied') : text('复制路径', 'Copy path')} aria-label={text(`复制路径 ${file.path}`, `Copy path ${file.path}`)}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
  </li>;
}

function EditedFilesCard({ projection }: { projection: ArtifactProjection['files'] }) {
  const { text } = useUiLanguage();
  const [expanded, setExpanded] = useState(false);
  if (!projection.totalFiles) return null;
  const previewCount = 3;
  const knownHidden = Math.max(0, projection.items.length - previewCount);
  const unknownPaths = Math.max(0, projection.totalFiles - projection.items.length);
  const visibleFiles = expanded ? projection.items : projection.items.slice(0, previewCount);
  const exactLines = projection.lineCountCoverage === 'complete' && projection.additions !== null && projection.deletions !== null;
  const title = projection.scope === 'agent'
    ? text(`已编辑 ${projection.totalFiles} 个文件`, `Edited ${projection.totalFiles} file${projection.totalFiles === 1 ? '' : 's'}`)
    : text(`检测到 ${projection.totalFiles} 个工作区变更`, `Detected ${projection.totalFiles} workspace change${projection.totalFiles === 1 ? '' : 's'}`);
  return <section className="edited-files-card" aria-label={title}>
    <header className="edited-files-head"><span className="edited-files-mark" aria-hidden="true"><Files size={20} strokeWidth={1.7} /></span><div><strong>{title}</strong>{projection.scope === 'workspace' && <small>{text('来自工作区 diff，未归因给 Agent', 'From the workspace diff; not attributed to the Agent')}</small>}</div>{exactLines && <span className="edited-files-total"><span>+{projection.additions}</span><span>-{projection.deletions}</span></span>}</header>
    {visibleFiles.length > 0 && <ul className="edited-file-list">{visibleFiles.map((file) => <FilePathRow file={file} key={`${file.previousPath || ''}:${file.path}`} />)}</ul>}
    {unknownPaths > 0 && <p className="edited-files-unknown">{text(`另有 ${unknownPaths} 个文件，当前轨迹未列出路径`, `${unknownPaths} additional file path${unknownPaths === 1 ? '' : 's'} were not listed in the current trajectory`)}</p>}
    {knownHidden > 0 && <button type="button" className="edited-files-toggle" onClick={() => setExpanded((current) => !current)}>{expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}<span>{expanded ? text('收起文件', 'Hide files') : text(`再显示 ${knownHidden} 个文件`, `Show ${knownHidden} more file${knownHidden === 1 ? '' : 's'}`)}</span></button>}
  </section>;
}

function validationOperationLabel(operation: ValidationResultSummary['operations'][number], language: UiLanguage): string {
  const labels = language === 'zh'
    ? { test: '测试', typecheck: '类型检查', build: '构建', lint: '静态检查', 'diff-check': 'Diff 检查' }
    : { test: 'Tests', typecheck: 'Type check', build: 'Build', lint: 'Lint', 'diff-check': 'Diff check' };
  return labels[operation];
}

function ValidationIcon({ status }: { status: ValidationResultSummary['status'] }) {
  if (status === 'passed') return <CircleCheck size={17} strokeWidth={1.9} />;
  if (status === 'failed') return <CircleX size={17} strokeWidth={1.9} />;
  if (status === 'running') return <LoaderCircle className="trajectory-spinner" size={17} strokeWidth={1.9} />;
  return <CircleDotDashed size={17} strokeWidth={1.9} />;
}

function ValidationResults({ projection }: { projection: ArtifactProjection['validations'] }) {
  const { language, text } = useUiLanguage();
  const [expanded, setExpanded] = useState(false);
  if (!projection.total) return null;
  const visibleItems = expanded ? projection.items : projection.items.slice(-6);
  const hidden = Math.max(0, projection.items.length - visibleItems.length);
  return <section className="validation-results" aria-label={text('验证结果', 'Validation results')}>
    <div className="evidence-section-title"><span><ShieldCheck size={16} />{text('验证结果', 'Validation results')}</span><small>{text(`${projection.passed} 通过`, `${projection.passed} passed`)}{projection.failed ? text(` · ${projection.failed} 失败`, ` · ${projection.failed} failed`) : ''}{projection.running ? text(` · ${projection.running} 运行中`, ` · ${projection.running} running`) : ''}</small></div>
    <div className="validation-list">{visibleItems.map((item) => <article className={`validation-row validation-${item.status}`} key={item.id}>
      <span className="validation-icon" aria-hidden="true"><ValidationIcon status={item.status} /></span>
      <div className="validation-main"><div className="validation-head"><strong>{item.label || 'Validation'}</strong><span>{item.operations.map((operation) => validationOperationLabel(operation, language)).join(' / ')}</span><small>R{item.roundIndex} · {roleTitle(item.role)}</small></div><div className="validation-summary">
        {item.passedCount !== null && <code>{text(`${item.passedCount} 通过`, `${item.passedCount} passed`)}</code>}
        {item.failedCount !== null && item.failedCount > 0 && <code className="validation-failed-chip">{text(`${item.failedCount} 失败`, `${item.failedCount} failed`)}</code>}
        {item.skippedCount !== null && item.skippedCount > 0 && <code>{text(`${item.skippedCount} 跳过`, `${item.skippedCount} skipped`)}</code>}
        {item.moduleCount !== null && <code>{text(`${item.moduleCount} 个模块`, `${item.moduleCount} modules`)}</code>}
        {item.passedCount === null && item.failedCount === null && item.moduleCount === null && <span>{item.status === 'running' ? text('正在执行', 'Running') : item.status === 'passed' ? text('通过', 'Passed') : item.status === 'failed' ? text('未通过', 'Failed') : item.summary}</span>}
      </div><details className="validation-detail"><summary>{text('查看命令', 'View command')}</summary><code>{item.command}</code></details></div>
    </article>)}</div>
    {(hidden > 0 || expanded && projection.items.length > 6) && <button type="button" className="validation-toggle" onClick={() => setExpanded((current) => !current)}>{expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}<span>{expanded ? text('收起较早结果', 'Hide earlier results') : text(`显示较早 ${hidden} 项`, `Show ${hidden} earlier results`)}</span></button>}
  </section>;
}

function ExecutionArtifacts({ projection, live }: { projection: ArtifactProjection; live: boolean }) {
  const { text } = useUiLanguage();
  if (!hasExecutionArtifacts(projection)) return null;
  return <div className={`execution-artifacts ${live ? 'execution-artifacts-live' : ''}`}>
    <div className="execution-artifacts-label"><Sparkles size={14} /><span>{live ? text('实时执行产物', 'Live execution artifacts') : text('执行产物', 'Execution artifacts')}</span>{live && <small>{text('随轨迹更新', 'Updates with the trajectory')}</small>}</div>
    <EditedFilesCard projection={projection.files} />
    <ValidationResults projection={projection.validations} />
  </div>;
}

function ConversationMessage({ message }: { message: ConversationMessage }) {
  const { text } = useUiLanguage();
  const [expanded, setExpanded] = useState(false);
  const [showInput, setShowInput] = useState(false);
  const limit = message.kind === 'plan' ? 320 : message.kind === 'verification' ? 720 : message.kind === 'final' ? 2200 : 1200;
  const clipped = !expanded && message.text.length > limit;
  const displayText = clipped ? `${message.text.slice(0, limit).trimEnd()}…` : message.text;
  const isAgent = ['plan', 'assistant', 'verification'].includes(message.kind);
  return <article className={`conversation-message message-${message.kind}`}><div className="message-avatar">{message.kind === 'user' ? text('你', 'You') : message.kind === 'final' ? <Sparkles size={12} /> : message.role.slice(0, 1)}</div><div className="message-body"><div className="message-meta"><strong>{message.title}</strong>{message.time && <time>{formatTime(message.time)}</time>}</div>{isAgent && message.input && <><button className="agent-input-row" onClick={() => setShowInput((current) => !current)}><span className="agent-step-icon"><ArrowRight size={14} /></span><span className="agent-input-copy"><small>{text('输入', 'Input')}</small>{compactText(message.input, 220)}</span><span className="agent-chevron">{showInput ? <ChevronUp size={14} /> : <ChevronDown size={14} />}</span></button>{showInput && <div className="agent-input-expanded"><MessageText text={message.input} /></div>}<div className="agent-output-label">{message.kind === 'plan' ? text('计划摘要', 'Plan summary') : message.kind === 'verification' ? text('验证结论', 'Verification result') : text('执行结果', 'Execution result')}</div></>}{message.activity && message.activity.length > 0 && <TrajectorySteps steps={message.activity} />}<MessageText text={displayText} />{message.artifacts && <ExecutionArtifacts projection={message.artifacts} live={message.kind === 'live'} />}{clipped && <details className="raw-output-details"><summary>{text('查看完整输出', 'View full output')}</summary><MessageText text={message.text} /></details>}{message.kind === 'plan' && <span className="message-hint">{text('已隐藏内部 prompt，仅保留可执行摘要', 'Internal prompts are hidden; only the actionable summary is shown')}</span>}{message.kind === 'final' && <span className={`final-badge final-authority-${message.authority || 'none'}`}>{message.authority === 'final_response' ? text('LongHorizon 最终回复', 'LongHorizon final response') : message.authority === 'report' ? text('Auditor 最终报告', 'Auditor final report') : message.authority === 'auditor' ? text('Auditor 验证', 'Auditor verification') : text('等待权威报告', 'Awaiting authoritative report')}</span>}</div></article>;
}

function ImageGallery({ images, label }: { images: string[]; label: string }) {
  const { text } = useUiLanguage();
  const [selected, setSelected] = useState<string | null>(null);
  const [fitToWindow, setFitToWindow] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [naturalSize, setNaturalSize] = useState({ width: 0, height: 0 });
  const [resolved, setResolved] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<Record<string, boolean>>({});
  const attempted = useRef(new Set<string>());
  const blobUrls = useRef(new Map<string, string>());
  const activeSources = useRef(new Set(images));
  const mounted = useRef(true);
  activeSources.current = new Set(images);

  const retry = (source: string) => {
    const objectUrl = blobUrls.current.get(source);
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    blobUrls.current.delete(source);
    attempted.current.delete(source);
    setResolved((current) => {
      const next = { ...current };
      delete next[source];
      return next;
    });
    setFailed((current) => {
      const next = { ...current };
      delete next[source];
      return next;
    });
  };

  // A gallery can live for the duration of a long run. Revoke object URLs as
  // soon as their source leaves the projection, and always release the
  // remaining URLs when the gallery unmounts.
  const imageKey = images.join('\u0000');
  useEffect(() => {
    const active = new Set(images.filter((source) => source.startsWith('/api/')));
    const activeImages = new Set(images);
    for (const [source, objectUrl] of blobUrls.current.entries()) {
      if (!active.has(source)) {
        URL.revokeObjectURL(objectUrl);
        blobUrls.current.delete(source);
      }
    }
    for (const source of attempted.current) {
      if (!active.has(source)) attempted.current.delete(source);
    }
    setResolved((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([source]) => active.has(source)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
    setFailed((current) => {
      const next = Object.fromEntries(Object.entries(current).filter(([source]) => activeImages.has(source)));
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [imageKey]);

  useEffect(() => {
    // React StrictMode replays effect setup/cleanup in development. Reset the
    // flag in setup so the real mounted pass can still accept completed loads.
    mounted.current = true;
    return () => {
      mounted.current = false;
      for (const objectUrl of blobUrls.current.values()) URL.revokeObjectURL(objectUrl);
      blobUrls.current.clear();
    };
  }, []);

  useEffect(() => {
    // `resolved[source]` is intentionally allowed to be an empty string after
    // a failed request. Use key presence, not truthiness, so a broken image is
    // rendered once as unavailable instead of entering an infinite retry loop.
    const pending = [...new Set(images.filter((source) => source.startsWith('/api/')
      && !Object.hasOwn(resolved, source)
      && !attempted.current.has(source)))];
    if (!pending.length) return undefined;
    pending.forEach((source) => attempted.current.add(source));
    void Promise.all(pending.map(async (source) => {
      try {
        const objectUrl = await fetchImageSource(source);
        return [source, objectUrl] as const;
      } catch {
        return [source, ''] as const;
      }
    })).then((items) => {
      const accepted = items.filter(([source, objectUrl]) => {
        if (mounted.current && activeSources.current.has(source)) return true;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        return false;
      });
      if (!mounted.current || !accepted.length) return;
      for (const [source, objectUrl] of accepted) if (objectUrl) blobUrls.current.set(source, objectUrl);
      setResolved((current) => ({ ...current, ...Object.fromEntries(accepted) }));
    });
    // Do not cancel an in-flight fetch just because the SSE feed caused a
    // rerender. `attempted` prevents a duplicate request, while `activeSources`
    // rejects and releases results for images that really left the gallery.
    return undefined;
  }, [imageKey, resolved]);

  useEffect(() => {
    if (!selected) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelected(null);
      if (event.key === '+' || event.key === '=') { setFitToWindow(false); setZoom((value) => Math.min(4, value + 0.25)); }
      if (event.key === '-') { setFitToWindow(false); setZoom((value) => Math.max(0.25, value - 0.25)); }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [selected]);

  const openImage = (source: string) => {
    setNaturalSize({ width: 0, height: 0 });
    setZoom(1);
    setFitToWindow(true);
    setSelected(source);
  };

  if (!images.length) return null;
  return <>
    <div className="image-gallery" aria-label={label}>
      {images.map((source, index) => { const apiSource = source.startsWith('/api/'); const pending = apiSource && !Object.hasOwn(resolved, source); const display = apiSource ? resolved[source] : source; const unavailable = !pending && (!display || failed[source]); return <button type="button" className={`image-thumb-button ${unavailable ? 'image-thumb-unavailable' : ''}`} key={`${source.slice(0, 32)}-${index}`} onClick={() => unavailable ? retry(source) : display && openImage(display)} aria-label={unavailable ? text(`重试${label} ${index + 1}`, `Retry ${label} ${index + 1}`) : text(`查看${label} ${index + 1}`, `View ${label} ${index + 1}`)} disabled={pending}>{pending ? <span>{text('加载图片…', 'Loading image…')}</span> : unavailable ? <span>{text('图片不可用 · 点击重试', 'Image unavailable · Click to retry')}</span> : <img className="image-thumb" src={display} alt={`${label} ${index + 1}`} loading="lazy" onError={() => setFailed((current) => ({ ...current, [source]: true }))} />}</button>; })}
    </div>
    {selected && createPortal(<div className="image-lightbox" role="dialog" aria-modal="true" aria-label={text('图片预览', 'Image preview')}>
      <div className="image-lightbox-toolbar">
        <button type="button" onClick={() => { setFitToWindow(false); setZoom((value) => Math.max(0.25, value - 0.25)); }} aria-label={text('缩小', 'Zoom out')}><ZoomOut size={17} /></button>
        <button type="button" className={fitToWindow ? 'active' : ''} onClick={() => setFitToWindow(true)}>{text('适合窗口', 'Fit to window')}</button>
        <button type="button" className={!fitToWindow && zoom === 1 ? 'active' : ''} onClick={() => { setFitToWindow(false); setZoom(1); }}>100%</button>
        <button type="button" onClick={() => { setFitToWindow(false); setZoom((value) => Math.min(4, value + 0.25)); }} aria-label={text('放大', 'Zoom in')}><ZoomIn size={17} /></button>
        <span>{fitToWindow ? text('自适应', 'Fit') : `${Math.round(zoom * 100)}%`}{naturalSize.width ? ` · ${naturalSize.width}×${naturalSize.height}` : ''}</span>
        <button type="button" className="image-lightbox-close" onClick={() => setSelected(null)} aria-label={text('关闭图片预览', 'Close image preview')}><X size={19} /></button>
      </div>
      <div className="image-lightbox-stage" onClick={() => setSelected(null)}>
        <img
          className={`image-lightbox-image ${fitToWindow ? 'image-lightbox-image-fit' : ''}`}
          src={selected}
          alt={text('放大预览', 'Enlarged preview')}
          style={!fitToWindow && naturalSize.width ? { width: `${naturalSize.width * zoom}px`, maxWidth: 'none', maxHeight: 'none' } : undefined}
          onLoad={(event) => setNaturalSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
          onClick={(event) => event.stopPropagation()}
        />
      </div>
    </div>, document.body)}
  </>;
}

function TrajectoryDetail({ data, error, onRetry }: { data: TrajectoryView | null; error?: string; onRetry?: () => void }) {
  const { text } = useUiLanguage();
  const [visibleCount, setVisibleCount] = useState(MAX_TRAJECTORY_STEPS);
  useEffect(() => setVisibleCount(MAX_TRAJECTORY_STEPS), [data?.round_index, data?.role, data?.raw_chars]);
  if (error) return <div className="drawer-pre trajectory-detail-list">{/^(暂无轮次|No round results)/u.test(error) ? <span className="drawer-muted">{error}</span> : <div className="drawer-error-state"><span className="drawer-error">{text('轨迹加载失败：', 'Failed to load trajectory: ')}{compactText(error, 240)}</span>{onRetry && <button type="button" onClick={onRetry}>{text('重试', 'Retry')}</button>}</div>}</div>;
  if (!data) return <div className="drawer-pre trajectory-detail-list">{text('请选择角色轨迹。', 'Select a role trajectory.')}</div>;
  const displaySteps = data.steps
    .map((step, sourceIndex) => ({ step, sourceIndex }))
    .filter(({ step }) => !isTrajectoryNoise(trajectoryStepText(step)));
  const total = displaySteps.length;
  const visible = Math.min(total, visibleCount);
  const start = Math.max(0, total - visible);
  const hidden = start;
  return <div className="drawer-pre trajectory-detail-list">
    {hidden > 0 && <button type="button" className="trajectory-detail-more" onClick={() => setVisibleCount((count) => Math.min(total, count + MAX_TRAJECTORY_STEPS))}>{text(`显示更早 ${hidden} 步`, `Show ${hidden} earlier steps`)}</button>}
    {data.warning && <p className="drawer-muted">{data.warning}</p>}
    {displaySteps.slice(start).map(({ step, sourceIndex }) => {
      const stepText = trajectoryStepText(step);
      const imageProjection = stepImageProjection(step);
      const images = imageProjection.images;
      return <section className="trajectory-detail-step" key={`trajectory-${sourceIndex}`}><div className="trajectory-detail-heading">{String(sourceIndex + 1).padStart(2, '0')} <span>{String(step.kind || 'step').toUpperCase()}</span></div>{stepText && <pre>{stepText}</pre>}{images.length > 0 && <ImageGallery images={images} label={text('中间截图', 'intermediate screenshot')} />}{imageProjection.omittedLargeDataUrls > 0 && <div className="trajectory-image-warning"><AlertTriangle size={12} /><span>{text(`${imageProjection.omittedLargeDataUrls} 张截图超过 2MB，已隐藏`, `${imageProjection.omittedLargeDataUrls} screenshots over 2 MB were hidden`)}</span></div>}</section>;
    })}
    {total > MAX_TRAJECTORY_STEPS && <small className="drawer-field-note">{text(`已显示最近 ${visible} / ${total} 步，按需加载更早结果。`, `Showing the latest ${visible} of ${total} steps. Load earlier results as needed.`)}</small>}
  </div>;
}

function inlineMessageText(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|https?:\/\/[^\s<>"'`]+)/giu;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const part = match[0];
    const index = match.index ?? cursor;
    if (index > cursor) nodes.push(<span key={`text-${cursor}`}>{text.slice(cursor, index)}</span>);
    if (part.startsWith('`') && part.endsWith('`')) {
      nodes.push(<code key={`code-${index}`}>{part.slice(1, -1)}</code>);
    } else if (part.startsWith('**') && part.endsWith('**')) {
      nodes.push(<strong key={`strong-${index}`}>{part.slice(2, -2)}</strong>);
    } else {
      const href = normalizeLink(part);
      nodes.push(href
        ? <a href={href} target="_blank" rel="noreferrer" key={`link-${index}`}>{href}</a>
        : <span key={`text-${index}`}>{part}</span>);
    }
    cursor = index + part.length;
  }
  if (cursor < text.length) nodes.push(<span key={`text-${cursor}`}>{text.slice(cursor)}</span>);
  return nodes.length ? nodes : [<span key="text-empty">{text}</span>];
}

function MessageText({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/u).filter(Boolean);
  return <div className="message-text">{blocks.map((block, index) => {
    const lines = block.split('\n');
    if (block.startsWith('```')) {
      return <pre className="message-code" key={index}><code>{block.replace(/^```[^\n]*\n?/u, '').replace(/\n?```$/u, '')}</code></pre>;
    }
    if (lines.length && /^#{1,3}\s+/u.test(lines[0])) {
      return <div className="message-heading" key={index}><h3>{lines[0].replace(/^#{1,3}\s+/u, '')}</h3>{lines.slice(1).map((line, lineIndex) => <p key={lineIndex}>{inlineMessageText(line)}</p>)}</div>;
    }
    if (lines.length && lines.every((line) => /^[-*]\s+/u.test(line))) {
      return <ul key={index}>{lines.map((line, lineIndex) => <li key={lineIndex}>{inlineMessageText(line.replace(/^[-*]\s+/u, ''))}</li>)}</ul>;
    }
    return <p key={index}>{lines.map((line, lineIndex) => <span key={lineIndex}>{line}{lineIndex < lines.length - 1 && <br />}</span>)}</p>;
  })}</div>;
}

function ApprovalCard({ approval, busy, userInput, onUserInput, onApprove }: { approval: Snapshot['approvals'][number]; busy: boolean; userInput: string; onUserInput: (value: string) => void; onApprove: (id: string, action: string, userInput?: string) => void }) {
  const { text } = useUiLanguage();
  const options = approval.options.length ? approval.options : [{ value: 'continue', label: 'Continue' }, { value: 'stop', label: 'Stop', style: 'danger' }];
  const completed = approval.context?.trigger === 'completed' || approval.title === 'Task complete. Continue the run?';
  const title = completed ? text('结果已生成，要结束任务吗？', 'The result is ready. End the task?') : approval.title;
  const message = completed ? text('Manager 已确认任务完成，最终回答已显示在上方。你可以结束任务，也可以继续追加轮次。', 'The Manager confirmed the task is complete and the final answer is shown above. You can end the task or continue with another round.') : approval.message;
  const inputPlaceholder = approval.input_label && !/^Optional/i.test(approval.input_label) ? approval.input_label : text('可选：输入回答或指令', 'Optional: enter an answer or instruction');
  const optionLabel = (label: string) => /continue/i.test(label) ? text('继续运行', 'Continue run') : /end/i.test(label) ? text('结束任务', 'End task') : /stop/i.test(label) ? text('停止', 'Stop') : label;
  return <article className="approval-card"><div className="message-avatar approval-avatar"><AlertTriangle size={12} /></div><div className="message-body"><div className="message-meta"><strong>{title}</strong><span className="approval-label">{text('需要你的输入', 'Input required')}</span></div><MessageText text={message} />{approval.allow_input && <textarea className="approval-input" value={userInput} onChange={(event) => onUserInput(event.target.value)} placeholder={inputPlaceholder} disabled={busy} />} {approval.answers.length > 0 && <div className="approval-answers" aria-label={text('快速回答', 'Quick answers')}>{approval.answers.map((answer) => <button type="button" key={answer} disabled={busy} onClick={() => onApprove(approval.approval_id, 'continue', answer)}>{answer}</button>)}</div>}<div className="approval-actions">{options.map((option) => <button key={option.value} disabled={busy} className={option.style === 'danger' ? 'danger-text' : ''} onClick={() => onApprove(approval.approval_id, option.value)}>{optionLabel(option.label)}</button>)}</div></div></article>;
}

function RoleRuntimePicker({ role, selection, meta, onChange }: { role: typeof PUBLIC_ROLES[number]; selection: RoleSelection; meta: WebMeta | null; onChange: (value: RoleSelection) => void }) {
  const { text } = useUiLanguage();
  const agentChoices = meta?.agents?.length
    ? meta.agents.map((item) => ({ id: item.id, label: item.label || item.id, available: item.available }))
    : [{ id: 'codex', label: 'Codex', available: undefined }, { id: 'claude_code', label: 'Claude Code', available: undefined }, { id: 'deepseek_harness', label: 'DeepSeek Harness (CLI)', available: undefined }, { id: 'opencode', label: 'OpenCode', available: undefined }];
  const discovered = normalizedModelChoices(
    meta?.models?.[selection.agent] || meta?.agents?.find((item) => item.id === selection.agent)?.models,
  );
  const choices = discovered.length ? discovered : MODEL_PRESETS[selection.agent] || [];
  const providerDefault = defaultModel(meta, selection.agent) || 'provider default';
  const selectValue = selection.custom
    ? '__custom__'
    : selection.model && choices.some((choice) => choice.id === selection.model)
      ? selection.model
      : selection.model ? '__custom__' : '';
  const discovery = meta?.model_discovery?.[selection.agent]
    || meta?.agents?.find((item) => item.id === selection.agent)?.discovery;
  const discoveryNote = discovery?.account_scoped
    ? text(`已从当前 ${selection.agent === 'codex' ? 'Codex 登录态' : '账号'}检测到 ${choices.length} 个模型。`, `Detected ${choices.length} models from the current ${selection.agent === 'codex' ? 'Codex session' : 'account'}.`)
    : discovery?.warning || text('模型可用性会在 worker 启动时由 provider 验证。', 'The provider will verify model availability when the worker starts.');
  const backendScopeNote = selection.agent === 'deepseek_harness'
    ? text('DeepSeek Harness 当前仅执行 CLI/代码任务，不包含 computer-use 或 MCP。', 'DeepSeek Harness currently runs CLI/code tasks only; computer-use and MCP are not included.')
    : selection.agent === 'opencode'
      ? text('OpenCode 当前仅执行 CLI/代码任务，不包含 computer-use 或 MCP。', 'OpenCode currently runs CLI/code tasks only; computer-use and MCP are not included.')
      : '';
  const roleDescription = role.id === 'manager'
    ? text('规划、路由与完成判定', 'Planning, routing, and completion decisions')
    : role.id === 'executor'
      ? text('执行 GUI / CLI 子任务', 'Executes GUI and CLI subtasks')
      : text('独立验证与验收', 'Independent verification and acceptance');
  return <section className="role-runtime-card">
    <div className="role-runtime-title"><span className="role-runtime-mark">{role.label[0]}</span><div><strong>{role.label}</strong><small>{roleDescription}</small></div></div>
    <div className="role-runtime-grid">
      <label className="drawer-field"><span>Harness</span><select value={selection.agent} onChange={(event) => onChange({ agent: event.target.value, model: '', effort: selection.effort, custom: false })}>{agentChoices.map((choice) => <option value={choice.id} key={choice.id} disabled={choice.available === false}>{choice.label}{choice.available === false ? text(' · 未安装', ' · Not installed') : ''}</option>)}</select></label>
      <label className="drawer-field"><span>Model</span><select value={selectValue} onChange={(event) => { const value = event.target.value; onChange(value === '__custom__' ? { ...selection, model: '', custom: true } : { ...selection, model: value, custom: false }); }}><option value="">{text(`Provider 默认（${providerDefault}）`, `Provider default (${providerDefault})`)}</option>{choices.map((choice) => <option value={choice.id} key={choice.id}>{choice.label}</option>)}<option value="__custom__">{text('自定义模型…', 'Custom model…')}</option></select></label>
      <label className="drawer-field"><span>{text('思考强度', 'Effort')}</span><select value={selection.effort || ''} onChange={(event) => onChange({ ...selection, effort: event.target.value || undefined })}><option value="">{text('默认', 'Default')}</option>{(meta?.defaults?.effort_levels?.length ? meta.defaults.effort_levels : EFFORT_LEVEL_FALLBACK).map((level) => <option value={level} key={level}>{level}</option>)}</select></label>
    </div>
    {selection.custom && <input className="role-runtime-custom" autoFocus value={selection.model || ''} onChange={(event) => onChange({ ...selection, model: event.target.value })} placeholder={text('输入 provider 暴露的模型名', 'Enter a model name exposed by the provider')} />}
    <small className={`drawer-field-note ${discovery?.account_scoped ? 'model-detected' : ''}`}>{selection.custom ? text('自定义模型不在检测目录时仍允许尝试；若不存在、无权限或凭据错误，任务会立即失败并显示 provider 原因。', 'You may try a custom model that was not detected. If it is unavailable, unauthorized, or the credentials are invalid, the task will fail immediately with the provider reason.') : [backendScopeNote, discoveryNote].filter(Boolean).join(' ')}</small>
  </section>;
}

function DetailsDrawer({ creating, runId, snapshot, meta, selectedRound, setSelectedRound, tab, setTab, artifactList, artifactError, artifactName, artifactText, openArtifact, retryArtifacts, trajectoryRole, setTrajectoryRole, trajectoryData, trajectoryError, retryTrajectory, onClose, onCreate, onRefreshModels, controlBusy }: { creating: boolean; runId: string; snapshot: Snapshot; meta: WebMeta | null; selectedRound: number | null; setSelectedRound: (value: number) => void; tab: DetailsTab; setTab: (value: DetailsTab) => void; artifactList: ArtifactList | null; artifactError?: string; artifactName: string; artifactText: string; openArtifact: (round: number, name: string) => Promise<void>; retryArtifacts?: () => void; trajectoryRole: string; setTrajectoryRole: (value: string) => void; trajectoryData: TrajectoryView | null; trajectoryError?: string; retryTrajectory?: () => void; onClose: () => void; onCreate: (task: string, roles: Record<PublicRole, RoleRuntimeConfig>, workspace: string, maxRounds: string, promptLanguage: 'en' | 'zh') => Promise<void>; onRefreshModels: () => Promise<void>; controlBusy: boolean }) {
  const { language, text } = useUiLanguage();
  const [task, setTask] = useState('');
  const [roleSelections, setRoleSelections] = useState<Record<PublicRole, RoleSelection>>({
    manager: { agent: 'codex', model: '', effort: undefined, custom: false },
    executor: { agent: 'codex', model: '', effort: undefined, custom: false },
    auditor: { agent: 'codex', model: '', effort: undefined, custom: false },
  });
  const rolesInitialised = useRef(false);
  const [modelRefreshBusy, setModelRefreshBusy] = useState(false);
  const [modelRefreshError, setModelRefreshError] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [maxRounds, setMaxRounds] = useState('25');
  const [promptLanguage, setPromptLanguage] = useState<'en' | 'zh'>('zh');
  const [roundLimit, setRoundLimit] = useState(MAX_TRAJECTORY_ROUNDS);
  useEffect(() => {
    if (!meta || rolesInitialised.current) return;
    const fallbackAgent = defaultAgent(meta);
    setRoleSelections(Object.fromEntries(PUBLIC_ROLES.map(({ id }) => {
      const configured = meta.defaults?.roles?.[id];
      const roleAgent = configured?.agent || fallbackAgent;
      return [id, { agent: roleAgent, model: configured?.model || '', effort: configured?.effort || undefined, custom: false }];
    })) as Record<PublicRole, RoleSelection>);
    rolesInitialised.current = true;
  }, [meta]);
  const roundChoices = useMemo(() => {
    const recent = snapshot.rounds.slice(-roundLimit);
    if (selectedRound !== null && !recent.some((round) => round.round_index === selectedRound)) {
      const selected = snapshot.rounds.find((round) => round.round_index === selectedRound);
      return selected ? [selected, ...recent] : recent;
    }
    return recent;
  }, [snapshot.rounds, roundLimit, selectedRound]);
  const hiddenRounds = Math.max(0, snapshot.rounds.length - roundChoices.length);
  const roles = snapshot.rounds.find((round) => round.round_index === selectedRound)?.roles || [];
  const events = dedupeEvents(snapshot.events).slice(-80);
  const resolvedRoles = Object.fromEntries(PUBLIC_ROLES.map(({ id }) => [id, {
    agent: roleSelections[id].agent,
    model: roleSelections[id].model?.trim() || defaultModel(meta, roleSelections[id].agent),
    ...(roleSelections[id].effort ? { effort: roleSelections[id].effort } : {}),
  }])) as Record<PublicRole, RoleRuntimeConfig>;
  return <div className="drawer-backdrop" onClick={onClose}><aside className={`details-drawer ${creating ? 'create-drawer' : ''}`} role="dialog" aria-modal="true" aria-label={creating ? text('创建新任务', 'Create new task') : text('会话详情', 'Task details')} onClick={(event) => event.stopPropagation()}>
    <div className="drawer-header"><div><div className="drawer-eyebrow">{creating ? text('新任务', 'NEW TASK') : text('会话详情', 'TASK DETAILS')}</div><h2>{creating ? text('创建 LongHorizon 任务', 'Create a LongHorizon task') : text('详情', 'Details')}</h2></div><button className="drawer-close" onClick={onClose} aria-label={text('关闭', 'Close')}><X size={18} /></button></div>
    {creating ? <>
      <p className="drawer-copy">{text('任务会由真实 worker 执行。右侧状态面板会持续显示进度，中间区域只保留可读的步骤结果。', 'A real worker will execute the task. The status panel tracks progress while the conversation shows readable step results.')}</p>
      <label className="drawer-field"><span>{text('任务', 'Task')}</span><textarea autoFocus value={task} onChange={(event) => setTask(event.target.value)} placeholder={text('请描述 LongHorizon 需要完成的任务', 'What should LongHorizon do?')} /></label>
      <div className="role-runtime-head"><div><strong>{text('角色运行配置', 'Role runtime configuration')}</strong><small>{text('每个角色可独立选择 Codex / Claude Code / DeepSeek Harness 和模型', 'Choose Codex, Claude Code, or DeepSeek Harness and a model independently for each role')}</small></div><button type="button" disabled={modelRefreshBusy} onClick={() => { setModelRefreshBusy(true); setModelRefreshError(''); void onRefreshModels().catch((reason) => setModelRefreshError(String(reason))).finally(() => setModelRefreshBusy(false)); }}>{modelRefreshBusy ? text('检测中…', 'Detecting…') : text('重新检测模型', 'Refresh models')}</button></div>
      {modelRefreshError && <div className="drawer-error-state"><span className="drawer-error">{text('模型检测失败：', 'Model detection failed: ')}{compactText(modelRefreshError, 240)}</span></div>}
      <div className="role-runtime-list">{PUBLIC_ROLES.map((role) => <RoleRuntimePicker key={role.id} role={role} selection={roleSelections[role.id]} meta={meta} onChange={(value) => setRoleSelections((current) => ({ ...current, [role.id]: value }))} />)}</div>
      <label className="drawer-field"><span>{text('最大轮次', 'Max rounds')} <small>1–{MAX_ROUNDS}</small></span><input inputMode="numeric" type="text" pattern="[0-9]*" value={maxRounds} onChange={(event) => setMaxRounds(event.target.value.replace(/\D+/gu, ''))} onBlur={() => setMaxRounds(String(normaliseMaxRounds(maxRounds)))} /></label>
      <label className="drawer-field"><span>{text('角色提示词语言', 'Agent prompt language')}</span><select value={promptLanguage} onChange={(event) => setPromptLanguage(event.target.value as 'en' | 'zh')}><option value="zh">中文</option><option value="en">English</option></select><small>{text('仅控制 Manager、Executor 和 Auditor 的工作提示词，与界面语言无关。', 'Controls only the Manager, Executor, and Auditor prompts; it is independent of the interface language.')}</small></label>
      <label className="drawer-field"><span>Workspace <small>{text('可选', 'optional')}</small></span><input value={workspace} onChange={(event) => setWorkspace(event.target.value)} placeholder={text('使用 Web 服务的工作区根目录', 'Use the Web server workspace root')} /></label>
      <div className="drawer-actions"><button onClick={onClose}>{text('取消', 'Cancel')}</button><button className="primary-action" disabled={!task.trim() || controlBusy || PUBLIC_ROLES.some(({ id }) => !roleSelections[id].agent || roleSelections[id].custom && !roleSelections[id].model?.trim())} onClick={() => void onCreate(task.trim(), resolvedRoles, workspace.trim(), maxRounds, promptLanguage)}>{controlBusy ? text('正在启动…', 'Starting…') : text('开始任务', 'Start task')}</button></div>
    </> : <>
      {snapshot.run.role_configs && <div className="role-binding-strip" aria-label={text('角色运行配置', 'Role runtime configuration')}>{PUBLIC_ROLES.map(({ id, label }) => { const spec = snapshot.run.role_configs?.[id]; return spec ? <span className="role-binding-chip" key={id} title={`${label}: ${spec.agent} · ${spec.model}${spec.effort ? ` · effort ${spec.effort}` : ''}`}><strong>{label}</strong><span>{spec.model}</span>{spec.effort && <em>{spec.effort}</em>}</span> : null; })}</div>}
      <div className="details-tabs" role="tablist" aria-label={text('详情类别', 'Detail categories')}><button id="details-tab-artifacts" role="tab" aria-controls="details-panel-artifacts" aria-selected={tab === 'artifacts'} tabIndex={tab === 'artifacts' ? 0 : -1} className={tab === 'artifacts' ? 'active' : ''} onClick={() => setTab('artifacts')}>{text('运行记录', 'Run records')}</button><button id="details-tab-trajectory" role="tab" aria-controls="details-panel-trajectory" aria-selected={tab === 'trajectory'} tabIndex={tab === 'trajectory' ? 0 : -1} className={tab === 'trajectory' ? 'active' : ''} onClick={() => setTab('trajectory')}>{text('执行轨迹', 'Trajectory')}</button><button id="details-tab-events" role="tab" aria-controls="details-panel-events" aria-selected={tab === 'events'} tabIndex={tab === 'events' ? 0 : -1} className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')}>{text('事件', 'Events')}</button></div>
      <div className="drawer-rounds"><span>{text('轮次', 'Rounds')}</span>{hiddenRounds > 0 && <button className="drawer-round-more" onClick={() => setRoundLimit((limit) => Math.min(snapshot.rounds.length, limit + MAX_TRAJECTORY_ROUNDS))}>{text(`+${hiddenRounds} 更早`, `+${hiddenRounds} earlier`)}</button>}{roundChoices.map((round) => <button className={round.round_index === selectedRound ? 'active' : ''} key={round.round_index} onClick={() => setSelectedRound(round.round_index)}>R{round.round_index}</button>)}</div>
      {tab === 'artifacts' && <div id="details-panel-artifacts" className="drawer-content" role="tabpanel" aria-labelledby="details-tab-artifacts"><p className="drawer-artifact-note">{text(`这里是本轮三角色的运行记录。真实交付物保存在任务 Workspace${snapshot.run.workspace ? `：${snapshot.run.workspace}` : ''} 或目标应用中，Auditor 会在同一真实环境里独立检查。`, `These are the three roles' records for this round. Actual deliverables are saved in the task workspace${snapshot.run.workspace ? `: ${snapshot.run.workspace}` : ''} or the target application, where the Auditor independently checks them.`)}</p><div className="artifact-list-drawer">{artifactList?.artifacts.map((name) => <button className={name === artifactName ? 'active' : ''} title={name} key={name} onClick={() => void openArtifact(selectedRound || 0, name)}>{name}</button>)}{selectedRound === null && <span className="drawer-muted">{text('暂无轮次结果。', 'No round results yet.')}</span>}{selectedRound !== null && artifactError && <div className="drawer-error-state"><span className="drawer-error">{text('记录加载失败：', 'Failed to load records: ')}{compactText(artifactError, 240)}</span>{retryArtifacts && <button type="button" onClick={retryArtifacts}>{text('重试', 'Retry')}</button>}</div>}{selectedRound !== null && !artifactError && !artifactList && <span className="drawer-muted">{text('正在加载运行记录…', 'Loading run records…')}</span>}{artifactList && !artifactList.artifacts.length && <span className="drawer-muted">{text('本轮暂无运行记录。', 'No run records for this round.')}</span>}</div>{artifactName && isImageArtifact(artifactName) ? <div className="drawer-image-preview"><ImageGallery images={[artifactRawUrl(runId, selectedRound || 0, artifactName)]} label={artifactName} /></div> : <pre className="drawer-pre">{artifactText || text('请选择一条运行记录。', 'Select a run record.')}</pre>}</div>}
      {tab === 'trajectory' && <div id="details-panel-trajectory" className="drawer-content" role="tabpanel" aria-labelledby="details-tab-trajectory"><div className="drawer-rounds role-picker"><span>{text('角色', 'Role')}</span>{roles.map((role) => <button className={role === trajectoryRole ? 'active' : ''} key={role} onClick={() => setTrajectoryRole(role)}>{roleTitle(role)}</button>)}</div><TrajectoryDetail data={trajectoryData} error={selectedRound === null ? text('暂无轮次结果。', 'No round results yet.') : trajectoryError} onRetry={retryTrajectory} /></div>}
      {tab === 'events' && <div id="details-panel-events" className="drawer-content" role="tabpanel" aria-labelledby="details-tab-events"><div className="event-list-drawer">{events.map((event) => <div key={event.event_id}><time>{formatTime(event.ts)}</time><span>{eventSummary(event, language)}</span></div>)}</div></div>}
    </>}
  </aside></div>;
}

function AuthDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const { text } = useUiLanguage();
  const [token, setToken] = useState(storedAuthToken);
  const save = () => {
    setStoredAuthToken(token.trim());
    onSaved();
  };
  return <div className="auth-backdrop" onClick={onClose}>
    <form className="auth-dialog" role="dialog" aria-modal="true" aria-label={text('连接设置', 'Connection settings')} onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); save(); }}>
      <div className="auth-dialog-head"><span className="auth-dialog-icon"><KeyRound size={16} /></span><div><span className="drawer-eyebrow">CONNECTION</span><h2>{text('连接设置', 'Connection settings')}</h2></div><button type="button" className="drawer-close" onClick={onClose} aria-label={text('关闭', 'Close')}><X size={18} /></button></div>
      <p className="auth-dialog-copy">{text('如果 Web 服务配置了 ', 'If the Web service uses ')}<code>LH_HARNESS_WEB_TOKEN</code>{text('，在这里填写 Bearer 令牌即可连接。令牌只保存在当前浏览器标签页。', ', enter the Bearer token here. The token is stored only in the current browser tab.')}</p>
      <label className="drawer-field"><span>{text('访问令牌', 'Access token')}</span><input autoFocus type="password" value={token} onChange={(event) => setToken(event.target.value)} placeholder={text('粘贴 Web 服务令牌', 'Paste the Web service token')} autoComplete="off" /></label>
      <div className="auth-dialog-actions"><button type="button" onClick={() => { setToken(''); setStoredAuthToken(''); onSaved(); }}>{text('清除令牌', 'Clear token')}</button><span /><button type="button" onClick={onClose}>{text('取消', 'Cancel')}</button><button type="submit" className="primary-action">{text('保存并重连', 'Save and reconnect')}</button></div>
    </form>
  </div>;
}
