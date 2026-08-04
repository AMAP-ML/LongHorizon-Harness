import Anthropic from "@anthropic-ai/sdk";
import { getEnvApiKey } from "../env-api-keys.js";
import { calculateCost } from "../models.js";
import { AssistantMessageEventStream } from "../utils/event-stream.js";
import { parseStreamingJson } from "../utils/json-parse.js";
import { sanitizeSurrogates } from "../utils/sanitize-unicode.js";
import { buildCopilotDynamicHeaders, hasCopilotVisionInput } from "./github-copilot-headers.js";
import { adjustMaxTokensForThinking, buildBaseOptions } from "./simple-options.js";
import { transformMessages } from "./transform-messages.js";
/**
 * Resolve cache retention preference.
 * Defaults to "short" and uses PI_CACHE_RETENTION for backward compatibility.
 */
function resolveCacheRetention(cacheRetention) {
    if (cacheRetention) {
        return cacheRetention;
    }
    if (typeof process !== "undefined" && process.env.PI_CACHE_RETENTION === "long") {
        return "long";
    }
    return "short";
}
function getCacheControl(baseUrl, cacheRetention) {
    const retention = resolveCacheRetention(cacheRetention);
    if (retention === "none") {
        return { retention };
    }
    const ttl = retention === "long" && baseUrl.includes("api.anthropic.com") ? "1h" : undefined;
    return {
        retention,
        cacheControl: { type: "ephemeral", ...(ttl && { ttl }) },
    };
}
// Stealth mode: Mimic Claude Code's tool naming exactly
const claudeCodeVersion = "2.1.62";
// Claude Code 2.x tool names (canonical casing)
// Source: https://cchistory.mariozechner.at/data/prompts-2.1.11.md
// To update: https://github.com/badlogic/cchistory
const claudeCodeTools = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "KillShell",
    "NotebookEdit",
    "Skill",
    "Task",
    "TaskOutput",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
];
const ccToolLookup = new Map(claudeCodeTools.map((t) => [t.toLowerCase(), t]));
// Convert tool name to CC canonical casing if it matches (case-insensitive)
const toClaudeCodeName = (name) => ccToolLookup.get(name.toLowerCase()) ?? name;
const fromClaudeCodeName = (name, tools) => {
    if (tools && tools.length > 0) {
        const lowerName = name.toLowerCase();
        const matchedTool = tools.find((tool) => tool.name.toLowerCase() === lowerName);
        if (matchedTool)
            return matchedTool.name;
    }
    return name;
};
/**
 * Convert content blocks to Anthropic API format
 */
function convertContentBlocks(content) {
    // If only text blocks, return as concatenated string for simplicity
    const hasImages = content.some((c) => c.type === "image");
    if (!hasImages) {
        return sanitizeSurrogates(content.map((c) => c.text).join("\n"));
    }
    // If we have images, convert to content block array
    const blocks = content.map((block) => {
        if (block.type === "text") {
            return {
                type: "text",
                text: sanitizeSurrogates(block.text),
            };
        }
        return {
            type: "image",
            source: {
                type: "base64",
                media_type: block.mimeType,
                data: block.data,
            },
        };
    });
    // If only images (no text), add placeholder text block
    const hasText = blocks.some((b) => b.type === "text");
    if (!hasText) {
        blocks.unshift({
            type: "text",
            text: "(see attached image)",
        });
    }
    return blocks;
}
function mergeHeaders(...headerSources) {
    const merged = {};
    for (const headers of headerSources) {
        if (headers) {
            Object.assign(merged, headers);
        }
    }
    return merged;
}
export const streamAnthropic = (model, context, options) => {
    const stream = new AssistantMessageEventStream();
    (async () => {
        const output = {
            role: "assistant",
            content: [],
            api: model.api,
            provider: model.provider,
            model: model.id,
            usage: {
                input: 0,
                output: 0,
                reasoning: 0,
                cacheRead: 0,
                cacheWrite: 0,
                totalTokens: 0,
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
            },
            stopReason: "stop",
            timestamp: Date.now(),
        };
        // === WeaveBench retry wrapper ===
        // Catch ANY exception (SSE event-order, network, 5xx, transient timeouts) and retry the
        // full Anthropic request from scratch. Disabled when downstream has already seen any
        // non-"start" event (text/tool/thinking content) to avoid duplicating output to the
        // openclaw agent loop. Tunable via env:
        //   WEAVEBENCH_LLM_MAX_RETRIES  (default 5)   — total attempts including the first
        //   WEAVEBENCH_LLM_BACKOFF_MS   (default 2000) — base ms; backoff = base * 2^(attempt-2)
        const MAX_RETRIES = Math.max(1, parseInt(process.env.WEAVEBENCH_LLM_MAX_RETRIES ?? "5", 10));
        const BACKOFF_BASE_MS = Math.max(0, parseInt(process.env.WEAVEBENCH_LLM_BACKOFF_MS ?? "2000", 10));
        // Push `start` exactly once so the downstream consumer sees one logical
        // conversation start regardless of how many retries we do underneath.
        stream.push({ type: "start", partial: output });
        let lastError = null;
        let succeeded = false;
        let unrecoverable = false;
        for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            if (attempt > 1) {
                // Reset per-attempt state — fresh slate for the retry.
                output.content.length = 0;
                output.usage.input = 0;
                output.usage.output = 0;
                output.usage.reasoning = 0;
                output.usage.cacheRead = 0;
                output.usage.cacheWrite = 0;
                output.usage.totalTokens = 0;
                output.usage.cost = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };
                output.stopReason = "stop";
                const backoffMs = BACKOFF_BASE_MS * Math.pow(2, attempt - 2);
                console.error(`[weavebench-retry] anthropic attempt ${attempt}/${MAX_RETRIES} after ${backoffMs}ms (prev error: ${lastError?.message ?? lastError})`);
                await new Promise((r) => setTimeout(r, backoffMs));
                if (options?.signal?.aborted) { unrecoverable = true; break; }
            }
            try {
                const apiKey = options?.apiKey ?? getEnvApiKey(model.provider) ?? "";
                let copilotDynamicHeaders;
                if (model.provider === "github-copilot") {
                    const hasImages = hasCopilotVisionInput(context.messages);
                    copilotDynamicHeaders = buildCopilotDynamicHeaders({
                        messages: context.messages,
                        hasImages,
                    });
                }
                const { client, isOAuthToken } = createClient(model, apiKey, options?.interleavedThinking ?? true, options?.headers, copilotDynamicHeaders);
                let params = buildParams(model, context, isOAuthToken, options);
                const nextParams = await options?.onPayload?.(params, model);
                if (nextParams !== undefined) {
                    params = nextParams;
                }
                const anthropicStream = client.messages.stream({ ...params, stream: true }, { signal: options?.signal });
                const blocks = output.content;
                for await (const event of anthropicStream) {
                    if (event.type === "message_start") {
                        // Capture initial token usage from message_start event
                        // This ensures we have input token counts even if the stream is aborted early
                        output.usage.input = event.message.usage.input_tokens || 0;
                        output.usage.output = event.message.usage.output_tokens || 0;
                        output.usage.cacheRead = event.message.usage.cache_read_input_tokens || 0;
                        output.usage.cacheWrite = event.message.usage.cache_creation_input_tokens || 0;
                        // keep schema homogeneous with Responses/Chat backends — surface a
                        // 'reasoning' field even though the public Anthropic Messages API does NOT
                        // report extended-thinking tokens separately (thinking tokens are folded
                        // into output_tokens). If a future Anthropic API revision (or proxy such
                        // as some Anthropic-API-bridged gateways) starts reporting a top-level
                        // thinking_tokens or reasoning_tokens field, capture it; otherwise
                        // reasoning stays 0 (an honest signal that the API didn't break it out).
                        output.usage.reasoning =
                            event.message.usage.thinking_tokens ||
                            event.message.usage.reasoning_tokens || 0;
                        // Anthropic doesn't provide total_tokens, compute from components
                        output.usage.totalTokens =
                            output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite;
                        calculateCost(model, output.usage);
                    }
                    else if (event.type === "content_block_start") {
                        if (event.content_block.type === "text") {
                            const block = {
                                type: "text",
                                text: "",
                                index: event.index,
                            };
                            output.content.push(block);
                            stream.push({ type: "text_start", contentIndex: output.content.length - 1, partial: output });
                        }
                        else if (event.content_block.type === "thinking") {
                            const block = {
                                type: "thinking",
                                thinking: "",
                                thinkingSignature: "",
                                index: event.index,
                            };
                            output.content.push(block);
                            stream.push({ type: "thinking_start", contentIndex: output.content.length - 1, partial: output });
                        }
                        else if (event.content_block.type === "redacted_thinking") {
                            const block = {
                                type: "thinking",
                                thinking: "[Reasoning redacted]",
                                thinkingSignature: event.content_block.data,
                                redacted: true,
                                index: event.index,
                            };
                            output.content.push(block);
                            stream.push({ type: "thinking_start", contentIndex: output.content.length - 1, partial: output });
                        }
                        else if (event.content_block.type === "tool_use") {
                            const block = {
                                type: "toolCall",
                                id: event.content_block.id,
                                name: isOAuthToken
                                    ? fromClaudeCodeName(event.content_block.name, context.tools)
                                    : event.content_block.name,
                                arguments: event.content_block.input ?? {},
                                partialJson: "",
                                index: event.index,
                            };
                            output.content.push(block);
                            stream.push({ type: "toolcall_start", contentIndex: output.content.length - 1, partial: output });
                        }
                    }
                    else if (event.type === "content_block_delta") {
                        if (event.delta.type === "text_delta") {
                            const index = blocks.findIndex((b) => b.index === event.index);
                            const block = blocks[index];
                            if (block && block.type === "text") {
                                block.text += event.delta.text;
                                stream.push({
                                    type: "text_delta",
                                    contentIndex: index,
                                    delta: event.delta.text,
                                    partial: output,
                                });
                            }
                        }
                        else if (event.delta.type === "thinking_delta") {
                            const index = blocks.findIndex((b) => b.index === event.index);
                            const block = blocks[index];
                            if (block && block.type === "thinking") {
                                block.thinking += event.delta.thinking;
                                stream.push({
                                    type: "thinking_delta",
                                    contentIndex: index,
                                    delta: event.delta.thinking,
                                    partial: output,
                                });
                            }
                        }
                        else if (event.delta.type === "input_json_delta") {
                            const index = blocks.findIndex((b) => b.index === event.index);
                            const block = blocks[index];
                            if (block && block.type === "toolCall") {
                                block.partialJson += event.delta.partial_json;
                                block.arguments = parseStreamingJson(block.partialJson);
                                stream.push({
                                    type: "toolcall_delta",
                                    contentIndex: index,
                                    delta: event.delta.partial_json,
                                    partial: output,
                                });
                            }
                        }
                        else if (event.delta.type === "signature_delta") {
                            const index = blocks.findIndex((b) => b.index === event.index);
                            const block = blocks[index];
                            if (block && block.type === "thinking") {
                                block.thinkingSignature = block.thinkingSignature || "";
                                block.thinkingSignature += event.delta.signature;
                            }
                        }
                    }
                    else if (event.type === "content_block_stop") {
                        const index = blocks.findIndex((b) => b.index === event.index);
                        const block = blocks[index];
                        if (block) {
                            delete block.index;
                            if (block.type === "text") {
                                stream.push({
                                    type: "text_end",
                                    contentIndex: index,
                                    content: block.text,
                                    partial: output,
                                });
                            }
                            else if (block.type === "thinking") {
                                stream.push({
                                    type: "thinking_end",
                                    contentIndex: index,
                                    content: block.thinking,
                                    partial: output,
                                });
                            }
                            else if (block.type === "toolCall") {
                                block.arguments = parseStreamingJson(block.partialJson);
                                delete block.partialJson;
                                stream.push({
                                    type: "toolcall_end",
                                    contentIndex: index,
                                    toolCall: block,
                                    partial: output,
                                });
                            }
                        }
                    }
                    else if (event.type === "message_delta") {
                        if (event.delta.stop_reason) {
                            output.stopReason = mapStopReason(event.delta.stop_reason);
                        }
                        // Only update usage fields if present (not null).
                        // Preserves input_tokens from message_start when proxies omit it in message_delta.
                        if (event.usage.input_tokens != null) {
                            output.usage.input = event.usage.input_tokens;
                        }
                        if (event.usage.output_tokens != null) {
                            output.usage.output = event.usage.output_tokens;
                        }
                        if (event.usage.cache_read_input_tokens != null) {
                            output.usage.cacheRead = event.usage.cache_read_input_tokens;
                        }
                        if (event.usage.cache_creation_input_tokens != null) {
                            output.usage.cacheWrite = event.usage.cache_creation_input_tokens;
                        }
                        // forward-compat — capture reasoning_tokens / thinking_tokens
                        // from message_delta if a future API revision adds them. See the
                        // matching block in message_start above for the rationale.
                        if (event.usage.thinking_tokens != null) {
                            output.usage.reasoning = event.usage.thinking_tokens;
                        } else if (event.usage.reasoning_tokens != null) {
                            output.usage.reasoning = event.usage.reasoning_tokens;
                        }
                        // Anthropic doesn't provide total_tokens, compute from components
                        output.usage.totalTokens =
                            output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite;
                        calculateCost(model, output.usage);
                    }
                }
                if (options?.signal?.aborted) {
                    throw new Error("Request was aborted");
                }
                if (output.stopReason === "aborted" || output.stopReason === "error") {
                    throw new Error("An unknown error occurred");
                }
                // NewAPI's `_type:newapi_channel_conn` channel-rotation can return an
                // "empty success" — message_start + message_stop with zero content blocks
                // and 0 output tokens. SDK treats this as normal completion, but the
                // downstream openclaw agent loop interprets stopReason="stop" with no
                // tool_use as end_turn and exits the agentic loop early. Treat it as a
                // transient upstream error so retry kicks in (safe because no content
                // was emitted to downstream — the catch block's "no content yet → retry"
                // branch handles it cleanly).
                if (output.content.length === 0 && output.usage.output === 0) {
                    throw new Error(`Empty stream from upstream (0 content blocks, 0 output tokens, stopReason=${output.stopReason}) — likely NewAPI channel rotation`);
                }
                succeeded = true;
                break;
            }
            catch (error) {
                lastError = error;
                if (options?.signal?.aborted) { unrecoverable = true; break; }
                if (output.content.length > 0) {
                    // Downstream consumer has already seen partial content — retrying would
                    // duplicate text/tool calls into the agent loop. Bail out instead.
                    console.error(`[weavebench-retry] anthropic giving up after attempt ${attempt} (downstream already saw ${output.content.length} content block(s)): ${error?.message ?? error}`);
                    unrecoverable = true;
                    break;
                }
                // No content emitted yet — safe to loop and retry transparently.
            }
        }
        if (succeeded) {
            stream.push({ type: "done", reason: output.stopReason, message: output });
            stream.end();
        }
        else {
            for (const block of output.content)
                delete block.index;
            output.stopReason = options?.signal?.aborted ? "aborted" : "error";
            output.errorMessage = lastError instanceof Error ? lastError.message : JSON.stringify(lastError);
            stream.push({ type: "error", reason: output.stopReason, error: output });
            stream.end();
        }
    })();
    return stream;
};
/**
 * Check if a model supports adaptive thinking (Opus 4.6 and Sonnet 4.6)
 */
function supportsAdaptiveThinking(modelId) {
    // Opus 4.6 and Sonnet 4.6 model IDs (with or without date suffix)
    return (modelId.includes("opus-4-6") ||
        modelId.includes("opus-4.6") ||
        modelId.includes("sonnet-4-6") ||
        modelId.includes("sonnet-4.6"));
}
/**
 * Map ThinkingLevel to Anthropic effort levels for adaptive thinking.
 * Note: effort "max" is only valid on Opus 4.6.
 */
function mapThinkingLevelToEffort(level, modelId) {
    switch (level) {
        case "minimal":
            return "low";
        case "low":
            return "low";
        case "medium":
            return "medium";
        case "high":
            return "high";
        case "xhigh":
            return modelId.includes("opus-4-6") || modelId.includes("opus-4.6") ? "max" : "high";
        default:
            return "high";
    }
}
export const streamSimpleAnthropic = (model, context, options) => {
    const apiKey = options?.apiKey || getEnvApiKey(model.provider);
    if (!apiKey) {
        throw new Error(`No API key for provider: ${model.provider}`);
    }
    const base = buildBaseOptions(model, options, apiKey);
    if (!options?.reasoning) {
        return streamAnthropic(model, context, { ...base, thinkingEnabled: false });
    }
    // For Opus 4.6 and Sonnet 4.6: use adaptive thinking with effort level
    // For older models: use budget-based thinking
    if (supportsAdaptiveThinking(model.id)) {
        const effort = mapThinkingLevelToEffort(options.reasoning, model.id);
        return streamAnthropic(model, context, {
            ...base,
            thinkingEnabled: true,
            effort,
        });
    }
    const adjusted = adjustMaxTokensForThinking(base.maxTokens || 0, model.maxTokens, options.reasoning, options.thinkingBudgets);
    return streamAnthropic(model, context, {
        ...base,
        maxTokens: adjusted.maxTokens,
        thinkingEnabled: true,
        thinkingBudgetTokens: adjusted.thinkingBudget,
    });
};
function isOAuthToken(apiKey) {
    return apiKey.includes("sk-ant-oat");
}
function createClient(model, apiKey, interleavedThinking, optionsHeaders, dynamicHeaders) {
    // Adaptive thinking models (Opus 4.6, Sonnet 4.6) have interleaved thinking built-in.
    // The beta header is deprecated on Opus 4.6 and redundant on Sonnet 4.6, so skip it.
    const needsInterleavedBeta = interleavedThinking && !supportsAdaptiveThinking(model.id);
    // Copilot: Bearer auth, selective betas (no fine-grained-tool-streaming)
    if (model.provider === "github-copilot") {
        const betaFeatures = [];
        if (needsInterleavedBeta) {
            betaFeatures.push("interleaved-thinking-2025-05-14");
        }
        const client = new Anthropic({
            apiKey: null,
            authToken: apiKey,
            baseURL: model.baseUrl,
            dangerouslyAllowBrowser: true,
            defaultHeaders: mergeHeaders({
                accept: "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
                ...(betaFeatures.length > 0 ? { "anthropic-beta": betaFeatures.join(",") } : {}),
            }, model.headers, dynamicHeaders, optionsHeaders),
            // WeaveBench patch: see openai-completions.patched.js for rationale.
            //   timeout: 30 min — covers worst tail under high-concurrency batches.
            //   maxRetries: 3 — Anthropic SDK auto-retries on undici ECONNRESET /
            //     408 / 409 / 429 / 5xx; without this, turn-1 cold-start RST kills
            //     the whole task and openclaw handleAgentEnd locks isError=true.
            timeout: 1800000,
            maxRetries: 3,
        });
        return { client, isOAuthToken: false };
    }
    const betaFeatures = ["fine-grained-tool-streaming-2025-05-14"];
    if (needsInterleavedBeta) {
        betaFeatures.push("interleaved-thinking-2025-05-14");
    }
    // OAuth: Bearer auth, Claude Code identity headers
    if (isOAuthToken(apiKey)) {
        const client = new Anthropic({
            apiKey: null,
            authToken: apiKey,
            baseURL: model.baseUrl,
            dangerouslyAllowBrowser: true,
            defaultHeaders: mergeHeaders({
                accept: "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
                "anthropic-beta": `claude-code-20250219,oauth-2025-04-20,${betaFeatures.join(",")}`,
                "user-agent": `claude-cli/${claudeCodeVersion}`,
                "x-app": "cli",
            }, model.headers, optionsHeaders),
            // WeaveBench patch: see openai-completions.patched.js for rationale.
            timeout: 1800000,
            maxRetries: 3,
        });
        return { client, isOAuthToken: true };
    }
    // API key auth
    const client = new Anthropic({
        apiKey,
        baseURL: model.baseUrl,
        dangerouslyAllowBrowser: true,
        defaultHeaders: mergeHeaders({
            accept: "application/json",
            "anthropic-dangerous-direct-browser-access": "true",
            "anthropic-beta": betaFeatures.join(","),
        }, model.headers, optionsHeaders),
        // WeaveBench patch: see openai-completions.patched.js for rationale.
        timeout: 1800000,
        maxRetries: 3,
    });
    return { client, isOAuthToken: false };
}
function buildParams(model, context, isOAuthToken, options) {
    const { cacheControl } = getCacheControl(model.baseUrl, options?.cacheRetention);
    const params = {
        model: model.id,
        messages: convertMessages(context.messages, model, isOAuthToken, cacheControl),
        max_tokens: options?.maxTokens || (model.maxTokens / 3) | 0,
        stream: true,
    };
    // For OAuth tokens, we MUST include Claude Code identity
    if (isOAuthToken) {
        params.system = [
            {
                type: "text",
                text: "You are Claude Code, Anthropic's official CLI for Claude.",
                ...(cacheControl ? { cache_control: cacheControl } : {}),
            },
        ];
        if (context.systemPrompt) {
            params.system.push({
                type: "text",
                text: sanitizeSurrogates(context.systemPrompt),
                ...(cacheControl ? { cache_control: cacheControl } : {}),
            });
        }
    }
    else if (context.systemPrompt) {
        // Add cache control to system prompt for non-OAuth tokens
        params.system = [
            {
                type: "text",
                text: sanitizeSurrogates(context.systemPrompt),
                ...(cacheControl ? { cache_control: cacheControl } : {}),
            },
        ];
    }
    // Temperature is incompatible with extended thinking (adaptive or budget-based).
    if (options?.temperature !== undefined && !options?.thinkingEnabled) {
        params.temperature = options.temperature;
    }
    if (context.tools) {
        params.tools = convertTools(context.tools, isOAuthToken);
    }
    // Configure thinking mode: adaptive (Opus 4.6 and Sonnet 4.6) or budget-based (older models)
    if (options?.thinkingEnabled && model.reasoning) {
        if (supportsAdaptiveThinking(model.id)) {
            // Adaptive thinking: Claude decides when and how much to think
            params.thinking = { type: "adaptive" };
            if (options.effort) {
                params.output_config = { effort: options.effort };
            }
        }
        else {
            // Budget-based thinking for older models
            params.thinking = {
                type: "enabled",
                budget_tokens: options.thinkingBudgetTokens || 1024,
            };
        }
    }
    if (options?.metadata) {
        const userId = options.metadata.user_id;
        if (typeof userId === "string") {
            params.metadata = { user_id: userId };
        }
    }
    if (options?.toolChoice) {
        if (typeof options.toolChoice === "string") {
            params.tool_choice = { type: options.toolChoice };
        }
        else {
            params.tool_choice = options.toolChoice;
        }
    }
    // ===== WeaveBench PATCH: Anthropic per-model image cap by total bytes =====
    // GitHub Copilot upstream (`api.{enterprise}.githubcopilot.com`) reject
    // requests with body > ~5 MB (returns 413 "Request Entity Too Large" or
    // TCP RST). openclaw's [agents/tool-images] resizer only fires *after*
    // the upstream complains, so multi-turn GUI tasks accumulate 4-7 large
    // screenshots and trip the limit. Trim oldest images here, before the
    // request leaves the SDK. Symmetric to the openai-completions.patched.js
    // PROXY_IMAGE_CAPS logic, but operates on Anthropic-shaped image blocks
    // (`{type:'image', source:{type:'base64', media_type, data}}`).
    try {
        const PROXY_IMAGE_CAPS = {
            // 2026-05-03 实测:
            //   - gemini-3-flash-preview 截图 ~1.4 MB → cap=3 给 ~4.2 MB OK
            //   - claude-opus-4.6 截图 ~2.1 MB → cap=3 给 ~6.3 MB 超 5MB!
            //     必须 cap=2 给 ~4.2 MB
            //   - sonnet/haiku also use cap=2 for safety
            //   - gemini-3.1-pro-preview screenshots are ~1.4 MB → cap=3
            //
            // Note: some upstream gateways have no body-size limit; in that
            // case caps can be raised (or set to 0 to disable cropping —
            // entries that are missing or 0 bypass the table; see
            // `if (cap > 0)` below).
            "claude-opus-4.6": 2,
            "claude-sonnet-4.6": 2,
            "claude-opus-4.7": 4,        // dynamic: cap=4, fallback to 3/2 if body > 4.5MB
            "claude-opus-4-7": 4,        // alternate id alias
            "claude-haiku-4.5": 2,
            "gemini-3.1-pro-preview": 4, // dynamic: cap=4, fallback if body > 4.5MB
            "gemini-3-flash-preview": 3,
        };
        const MAX_BODY_BYTES = 4.5 * 1024 * 1024; // 4.5MB safety margin under 5MB limit
        // WeaveBench 2026-05-25: env-var override. When WEAVEBENCH_FORCE_IMAGE_CAP is set,
        // use that value instead of the per-model default. Allows the SAME
        // model id (e.g. claude-sonnet-4.6) to get cap=2 default but cap=4
        // when launched with a wrapper that sets WEAVEBENCH_FORCE_IMAGE_CAP=4.
        // 0 = bypass (no trimming).
        let cap;
        const forcedCap = process.env.WEAVEBENCH_FORCE_IMAGE_CAP;
        if (forcedCap !== undefined && forcedCap !== "") {
            cap = parseInt(forcedCap, 10) | 0;
        } else {
            cap = (model && PROXY_IMAGE_CAPS[model.id]) | 0;
        }
        if (cap > 0 && Array.isArray(params.messages)) {
            // Collect all image positions
            const imgPositions = [];
            for (let mi = 0; mi < params.messages.length; mi++) {
                const c = params.messages[mi].content;
                if (Array.isArray(c)) {
                    for (let bi = 0; bi < c.length; bi++) {
                        if (c[bi] && c[bi].type === "image") {
                            imgPositions.push([mi, bi]);
                        }
                    }
                }
            }
            const total = imgPositions.length;
            // Phase 1: apply count-based cap
            let effectiveCap = cap;
            if (total > effectiveCap) {
                const numToDrop = total - effectiveCap;
                const dropSet = new Set(
                    imgPositions.slice(0, numToDrop).map(([m, b]) => m + ":" + b)
                );
                for (let mi = 0; mi < params.messages.length; mi++) {
                    const c = params.messages[mi].content;
                    if (!Array.isArray(c)) continue;
                    const filtered = c.filter((_blk, bi) => !dropSet.has(mi + ":" + bi));
                    if (filtered.length === 0) {
                        params.messages[mi].content = [
                            { type: "text", text: "[image dropped to satisfy " + model.id + " image cap]" },
                        ];
                    } else if (filtered.length !== c.length) {
                        params.messages[mi].content = filtered;
                    }
                }
            }
            // Phase 2: byte-level safety — if body still > 4.5MB, drop more images
            // until it fits (dynamic fallback from cap=4 → 3 → 2 → 1)
            let bodySize = 0;
            try { bodySize = JSON.stringify(params).length; } catch(_) {}
            while (bodySize > MAX_BODY_BYTES && effectiveCap > 1) {
                effectiveCap--;
                // Find and drop the oldest remaining image
                for (let mi = 0; mi < params.messages.length; mi++) {
                    const c = params.messages[mi].content;
                    if (!Array.isArray(c)) continue;
                    const imgIdx = c.findIndex((b) => b && b.type === "image");
                    if (imgIdx >= 0) {
                        c.splice(imgIdx, 1, { type: "text", text: "[image dropped — body overflow fallback]" });
                        break;
                    }
                }
                try { bodySize = JSON.stringify(params).length; } catch(_) { break; }
            }
            const finalImgCount = imgPositions.length <= cap ? imgPositions.length : effectiveCap;
            if (total > effectiveCap || effectiveCap < cap) {
                try {
                    console.error(
                        "[weavebench image-cap anthropic] " + model.id + " trimmed " + total +
                        " images → kept " + effectiveCap + " (cap=" + cap +
                        ", body=" + (bodySize/1024/1024).toFixed(1) + "MB)"
                    );
                } catch (_) {}
            }
        }
    } catch (e) {
        try {
            console.error("[weavebench image-cap anthropic] failed: " + (e && e.message));
        } catch (_) {}
    }
    return params;
}
// Normalize tool call IDs to match Anthropic's required pattern and length
function normalizeToolCallId(id) {
    return id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
}
function convertMessages(messages, model, isOAuthToken, cacheControl) {
    const params = [];
    // Transform messages for cross-provider compatibility
    const transformedMessages = transformMessages(messages, model, normalizeToolCallId);
    for (let i = 0; i < transformedMessages.length; i++) {
        const msg = transformedMessages[i];
        if (msg.role === "user") {
            if (typeof msg.content === "string") {
                if (msg.content.trim().length > 0) {
                    params.push({
                        role: "user",
                        content: sanitizeSurrogates(msg.content),
                    });
                }
            }
            else {
                const blocks = msg.content.map((item) => {
                    if (item.type === "text") {
                        return {
                            type: "text",
                            text: sanitizeSurrogates(item.text),
                        };
                    }
                    else {
                        return {
                            type: "image",
                            source: {
                                type: "base64",
                                media_type: item.mimeType,
                                data: item.data,
                            },
                        };
                    }
                });
                let filteredBlocks = !model?.input.includes("image") ? blocks.filter((b) => b.type !== "image") : blocks;
                filteredBlocks = filteredBlocks.filter((b) => {
                    if (b.type === "text") {
                        return b.text.trim().length > 0;
                    }
                    return true;
                });
                if (filteredBlocks.length === 0)
                    continue;
                params.push({
                    role: "user",
                    content: filteredBlocks,
                });
            }
        }
        else if (msg.role === "assistant") {
            const blocks = [];
            for (const block of msg.content) {
                if (block.type === "text") {
                    if (block.text.trim().length === 0)
                        continue;
                    blocks.push({
                        type: "text",
                        text: sanitizeSurrogates(block.text),
                    });
                }
                else if (block.type === "thinking") {
                    // Redacted thinking: pass the opaque payload back as redacted_thinking
                    if (block.redacted) {
                        blocks.push({
                            type: "redacted_thinking",
                            data: block.thinkingSignature,
                        });
                        continue;
                    }
                    if (block.thinking.trim().length === 0)
                        continue;
                    // If thinking signature is missing/empty (e.g., from aborted stream),
                    // convert to plain text block without <thinking> tags to avoid API rejection
                    // and prevent Claude from mimicking the tags in responses
                    if (!block.thinkingSignature || block.thinkingSignature.trim().length === 0) {
                        blocks.push({
                            type: "text",
                            text: sanitizeSurrogates(block.thinking),
                        });
                    }
                    else {
                        blocks.push({
                            type: "thinking",
                            thinking: sanitizeSurrogates(block.thinking),
                            signature: block.thinkingSignature,
                        });
                    }
                }
                else if (block.type === "toolCall") {
                    blocks.push({
                        type: "tool_use",
                        id: block.id,
                        name: isOAuthToken ? toClaudeCodeName(block.name) : block.name,
                        input: block.arguments ?? {},
                    });
                }
            }
            if (blocks.length === 0)
                continue;
            params.push({
                role: "assistant",
                content: blocks,
            });
        }
        else if (msg.role === "toolResult") {
            // Collect all consecutive toolResult messages, needed for z.ai Anthropic endpoint
            const toolResults = [];
            // Add the current tool result
            toolResults.push({
                type: "tool_result",
                tool_use_id: msg.toolCallId,
                content: convertContentBlocks(msg.content),
                is_error: msg.isError,
            });
            // Look ahead for consecutive toolResult messages
            let j = i + 1;
            while (j < transformedMessages.length && transformedMessages[j].role === "toolResult") {
                const nextMsg = transformedMessages[j]; // We know it's a toolResult
                toolResults.push({
                    type: "tool_result",
                    tool_use_id: nextMsg.toolCallId,
                    content: convertContentBlocks(nextMsg.content),
                    is_error: nextMsg.isError,
                });
                j++;
            }
            // Skip the messages we've already processed
            i = j - 1;
            // Add a single user message with all tool results
            params.push({
                role: "user",
                content: toolResults,
            });
        }
    }
    // Add cache_control to the last user message to cache conversation history
    if (cacheControl && params.length > 0) {
        const lastMessage = params[params.length - 1];
        if (lastMessage.role === "user") {
            if (Array.isArray(lastMessage.content)) {
                const lastBlock = lastMessage.content[lastMessage.content.length - 1];
                if (lastBlock &&
                    (lastBlock.type === "text" || lastBlock.type === "image" || lastBlock.type === "tool_result")) {
                    lastBlock.cache_control = cacheControl;
                }
            }
            else if (typeof lastMessage.content === "string") {
                lastMessage.content = [
                    {
                        type: "text",
                        text: lastMessage.content,
                        cache_control: cacheControl,
                    },
                ];
            }
        }
    }
    return params;
}
function convertTools(tools, isOAuthToken) {
    if (!tools)
        return [];
    return tools.map((tool) => {
        const jsonSchema = tool.parameters; // TypeBox already generates JSON Schema
        return {
            name: isOAuthToken ? toClaudeCodeName(tool.name) : tool.name,
            description: tool.description,
            input_schema: {
                type: "object",
                properties: jsonSchema.properties || {},
                required: jsonSchema.required || [],
            },
        };
    });
}
function mapStopReason(reason) {
    switch (reason) {
        case "end_turn":
            return "stop";
        case "max_tokens":
            return "length";
        case "tool_use":
            return "toolUse";
        case "refusal":
            return "error";
        case "pause_turn": // Stop is good enough -> resubmit
            return "stop";
        case "stop_sequence":
            return "stop"; // We don't supply stop sequences, so this should never happen
        case "sensitive": // Content flagged by safety filters (not yet in SDK types)
            return "error";
        default:
            // Handle unknown stop reasons gracefully (API may add new values)
            throw new Error(`Unhandled stop reason: ${reason}`);
    }
}
//# sourceMappingURL=anthropic.js.map