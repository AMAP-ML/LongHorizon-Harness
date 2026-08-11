import type { TrajectoryStep, TrajectoryView } from './trajectoryView';

export type FileChangeKind = 'added' | 'modified' | 'deleted' | 'renamed' | 'unknown';
export type FileChangeScope = 'agent' | 'workspace' | 'none';
export type LineCountCoverage = 'complete' | 'partial' | 'unavailable';
export type ValidationStatus = 'running' | 'passed' | 'failed' | 'unknown';
export type ValidationOperation = 'test' | 'typecheck' | 'build' | 'lint' | 'diff-check';

export interface FileChangeItem {
  path: string;
  previousPath: string | null;
  kind: FileChangeKind;
  extension: string | null;
  additions: number | null;
  deletions: number | null;
  source: 'tool' | 'git-numstat' | 'git-stat';
  trajectoryIndex: number;
  stepIndex: number;
}

export interface FileChangeSummary {
  /** Agent tool events are preferred. Workspace diff output is used only as a fallback. */
  scope: FileChangeScope;
  totalFiles: number;
  knownFiles: number;
  shownFiles: number;
  hiddenFiles: number;
  additions: number | null;
  deletions: number | null;
  lineCountCoverage: LineCountCoverage;
  items: FileChangeItem[];
}

export interface ValidationResultSummary {
  id: string;
  label: string;
  targets: string[];
  operations: ValidationOperation[];
  command: string;
  status: ValidationStatus;
  summary: string;
  passedCount: number | null;
  failedCount: number | null;
  skippedCount: number | null;
  moduleCount: number | null;
  exitCode: number | null;
  durationMs: number | null;
  trajectoryIndex: number;
  roundIndex: number;
  role: string;
  stepIndex: number;
}

export interface ValidationSummary {
  total: number;
  shown: number;
  hidden: number;
  passed: number;
  failed: number;
  running: number;
  unknown: number;
  items: ValidationResultSummary[];
}

export interface ArtifactProjection {
  files: FileChangeSummary;
  validations: ValidationSummary;
}

export interface ArtifactProjectionOptions {
  maxFiles?: number;
  maxValidations?: number;
  maxSummaryChars?: number;
}

interface StepRef {
  trajectory: TrajectoryView;
  trajectoryIndex: number;
  step: TrajectoryStep;
  stepIndex: number;
  order: number;
}

interface MutableFile extends FileChangeItem {
  lastChangeOrder: number;
  statsOrder: number;
}

interface DiffStatSnapshot {
  order: number;
  paths: string[];
  totalFiles: number;
  additions: number | null;
  deletions: number | null;
}

interface ResultEvidence {
  text: string;
  exitCode: number | null;
  isError: boolean;
  /** A protocol-level success signal, as opposed to merely non-error prose. */
  explicitSuccess: boolean;
  /** A protocol-level failure signal (exit code, status, or structured error). */
  explicitFailure: boolean;
  durationMs: number | null;
}

interface DetectedValidation {
  operations: ValidationOperation[];
  targets: string[];
}

const DEFAULT_MAX_FILES = 200;
const DEFAULT_MAX_VALIDATIONS = 100;
const DEFAULT_MAX_SUMMARY_CHARS = 180;
const FILE_TOOL_NAMES = new Set(['apply_patch', 'edit', 'file_change', 'multiedit', 'notebookedit', 'write']);
const OPERATION_ORDER: ValidationOperation[] = ['test', 'typecheck', 'build', 'lint', 'diff-check'];
const SUCCESS_STATUSES = new Set([
  'ok', 'success', 'successful', 'succeeded', 'pass', 'passed', 'complete',
  'completed', 'done', 'finished',
]);
const FAILURE_STATUSES = new Set([
  'error', 'errored', 'failure', 'failed', 'fail', 'cancelled', 'canceled',
  'aborted', 'blocked', 'stopped', 'timeout', 'timed_out', 'rejected',
]);

function limit(value: number | undefined, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(1, Math.floor(value as number));
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && /^-?\d+(?:\.\d+)?$/u.test(value.trim())) return Number(value);
  return null;
}

function compactText(value: string, max: number): string {
  const text = value.trim();
  return text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1)).trimEnd()}…`;
}

function normalizePath(value: unknown): string {
  let path = stringValue(value).replace(/^['"]|['"]$/gu, '').replace(/\\/gu, '/');
  path = path.replace(/\{[^{}]*\s=>\s([^{}]*)\}/gu, '$1');
  if (path.includes(' => ')) path = path.split(' => ').at(-1) || path;
  if (path.startsWith('./')) path = path.slice(2);
  if (/^[ab]\//u.test(path)) path = path.slice(2);
  return path.trim();
}

function extensionOf(path: string): string | null {
  const name = path.split('/').at(-1) || '';
  const dot = name.lastIndexOf('.');
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : null;
}

function fileKind(value: unknown, fallback: FileChangeKind = 'unknown'): FileChangeKind {
  const kind = stringValue(value).toLowerCase();
  if (/^(?:add|added|create|created|new)$/u.test(kind)) return 'added';
  if (/^(?:delete|deleted|remove|removed)$/u.test(kind)) return 'deleted';
  if (/^(?:rename|renamed|move|moved)$/u.test(kind)) return 'renamed';
  if (/^(?:change|changed|edit|edited|modify|modified|update|updated)$/u.test(kind)) return 'modified';
  return fallback;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function trajectoriesOf(input: TrajectoryView | readonly (TrajectoryView | null | undefined)[] | null | undefined): TrajectoryView[] {
  if (!input) return [];
  return (Array.isArray(input) ? input : [input]).filter((item): item is TrajectoryView => Boolean(item && typeof item === 'object' && Array.isArray(item.steps)));
}

function flattenSteps(trajectories: readonly TrajectoryView[]): StepRef[] {
  let order = 0;
  return trajectories.flatMap((trajectory, trajectoryIndex) => trajectory.steps
    .filter((step): step is TrajectoryStep => Boolean(step && typeof step === 'object'))
    .map((step, stepIndex) => ({
    trajectory,
    trajectoryIndex,
    step,
    stepIndex,
    order: order++,
  })));
}

function toolName(step: TrajectoryStep): string {
  return stringValue(step.name).toLowerCase();
}

function toolId(step: TrajectoryStep): string {
  return stringValue(step.id);
}

function resultToolId(step: TrajectoryStep): string {
  return stringValue(step.tool_use_id) || stringValue(step.id);
}

function toolInput(step: TrajectoryStep): Record<string, unknown> {
  return recordValue(step.input) || {};
}

function commandOf(step: TrajectoryStep): string {
  const input = toolInput(step);
  return stringValue(input.command) || stringValue(input.cmd) || stringValue(step.command);
}

function signalBoolean(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value;
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase();
  if (normalized === 'true' || normalized === 'yes' || normalized === '1') return true;
  if (normalized === 'false' || normalized === 'no' || normalized === '0') return false;
  return null;
}

function signalStatus(value: unknown): { success: boolean; failure: boolean } {
  const normalized = stringValue(value).toLowerCase().replace(/[\s-]+/gu, '_');
  return {
    success: SUCCESS_STATUSES.has(normalized),
    failure: FAILURE_STATUSES.has(normalized),
  };
}

function unwrapResult(step: TrajectoryStep): ResultEvidence {
  const rawOutput = step.output;
  const rawText = stringValue(step.text)
    || stringValue(rawOutput)
    || stringValue(step.stdout)
    || (rawOutput && typeof rawOutput === 'object' ? JSON.stringify(rawOutput) : '');
  let text = rawText;
  const directExit = finiteNumber(step.exit_code) ?? finiteNumber(step.exitCode);
  const exitCodes: number[] = directExit === null ? [] : [directExit];
  let explicitFailure = step.is_error === true;
  let explicitSuccess = false;
  let durationMs = finiteNumber(step.duration_ms) ?? finiteNumber(step.durationMs);
  const directStatus = signalStatus(step.status ?? step.outcome);
  explicitFailure = explicitFailure || directStatus.failure;
  explicitSuccess = explicitSuccess || directStatus.success;
  for (const key of ['success', 'ok', 'is_success', 'isSuccess']) {
    const value = signalBoolean(step[key]);
    if (value === true) explicitSuccess = true;
    if (value === false) explicitFailure = true;
  }
  if (rawText.startsWith('{') && rawText.endsWith('}')) {
    try {
      const parsed = recordValue(JSON.parse(rawText));
      if (parsed) {
        const output = stringValue(parsed.output) || stringValue(parsed.stdout) || stringValue(parsed.text) || stringValue(parsed.content);
        const error = stringValue(parsed.error) || stringValue(parsed.stderr);
        text = [output, error].filter(Boolean).join('\n') || rawText;
        const parsedExit = finiteNumber(parsed.exit_code) ?? finiteNumber(parsed.exitCode);
        if (parsedExit !== null) exitCodes.push(parsedExit);
        durationMs = finiteNumber(parsed.duration_ms) ?? finiteNumber(parsed.durationMs) ?? durationMs;
        const parsedStatus = signalStatus(parsed.status ?? parsed.outcome);
        explicitFailure = explicitFailure || Boolean(error) || parsedStatus.failure;
        explicitSuccess = explicitSuccess
          || parsedStatus.success;
        for (const key of ['success', 'ok', 'is_success', 'isSuccess']) {
          const value = signalBoolean(parsed[key]);
          if (value === true) explicitSuccess = true;
          if (value === false) explicitFailure = true;
        }
        for (const key of ['result', 'data', 'metadata']) {
          const nested = recordValue(parsed[key]);
          if (!nested) continue;
          const nestedExit = finiteNumber(nested.exit_code) ?? finiteNumber(nested.exitCode);
          if (nestedExit !== null) exitCodes.push(nestedExit);
          const nestedStatus = signalStatus(nested.status ?? nested.outcome);
          explicitFailure = explicitFailure || nestedStatus.failure;
          explicitSuccess = explicitSuccess || nestedStatus.success;
          for (const key of ['success', 'ok', 'is_success', 'isSuccess']) {
            const value = signalBoolean(nested[key]);
            if (value === true) explicitSuccess = true;
            if (value === false) explicitFailure = true;
          }
        }
      }
    } catch {
      // Tool output is often JSON-shaped prose; retain it verbatim when parsing fails.
    }
  }
  const exitPatterns = [
    /\[exit[_ ]?code\s*=\s*(-?\d+)\]/iu,
    /["']exit[_ ]?code["']\s*[:=]\s*(-?\d+)/iu,
    /\bexit[_ ]?code\s*[:=]\s*(-?\d+)/iu,
  ];
  for (const pattern of exitPatterns) {
    const exitMatch = rawText.match(pattern);
    if (exitMatch) exitCodes.push(Number(exitMatch[1]));
  }
  // A non-zero code wins over any contradictory wrapper that also reports 0.
  const exitCode = exitCodes.find((code) => code !== 0) ?? exitCodes[0] ?? null;
  if (exitCode !== null) {
    if (exitCode !== 0) explicitFailure = true;
    else explicitSuccess = true;
  }
  return {
    text,
    exitCode,
    isError: explicitFailure,
    explicitSuccess: explicitSuccess && !explicitFailure,
    explicitFailure,
    durationMs,
  };
}

function resultMaps(steps: readonly StepRef[]): Map<number, Map<string, StepRef>> {
  const maps = new Map<number, Map<string, StepRef>>();
  for (const ref of steps) {
    if (stringValue(ref.step.kind) !== 'tool_result') continue;
    const id = resultToolId(ref.step);
    if (!id) continue;
    const map = maps.get(ref.trajectoryIndex) || new Map<string, StepRef>();
    map.set(id, ref);
    maps.set(ref.trajectoryIndex, map);
  }
  return maps;
}

function useMaps(steps: readonly StepRef[]): Map<number, Map<string, StepRef>> {
  const maps = new Map<number, Map<string, StepRef>>();
  for (const ref of steps) {
    if (stringValue(ref.step.kind) !== 'tool_use') continue;
    const id = toolId(ref.step);
    if (!id) continue;
    const map = maps.get(ref.trajectoryIndex) || new Map<string, StepRef>();
    map.set(id, ref);
    maps.set(ref.trajectoryIndex, map);
  }
  return maps;
}

function parseStructuredChanges(step: TrajectoryStep): Array<{
  path: string;
  previousPath: string | null;
  kind: FileChangeKind;
  additions: number | null;
  deletions: number | null;
}> {
  const name = toolName(step);
  const input = toolInput(step);
  if (!FILE_TOOL_NAMES.has(name)) return [];
  const changes = Array.isArray(input.changes) ? input.changes : [];
  const parsed = changes.flatMap((value) => {
    const change = recordValue(value);
    if (!change) return [];
    const path = normalizePath(change.path || change.file_path || change.new_path);
    if (!path) return [];
    return [{
      path,
      previousPath: normalizePath(change.old_path || change.previous_path) || null,
      kind: fileKind(change.kind || change.type, 'modified'),
      additions: finiteNumber(change.additions) ?? finiteNumber(change.insertions),
      deletions: finiteNumber(change.deletions) ?? finiteNumber(change.removals),
    }];
  });
  if (parsed.length) return parsed;
  const path = normalizePath(input.file_path || input.path || input.notebook_path);
  if (!path) return [];
  const fallback = name === 'edit' || name === 'multiedit' || name === 'notebookedit' || name === 'apply_patch'
    ? 'modified'
    : 'unknown';
  return [{ path, previousPath: null, kind: fallback, additions: null, deletions: null }];
}

function parseNumstat(text: string): Array<{ path: string; additions: number | null; deletions: number | null }> {
  const rows: Array<{ path: string; additions: number | null; deletions: number | null }> = [];
  for (const line of text.split('\n')) {
    const match = line.match(/^\s*(-|\d+)\t(-|\d+)\t(.+?)\s*$/u);
    if (!match) continue;
    const rawPath = match[3].includes('\t') ? match[3].split('\t').at(-1) || match[3] : match[3];
    const path = normalizePath(rawPath);
    if (!path) continue;
    rows.push({
      path,
      additions: match[1] === '-' ? null : Number(match[1]),
      deletions: match[2] === '-' ? null : Number(match[2]),
    });
  }
  return rows;
}

function parseDiffStat(text: string, order: number): DiffStatSnapshot | null {
  const summary = [...text.matchAll(/(\d+)\s+files? changed(?:,\s*(\d+)\s+insertions?\(\+\))?(?:,\s*(\d+)\s+deletions?\(-\))?/giu)].at(-1);
  if (!summary) return null;
  const paths: string[] = [];
  for (const line of text.split('\n')) {
    const match = line.match(/^\s*(.+?)\s+\|\s+(?:Bin\s+\d+\s+->\s+\d+\s+bytes|\d+(?:\s+[+\-]+)?)\s*$/u);
    const path = match ? normalizePath(match[1]) : '';
    if (path && !paths.includes(path)) paths.push(path);
  }
  return {
    order,
    paths,
    totalFiles: Number(summary[1]),
    additions: summary[2] === undefined ? 0 : Number(summary[2]),
    deletions: summary[3] === undefined ? 0 : Number(summary[3]),
  };
}

function cleanShellSegment(segment: string): string {
  return segment
    .replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"[^"]*"|\S+)\s+)+/u, '')
    .replace(/^\s*(?:command\s+|env\s+(?:-[^\s]+\s+)*)/u, '')
    .trim();
}

function isGitDiffCommand(command: string, flag: '--numstat' | '--stat'): boolean {
  return shellSegments(command).some((rawSegment) => {
    const segment = cleanShellSegment(rawSegment);
    return /^git\b(?:\s+(?:-[^\s]+(?:\s+[^\s]+)?))*\s+diff\b/iu.test(segment)
      && new RegExp(`${flag}(?:\\s|=|$)`, 'iu').test(segment);
  });
}

function isNumstatCommand(command: string): boolean {
  return isGitDiffCommand(command, '--numstat');
}

function isStatCommand(command: string): boolean {
  return isGitDiffCommand(command, '--stat') && !isNumstatCommand(command);
}

function projectFiles(steps: readonly StepRef[], maxFiles: number): FileChangeSummary {
  const uses = useMaps(steps);
  const files = new Map<string, MutableFile>();
  let scope: FileChangeScope = 'none';
  let structured = false;
  let workspaceTotal: number | null = null;
  let latestStat: DiffStatSnapshot | null = null;

  const put = (
    ref: StepRef,
    path: string,
    kind: FileChangeKind,
    source: FileChangeItem['source'],
    additions: number | null,
    deletions: number | null,
    previousPath: string | null = null,
    changed = false,
  ) => {
    const existing = files.get(path);
    const next: MutableFile = existing || {
      path,
      previousPath,
      kind,
      extension: extensionOf(path),
      additions,
      deletions,
      source,
      trajectoryIndex: ref.trajectoryIndex,
      stepIndex: ref.stepIndex,
      lastChangeOrder: changed ? ref.order : -1,
      statsOrder: additions !== null && deletions !== null ? ref.order : -1,
    };
    next.previousPath = previousPath || next.previousPath;
    if (source === 'tool' || next.source !== 'tool') {
      next.kind = kind === 'unknown' ? next.kind : kind;
    }
    const retainToolLocation = next.source === 'tool' && source !== 'tool';
    next.source = source === 'tool' ? 'tool' : next.source;
    if (!retainToolLocation) {
      next.trajectoryIndex = ref.trajectoryIndex;
      next.stepIndex = ref.stepIndex;
    }
    if (changed) {
      next.lastChangeOrder = ref.order;
      if (additions === null || deletions === null) {
        next.additions = null;
        next.deletions = null;
        next.statsOrder = -1;
      }
    }
    if (additions !== null && deletions !== null) {
      next.additions = additions;
      next.deletions = deletions;
      next.statsOrder = ref.order;
    }
    files.set(path, next);
  };

  for (const ref of steps) {
    if (stringValue(ref.step.kind) === 'tool_use') {
      const changes = parseStructuredChanges(ref.step);
      if (changes.length) {
        if (!structured) files.clear();
        structured = true;
        scope = 'agent';
        workspaceTotal = null;
        latestStat = null;
        for (const change of changes) {
          put(ref, change.path, change.kind, 'tool', change.additions, change.deletions, change.previousPath, true);
        }
      }
      continue;
    }
    if (stringValue(ref.step.kind) !== 'tool_result') continue;
    const id = resultToolId(ref.step);
    const use = id ? uses.get(ref.trajectoryIndex)?.get(id) : undefined;
    const command = use ? commandOf(use.step) : '';
    if (!command || (!isNumstatCommand(command) && !isStatCommand(command))) continue;
    const evidence = unwrapResult(ref.step);
    if (evidence.isError) continue;
    if (isNumstatCommand(command)) {
      const rows = parseNumstat(evidence.text);
      if (!rows.length) continue;
      latestStat = null;
      if (!structured) {
        files.clear();
        scope = 'workspace';
        workspaceTotal = rows.length;
      }
      for (const row of rows) {
        if (structured && !files.has(row.path)) continue;
        put(use || ref, row.path, 'modified', 'git-numstat', row.additions, row.deletions);
      }
      continue;
    }
    const snapshot = parseDiffStat(evidence.text, ref.order);
    if (!snapshot) continue;
    latestStat = snapshot;
    if (!structured) {
      files.clear();
      scope = 'workspace';
      workspaceTotal = snapshot.totalFiles;
      for (const path of snapshot.paths) put(use || ref, path, 'modified', 'git-stat', null, null);
    }
  }

  const allFiles = [...files.values()];
  const knownFiles = allFiles.length;
  const totalFiles = Math.max(knownFiles, workspaceTotal || 0);
  let additions: number | null = null;
  let deletions: number | null = null;
  let lineCountCoverage: LineCountCoverage = 'unavailable';
  const exactPerFile = knownFiles > 0 && allFiles.every((item) => (
    item.additions !== null && item.deletions !== null && item.statsOrder >= item.lastChangeOrder
  ));
  const somePerFile = allFiles.some((item) => item.additions !== null || item.deletions !== null);
  const statMatchesAgent = Boolean(latestStat && structured
    && latestStat.order > Math.max(-1, ...allFiles.map((item) => item.lastChangeOrder))
    && latestStat.totalFiles === knownFiles
    && allFiles.every((item) => latestStat?.paths.includes(item.path)));
  if (latestStat && (!structured || statMatchesAgent)) {
    additions = latestStat.additions;
    deletions = latestStat.deletions;
    lineCountCoverage = 'complete';
  } else if (exactPerFile && totalFiles === knownFiles) {
    additions = allFiles.reduce((sum, item) => sum + (item.additions || 0), 0);
    deletions = allFiles.reduce((sum, item) => sum + (item.deletions || 0), 0);
    lineCountCoverage = 'complete';
  } else if (somePerFile || latestStat) {
    lineCountCoverage = 'partial';
  }
  const items = allFiles.slice(0, maxFiles).map(({ lastChangeOrder: _last, statsOrder: _stats, ...item }) => item);
  return {
    scope,
    totalFiles,
    knownFiles,
    shownFiles: items.length,
    hiddenFiles: Math.max(0, totalFiles - items.length),
    additions,
    deletions,
    lineCountCoverage,
    items,
  };
}

function shellScript(command: string): string {
  const match = command.match(/(?:^|\s)-(?:lc|c)\s+(['"])([\s\S]*)\1\s*$/u);
  return match ? match[2] : command;
}

function shellSegments(command: string): string[] {
  const source = shellScript(command);
  const segments: string[] = [];
  let current = '';
  let quote = '';
  let escaped = false;
  const flush = () => {
    const value = current.trim();
    if (value) segments.push(value);
    current = '';
  };
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === '\\' && quote !== "'") {
      current += char;
      escaped = true;
      continue;
    }
    if (quote) {
      current += char;
      if (char === quote) quote = '';
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      current += char;
      continue;
    }
    const pair = source.slice(index, index + 2);
    if (pair === '&&' || pair === '||') {
      flush();
      index += 1;
      continue;
    }
    if (char === ';' || char === '\n' || char === '|') {
      flush();
      continue;
    }
    current += char;
  }
  flush();
  return segments;
}

function targetLabel(path: string): string {
  const normalized = normalizePath(path).replace(/\/$/u, '');
  const name = normalized.split('/').at(-1) || '';
  if (/^web$/iu.test(name)) return 'Web';
  if (/^core$/iu.test(name)) return 'Core';
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : '';
}

function packagePath(segment: string, cwd: string): string {
  const prefix = segment.match(/(?:--prefix|-C|--cwd)(?:=|\s+)(['"]?)([^\s'"]+)\1/iu);
  return prefix?.[2] || cwd;
}

function detectValidation(command: string): DetectedValidation | null {
  const operations = new Set<ValidationOperation>();
  const targets = new Set<string>();
  let cwd = '';
  for (const rawSegment of shellSegments(command)) {
    const segment = cleanShellSegment(rawSegment);
    const cd = segment.match(/^cd\s+(['"]?)([^\s'"]+)\1\s*$/u);
    if (cd) {
      cwd = cd[2];
      continue;
    }
    let operation: ValidationOperation | null = null;
    let target = '';
    if (/^git\s+(?:-[^\s]+\s+)*diff\b.*--check(?:\s|$)/iu.test(segment)) {
      operation = 'diff-check';
      target = 'Git';
    } else if (/^(?:(?:python\d*(?:\.\d+)*)\s+-m\s+)?(?:pytest|unittest)\b/iu.test(segment)
      || /^python\d*(?:\.\d+)*\s+-m\s+(?:pytest|unittest)\b/iu.test(segment)
      || /^(?:cargo|go)\s+test\b/iu.test(segment)) {
      operation = 'test';
      target = /^(?:cargo|go)\b/iu.test(segment) ? targetLabel(cwd) || 'Tests' : 'Python';
    } else if (/^(?:npm|pnpm|yarn|bun)\b/iu.test(segment)) {
      const script = segment.match(/(?:^|\s)(?:run\s+)?(test|test:[\w:-]+|typecheck|check-types|build|lint)(?:\s|$)/iu)?.[1].toLowerCase();
      if (script?.startsWith('test')) operation = 'test';
      else if (script === 'typecheck' || script === 'check-types') operation = 'typecheck';
      else if (script === 'build') operation = 'build';
      else if (script === 'lint') operation = 'lint';
      target = targetLabel(packagePath(segment, cwd));
    } else if (/^(?:(?:npx|bunx)\s+)?tsc\b/iu.test(segment)) {
      operation = 'typecheck';
      target = targetLabel(cwd);
    } else if (/^(?:(?:npx|bunx)\s+)?(?:vitest|jest)\b/iu.test(segment)) {
      operation = 'test';
      target = targetLabel(cwd);
    } else if (/^(?:(?:npx|bunx)\s+)?(?:vite|webpack|esbuild)\b.*\bbuild\b/iu.test(segment)) {
      operation = 'build';
      target = targetLabel(cwd);
    } else if (/^(?:ruff\s+check|mypy|pyright|(?:(?:npx|bunx)\s+)?(?:eslint|biome\s+check))\b/iu.test(segment)) {
      operation = 'lint';
      target = targetLabel(cwd);
    } else {
      const make = segment.match(/^make\s+(test|check|typecheck|build|lint)\b/iu)?.[1].toLowerCase();
      if (make === 'test' || make === 'check') operation = 'test';
      else if (make === 'typecheck') operation = 'typecheck';
      else if (make === 'build') operation = 'build';
      else if (make === 'lint') operation = 'lint';
      target = targetLabel(cwd);
    }
    if (!operation) continue;
    operations.add(operation);
    targets.add(target || (operation === 'typecheck' || operation === 'build' ? 'Frontend' : 'Validation'));
  }
  if (!operations.size) return null;
  return {
    operations: OPERATION_ORDER.filter((operation) => operations.has(operation)),
    targets: [...targets],
  };
}

interface ValidationCounts {
  passed: number | null;
  failed: number | null;
  skipped: number | null;
  modules: number | null;
}

function countsOnLine(line: string): Omit<ValidationCounts, 'modules'> {
  const matchCount = (pattern: RegExp): number | null => {
    const match = line.match(pattern);
    if (!match) return null;
    const value = match.slice(1).find((item) => item !== undefined);
    return value === undefined ? null : Number(value);
  };
  return {
    passed: matchCount(/(?:^|[\s,|])(?:passed|pass)\s*[:=]?\s*(\d+)|(?:^|[\s,|])(\d+)\s+(?:passed|pass)\b/iu)
      ?? matchCount(/^\s*#\s*pass\s+(\d+)\s*$/iu),
    failed: matchCount(/(?:^|[\s,|])(?:failed|fail)\s*[:=]?\s*(\d+)|(?:^|[\s,|])(\d+)\s+(?:failed|fail)\b/iu)
      ?? matchCount(/^\s*#\s*fail\s+(\d+)\s*$/iu),
    skipped: matchCount(/(?:^|[\s,|])(?:skipped|pending|todo)\s*[:=]?\s*(\d+)|(?:^|[\s,|])(\d+)\s+(?:skipped|pending|todo)\b/iu)
      ?? matchCount(/^\s*#\s*skipped\s+(\d+)\s*$/iu),
  };
}

function namedCounter(line: string): { key: 'passed' | 'failed' | 'skipped' | 'total'; value: number } | null {
  const normalized = line.replace(/^\s*(?:#|ℹ)\s*/u, '').trim();
  const labelFirst = normalized.match(/^(tests?|pass(?:ed)?|fail(?:ed)?|skip(?:ped)?|pending|todo)\s*[:=]?\s*(\d+)\s*$/iu);
  const countFirst = normalized.match(/^(\d+)\s+(tests?|pass(?:ed)?|fail(?:ed)?|skip(?:ped)?|pending|todo)\s*$/iu);
  const label = (labelFirst?.[1] || countFirst?.[2] || '').toLowerCase();
  const rawValue = labelFirst?.[2] || countFirst?.[1];
  if (!label || rawValue === undefined) return null;
  const key = /^tests?$/u.test(label)
    ? 'total'
    : /^pass/u.test(label)
      ? 'passed'
      : /^fail/u.test(label)
        ? 'failed'
        : 'skipped';
  return { key, value: Number(rawValue) };
}

/**
 * Parse the final aggregate test summary instead of summing every occurrence
 * in a log.  Test runners commonly print a per-file summary and then a second
 * aggregate line; adding both makes the UI claim more tests passed than ran.
 * We retain a best-effort line when no aggregate marker exists, but callers
 * still require an explicit success signal before labelling it `passed`.
 */
function validationCounts(text: string): ValidationCounts {
  const clean = text.replace(/\x1b\[[0-9;]*m/gu, '');
  const lines = clean.split(/\r?\n/u).map((line) => line.trim()).filter(Boolean);
  let best: { counts: Omit<ValidationCounts, 'modules'>; score: number; index: number } | null = null;
  const counterBlocks: Array<{
    counts: Omit<ValidationCounts, 'modules'>;
    entries: number;
    end: number;
  }> = [];
  let counterBlock: { counts: Omit<ValidationCounts, 'modules'>; entries: number; end: number } | null = null;
  const explicitBlocks: Array<{ counts: Omit<ValidationCounts, 'modules'>; end: number }> = [];
  lines.forEach((line, index) => {
    const counter = namedCounter(line);
    if (counter) {
      counterBlock ||= { counts: { passed: null, failed: null, skipped: null }, entries: 0, end: index };
      counterBlock.entries += 1;
      counterBlock.end = index;
      if (counter.key !== 'total') counterBlock.counts[counter.key] = counter.value;
    } else if (counterBlock) {
      counterBlocks.push(counterBlock);
      counterBlock = null;
    }
    const counts = countsOnLine(line);
    if (counts.passed === null && counts.failed === null && counts.skipped === null) return;
    // A complete summary line is authoritative. Keep the last such line (or
    // the last line with an explicit runner header), rather than summing all
    // nested per-file summaries.
    if (counts.passed !== null || counts.failed !== null || counts.skipped !== null) {
      const aggregate = /(?:\bin\s+\d+(?:\.\d+)?\s*(?:s|ms)\b|\b(?:total|collected|tests?)\b|,)/iu.test(line)
        && !/^[✓✔✕✘×⚠]/u.test(line)
        && !/\.(?:test|spec)\.[\w.-]+:/iu.test(line);
      if (aggregate) explicitBlocks.push({ counts, end: index });
    }
    let score = 0;
    // Explicit runner aggregate labels are the strongest evidence.
    if (/^(?:tests?|test suites?|test files?|suites?)\s*:/iu.test(line)) score += 10;
    if (/\b(?:total|collected)\b/iu.test(line)) score += 4;
    if (/\bin\s+\d+(?:\.\d+)?\s*(?:s|ms)\b/iu.test(line)) score += 4;
    if (/^\d+\s+(?:passed|failed|skipped|pending|todo)\b/iu.test(line)) score += 2;
    // Individual test lines often start with a checkmark or contain a test
    // path; penalise them so a later aggregate line wins.
    if (/^[✓✔✕✘×⚠]/u.test(line) || /\.(?:test|spec)\.[\w.-]+:/iu.test(line)) score -= 5;
    if (line.includes(',')) score += 1;
    if (!best || score > best.score || (score === best.score && index > best.index)) {
      best = { counts, score, index };
    }
  });
  if (counterBlock) counterBlocks.push(counterBlock);
  // Node's TAP summary and Bun's summary use one labelled count per line.
  // Treat the last contiguous block as one aggregate instead of choosing only
  // its final `fail 0` row or combining it with an earlier nested run.
  const block = counterBlocks.filter((item) => item.entries >= 2).at(-1) || null;
  // TypeScript cannot observe assignments made inside `forEach`; keep the
  // intentionally nullable shape explicit at this merge boundary.
  const bestCounts = best as { counts: Omit<ValidationCounts, 'modules'>; score: number; index: number } | null;
  const selected = explicitBlocks.at(-1)?.counts
    || (block && (!bestCounts || block.end >= bestCounts.index) ? block.counts : bestCounts?.counts);
  const modules = [...clean.matchAll(/\b(\d+)\s+modules?\s+(?:transformed|built)\b/giu)].at(-1)?.[1];
  return {
    passed: selected?.passed ?? null,
    failed: selected?.failed ?? null,
    skipped: selected?.skipped ?? null,
    modules: modules === undefined ? null : Number(modules),
  };
}

function validationSummary(
  detected: DetectedValidation,
  evidence: ResultEvidence | null,
  status: ValidationStatus,
  maxChars: number,
): { summary: string; passed: number | null; failed: number | null; skipped: number | null; modules: number | null } {
  const counts = validationCounts(evidence?.text || '');
  const modules = counts.modules;
  const parts: string[] = [];
  if (counts.passed !== null) parts.push(`${counts.passed} passed`);
  if (counts.failed !== null) parts.push(`${counts.failed} failed`);
  if (counts.skipped !== null) parts.push(`${counts.skipped} skipped`);
  if (modules !== null) parts.push(`${modules} modules built`);
  if (parts.length) return { summary: parts.join(', '), passed: counts.passed, failed: counts.failed, skipped: counts.skipped, modules };
  const operations = detected.operations.join('/');
  if (status === 'running') return { summary: `${operations} running`, passed: null, failed: null, skipped: null, modules };
  if (status === 'passed') return { summary: `${operations} passed`, passed: null, failed: null, skipped: null, modules };
  if (status === 'failed') {
    const errorLine = (evidence?.text || '').split('\n').map((line) => line.trim()).find((line) => /(?:error|failed|failure)/iu.test(line));
    return { summary: compactText(errorLine || `${operations} failed`, maxChars), passed: null, failed: null, skipped: null, modules };
  }
  return { summary: `${operations} status unknown`, passed: null, failed: null, skipped: null, modules };
}

function projectValidations(steps: readonly StepRef[], maxItems: number, maxSummaryChars: number): ValidationSummary {
  const results = resultMaps(steps);
  const items: ValidationResultSummary[] = [];
  for (const ref of steps) {
    if (stringValue(ref.step.kind) !== 'tool_use') continue;
    const command = commandOf(ref.step);
    const detected = command ? detectValidation(command) : null;
    if (!detected) continue;
    const id = toolId(ref.step);
    const result = id ? results.get(ref.trajectoryIndex)?.get(id) : undefined;
    const evidence = result ? unwrapResult(result.step) : null;
    // A missing result is genuinely in flight.  A result without an exit code
    // or explicit success/failure state is deliberately `unknown`: stdout
    // saying "12 passed" is evidence of a count, not proof that the command
    // itself exited successfully.
    const evidenceSummary = validationSummary(detected, evidence, 'unknown', maxSummaryChars);
    const preliminaryStatus: ValidationStatus = !result
      ? 'running'
      : evidence?.explicitFailure || (evidenceSummary.failed ?? 0) > 0
        ? 'failed'
        : evidence?.explicitSuccess
          ? 'passed'
          : 'unknown';
    const status = evidenceSummary.failed !== null && evidenceSummary.failed > 0 ? 'failed' : preliminaryStatus;
    const summary = validationSummary(detected, evidence, status, maxSummaryChars);
    const normalizedSummary = status === preliminaryStatus
      ? summary
      : validationSummary(detected, evidence, status, maxSummaryChars);
    const label = detected.targets.length === 1 ? detected.targets[0] : detected.targets.join(' + ');
    items.push({
      id: `${ref.trajectory.run_id || 'run'}:${ref.trajectory.round_index}:${ref.trajectory.role || 'role'}:${id || ref.stepIndex}`,
      label,
      targets: detected.targets,
      operations: detected.operations,
      command,
      status,
      summary: normalizedSummary.summary,
      passedCount: normalizedSummary.passed,
      failedCount: normalizedSummary.failed,
      skippedCount: normalizedSummary.skipped,
      moduleCount: normalizedSummary.modules,
      exitCode: evidence?.exitCode ?? null,
      durationMs: evidence?.durationMs ?? null,
      trajectoryIndex: ref.trajectoryIndex,
      roundIndex: ref.trajectory.round_index,
      role: ref.trajectory.role,
      stepIndex: ref.stepIndex,
    });
  }
  const shownItems = items.slice(-maxItems);
  return {
    total: items.length,
    shown: shownItems.length,
    hidden: Math.max(0, items.length - shownItems.length),
    passed: items.filter((item) => item.status === 'passed').length,
    failed: items.filter((item) => item.status === 'failed').length,
    running: items.filter((item) => item.status === 'running').length,
    unknown: items.filter((item) => item.status === 'unknown').length,
    items: shownItems,
  };
}

/**
 * Build conservative, render-ready evidence for Codex-style change and validation cards.
 *
 * The current backend does not expose a first-class diff/test schema. This projector
 * therefore trusts normalized file-tool records and paired command results, while
 * keeping unavailable line counts null instead of deriving them from assistant prose.
 */
export function projectArtifactView(
  input: TrajectoryView | readonly (TrajectoryView | null | undefined)[] | null | undefined,
  options: ArtifactProjectionOptions = {},
): ArtifactProjection {
  const trajectories = trajectoriesOf(input);
  const steps = flattenSteps(trajectories);
  return {
    files: projectFiles(steps, limit(options.maxFiles, DEFAULT_MAX_FILES)),
    validations: projectValidations(
      steps,
      limit(options.maxValidations, DEFAULT_MAX_VALIDATIONS),
      limit(options.maxSummaryChars, DEFAULT_MAX_SUMMARY_CHARS),
    ),
  };
}

export const buildArtifactView = projectArtifactView;
export const projectExecutionArtifacts = projectArtifactView;
