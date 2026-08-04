import OpenAI from "openai";
import { getEnvApiKey } from "../env-api-keys.js";
import { calculateCost, supportsXhigh } from "../models.js";
import { AssistantMessageEventStream } from "../utils/event-stream.js";
import { parseStreamingJson } from "../utils/json-parse.js";
import { sanitizeSurrogates } from "../utils/sanitize-unicode.js";
import { buildCopilotDynamicHeaders, hasCopilotVisionInput } from "./github-copilot-headers.js";
import { buildBaseOptions, clampReasoning } from "./simple-options.js";
import { transformMessages } from "./transform-messages.js";
/**
 * Check if conversation messages contain tool calls or tool results.
 * This is needed because Anthropic (via proxy) requires the tools param
 * to be present when messages include tool_calls or tool role messages.
 */
function hasToolHistory(messages) {
    for (const msg of messages) {
        if (msg.role === "toolResult") {
            return true;
        }
        if (msg.role === "assistant") {
            if (msg.content.some((block) => block.type === "toolCall")) {
                return true;
            }
        }
    }
    return false;
}
export const streamOpenAICompletions = (model, context, options) => {
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
                cacheRead: 0,
                cacheWrite: 0,
                totalTokens: 0,
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
            },
            stopReason: "stop",
            timestamp: Date.now(),
        };
        try {
            const apiKey = options?.apiKey || getEnvApiKey(model.provider) || "";
            const client = createClient(model, context, apiKey, options?.headers);
            let params = buildParams(model, context, options);
            const nextParams = await options?.onPayload?.(params, model);
            if (nextParams !== undefined) {
                params = nextParams;
            }
            const openaiStream = await client.chat.completions.create(params, { signal: options?.signal });
            stream.push({ type: "start", partial: output });
            let currentBlock = null;
            const blocks = output.content;
            const blockIndex = () => blocks.length - 1;
            const finishCurrentBlock = (block) => {
                if (block) {
                    if (block.type === "text") {
                        stream.push({
                            type: "text_end",
                            contentIndex: blockIndex(),
                            content: block.text,
                            partial: output,
                        });
                    }
                    else if (block.type === "thinking") {
                        stream.push({
                            type: "thinking_end",
                            contentIndex: blockIndex(),
                            content: block.thinking,
                            partial: output,
                        });
                    }
                    else if (block.type === "toolCall") {
                        block.arguments = parseStreamingJson(block.partialArgs);
                        delete block.partialArgs;
                        stream.push({
                            type: "toolcall_end",
                            contentIndex: blockIndex(),
                            toolCall: block,
                            partial: output,
                        });
                    }
                }
            };
            for await (const chunk of openaiStream) {
                if (!chunk || typeof chunk !== "object")
                    continue;
                // OpenAI documents ChatCompletionChunk.id as the unique chat completion identifier,
                // and each chunk in a streamed completion carries the same id.
                output.responseId ||= chunk.id;
                if (chunk.usage) {
                    output.usage = parseChunkUsage(chunk.usage, model);
                }
                const choice = Array.isArray(chunk.choices) ? chunk.choices[0] : undefined;
                if (!choice)
                    continue;
                // Fallback: some providers (e.g., Moonshot) return usage
                // in choice.usage instead of the standard chunk.usage
                if (!chunk.usage && choice.usage) {
                    output.usage = parseChunkUsage(choice.usage, model);
                }
                if (choice.finish_reason) {
                    const finishReasonResult = mapStopReason(choice.finish_reason);
                    output.stopReason = finishReasonResult.stopReason;
                    if (finishReasonResult.errorMessage) {
                        output.errorMessage = finishReasonResult.errorMessage;
                    }
                }
                if (choice.delta) {
                    if (choice.delta.content !== null &&
                        choice.delta.content !== undefined &&
                        choice.delta.content.length > 0) {
                        if (!currentBlock || currentBlock.type !== "text") {
                            finishCurrentBlock(currentBlock);
                            currentBlock = { type: "text", text: "" };
                            output.content.push(currentBlock);
                            stream.push({ type: "text_start", contentIndex: blockIndex(), partial: output });
                        }
                        if (currentBlock.type === "text") {
                            currentBlock.text += choice.delta.content;
                            stream.push({
                                type: "text_delta",
                                contentIndex: blockIndex(),
                                delta: choice.delta.content,
                                partial: output,
                            });
                        }
                    }
                    // Some endpoints return reasoning in reasoning_content (llama.cpp),
                    // or reasoning (other openai compatible endpoints)
                    // Use the first non-empty reasoning field to avoid duplication
                    // (e.g., chutes.ai returns both reasoning_content and reasoning with same content)
                    const reasoningFields = ["reasoning_content", "reasoning", "reasoning_text"];
                    let foundReasoningField = null;
                    for (const field of reasoningFields) {
                        if (choice.delta[field] !== null &&
                            choice.delta[field] !== undefined &&
                            choice.delta[field].length > 0) {
                            if (!foundReasoningField) {
                                foundReasoningField = field;
                                break;
                            }
                        }
                    }
                    if (foundReasoningField) {
                        if (!currentBlock || currentBlock.type !== "thinking") {
                            finishCurrentBlock(currentBlock);
                            currentBlock = {
                                type: "thinking",
                                thinking: "",
                                thinkingSignature: foundReasoningField,
                            };
                            output.content.push(currentBlock);
                            stream.push({ type: "thinking_start", contentIndex: blockIndex(), partial: output });
                        }
                        if (currentBlock.type === "thinking") {
                            const delta = choice.delta[foundReasoningField];
                            currentBlock.thinking += delta;
                            stream.push({
                                type: "thinking_delta",
                                contentIndex: blockIndex(),
                                delta,
                                partial: output,
                            });
                        }
                    }
                    if (choice?.delta?.tool_calls) {
                        for (const toolCall of choice.delta.tool_calls) {
                            if (!currentBlock ||
                                currentBlock.type !== "toolCall" ||
                                (toolCall.id && currentBlock.id !== toolCall.id)) {
                                finishCurrentBlock(currentBlock);
                                currentBlock = {
                                    type: "toolCall",
                                    id: toolCall.id || "",
                                    name: toolCall.function?.name || "",
                                    arguments: {},
                                    partialArgs: "",
                                };
                                output.content.push(currentBlock);
                                stream.push({ type: "toolcall_start", contentIndex: blockIndex(), partial: output });
                            }
                            if (currentBlock.type === "toolCall") {
                                if (toolCall.id)
                                    currentBlock.id = toolCall.id;
                                if (toolCall.function?.name)
                                    currentBlock.name = toolCall.function.name;
                                let delta = "";
                                if (toolCall.function?.arguments) {
                                    delta = toolCall.function.arguments;
                                    currentBlock.partialArgs += toolCall.function.arguments;
                                    currentBlock.arguments = parseStreamingJson(currentBlock.partialArgs);
                                }
                                stream.push({
                                    type: "toolcall_delta",
                                    contentIndex: blockIndex(),
                                    delta,
                                    partial: output,
                                });
                            }
                        }
                    }
                    const reasoningDetails = choice.delta.reasoning_details;
                    if (reasoningDetails && Array.isArray(reasoningDetails)) {
                        for (const detail of reasoningDetails) {
                            if (detail.type === "reasoning.encrypted" && detail.id && detail.data) {
                                const matchingToolCall = output.content.find((b) => b.type === "toolCall" && b.id === detail.id);
                                if (matchingToolCall) {
                                    matchingToolCall.thoughtSignature = JSON.stringify(detail);
                                }
                            }
                        }
                    }
                }
            }
            finishCurrentBlock(currentBlock);
            if (options?.signal?.aborted) {
                throw new Error("Request was aborted");
            }
            if (output.stopReason === "aborted") {
                throw new Error("Request was aborted");
            }
            if (output.stopReason === "error") {
                throw new Error(output.errorMessage || "Provider returned an error stop reason");
            }
            stream.push({ type: "done", reason: output.stopReason, message: output });
            stream.end();
        }
        catch (error) {
            for (const block of output.content)
                delete block.index;
            output.stopReason = options?.signal?.aborted ? "aborted" : "error";
            output.errorMessage = error instanceof Error ? error.message : JSON.stringify(error);
            // Some providers via OpenRouter give additional information in this field.
            const rawMetadata = error?.error?.metadata?.raw;
            if (rawMetadata)
                output.errorMessage += `\n${rawMetadata}`;
            stream.push({ type: "error", reason: output.stopReason, error: output });
            stream.end();
        }
    })();
    return stream;
};
export const streamSimpleOpenAICompletions = (model, context, options) => {
    const apiKey = options?.apiKey || getEnvApiKey(model.provider);
    if (!apiKey) {
        throw new Error(`No API key for provider: ${model.provider}`);
    }
    const base = buildBaseOptions(model, options, apiKey);
    const reasoningEffort = supportsXhigh(model) ? options?.reasoning : clampReasoning(options?.reasoning);
    const toolChoice = options?.toolChoice;
    return streamOpenAICompletions(model, context, {
        ...base,
        reasoningEffort,
        toolChoice,
    });
};
function createClient(model, context, apiKey, optionsHeaders) {
    if (!apiKey) {
        if (!process.env.OPENAI_API_KEY) {
            throw new Error("OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it as an argument.");
        }
        apiKey = process.env.OPENAI_API_KEY;
    }
    const headers = { ...model.headers };
    if (model.provider === "github-copilot") {
        const hasImages = hasCopilotVisionInput(context.messages);
        const copilotHeaders = buildCopilotDynamicHeaders({
            messages: context.messages,
            hasImages,
        });
        Object.assign(headers, copilotHeaders);
    }
    // Merge options headers last so they can override defaults
    if (optionsHeaders) {
        Object.assign(headers, optionsHeaders);
    }
    return new OpenAI({
        apiKey,
        baseURL: model.baseUrl,
        dangerouslyAllowBrowser: true,
        defaultHeaders: headers,
        // WeaveBench patch: bump per-request timeout from SDK default (10min).
        // The Copilot reverse proxy serializes requests under concurrency
        // (probe: 8 concurrent → p95 44 s, 20 concurrent → 60-120 s+ for
        // sonnet/opus with screenshots), and the openclaw client surfaces
        // SDK timeouts as "LLM request timed out" which silently kills the
        // agent. 30 min covers the worst tail and we already have a
        // higher-level --timeout on `openclaw agent`.
        timeout: 1800000,
        // WeaveBench patch: enable SDK auto-retry for transient errors (default 2).
        // Critical for batch runs through some upstream gateway chains
        // → Vertex AI: undici sees ECONNRESET on ~7-15% of requests (especially
        // turn 1 cold-start) when any link in the chain transiently RSTs.
        // Without retry, openclaw 'Connection error.' propagates to gateway,
        // and handleAgentEnd locks isError=true even after later turns
        // recover, causing 'No reply from agent' early-exit.
        // SDK auto-retries on:
        //   - undici fetch failed (ECONNRESET, socket hang up, etc) — see
        //     openai/client.js:309-317 (no shouldRetry check needed)
        //   - HTTP 408 / 409 / 429 / 5xx
        // Retry uses exponential backoff (~0.5s, 1s, 2s) so worst-case adds
        // ~3.5s per failed call.
        maxRetries: 3,
    });
}
function buildParams(model, context, options) {
    const compat = getCompat(model);
    const messages = convertMessages(model, context, compat);
    maybeAddOpenRouterAnthropicCacheControl(model, messages);
    const params = {
        model: model.id,
        messages,
        stream: true,
    };
    if (compat.supportsUsageInStreaming !== false) {
        params.stream_options = { include_usage: true };
    }
    if (compat.supportsStore) {
        params.store = false;
    }
    if (options?.maxTokens) {
        if (compat.maxTokensField === "max_tokens") {
            params.max_tokens = options.maxTokens;
        }
        else {
            params.max_completion_tokens = options.maxTokens;
        }
    }
    if (options?.temperature !== undefined) {
        params.temperature = options.temperature;
    }
    if (context.tools) {
        params.tools = convertTools(context.tools, compat);
    }
    else if (hasToolHistory(context.messages)) {
        // Anthropic (via LiteLLM/proxy) requires tools param when conversation has tool_calls/tool_results
        params.tools = [];
    }
    if (options?.toolChoice) {
        params.tool_choice = options.toolChoice;
    }
    if (compat.thinkingFormat === "zai" && model.reasoning) {
        params.enable_thinking = !!options?.reasoningEffort;
    }
    else if (compat.thinkingFormat === "qwen" && model.reasoning) {
        params.enable_thinking = !!options?.reasoningEffort;
    }
    else if (compat.thinkingFormat === "qwen-chat-template" && model.reasoning) {
        params.chat_template_kwargs = { enable_thinking: !!options?.reasoningEffort };
    }
    else if (compat.thinkingFormat === "openrouter" && model.reasoning) {
        // OpenRouter normalizes reasoning across providers via a nested reasoning object.
        const openRouterParams = params;
        if (options?.reasoningEffort) {
            openRouterParams.reasoning = {
                effort: mapReasoningEffort(options.reasoningEffort, compat.reasoningEffortMap),
            };
        }
        else {
            openRouterParams.reasoning = { effort: "none" };
        }
    }
    else if (options?.reasoningEffort && model.reasoning && compat.supportsReasoningEffort) {
        // OpenAI-style reasoning_effort
        params.reasoning_effort = mapReasoningEffort(options.reasoningEffort, compat.reasoningEffortMap);
    }
    // OpenRouter provider routing preferences
    if (model.baseUrl.includes("openrouter.ai") && model.compat?.openRouterRouting) {
        params.provider = model.compat.openRouterRouting;
    }
    // Vercel AI Gateway provider routing preferences
    if (model.baseUrl.includes("ai-gateway.vercel.sh") && model.compat?.vercelGatewayRouting) {
        const routing = model.compat.vercelGatewayRouting;
        if (routing.only || routing.order) {
            const gatewayOptions = {};
            if (routing.only)
                gatewayOptions.only = routing.only;
            if (routing.order)
                gatewayOptions.order = routing.order;
            params.providerOptions = { gateway: gatewayOptions };
        }
    }
    return params;
}
function mapReasoningEffort(effort, reasoningEffortMap) {
    return reasoningEffortMap[effort] ?? effort;
}
function maybeAddOpenRouterAnthropicCacheControl(model, messages) {
    if (model.provider !== "openrouter" || !model.id.startsWith("anthropic/"))
        return;
    // Anthropic-style caching requires cache_control on a text part. Add a breakpoint
    // on the last user/assistant message (walking backwards until we find text content).
    for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        if (msg.role !== "user" && msg.role !== "assistant")
            continue;
        const content = msg.content;
        if (typeof content === "string") {
            msg.content = [
                Object.assign({ type: "text", text: content }, { cache_control: { type: "ephemeral" } }),
            ];
            return;
        }
        if (!Array.isArray(content))
            continue;
        // Find last text part and add cache_control
        for (let j = content.length - 1; j >= 0; j--) {
            const part = content[j];
            if (part?.type === "text") {
                Object.assign(part, { cache_control: { type: "ephemeral" } });
                return;
            }
        }
    }
}
export function convertMessages(model, context, compat) {
    const params = [];
    const normalizeToolCallId = (id) => {
        // Handle pipe-separated IDs from OpenAI Responses API
        // Format: {call_id}|{id} where {id} can be 400+ chars with special chars (+, /, =)
        // These come from providers like github-copilot, openai-codex, opencode
        // Extract just the call_id part and normalize it
        if (id.includes("|")) {
            const [callId] = id.split("|");
            // Sanitize to allowed chars and truncate to 40 chars (OpenAI limit)
            return callId.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 40);
        }
        if (model.provider === "openai")
            return id.length > 40 ? id.slice(0, 40) : id;
        return id;
    };
    const transformedMessages = transformMessages(context.messages, model, (id) => normalizeToolCallId(id));
    if (context.systemPrompt) {
        const useDeveloperRole = model.reasoning && compat.supportsDeveloperRole;
        const role = useDeveloperRole ? "developer" : "system";
        params.push({ role: role, content: sanitizeSurrogates(context.systemPrompt) });
    }
    let lastRole = null;
    for (let i = 0; i < transformedMessages.length; i++) {
        const msg = transformedMessages[i];
        // Some providers don't allow user messages directly after tool results
        // Insert a synthetic assistant message to bridge the gap
        if (compat.requiresAssistantAfterToolResult && lastRole === "toolResult" && msg.role === "user") {
            params.push({
                role: "assistant",
                content: "I have processed the tool results.",
            });
        }
        if (msg.role === "user") {
            if (typeof msg.content === "string") {
                params.push({
                    role: "user",
                    content: sanitizeSurrogates(msg.content),
                });
            }
            else {
                const content = msg.content.map((item) => {
                    if (item.type === "text") {
                        return {
                            type: "text",
                            text: sanitizeSurrogates(item.text),
                        };
                    }
                    else {
                        return {
                            type: "image_url",
                            image_url: {
                                url: `data:${item.mimeType};base64,${item.data}`,
                            },
                        };
                    }
                });
                const filteredContent = !model.input.includes("image")
                    ? content.filter((c) => c.type !== "image_url")
                    : content;
                if (filteredContent.length === 0)
                    continue;
                params.push({
                    role: "user",
                    content: filteredContent,
                });
            }
        }
        else if (msg.role === "assistant") {
            // Some providers don't accept null content, use empty string instead
            const assistantMsg = {
                role: "assistant",
                content: compat.requiresAssistantAfterToolResult ? "" : null,
            };
            const textBlocks = msg.content.filter((b) => b.type === "text");
            // Filter out empty text blocks to avoid API validation errors
            const nonEmptyTextBlocks = textBlocks.filter((b) => b.text && b.text.trim().length > 0);
            if (nonEmptyTextBlocks.length > 0) {
                // Always send assistant content as a plain string (OpenAI Chat Completions
                // API standard format). Sending as an array of {type:"text", text:"..."}
                // objects is non-standard and causes some models (e.g. DeepSeek V3.2 via
                // NVIDIA NIM) to mirror the content-block structure literally in their
                // output, producing recursive nesting like [{'type':'text','text':'[{...}]'}].
                assistantMsg.content = nonEmptyTextBlocks.map((b) => sanitizeSurrogates(b.text)).join("");
            }
            // Handle thinking blocks
            const thinkingBlocks = msg.content.filter((b) => b.type === "thinking");
            // Filter out empty thinking blocks to avoid API validation errors
            const nonEmptyThinkingBlocks = thinkingBlocks.filter((b) => b.thinking && b.thinking.trim().length > 0);
            if (nonEmptyThinkingBlocks.length > 0) {
                if (compat.requiresThinkingAsText) {
                    // Convert thinking blocks to plain text (no tags to avoid model mimicking them)
                    const thinkingText = nonEmptyThinkingBlocks.map((b) => b.thinking).join("\n\n");
                    const textContent = assistantMsg.content;
                    if (textContent) {
                        textContent.unshift({ type: "text", text: thinkingText });
                    }
                    else {
                        assistantMsg.content = [{ type: "text", text: thinkingText }];
                    }
                }
                else {
                    // Use the signature from the first thinking block if available (for llama.cpp server + gpt-oss)
                    const signature = nonEmptyThinkingBlocks[0].thinkingSignature;
                    if (signature && signature.length > 0) {
                        assistantMsg[signature] = nonEmptyThinkingBlocks.map((b) => b.thinking).join("\n");
                    }
                }
            }
            const toolCalls = msg.content.filter((b) => b.type === "toolCall");
            if (toolCalls.length > 0) {
                assistantMsg.tool_calls = toolCalls.map((tc) => ({
                    id: tc.id,
                    type: "function",
                    function: {
                        name: tc.name,
                        arguments: JSON.stringify(tc.arguments),
                    },
                }));
                const reasoningDetails = toolCalls
                    .filter((tc) => tc.thoughtSignature)
                    .map((tc) => {
                    try {
                        return JSON.parse(tc.thoughtSignature);
                    }
                    catch {
                        return null;
                    }
                })
                    .filter(Boolean);
                if (reasoningDetails.length > 0) {
                    assistantMsg.reasoning_details = reasoningDetails;
                }
            }
            // Skip assistant messages that have no content and no tool calls.
            // Some providers require "either content or tool_calls, but not none".
            // Other providers also don't accept empty assistant messages.
            // This handles aborted assistant responses that got no content.
            const content = assistantMsg.content;
            const hasContent = content !== null &&
                content !== undefined &&
                (typeof content === "string" ? content.length > 0 : content.length > 0);
            if (!hasContent && !assistantMsg.tool_calls) {
                continue;
            }
            params.push(assistantMsg);
        }
        else if (msg.role === "toolResult") {
            const imageBlocks = [];
            let j = i;
            for (; j < transformedMessages.length && transformedMessages[j].role === "toolResult"; j++) {
                const toolMsg = transformedMessages[j];
                // Extract text and image content
                const textResult = toolMsg.content
                    .filter((c) => c.type === "text")
                    .map((c) => c.text)
                    .join("\n");
                const hasImages = toolMsg.content.some((c) => c.type === "image");
                // Always send tool result with text (or placeholder if only images)
                const hasText = textResult.length > 0;
                // Some providers require the 'name' field in tool results
                const toolResultMsg = {
                    role: "tool",
                    content: sanitizeSurrogates(hasText ? textResult : "(see attached image)"),
                    tool_call_id: toolMsg.toolCallId,
                };
                if (compat.requiresToolResultName && toolMsg.toolName) {
                    toolResultMsg.name = toolMsg.toolName;
                }
                params.push(toolResultMsg);
                if (hasImages && model.input.includes("image")) {
                    for (const block of toolMsg.content) {
                        if (block.type === "image") {
                            imageBlocks.push({
                                type: "image_url",
                                image_url: {
                                    url: `data:${block.mimeType};base64,${block.data}`,
                                },
                            });
                        }
                    }
                }
            }
            i = j - 1;
            if (imageBlocks.length > 0) {
                if (compat.requiresAssistantAfterToolResult) {
                    params.push({
                        role: "assistant",
                        content: "I have processed the tool results.",
                    });
                }
                params.push({
                    role: "user",
                    content: [
                        {
                            type: "text",
                            text: "Attached image(s) from tool result:",
                        },
                        ...imageBlocks,
                    ],
                });
                lastRole = "user";
            }
            else {
                lastRole = "toolResult";
            }
            continue;
        }
        lastRole = msg.role;
    }
    // ===== WeaveBench PATCH: per-model image cap =====
    // Some models on the local Copilot reverse proxy enforce a hard upper bound
    // on the number of inline images per request, e.g.:
    //   gemini-3-flash-preview  -> max 10
    // openclaw never trims its trajectory, so on multi-screenshot tasks we hit
    // 400 "too many images" after ~10 turns. Trim oldest image_url blocks here.
    // Replace each dropped image with a tiny text placeholder so the surrounding
    // user/tool message stays well-formed.
    try {
        const PROXY_IMAGE_CAPS = {
            // WeaveBench 2026-05-23: dynamic cap — try cap=4, fallback to 3/2 if body > 4.5MB.
            // WeaveBench 2026-05-24: bumped flash from 2 → 4 (same as pro). Gateway logs
            // showed flash bodies max 3.8MB at cap=2 → tons of headroom; dynamic
            // fallback handles the rare 4.5MB overflow. cap=2 was hurting flash
            // (HIGH-thinking run only saw last 2 screenshots out of 30+ history).
            "gemini-3-flash-preview": 4,    // dynamic: cap=4, fallback to 3/2 if body > 4.5MB
            "gemini-3.1-pro-preview": 4,    // dynamic: cap=4, fallback if body > 4.5MB
            "gpt-5.2": 3,
            // WeaveBench 2026-05-24: some public endpoints have no 5 MB body limit
            // (no GitHub Copilot reverse-proxy in the path). qwen3.5-397b-a17b
            // is a 397B-active-17B MoE vision model going through some gateways'
            // openai-completions compatibility layer; we want to send the
            // full screenshot history with no client-side trimming so the
            // model has full visual context. cap=0 → bypass (see if-guard
            // `if (cap > 0)` below).
            "qwen3.5-397b-a17b": 0,
        };
        const MAX_BODY_BYTES = 4.5 * 1024 * 1024;
        // WeaveBench 2026-05-25: env-var override. When WEAVEBENCH_FORCE_IMAGE_CAP is set
        // (e.g. via gateways without a 5MB body limit), use that
        // value instead of the per-model default. 0 = bypass (no trimming).
        // This lets the SAME model id (e.g. gemini-3-flash-preview) get
        // cap differs across upstream gateways without table edits.
        let cap;
        const forcedCap = process.env.WEAVEBENCH_FORCE_IMAGE_CAP;
        if (forcedCap !== undefined && forcedCap !== "") {
            cap = parseInt(forcedCap, 10) | 0;
        } else {
            cap = (model && PROXY_IMAGE_CAPS[model.id]) | 0;
        }
        if (cap > 0) {
            const imgPositions = [];
            for (let mi = 0; mi < params.length; mi++) {
                const c = params[mi].content;
                if (Array.isArray(c)) {
                    for (let bi = 0; bi < c.length; bi++) {
                        // Match both OpenAI format (image_url) and openclaw native (image)
                        if (c[bi] && (c[bi].type === "image_url" || c[bi].type === "image")) {
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
                    imgPositions.slice(0, numToDrop).map(([m, b]) => `${m}:${b}`)
                );
                for (let mi = 0; mi < params.length; mi++) {
                    const c = params[mi].content;
                    if (!Array.isArray(c)) continue;
                    const filtered = c.filter((_blk, bi) => !dropSet.has(`${mi}:${bi}`));
                    if (filtered.length === 0) {
                        params[mi].content = [
                            { type: "text", text: "[image dropped to satisfy " + model.id + " image cap]" },
                        ];
                    } else if (filtered.length !== c.length) {
                        params[mi].content = filtered;
                    }
                }
            }
            // Phase 2: byte-level safety — if body still > 4.5MB, drop more images
            let bodySize = 0;
            try { bodySize = JSON.stringify(params).length; } catch(_) {}
            while (bodySize > MAX_BODY_BYTES && effectiveCap > 1) {
                effectiveCap--;
                let dropped = false;
                for (let mi = 0; mi < params.length && !dropped; mi++) {
                    const c = params[mi].content;
                    if (!Array.isArray(c)) continue;
                    const imgIdx = c.findIndex((b) => b && (b.type === "image_url" || b.type === "image"));
                    if (imgIdx >= 0) {
                        c.splice(imgIdx, 1, { type: "text", text: "[image dropped — body overflow fallback]" });
                        dropped = true;
                    }
                }
                if (!dropped) break;
                try { bodySize = JSON.stringify(params).length; } catch(_) { break; }
            }
            if (total > effectiveCap || effectiveCap < cap) {
                try {
                    console.error(
                        "[weavebench image-cap chat] " + model.id + " trimmed " + total +
                        " images → kept " + effectiveCap + " (cap=" + cap +
                        ", body=" + (bodySize/1024/1024).toFixed(1) + "MB)"
                    );
                } catch (_) {}
            }
        }
    } catch (e) {
        try {
            console.error("[weavebench image-cap] failed: " + (e && e.message));
        } catch (_) {}
    }
    return params;
}
function convertTools(tools, compat) {
    return tools.map((tool) => ({
        type: "function",
        function: {
            name: tool.name,
            description: tool.description,
            parameters: tool.parameters, // TypeBox already generates JSON Schema
            // Only include strict if provider supports it. Some reject unknown fields.
            ...(compat.supportsStrictMode !== false && { strict: false }),
        },
    }));
}
function parseChunkUsage(rawUsage, model) {
    const cachedTokens = rawUsage.prompt_tokens_details?.cached_tokens || 0;
    // read reasoning_tokens from BOTH possible locations:
    //   - Standard OpenAI Chat Completions: usage.completion_tokens_details.reasoning_tokens
    //   - some gateways: usage.reasoning_tokens at top level
    // Empirically verified against gateways that put the field
    // at top level — without this OR fallback the reasoning count would be lost for
    // every Gemini call routed through such gateways.
    const reasoningTokens =
        (rawUsage.completion_tokens_details?.reasoning_tokens) ||
        rawUsage.reasoning_tokens || 0;
    // OpenAI includes cached tokens in prompt_tokens, so subtract to get non-cached input
    const input = (rawUsage.prompt_tokens || 0) - cachedTokens;
    const completion = rawUsage.completion_tokens || 0;
    // store reasoning as a sibling field (matches openai-responses-shared.patched.js
    // schema) instead of folding it into output. Three backends now agree:
    //   { input, output (= completion only), reasoning, cacheRead, cacheWrite, totalTokens, cost }
    // totalTokens accounts for both completion AND reasoning since some providers
    // (e.g. Groq) don't include reasoning in total_tokens.
    const usage = {
        input,
        output: completion,
        reasoning: reasoningTokens,
        cacheRead: cachedTokens,
        cacheWrite: 0,
        totalTokens: input + completion + reasoningTokens + cachedTokens,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    };
    calculateCost(model, usage);
    return usage;
}
function mapStopReason(reason) {
    if (reason === null)
        return { stopReason: "stop" };
    switch (reason) {
        case "stop":
        case "end":
            return { stopReason: "stop" };
        case "length":
            return { stopReason: "length" };
        case "function_call":
        case "tool_calls":
            return { stopReason: "toolUse" };
        case "content_filter":
            return { stopReason: "error", errorMessage: "Provider finish_reason: content_filter" };
        case "network_error":
            return { stopReason: "error", errorMessage: "Provider finish_reason: network_error" };
        default:
            return {
                stopReason: "error",
                errorMessage: `Provider finish_reason: ${reason}`,
            };
    }
}
/**
 * Detect compatibility settings from provider and baseUrl for known providers.
 * Provider takes precedence over URL-based detection since it's explicitly configured.
 * Returns a fully resolved OpenAICompletionsCompat object with all fields set.
 */
function detectCompat(model) {
    const provider = model.provider;
    const baseUrl = model.baseUrl;
    const isZai = provider === "zai" || baseUrl.includes("api.z.ai");
    const isNonStandard = provider === "cerebras" ||
        baseUrl.includes("cerebras.ai") ||
        provider === "xai" ||
        baseUrl.includes("api.x.ai") ||
        baseUrl.includes("chutes.ai") ||
        baseUrl.includes("deepseek.com") ||
        isZai ||
        provider === "opencode" ||
        baseUrl.includes("opencode.ai");
    const useMaxTokens = baseUrl.includes("chutes.ai");
    const isGrok = provider === "xai" || baseUrl.includes("api.x.ai");
    const isGroq = provider === "groq" || baseUrl.includes("groq.com");
    const reasoningEffortMap = isGroq && model.id === "qwen/qwen3-32b"
        ? {
            minimal: "default",
            low: "default",
            medium: "default",
            high: "default",
            xhigh: "default",
        }
        : {};
    return {
        supportsStore: !isNonStandard,
        supportsDeveloperRole: !isNonStandard,
        supportsReasoningEffort: !isGrok && !isZai,
        reasoningEffortMap,
        supportsUsageInStreaming: true,
        maxTokensField: useMaxTokens ? "max_tokens" : "max_completion_tokens",
        requiresToolResultName: false,
        requiresAssistantAfterToolResult: false,
        requiresThinkingAsText: false,
        thinkingFormat: isZai
            ? "zai"
            : provider === "openrouter" || baseUrl.includes("openrouter.ai")
                ? "openrouter"
                : "openai",
        openRouterRouting: {},
        vercelGatewayRouting: {},
        supportsStrictMode: true,
    };
}
/**
 * Get resolved compatibility settings for a model.
 * Uses explicit model.compat if provided, otherwise auto-detects from provider/URL.
 */
function getCompat(model) {
    const detected = detectCompat(model);
    if (!model.compat)
        return detected;
    return {
        supportsStore: model.compat.supportsStore ?? detected.supportsStore,
        supportsDeveloperRole: model.compat.supportsDeveloperRole ?? detected.supportsDeveloperRole,
        supportsReasoningEffort: model.compat.supportsReasoningEffort ?? detected.supportsReasoningEffort,
        reasoningEffortMap: model.compat.reasoningEffortMap ?? detected.reasoningEffortMap,
        supportsUsageInStreaming: model.compat.supportsUsageInStreaming ?? detected.supportsUsageInStreaming,
        maxTokensField: model.compat.maxTokensField ?? detected.maxTokensField,
        requiresToolResultName: model.compat.requiresToolResultName ?? detected.requiresToolResultName,
        requiresAssistantAfterToolResult: model.compat.requiresAssistantAfterToolResult ?? detected.requiresAssistantAfterToolResult,
        requiresThinkingAsText: model.compat.requiresThinkingAsText ?? detected.requiresThinkingAsText,
        thinkingFormat: model.compat.thinkingFormat ?? detected.thinkingFormat,
        openRouterRouting: model.compat.openRouterRouting ?? {},
        vercelGatewayRouting: model.compat.vercelGatewayRouting ?? detected.vercelGatewayRouting,
        supportsStrictMode: model.compat.supportsStrictMode ?? detected.supportsStrictMode,
    };
}
//# sourceMappingURL=openai-completions.js.map