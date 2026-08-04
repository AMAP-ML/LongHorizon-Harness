// computer-tool plugin: registers ONE GUI agent tool (`__computer__`) that
// patched @mariozechner/pi-ai exposes to OpenAI Responses as a standard
// function tool. The plugin runs INSIDE the openclaw VM with DISPLAY=:0;
// it dispatches each action via local pyautogui and ALWAYS returns a fresh
// PNG screenshot back to the agent loop after the action batch completes.
//
// History:
//   2026-05-07 native OpenAI computer_use_preview path removed
//   2026-05-07 split-mode `do_actions` peer removed (`__computer__` already
//              accepts batched `actions[]` and auto-screenshots, so a
//              separate act-only tool was redundant)
//   2026-05-07 auxiliary `screenshot` peer removed (`__computer__` already
//              auto-screenshots after every batch, so a separate observe-
//              only tool was redundant; if the model wants to re-observe
//              without acting, it can simply call __computer__ with an
//              empty `actions:[]` or with `{action:{type:"screenshot"}}`)
//
// Loaded by openclaw via jiti from
//   /home/user/.openclaw/extensions/computer-tool/index.ts
// No npm dependencies — only Node built-ins. Calls /usr/bin/python3 with a
// dynamically generated snippet using the system's pyautogui+Pillow.

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, extname, isAbsolute, join, relative, resolve } from "node:path";

const ACTION_TIMEOUT_MS = 60 * 1000;
const SCREENSHOT_TIMEOUT_MS = 30 * 1000;
const MAX_BUFFER = 32 * 1024 * 1024;
const RETURN_SCREENSHOT_MAX_WIDTH = Number(process.env.WEAVEBENCH_MCP_RETURN_SCREENSHOT_MAX_WIDTH || "0") || 0;
const RETURN_SCREENSHOT_JPEG_QUALITY = Math.max(
  25,
  Math.min(95, Number(process.env.WEAVEBENCH_MCP_RETURN_SCREENSHOT_JPEG_QUALITY || "60") || 60),
);

// Persist every captured screenshot under the shared workspace so the eval
// harness can rsync them back next to chat.jsonl. We use a monotonically
// increasing 4-digit step counter (per-process); index 0001 is the first
// screenshot, regardless of which CUA action triggered it.
const SCREENSHOT_DIR = "/tmp_workspace/_screenshots";
const RESULTS_DIR = process.env.WEAVEBENCH_MCP_RESULTS_DIR || "/tmp_workspace/results";
const COORD_LOG_PATH = "/tmp/openclaw/weavebench_computer_coords.log";
let screenshotStep = 0;
const OBSERVE_ONLY_ACTIONS = new Set(["screenshot", "cursor_position", "wait"]);
const COORDINATE_ACTIONS = new Set([
  "click", "left_click", "right_click", "middle_click",
  "double_click", "doubleClick", "triple_click",
  "move", "mouse_move", "mousemove",
  "scroll", "drag",
]);
type ScreenSize = { width: number; height: number };
type PreparedAction = { action: any; transform?: any };
let screenSizeCache: ScreenSize | null = null;

const KEY_MAP: Record<string, string> = {
  ENTER: "enter", RETURN: "enter", TAB: "tab", ESC: "esc", ESCAPE: "esc",
  BACKSPACE: "backspace", DELETE: "delete", SPACE: "space",
  UP: "up", DOWN: "down", LEFT: "left", RIGHT: "right",
  HOME: "home", END: "end", PAGEUP: "pageup", PAGEDOWN: "pagedown",
  CTRL: "ctrl", ALT: "alt", SHIFT: "shift", META: "win", WIN: "win",
  CMD: "ctrl", SUPER: "win",
};

function normKey(k: any): string {
  const s = String(k ?? "").trim();
  return KEY_MAP[s.toUpperCase()] || s.toLowerCase();
}

function pyRepr(v: any): string {
  return JSON.stringify(v);
}

function observeOnlyMode(): boolean {
  return /^(1|true|yes)$/i.test(String(process.env.WEAVEBENCH_COMPUTER_OBSERVE_ONLY || ""));
}

function normalizeCoordinateMode(raw: any): string {
  const s = String(raw ?? "").trim().toLowerCase();
  if (["norm1000", "normalized1000", "normalized-1000", "1000", "screenshot1000"].includes(s)) {
    return "norm1000";
  }
  if (["pixel", "pixels", "screen", "absolute", "abs"].includes(s)) {
    return "pixel";
  }
  return "pixel";
}

function defaultCoordinateMode(): string {
  return normalizeCoordinateMode(process.env.WEAVEBENCH_COMPUTER_COORD_MODE || "pixel");
}

function actionCoordinateMode(action: any): string {
  const explicit = action?.coordinate_space ?? action?.coord_space ?? action?.coordinateMode ?? action?.coordMode;
  return explicit == null ? defaultCoordinateMode() : normalizeCoordinateMode(explicit);
}

function isObservationAction(action: any): boolean {
  if (!action || typeof action !== "object") return false;
  return OBSERVE_ONLY_ACTIONS.has(String(action.type || "").toLowerCase());
}

function hasActionPayload(action: any): boolean {
  return Boolean(action) && typeof action === "object" && Object.keys(action).length > 0;
}

function coerceNumber(v: any): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function shouldScaleNorm1000(x: any, y: any): boolean {
  const nx = coerceNumber(x);
  const ny = coerceNumber(y);
  return nx != null && ny != null && nx >= 0 && nx <= 1000 && ny >= 0 && ny <= 1000;
}

function scaleNorm1000Point(x: any, y: any, size: ScreenSize): { x: number; y: number } {
  const nx = coerceNumber(x) ?? 0;
  const ny = coerceNumber(y) ?? 0;
  return {
    x: Math.max(0, Math.min(size.width - 1, Math.round((nx / 1000) * size.width))),
    y: Math.max(0, Math.min(size.height - 1, Math.round((ny / 1000) * size.height))),
  };
}

async function getScreenSize(): Promise<ScreenSize | null> {
  if (screenSizeCache) return screenSizeCache;
  const py = `
import pyautogui
size = pyautogui.size()
print(f"{int(size.width)} {int(size.height)}")
`;
  const r = await runPython(py, SCREENSHOT_TIMEOUT_MS);
  if (r.code !== 0) return null;
  const m = String(r.stdout || "").trim().match(/^(\d+)\s+(\d+)$/);
  if (!m) return null;
  const width = Number(m[1]);
  const height = Number(m[2]);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return null;
  }
  screenSizeCache = { width, height };
  return screenSizeCache;
}

async function logCoordinateTransform(entry: any): Promise<void> {
  try {
    await fs.mkdir("/tmp/openclaw", { recursive: true });
    await fs.appendFile(COORD_LOG_PATH, `${JSON.stringify({ ts: new Date().toISOString(), ...entry })}\n`);
  } catch {
    // Best-effort diagnostics only.
  }
}

async function prepareActionCoordinates(action: any): Promise<PreparedAction> {
  if (!action || typeof action !== "object") return { action };
  const t = String(action.type || "");
  if (!COORDINATE_ACTIONS.has(t)) return { action };
  const mode = actionCoordinateMode(action);
  if (mode !== "norm1000") return { action };
  const size = await getScreenSize();
  if (!size) {
    return { action, transform: { mode, type: t, status: "screen_size_unavailable" } };
  }

  const out: any = { ...action };
  const points: any[] = [];

  const transformXY = (obj: any, pathLabel: string) => {
    if (!shouldScaleNorm1000(obj?.x, obj?.y)) {
      points.push({ path: pathLabel, status: "kept_pixel_like", x: obj?.x, y: obj?.y });
      return;
    }
    const before = { x: obj.x, y: obj.y };
    const after = scaleNorm1000Point(obj.x, obj.y, size);
    obj.x = after.x;
    obj.y = after.y;
    points.push({ path: pathLabel, status: "scaled", before, after });
  };

  if (t === "drag" && Array.isArray(out.path)) {
    out.path = out.path.map((point: any, index: number) => {
      const p = Array.isArray(point) ? { x: point?.[0] ?? 0, y: point?.[1] ?? 0 } : { ...point };
      transformXY(p, `path[${index}]`);
      return p;
    });
  } else {
    transformXY(out, "x,y");
  }

  return {
    action: out,
    transform: {
      mode,
      type: t,
      screen: size,
      points,
    },
  };
}

// Translate an OpenAI CUA action → an executable Python snippet using pyautogui.
function actionToPython(action: any): string {
  const t = action?.type;
  if (!t) return "";
  switch (t) {
    case "screenshot":
      // No-op on the actuator side; caller always returns a fresh screenshot.
      return "";
    case "click":
    case "left_click":
    case "right_click":
    case "middle_click": {
      const x = Number(action.x ?? 0) | 0;
      const y = Number(action.y ?? 0) | 0;
      let btn = String(action.button || (t === "right_click" ? "right" : t === "middle_click" ? "middle" : "left"));
      if (!["left", "right", "middle"].includes(btn)) btn = "left";
      return `pyautogui.click(${x}, ${y}, button=${pyRepr(btn)})`;
    }
    case "double_click":
    case "doubleClick": {
      const x = Number(action.x ?? 0) | 0;
      const y = Number(action.y ?? 0) | 0;
      return `pyautogui.doubleClick(${x}, ${y})`;
    }
    case "triple_click": {
      const x = Number(action.x ?? 0) | 0;
      const y = Number(action.y ?? 0) | 0;
      return `pyautogui.tripleClick(${x}, ${y})`;
    }
    case "move":
    case "mouse_move":
    case "mousemove": {
      const x = Number(action.x ?? 0) | 0;
      const y = Number(action.y ?? 0) | 0;
      return `pyautogui.moveTo(${x}, ${y}, duration=0.1)`;
    }
    case "type":
    case "keyboard": {
      const text = String(action.text ?? "");
      if (/^[\x00-\x7F]*$/.test(text)) {
        return `pyautogui.typewrite(${pyRepr(text)}, interval=0.02)`;
      }
      return [
        "try:",
        "    import tkinter as tk",
        "    _cua_clip_root = tk.Tk()",
        "    _cua_clip_root.withdraw()",
        "    _cua_clip_root.clipboard_clear()",
        `    _cua_clip_root.clipboard_append(${pyRepr(text)})`,
        "    _cua_clip_root.update()",
        "    pyautogui.hotkey('ctrl', 'v')",
        "    time.sleep(0.2)",
        "    _cua_clip_root.update()",
        "finally:",
        "    try:",
        "        _cua_clip_root.destroy()",
        "    except Exception:",
        "        pass",
      ].join("\n");
    }
    case "keypress":
    case "key": {
      let keys: any = action.keys;
      if (keys == null) keys = action.text || action.key;
      if (typeof keys === "string") keys = [keys];
      if (!Array.isArray(keys)) keys = [];
      const norm = keys.map(normKey);
      if (norm.length === 0) return "";
      if (norm.length === 1) return `pyautogui.press(${pyRepr(norm[0])})`;
      return `pyautogui.hotkey(${norm.map(pyRepr).join(", ")})`;
    }
    case "scroll": {
      const x = Number(action.x ?? 0) | 0;
      const y = Number(action.y ?? 0) | 0;
      const sx = Number(action.scroll_x ?? 0) | 0;
      const sy = Number(action.scroll_y ?? 0) | 0;
      // pyautogui scroll: positive = up. OpenAI sends positive sy = scroll down.
      const amt = sy !== 0 ? -sy : sx !== 0 ? -sx : 0;
      return `pyautogui.moveTo(${x}, ${y}); pyautogui.scroll(${amt})`;
    }
    case "drag": {
      const path = Array.isArray(action.path) ? action.path : [];
      if (path.length < 2) return "";
      let points = path
        .filter((p: any) => p && typeof p === "object")
        .map((p: any) => ({
          x: Number(p.x ?? 0) | 0,
          y: Number(p.y ?? 0) | 0,
        }));
      if (points.length < 2) return "";
      if (points.length === 2) {
        const [start, end] = points;
        points = Array.from({ length: 13 }, (_unused, index) => {
          const t = index / 12;
          const eased = (3 * t * t) - (2 * t * t * t);
          return {
            x: Math.round(start.x + ((end.x - start.x) * eased)),
            y: Math.round(start.y + ((end.y - start.y) * eased)),
          };
        });
      }
      const segmentDuration = Math.max(0.015, Math.min(0.08, 0.6 / Math.max(1, points.length - 1)));
      const lines = [
        `pyautogui.moveTo(${points[0].x}, ${points[0].y}, duration=0.05)`,
        `pyautogui.mouseDown(button="left")`,
        "try:",
        ...points.slice(1).map((p: any) => `    pyautogui.moveTo(${p.x}, ${p.y}, duration=${segmentDuration.toFixed(3)})`),
        "finally:",
        `    pyautogui.mouseUp(button="left")`,
      ];
      return lines.join("\n");
    }
    case "wait": {
      const ms = Number(action.ms ?? action.duration ?? 1000);
      return `time.sleep(${(ms / 1000).toFixed(3)})`;
    }
    case "cursor_position":
      // Reported in screenshot; nothing to actuate.
      return "";
    default:
      return "";
  }
}

function runPython(snippet: string, timeoutMs: number): Promise<{ stdout: string; stderr: string; code: number | null }> {
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      DISPLAY: process.env.DISPLAY || ":0",
      XAUTHORITY: process.env.XAUTHORITY || "/home/user/.Xauthority",
    };
    execFile(
      "/usr/bin/python3",
      ["-c", snippet],
      { env, timeout: timeoutMs, maxBuffer: MAX_BUFFER },
      (err: any, stdout, stderr) => {
        resolve({
          stdout: String(stdout || ""),
          stderr: String(stderr || ""),
          code: err && typeof err.code === "number" ? err.code : err ? -1 : 0,
        });
      },
    );
  });
}

type ScreenshotPayload = { data: string; mimeType: string };
type RawScreenshot = { buf: Buffer; width: number; height: number };

async function captureRawScreenshot(): Promise<RawScreenshot | null> {
  const tmp = join(tmpdir(), `cua_${process.pid}_${Date.now()}_raw.png`);
  const py = `
import json, pyautogui
img = pyautogui.screenshot()
img.save(${pyRepr(tmp)}, "PNG")
print(json.dumps({"width": img.size[0], "height": img.size[1]}))
`;
  const r = await runPython(py, SCREENSHOT_TIMEOUT_MS);
  if (r.code !== 0) {
    return null;
  }
  try {
    const buf = await fs.readFile(tmp);
    let width = 0;
    let height = 0;
    try {
      const meta = JSON.parse(r.stdout.trim().split(/\r?\n/).pop() || "{}");
      width = Number(meta.width || 0);
      height = Number(meta.height || 0);
    } catch {
      // Metadata is helpful but not required for the screenshot bytes.
    }
    return { buf, width, height };
  } catch {
    return null;
  } finally {
    await fs.unlink(tmp).catch(() => {});
  }
}

function pathIsUnder(target: string, root: string): boolean {
  const rel = relative(root, target);
  return rel === "" || (!!rel && !rel.startsWith("..") && !isAbsolute(rel));
}

async function validateScreenshotTarget(rawPath: any): Promise<{ path?: string; err?: string }> {
  const text = String(rawPath || "").trim();
  if (!text) return { err: "save_screenshot requires path" };
  if (!isAbsolute(text)) return { err: "save_screenshot path must be absolute" };
  if (extname(text).toLowerCase() !== ".png") return { err: "save_screenshot path must end with .png" };

  let root = resolve(RESULTS_DIR);
  try {
    root = await fs.realpath(root);
  } catch {
    // The results dir may not exist yet; resolve() is enough for that case.
  }
  const resolvedTarget = resolve(text);
  let resolvedParent = resolve(dirname(text));
  try {
    resolvedParent = await fs.realpath(dirname(text));
  } catch {
    // Parent may be created by save_screenshot.
  }
  if (!pathIsUnder(resolvedTarget, root) || !pathIsUnder(resolvedParent, root)) {
    return { err: `save_screenshot path must stay under ${RESULTS_DIR}` };
  }

  const st = await fs.lstat(text).catch(() => null);
  if (st?.isSymbolicLink()) return { err: "save_screenshot refuses to overwrite symlinks" };
  if (st && !st.isFile()) return { err: "save_screenshot target exists but is not a file" };
  return { path: text };
}

async function activeWindowTitle(): Promise<string> {
  try {
    const r = await new Promise<{ stdout: string; code: number }>((resolveRun) => {
      execFile(
        "xdotool",
        ["getactivewindow", "getwindowname"],
        {
          env: {
            ...process.env,
            DISPLAY: process.env.DISPLAY || ":0",
            XAUTHORITY: process.env.XAUTHORITY || "/home/user/.Xauthority",
          },
          timeout: 1000,
          maxBuffer: 1024 * 1024,
        },
        (err: any, stdout) => {
          resolveRun({ stdout: String(stdout || ""), code: err ? -1 : 0 });
        },
      );
    });
    return r.code === 0 ? r.stdout.trim() : "";
  } catch {
    return "";
  }
}

async function writeJsonAtomic(path: string, payload: any): Promise<void> {
  const tmp = join(dirname(path), `.${Date.now()}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`);
  try {
    await fs.writeFile(tmp, JSON.stringify(payload, null, 2) + "\n", "utf8");
    await fs.rename(tmp, path);
  } finally {
    await fs.unlink(tmp).catch(() => {});
  }
}

async function saveCurrentScreenshot(action: any): Promise<{ ok: boolean; err?: string; text?: string }> {
  const validated = await validateScreenshotTarget(action?.path ?? action?.file);
  if (!validated.path) return { ok: false, err: validated.err || "save_screenshot invalid path" };
  const target = validated.path;
  const overwrite = action?.overwrite == null ? true : /^(1|true|yes|on)$/i.test(String(action.overwrite));
  const existing = await fs.lstat(target).catch(() => null);
  if (existing && !overwrite) {
    return { ok: false, err: `save_screenshot target exists: ${target}` };
  }

  const shot = await captureRawScreenshot();
  if (!shot) {
    return { ok: false, err: "save_screenshot could not capture current screen" };
  }

  await fs.mkdir(dirname(target), { recursive: true });
  let previousSha256 = "";
  if (existing?.isFile()) {
    previousSha256 = createHash("sha256").update(await fs.readFile(target)).digest("hex");
  }
  const tmp = join(dirname(target), `.${Date.now()}.${process.pid}.${Math.random().toString(16).slice(2)}.png.tmp`);
  await fs.writeFile(tmp, shot.buf);
  await fs.rename(tmp, target);

  const sha256 = createHash("sha256").update(shot.buf).digest("hex");
  const metaPath = `${target}.meta.json`;
  const metadata: any = {
    action: "save_screenshot",
    active_window: await activeWindowTitle(),
    bytes: shot.buf.length,
    capture_source: "real_screen",
    capture_time: new Date().toISOString(),
    coord_mode: defaultCoordinateMode(),
    path: target,
    producer: "__computer__",
    resolution: [shot.width, shot.height],
    sha256,
    source: "pyautogui.screenshot",
  };
  if (previousSha256) {
    metadata.overwrote = true;
    metadata.previous_sha256 = previousSha256;
  }
  await writeJsonAtomic(metaPath, metadata);
  return {
    ok: true,
    text: `[save_screenshot] saved current real screen to ${target} (${shot.buf.length} bytes, sha256=${sha256}, meta=${metaPath})`,
  };
}

async function captureScreenshot(actionLabel: string): Promise<ScreenshotPayload | null> {
  // Write to /tmp file to dodge stdout binary issues, then read & base64 it.
  const tmp = join(tmpdir(), `cua_${process.pid}_${Date.now()}.png`);
  const tmpReturn = join(tmpdir(), `cua_${process.pid}_${Date.now()}_return.jpg`);
  const py = `
import pyautogui
img = pyautogui.screenshot()
img.save(${pyRepr(tmp)}, "PNG")
max_width = ${Math.max(0, Math.floor(RETURN_SCREENSHOT_MAX_WIDTH))}
if max_width > 0:
    from PIL import Image
    out = img.convert("RGB")
    if out.width > max_width:
        ratio = max_width / float(out.width)
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        out = out.resize((max_width, max(1, round(out.height * ratio))), resample)
    out.save(${pyRepr(tmpReturn)}, "JPEG", quality=${Math.floor(RETURN_SCREENSHOT_JPEG_QUALITY)}, optimize=True)
print("OK")
`;
  const r = await runPython(py, SCREENSHOT_TIMEOUT_MS);
  if (r.code !== 0) {
    return null;
  }
  try {
    const buf = await fs.readFile(tmp);
    const returnBuf = RETURN_SCREENSHOT_MAX_WIDTH > 0
      ? await fs.readFile(tmpReturn)
      : buf;
    const returnMime = RETURN_SCREENSHOT_MAX_WIDTH > 0 ? "image/jpeg" : "image/png";
    // Persist into the shared workspace screenshots dir for the eval harness.
    // Best-effort: if mkdir/writeFile fails (e.g. /tmp_workspace not present
    // outside an eval), we still return the base64 to the agent.
    try {
      screenshotStep += 1;
      const stepStr = String(screenshotStep).padStart(4, "0");
      const safeLabel = (actionLabel || "screenshot")
        .toString()
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 32) || "screenshot";
      await fs.mkdir(SCREENSHOT_DIR, { recursive: true });
      const persistPath = join(SCREENSHOT_DIR, `screenshot_${stepStr}_${safeLabel}.png`);
      await fs.writeFile(persistPath, buf);
    } catch {
      // ignore — persistence is for offline review only.
    }
    await fs.unlink(tmp).catch(() => {});
    await fs.unlink(tmpReturn).catch(() => {});
    return { data: returnBuf.toString("base64"), mimeType: returnMime };
  } catch {
    return null;
  }
}

async function executeAction(action: any): Promise<{ ok: boolean; err?: string; text?: string; transform?: any }> {
  const prepared = await prepareActionCoordinates(action);
  if (prepared.action?.type === "save_screenshot") {
    const saved = await saveCurrentScreenshot(prepared.action);
    return { ...saved, transform: prepared.transform };
  }
  const snippet = actionToPython(prepared.action);
  if (!snippet) {
    if (prepared.action?.type === "screenshot" || prepared.action?.type === "cursor_position") {
      return { ok: true, transform: prepared.transform };
    }
    return { ok: false, err: `unhandled action: ${JSON.stringify(action).slice(0, 200)}`, transform: prepared.transform };
  }
  const py = `import pyautogui, time\npyautogui.FAILSAFE = False\n${snippet}\n`;
  const r = await runPython(py, ACTION_TIMEOUT_MS);
  if (r.code !== 0) {
    return { ok: false, err: (r.stderr || r.stdout || "python exit nonzero").slice(-500), transform: prepared.transform };
  }
  // Tiny settle so subsequent screenshot captures the post-action state.
  await new Promise((res) => setTimeout(res, 150));
  return { ok: true, transform: prepared.transform };
}

// Mouse button code (int 1-5) → pyautogui button name. The integer encoding
// follows the OpenAI computer-use namespace convention (1=left, 2=wheel/
// middle, 3=right, 4=back, 5=forward).
const BUTTON_INT_TO_NAME: Record<number, string> = {
  1: "left",
  2: "middle",
  3: "right",
  4: "back",
  5: "forward",
};

// Translate a flat action shape ({action:"click", x, y, button:1, ...}) into
// the internal CUA-style action shape ({type:"click", x, y, button:"left",
// ...}) that `actionToPython` understands. Pass-through if the action is
// already CUA-shaped (has `type`).
function normalizeAction(a: any): any {
  if (!a || typeof a !== "object") return a;
  // Already CUA-shaped: passthrough.
  if (typeof a.type === "string") {
    const t = String(a.type).toLowerCase();
    if (["save_current_screenshot", "capture_screenshot", "capture_current_screen"].includes(t)) {
      return { ...a, type: "save_screenshot" };
    }
    return a;
  }
  let name = String(a.action || "").toLowerCase();
  if (["save_current_screenshot", "capture_screenshot", "capture_current_screen"].includes(name)) {
    name = "save_screenshot";
  }
  if (!name) return a;
  const out: any = { type: name };
  for (const k of Object.keys(a)) {
    if (k === "action") continue;
    out[k] = a[k];
  }
  // Button as int (1=left, 2=wheel, 3=right, 4=back, 5=forward).
  if (typeof out.button === "number") {
    out.button = BUTTON_INT_TO_NAME[out.button] || "left";
  }
  // Drag path may arrive as number[][] (e.g. [[x,y],[x,y]]); the executor
  // expects [{x,y},{x,y}].
  if (name === "drag" && Array.isArray(out.path) && out.path.length > 0
      && Array.isArray(out.path[0])) {
    out.path = out.path.map((p: any[]) => ({ x: p?.[0] ?? 0, y: p?.[1] ?? 0 }));
  }
  // `wait` may carry no args; default to ~1s pause.
  if (name === "wait" && out.ms == null && out.duration == null) {
    out.ms = 1000;
  }
  // `keypress` uses {keys: string[]}; actionToPython handles it directly.
  return out;
}

// (SCREENSHOT_DESCRIPTION removed 2026-05-07 along with the standalone
// `screenshot` tool; __computer__ already auto-screenshots after every
// batch, so a separate observe-only tool was redundant.)


export default function register(api: any) {
  const observeOnly = observeOnlyMode();
  const coordMode = defaultCoordinateMode();
  const coordHint = coordMode === "norm1000"
    ? "Coordinate mode: x/y coordinates are normalized to a 0-1000 screenshot space and will be scaled to the current 1920x1080-style screen before pyautogui executes them. If you deliberately need raw pixels for one action, set coordinate_space:\"pixel\" on that action."
    : "Coordinate mode: x/y coordinates are raw screen pixels. If a model emits normalized screenshot coordinates for one action, set coordinate_space:\"norm1000\" on that action.";
  // ---- __computer__ -------------------------------------------------------
  // The single GUI tool. A fat function tool that accepts either a
  // single-action shape `{action:{...}}` or a batched shape
  // `{actions:[...]}`, executes each step via pyautogui, and ALWAYS returns
  // a fresh screenshot. To re-observe without acting, call with
  // `{actions:[]}` or `{action:{type:"screenshot"}}` — both are no-op
  // execution paths that still trigger the trailing screenshot capture.
  api.registerTool({
    name: "__computer__",
    description: (
      observeOnly
        ? [
            "Observe the remote desktop and return a fresh screenshot.",
            "Verifier observe-only mode is active: mutating actions are rejected.",
            "Allowed actions: empty `actions:[]`, `screenshot`, `cursor_position`, and `wait`.",
            "Do not click, type, keypress, scroll, move the cursor, or drag.",
          ]
        : [
            "Perform one or more computer actions in sequence on the remote desktop.",
            "Valid actions: click, double_click, drag, keypress, move, scroll, type, wait, save_screenshot.",
            coordHint,
            "Call with either:",
            "  • single-action shape  `{action: {type:<name>, ...kwargs}}`",
            "  • batched shape        `{actions: [{action:<name>, ...kwargs}, ...]}`",
            "Example:",
            "  {\"actions\": [{\"action\":\"click\",\"x\":100,\"y\":200,\"button\":1}, {\"action\":\"type\",\"text\":\"Hello, world!\"}]}",
            "PREFER batching multiple imminent actions in a single call (e.g. click then type, click then keypress, scroll then click) to minimize round-trips.",
            "The plugin runs the actions via pyautogui and ALWAYS returns a fresh screenshot of the resulting screen state, so a separate observation tool is unnecessary. Pass `actions:[]` (empty batch) for pure observation — the screenshot is still returned.",
            "Use save_screenshot only to persist the current real screen to a task-requested /tmp_workspace/results/*.png path. It writes a .meta.json sidecar with sha256/resolution/source metadata. It cannot copy, edit, or synthesize images.",
            "IMPORTANT: After an observation-only call (`actions:[]`), you MUST continue on the next turn with concrete actions to make progress on the task. Observing the screen does NOT complete the task; do not end the turn after just receiving a screenshot.",
            "",
            "Action schemas:",
            "- click        {x:int, y:int, button:int (1-left, 2-wheel/middle, 3-right, 4-back, 5-forward), keys?:string[]}",
            "- double_click {x:int, y:int, keys?:string[]}",
            "- drag         {path: number[][]   // [[x,y],[x,y],...] , keys?:string[]}",
            "- keypress     {keys:string[]}     // e.g. ['ctrl','c']",
            "- move         {x:int, y:int, keys?:string[]}",
            "- scroll       {x:int, y:int, scroll_x:int, scroll_y:int, keys?:string[]}",
            "- type         {text:string}",
            "- wait         {ms?:int}           // optional; default 1000ms; brief pause",
            "- save_screenshot {path:string, overwrite?:bool} // current real screen -> /tmp_workspace/results/*.png",
          ]
    ).join("\n"),
    parameters: {
      type: "object",
      properties: {
        action: {
          type: "object",
          additionalProperties: true,
        },
        actions: {
          type: "array",
          items: {
            type: "object",
            additionalProperties: true,
          },
        },
      },
      // NOTE: neither `action` nor `actions` is required at the framework
      // level — the plugin executor demultiplexes both shapes. Without
      // this loosening, batched `{actions:[...]}` calls would be rejected
      // by openclaw's local schema validator before reaching execute().
      additionalProperties: true,
    },
    async execute(_id: string, params: any) {
      // Demux: prefer batched `actions[]` when present, else fall back to
      // the legacy single `action` shape. An empty `actions:[]` skips the
      // executeAction loop entirely and goes straight to the trailing
      // screenshot capture (pure-observation path).
      let ops: any[];
      if (Array.isArray(params?.actions)) {
        ops = params.actions.length > 0 ? params.actions.map(normalizeAction) : [];
      } else if (Array.isArray(params?.action?.actions)) {
        ops = params.action.actions.length > 0 ? params.action.actions.map(normalizeAction) : [];
      } else {
        const single = params?.action && typeof params.action === "object" ? params.action : params;
        ops = hasActionPayload(single) ? [normalizeAction(single)] : [];
      }

      if (observeOnly) {
        const blocked = ops.filter((op) => !isObservationAction(op));
        if (blocked.length > 0) {
          const labels = blocked.map((op) => String(op?.type || "unknown")).join(", ");
          const shot = await captureScreenshot("observe_only_blocked");
          const content: any[] = [{
            type: "text",
            text: `[__computer__ observe-only] blocked mutating GUI action(s): ${labels}`,
          }];
          if (shot) {
            content.push({ type: "image", mimeType: shot.mimeType, data: shot.data });
          }
          return { content };
        }
      }

      const errors: string[] = [];
      const infos: string[] = [];
      let lastLabel = "action";
      for (const op of ops) {
        const res = await executeAction(op);
        if (res.transform) {
          await logCoordinateTransform(res.transform);
        }
        if (op && typeof op === "object" && typeof op.type === "string") {
          lastLabel = String(op.type);
        }
        if (!res.ok) {
          errors.push(res.err || "unknown");
          // Continue executing remaining actions in the batch — best-effort:
          // a partial failure still returns the resulting screenshot so the
          // model can recover.
        } else if (res.text) {
          infos.push(res.text);
        }
      }
      const shot = await captureScreenshot(lastLabel);
      const content: any[] = [];
      if (errors.length > 0) {
        content.push({
          type: "text",
          text: `[__computer__ error] ${errors.join(" | ")}`,
        });
      }
      for (const info of infos) {
        content.push({ type: "text", text: info });
      }
      if (shot) {
        content.push({ type: "image", mimeType: shot.mimeType, data: shot.data });
      } else {
        content.push({ type: "text", text: "[__computer__] screenshot capture failed" });
      }
      return { content };
    },
  });
}
