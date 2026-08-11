import { MAX_ROUNDS, type Snapshot } from './types';

export type CommandName =
  | 'help'
  | 'runs'
  | 'new'
  | 'attach'
  | 'inject'
  | 'approve'
  | 'stop'
  | 'abort'
  | 'resume'
  | 'details'
  | 'events'
  | 'artifacts'
  | 'trajectory';

export interface CommandDefinition {
  name: CommandName;
  aliases?: string[];
  args?: string;
  description: string;
  capability?: string;
  requiresRun?: boolean;
}

export const COMMAND_CATALOG: readonly CommandDefinition[] = [
  { name: 'help', description: 'Show available commands' },
  { name: 'runs', description: 'List runs' },
  { name: 'new', args: '<task> [--language zh|en] [--manager-agent id] [--manager-model id] [--executor-agent id] [--executor-model id] [--auditor-agent id] [--auditor-model id] [--rounds n]', description: 'Start a new run', capability: 'create_run' },
  { name: 'attach', args: '<run_id>', description: 'Switch to a run' },
  { name: 'inject', args: '<text>', description: 'Queue an instruction', capability: 'injections', requiresRun: true },
  { name: 'approve', args: '<approval_id> <action>', description: 'Resolve an approval', capability: 'approvals', requiresRun: true },
  { name: 'stop', description: 'Request a graceful stop', capability: 'stop', requiresRun: true },
  { name: 'abort', description: 'Abort the active run', capability: 'abort', requiresRun: true },
  { name: 'resume', description: 'Resume a finished run', capability: 'resume', requiresRun: true },
  { name: 'details', description: 'Open run details', requiresRun: true },
  { name: 'events', description: 'Open the event panel', requiresRun: true },
  { name: 'artifacts', description: 'Open the artifact panel', requiresRun: true },
  { name: 'trajectory', description: 'Open the trajectory panel', requiresRun: true },
];

export interface ParsedCommand {
  name: CommandName;
  args: string[];
  raw: string;
}

export interface NewRunOptions {
  task: string;
  agent?: string;
  model?: string;
  workspace?: string;
  maxRounds?: number;
  promptLanguage?: 'en' | 'zh';
  roles?: Partial<Record<'manager' | 'executor' | 'auditor', { agent?: string; model?: string }>>;
  error?: string;
}

export function normaliseMaxRounds(value: string | number | null | undefined, fallback = 25): number {
  const safeFallback = Math.min(MAX_ROUNDS, Math.max(1, Number.isSafeInteger(fallback) ? fallback : 25));
  const text = String(value ?? '').trim();
  if (!/^\d+$/u.test(text)) return safeFallback;
  const parsed = Number(text);
  if (!Number.isSafeInteger(parsed)) return safeFallback;
  return Math.min(MAX_ROUNDS, Math.max(1, parsed));
}

/**
 * Parse the optional flags accepted by the `/new` composer command.
 *
 * Both `--flag x` and `--flag=x` are accepted; malformed values are returned
 * as a user-facing error instead of being sent to the worker with an accidental
 * default.
 */
export function parseNewRunArgs(args: readonly string[]): NewRunOptions {
  const task: string[] = [];
  let agent: string | undefined;
  let model: string | undefined;
  let workspace: string | undefined;
  let maxRounds: number | undefined;
  let promptLanguage: 'en' | 'zh' | undefined;
  const roles: NonNullable<NewRunOptions['roles']> = {};
  let parseFlags = true;
  const valueFor = (token: string, flag: string, next: string | undefined): { value?: string; consumed: number; error?: string } => {
    if (token.startsWith(`${flag}=`)) {
      const value = token.slice(flag.length + 1).trim();
      return value ? { value, consumed: 0 } : { consumed: 0, error: `${flag} 需要一个值` };
    }
    if (token !== flag) return { consumed: 0 };
    if (!next || next.startsWith('--')) return { consumed: 0, error: `${flag} 需要一个值` };
    return { value: next.trim(), consumed: 1 };
  };

  for (let index = 0; index < args.length; index += 1) {
    const token = String(args[index] ?? '');
    if (parseFlags && token === '--') {
      parseFlags = false;
      continue;
    }
    if (parseFlags) {
      let handledValueFlag = false;
      for (const flag of [
        '--agent', '--model', '--workspace',
        '--manager-agent', '--manager-model',
        '--executor-agent', '--executor-model',
        '--auditor-agent', '--auditor-model',
      ] as const) {
        const parsed = valueFor(token, flag, args[index + 1]);
        if (parsed.error) return { task: task.join(' ').trim(), error: parsed.error };
        if (parsed.value !== undefined) {
          if (flag === '--agent') agent = parsed.value;
          else if (flag === '--model') model = parsed.value;
          else if (flag === '--workspace') workspace = parsed.value;
          else {
            const match = /^--(manager|executor|auditor)-(agent|model)$/u.exec(flag);
            if (match) {
              const role = match[1] as 'manager' | 'executor' | 'auditor';
              const field = match[2] as 'agent' | 'model';
              roles[role] = { ...(roles[role] || {}), [field]: parsed.value };
            }
          }
          index += parsed.consumed;
          handledValueFlag = true;
          break;
        }
      }
      if (handledValueFlag) continue;
      const language = valueFor(token, '--language', args[index + 1]);
      const promptLanguageFlag = language.value === undefined && !language.error
        ? valueFor(token, '--prompt-language', args[index + 1])
        : { value: undefined, consumed: 0, error: undefined };
      const parsedLanguage = language.value !== undefined || language.error ? language : promptLanguageFlag;
      if (parsedLanguage.error) return { task: task.join(' ').trim(), error: parsedLanguage.error };
      if (parsedLanguage.value !== undefined) {
        if (parsedLanguage.value !== 'en' && parsedLanguage.value !== 'zh') {
          return { task: task.join(' ').trim(), error: '--language 必须是 zh 或 en' };
        }
        promptLanguage = parsedLanguage.value;
        index += parsedLanguage.consumed;
        continue;
      }
      const rounds = valueFor(token, '--rounds', args[index + 1]);
      const maxRoundsFlag = rounds.value === undefined && !rounds.error
        ? valueFor(token, '--max-rounds', args[index + 1])
        : { value: undefined, consumed: 0, error: undefined };
      const parsedRounds = rounds.value !== undefined || rounds.error ? rounds : maxRoundsFlag;
      if (parsedRounds.error) return { task: task.join(' ').trim(), error: parsedRounds.error };
      if (parsedRounds.value !== undefined) {
        if (!/^[1-9]\d*$/u.test(parsedRounds.value)) {
          return { task: task.join(' ').trim(), error: '--rounds 必须是 1–1000 的整数' };
        }
        const parsed = Number(parsedRounds.value);
        if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > MAX_ROUNDS) {
          return { task: task.join(' ').trim(), error: `--rounds 必须在 1–${MAX_ROUNDS} 之间` };
        }
        maxRounds = parsed;
        index += parsedRounds.consumed;
        continue;
      }
    }
    task.push(token);
  }
  return {
    task: task.join(' ').trim(),
    ...(agent ? { agent } : {}),
    ...(model ? { model } : {}),
    ...(workspace ? { workspace } : {}),
    ...(maxRounds !== undefined ? { maxRounds } : {}),
    ...(promptLanguage ? { promptLanguage } : {}),
    ...(Object.keys(roles).length ? { roles } : {}),
  };
}

export function parseCommand(input: string): ParsedCommand | null {
  const raw = input.trim();
  if (!raw.startsWith('/')) return null;
  const parts = raw.slice(1).trim().split(/\s+/u).filter(Boolean);
  const name = parts.shift()?.toLowerCase();
  const definition = COMMAND_CATALOG.find((item) => item.name === name || item.aliases?.includes(name || ''));
  return definition ? { name: definition.name, args: parts, raw } : null;
}

export function availableCommands(
  capabilities: Record<string, boolean>,
  snapshot: Snapshot | null,
  hasRun: boolean,
): CommandDefinition[] {
  return COMMAND_CATALOG.filter((command) => {
    if (command.requiresRun && !hasRun) return false;
    // An older client/API handshake may omit newer capability keys. Treat an
    // omitted key as unknown and let the server return the authoritative
    // error; only an explicit false disables a command.
    if (command.capability && capabilities[command.capability] === false) return false;
    if (command.name === 'inject' && snapshot && !snapshot.controls.can_inject) return false;
    if (command.name === 'stop' && snapshot && !snapshot.controls.can_abort) return false;
    if (command.name === 'abort' && snapshot && !snapshot.controls.can_abort) return false;
    if (command.name === 'resume' && snapshot && !snapshot.controls.can_resume) return false;
    return true;
  });
}

export function commandHelp(
  capabilities: Record<string, boolean>,
  snapshot: Snapshot | null,
  hasRun: boolean,
): string {
  return availableCommands(capabilities, snapshot, hasRun)
    .map((command) => `/${command.name}${command.args ? ` ${command.args}` : ''}  ${command.description}`)
    .join('\n');
}
