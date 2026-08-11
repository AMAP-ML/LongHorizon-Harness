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
}

export interface TrajectoryView {
  run_id?: string;
  round_index: number;
  role: string;
  step_count?: number;
  raw_chars?: number;
  steps: TrajectoryStep[];
}

export interface TrajectoryItem {
  index: number;
  kind: string;
  text: string;
  images: string[];
  hasImage: boolean;
  isError: boolean;
  searchText: string;
  raw: TrajectoryStep;
}

export interface TrajectoryProjection {
  runId: string | null;
  roundIndex: number;
  role: string;
  totalSteps: number;
  shownFrom: number;
  truncated: boolean;
  items: TrajectoryItem[];
}

export interface TrajectoryViewOptions {
  maxSteps?: number;
  maxTextChars?: number;
  maxImagesPerStep?: number;
  includeKinds?: readonly string[];
}

/**
 * Adapter diagnostics that are useful in a raw log but not as operator work.
 * Keep these out of the shared activity projection so Web surfaces do not
 * turn provider deprecation/reconnect chatter into fake task steps. The raw
 * trajectory and Events panel still retain the original record.
 */
export function isTrajectoryNoise(value: unknown): boolean {
  const text = String(value ?? '').trim();
  // Adapter messages are sometimes Markdown-formatted (backticks around the
  // feature name) or include a short prefix. Normalize only for matching so
  // the original audit text remains untouched everywhere else.
  const normalized = text.replace(/`/gu, '').replace(/…/gu, '...');
  // Providers append retry counters and transport details to this marker
  // (for example, `Reconnecting... 1/5 (...)`). Keep that chatter out of the
  // operator-facing trajectory while retaining the raw event for auditing.
  return /^reconnecting(?:\.\.\.)?(?:\s|$)/iu.test(normalized)
    || /\[features\]\.codex_hooks\s+is\s+deprecated\b/iu.test(normalized)
    || /\bcodex_hooks\b.*\bdeprecated\b/iu.test(normalized);
}

const DEFAULT_MAX_STEPS = 120;
const DEFAULT_MAX_TEXT_CHARS = 2000;
const DEFAULT_MAX_IMAGES = 8;
const IMAGE_KEYS = ['image', 'image_url', 'imageUrl'] as const;
const IMAGE_META_KEYS = new Set(['images', 'image', 'image_url', 'imageUrl', 'has_image']);

function limit(value: number | undefined, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(1, Math.floor(value as number));
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function compactText(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, Math.max(1, max - 1)).trimEnd()}…`;
}

function imageSources(step: TrajectoryStep, max: number): string[] {
  const values: unknown[] = [step.images, ...IMAGE_KEYS.map((key) => step[key])];
  const seen = new Set<string>();
  const images: string[] = [];
  for (const value of values) {
    for (const source of Array.isArray(value) ? value : [value]) {
      if (typeof source !== 'string' || !source.trim() || seen.has(source.trim())) continue;
      seen.add(source.trim());
      images.push(source.trim());
      if (images.length >= max) return images;
    }
  }
  return images;
}

function stepText(step: TrajectoryStep, max: number): string {
  const direct = textValue(step.text);
  if (direct) return compactText(direct, max);
  const command = step.input && textValue(step.input.command);
  if (command) return compactText(command, max);
  const visible = Object.fromEntries(Object.entries(step).filter(([key, value]) => !IMAGE_META_KEYS.has(key) && value !== undefined));
  return Object.keys(visible).length ? compactText(JSON.stringify(visible, null, 2), max) : '';
}

function failedByExitCode(step: TrajectoryStep): boolean {
  const raw = step.exit_code ?? step.exitCode;
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw !== 0;
  if (typeof raw === 'string' && /^-?\d+$/u.test(raw.trim())) return Number(raw) !== 0;
  const text = textValue(step.text);
  const match = text.match(/\[exit_code=(-?\d+)\]/iu) || text.match(/"exit_code"\s*:\s*(-?\d+)/iu);
  return Boolean(match && Number(match[1]) !== 0);
}

/** Turn raw trajectory records into a bounded, searchable display projection. */
export function projectTrajectoryView(
  trajectory: TrajectoryView | null | undefined,
  options: TrajectoryViewOptions = {},
): TrajectoryProjection {
  const maxSteps = limit(options.maxSteps, DEFAULT_MAX_STEPS);
  const maxTextChars = limit(options.maxTextChars, DEFAULT_MAX_TEXT_CHARS);
  const maxImages = limit(options.maxImagesPerStep, DEFAULT_MAX_IMAGES);
  const include = options.includeKinds ? new Set(options.includeKinds.map((kind) => kind.toLowerCase())) : null;
  const source = trajectory?.steps || [];
  const filtered = source
    .map((step, sourceIndex) => ({ step, sourceIndex }))
    .filter(({ step }) => Boolean(step && typeof step === 'object'))
    .filter(({ step }) => !include || include.has(String(step.kind || step.type || 'step').toLowerCase()));
  const shown = filtered.slice(-maxSteps);
  const items = shown.map(({ step, sourceIndex }): TrajectoryItem => {
    const kind = textValue(step.kind) || textValue(step.type) || 'step';
    const text = stepText(step, maxTextChars);
    const images = imageSources(step, maxImages);
    const status = textValue(step.status).toLowerCase();
    const isError = step.is_error === true || failedByExitCode(step) || ['error', 'failed', 'failure'].includes(status) || /error|fail/i.test(kind);
    return {
      index: sourceIndex,
      kind,
      text,
      images,
      hasImage: images.length > 0 || step.has_image === true,
      isError,
      searchText: `${kind} ${text}`.trim().toLowerCase(),
      raw: step,
    };
  });
  const totalSteps = filtered.length;
  return {
    runId: trajectory?.run_id || null,
    roundIndex: trajectory?.round_index ?? 0,
    role: trajectory?.role || '',
    totalSteps,
    shownFrom: totalSteps > maxSteps ? filtered[totalSteps - maxSteps].sourceIndex : (items[0]?.index ?? 0),
    truncated: totalSteps > maxSteps,
    items,
  };
}

export const buildTrajectoryView = projectTrajectoryView;
