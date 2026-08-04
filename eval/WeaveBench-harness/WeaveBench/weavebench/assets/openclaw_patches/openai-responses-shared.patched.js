// Patched copy of @mariozechner/pi-ai openai-responses-shared.js
//
// HISTORY (2026-05-07): native OpenAI Computer-Use Tool support has been
// PERMANENTLY REMOVED. GUI is now exposed strictly as function tools:
//   * gpt-5.4 / gpt-5.5  → split surface: `screenshot` + `do_actions`
//   * any other model    → legacy single fat `__computer__` function tool
// The full pre-removal history (with WEAVEBENCH_CUA_NATIVE / WEAVEBENCH_CUA_INCREMENTAL
// / computer_call replay / cuaModeActive image strip / etc.) is preserved
// verbatim in `openai-responses-shared.legacy_native_cua.js` for archival
// and rollback reference. That file is NOT loaded at runtime.
//
// Diff from upstream (3 surgical sites, all WeaveBench-owned):
//   1. convertResponsesTools: split GUI by (model in {gpt-5.4, gpt-5.5})
//        - gpt-5.4/5.5  → hide __computer__, expose screenshot + do_actions
//        - other models → expose __computer__ as standard function tool
//   2. convertResponsesMessages: function_call / function_call_output paths
//      handle every tool uniformly; multi-image user messages are still
//      split (one input_image per message) for proxy/cap robustness.
//   3. processResponsesStream: no GUI-specific changes; standard
//      function_call streaming path handles screenshot / do_actions /
//      __computer__ identically.
//
// Everything else copied verbatim from upstream so subsequent pi-ai upgrades
// can be re-applied by re-diffing against openai-responses-shared.original.js.

import { calculateCost } from "../models.js";
import { shortHash } from "../utils/hash.js";
import { parseStreamingJson } from "../utils/json-parse.js";
import { sanitizeSurrogates } from "../utils/sanitize-unicode.js";
import { transformMessages } from "./transform-messages.js";

// ---- WeaveBench CUA helpers --------------------------------------------------------
// ESM-safe debug logger. require() doesn't exist in ESM scope.
import * as wbFs from "fs";
function wcbDebugLog(label, obj) {
    try {
        wbFs.appendFileSync("/tmp/openclaw/weavebench_cua_debug.log",
            `${new Date().toISOString()} ${label} ${JSON.stringify(obj)}\n`);
    } catch (e) {}
}
const COMPUTER_TOOL_NAME = "__computer__";

function isComputerTool(tool) {
    return tool && tool.name === COMPUTER_TOOL_NAME;
}

// WeaveBench 2026-05-07 (final): the auxiliary `screenshot` peer was removed too.
// __computer__ ALWAYS auto-screenshots after every batch (including empty
// `actions:[]` batches, which is the new "pure observation" path), so a
// separate observe-only tool was redundant. The split-mode routing helpers
// have been removed entirely; convertResponsesTools below treats every
// other openclaw built-in uniformly as a function tool.

// ---- end WeaveBench CUA helpers ----------------------------------------------------

// =============================================================================
// Utilities
// =============================================================================
function encodeTextSignatureV1(id, phase) {
    const payload = { v: 1, id };
    if (phase)
        payload.phase = phase;
    return JSON.stringify(payload);
}
function parseTextSignature(signature) {
    if (!signature)
        return undefined;
    if (signature.startsWith("{")) {
        try {
            const parsed = JSON.parse(signature);
            if (parsed.v === 1 && typeof parsed.id === "string") {
                if (parsed.phase === "commentary" || parsed.phase === "final_answer") {
                    return { id: parsed.id, phase: parsed.phase };
                }
                return { id: parsed.id };
            }
        }
        catch {
            // Fall through to legacy plain-string handling.
        }
    }
    return { id: signature };
}
// =============================================================================
// Message conversion
// =============================================================================
export function convertResponsesMessages(model, context, allowedToolCallProviders, options) {
    const messages = [];
    const normalizeToolCallId = (id) => {
        if (!allowedToolCallProviders.has(model.provider))
            return id;
        if (!id.includes("|"))
            return id;
        const [callId, itemId] = id.split("|");
        const sanitizedCallId = callId.replace(/[^a-zA-Z0-9_-]/g, "_");
        let sanitizedItemId = itemId.replace(/[^a-zA-Z0-9_-]/g, "_");
        // OpenAI Responses API requires item id to start with "fc"
        if (!sanitizedItemId.startsWith("fc")) {
            sanitizedItemId = `fc_${sanitizedItemId}`;
        }
        // Truncate to 64 chars and strip trailing underscores (OpenAI Codex rejects them)
        let normalizedCallId = sanitizedCallId.length > 64 ? sanitizedCallId.slice(0, 64) : sanitizedCallId;
        let normalizedItemId = sanitizedItemId.length > 64 ? sanitizedItemId.slice(0, 64) : sanitizedItemId;
        normalizedCallId = normalizedCallId.replace(/_+$/, "");
        normalizedItemId = normalizedItemId.replace(/_+$/, "");
        return `${normalizedCallId}|${normalizedItemId}`;
    };
    const transformedMessages = transformMessages(context.messages, model, normalizeToolCallId);
    // WeaveBench 2026-05-07: native CUA path removed. We always send the full
    // transformed message slice; previous_response_id incremental loops and
    // computer_call/computer_call_output replay are gone (see
    // openai-responses-shared.legacy_native_cua.js for the historical
    // implementation if you need to reference it).
    const sessionId = options?.sessionId;
    let slice = transformedMessages;
    let includeSystemPrompt = options?.includeSystemPrompt ?? true;
    if (includeSystemPrompt && context.systemPrompt) {
        const role = model.reasoning ? "developer" : "system";
        messages.push({
            role,
            content: sanitizeSurrogates(context.systemPrompt),
        });
    }
    let msgIndex = 0;
    for (const msg of slice) {
        if (msg.role === "user") {
            if (typeof msg.content === "string") {
                messages.push({
                    role: "user",
                    content: [{ type: "input_text", text: sanitizeSurrogates(msg.content) }],
                });
            }
            else {
                const content = msg.content.map((item) => {
                    if (item.type === "text") {
                        return {
                            type: "input_text",
                            text: sanitizeSurrogates(item.text),
                        };
                    }
                    return {
                        type: "input_image",
                        detail: "auto",
                        image_url: `data:${item.mimeType};base64,${item.data}`,
                    };
                });
                const filteredContent = !model.input.includes("image")
                    ? content.filter((c) => c.type !== "input_image")
                    : content;
                if (filteredContent.length === 0)
                    continue;
                // split a multi-image user turn into N separate user
                // messages (one input_image per message, text blocks attached
                // to the FIRST split). Robust against picky proxy/serializer
                // implementations that reject multiple input_image blocks per
                // user message even outside the native Computer-Use surface.
                const imageCount = filteredContent.filter((c) => c.type === "input_image").length;
                if (imageCount > 1) {
                    wcbDebugLog("split-multi-image-user-msg", { imageCount });
                    const textBlocks = filteredContent.filter((c) => c.type !== "input_image");
                    const imageBlocks = filteredContent.filter((c) => c.type === "input_image");
                    let first = true;
                    for (const img of imageBlocks) {
                        const parts = first ? [...textBlocks, img] : [img];
                        first = false;
                        messages.push({ role: "user", content: parts });
                    }
                } else {
                    messages.push({
                        role: "user",
                        content: filteredContent,
                    });
                }
            }
        }
        else if (msg.role === "assistant") {
            const output = [];
            const assistantMsg = msg;
            const isDifferentModel = assistantMsg.model !== model.id &&
                assistantMsg.provider === model.provider &&
                assistantMsg.api === model.api;
            for (const block of msg.content) {
                if (block.type === "thinking") {
                    if (block.thinkingSignature) {
                        const reasoningItem = JSON.parse(block.thinkingSignature);
                        output.push(reasoningItem);
                    }
                }
                else if (block.type === "text") {
                    const textBlock = block;
                    const parsedSignature = parseTextSignature(textBlock.textSignature);
                    // OpenAI requires id to be max 64 characters
                    let msgId = parsedSignature?.id;
                    if (!msgId) {
                        msgId = `msg_${msgIndex}`;
                    }
                    else if (msgId.length > 64) {
                        msgId = `msg_${shortHash(msgId)}`;
                    }
                    output.push({
                        type: "message",
                        role: "assistant",
                        content: [{ type: "output_text", text: sanitizeSurrogates(textBlock.text), annotations: [] }],
                        status: "completed",
                        id: msgId,
                        phase: parsedSignature?.phase,
                    });
                }
                else if (block.type === "toolCall") {
                    const toolCall = block;
                    const [callId, itemIdRaw] = toolCall.id.split("|");
                    let itemId = itemIdRaw;
                    // For different-model messages, set id to undefined to avoid pairing validation.
                    // OpenAI tracks which fc_xxx IDs were paired with rs_xxx reasoning items.
                    // By omitting the id, we avoid triggering that validation (like cross-provider does).
                    if (isDifferentModel && itemId?.startsWith("fc_")) {
                        itemId = undefined;
                    }
                    output.push({
                        type: "function_call",
                        id: itemId,
                        call_id: callId,
                        name: toolCall.name,
                        arguments: JSON.stringify(toolCall.arguments),
                    });
                }
            }
            if (output.length === 0)
                continue;
            messages.push(...output);
        }
        else if (msg.role === "toolResult") {
            const [callId] = msg.toolCallId.split("|");
            // Extract text and image content
            const textResult = msg.content
                .filter((c) => c.type === "text")
                .map((c) => c.text)
                .join("\n");
            const hasImages = msg.content.some((c) => c.type === "image");
            // Always send function_call_output with text (or placeholder if only images)
            const hasText = textResult.length > 0;
            messages.push({
                type: "function_call_output",
                call_id: callId,
                output: sanitizeSurrogates(hasText ? textResult : "(see attached image)"),
            });
            // If there are images and model supports them, send a follow-up user message with images
            if (hasImages && model.input.includes("image")) {
                const textPrefix = {
                    type: "input_text",
                    text: "Attached image(s) from tool result:",
                };
                const imageParts = [];
                for (const block of msg.content) {
                    if (block.type === "image") {
                        imageParts.push({
                            type: "input_image",
                            detail: "auto",
                            image_url: `data:${block.mimeType};base64,${block.data}`,
                        });
                    }
                }
                // split multi-image tool result into one user message per
                // image (text prefix on the FIRST only). Same robustness reason
                // as the user-turn split above.
                if (imageParts.length > 1) {
                    wcbDebugLog("split-multi-image-toolresult", {
                        callId,
                        imageCount: imageParts.length,
                    });
                    let first = true;
                    for (const img of imageParts) {
                        const parts = first ? [textPrefix, img] : [img];
                        first = false;
                        messages.push({ role: "user", content: parts });
                    }
                } else {
                    messages.push({
                        role: "user",
                        content: [textPrefix, ...imageParts],
                    });
                }
            }
        }
        msgIndex++;
    }
    // PROXY_IMAGE_CAPS: trim old input_image blocks, keeping only the most recent N.
    // WeaveBench 2026-05-23: dynamic cap — try cap=4, fallback to 3/2 if body > 4.5MB.
    try {
        const PROXY_IMAGE_CAPS = {
            "gpt-5.4": 4,
            "gpt-5.5": 0,   // WeaveBench 2026-05-23: 1M context full-image test via Azure LiteLLM (no 5MB limit)
        };
        const MAX_BODY_BYTES = 4.5 * 1024 * 1024; // 4.5MB safety margin under 5MB limit
        const cap = (model && PROXY_IMAGE_CAPS[model.id]) | 0;
        if (cap > 0) {
            // Scan all user-role messages for input_image items
            const imgPositions = [];
            for (let mi = 0; mi < messages.length; mi++) {
                const msg = messages[mi];
                const c = msg.content || (msg.role === "user" ? msg.content : null);
                if (Array.isArray(c)) {
                    for (let bi = 0; bi < c.length; bi++) {
                        if (c[bi] && c[bi].type === "input_image") {
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
                for (let mi = 0; mi < messages.length; mi++) {
                    const c = messages[mi].content;
                    if (!Array.isArray(c)) continue;
                    const filtered = c.filter((_blk, bi) => !dropSet.has(mi + ":" + bi));
                    if (filtered.length === 0) {
                        messages[mi].content = [
                            { type: "input_text", text: "[image dropped to satisfy " + model.id + " image cap=" + cap + "]" },
                        ];
                    } else if (filtered.length !== c.length) {
                        messages[mi].content = filtered;
                    }
                }
            }
            // Phase 2: byte-level safety — if body still > 4.5MB, drop more images
            // until it fits (dynamic fallback from cap=4 → 3 → 2 → 1)
            let bodySize = 0;
            try { bodySize = JSON.stringify(messages).length; } catch(_) {}
            while (bodySize > MAX_BODY_BYTES && effectiveCap > 1) {
                effectiveCap--;
                // Find and drop the oldest remaining input_image
                let dropped = false;
                for (let mi = 0; mi < messages.length && !dropped; mi++) {
                    const c = messages[mi].content;
                    if (!Array.isArray(c)) continue;
                    const imgIdx = c.findIndex((b) => b && b.type === "input_image");
                    if (imgIdx >= 0) {
                        c.splice(imgIdx, 1, { type: "input_text", text: "[image dropped — body overflow fallback]" });
                        dropped = true;
                    }
                }
                if (!dropped) break;
                try { bodySize = JSON.stringify(messages).length; } catch(_) { break; }
            }
            if (total > effectiveCap || effectiveCap < cap) {
                try {
                    console.error(
                        "[weavebench image-cap responses] " + model.id + " trimmed " + total +
                        " images → kept " + effectiveCap + " (cap=" + cap +
                        ", body=" + (bodySize/1024/1024).toFixed(1) + "MB)"
                    );
                } catch (_) {}
            }
        }
    } catch (e) {
        try {
            console.error("[weavebench image-cap responses] failed: " + (e && e.message));
        } catch (_) {}
    }
    //
    // WeaveBench 2026-05-08: dedup `messages` by id BEFORE handing the array off to
    // OpenAI / Azure. Azure /responses returns a hard 400
    //     "Duplicate item found with id rs_xxxx... Remove duplicate items"
    // when a reasoning / function_call / message id repeats anywhere in
    // `input`. The history walk above can legitimately push the same
    // assistant block twice if the multi-turn history ever contains a
    // duplicate (refusal-retry replay, model self-repeat, accidental
    // re-injection of a prior turn, etc.). Since this dedup is server-safe
    // (every id is unique on the wire by API contract), suppressing later
    // duplicates is strictly an improvement: keeps order, keeps first
    // occurrence, drops nothing without an id.
    const seenIds = new Set();
    const dedupedMessages = [];
    let droppedDup = 0;
    for (const item of messages) {
        const itemId = item && typeof item.id === "string" ? item.id : null;
        if (itemId !== null) {
            if (seenIds.has(itemId)) {
                droppedDup++;
                continue;
            }
            seenIds.add(itemId);
        }
        dedupedMessages.push(item);
    }
    if (droppedDup > 0) {
        wcbDebugLog("dedup-input-ids", { droppedDup, kept: dedupedMessages.length });
    }
    return dedupedMessages;
}
// =============================================================================
// Tool conversion
// =============================================================================
// WeaveBench 2026-05-04: extra description hint injected for gpt-5.5 only. The
// openclaw `__computer__` tool exposes its `action` parameter as a TypeBox
// discriminated-union (oneOf with `type` discriminator). gpt-5.5 (reasoning
// model on cop-resp) can read the JSON-schema enough to know an `action`
// field exists, but does NOT introspect the nested oneOf sub-types — so on
// every fresh task it wastes 2 turns trying `{}` then `{action: "screenshot"}`
// before learning the correct `{action: {type: "screenshot"}}` shape.
// Concrete examples cribbed from successful traces eliminate that 2-turn
// bootstrap cost without changing any schema. Gated on model.id to avoid
// touching other models' tool definitions.
const WEAVEBENCH_COMPUTER_HINT = [
    "",
    "",
    "FORMAT NOTE — `action` is a discriminated-union object whose `type` field selects the variant. Always pass `action` as an OBJECT (never a bare string, never omit it). Use one of these shapes:",
    "  • `{ \"action\": { \"type\": \"screenshot\" } }`",
    "  • `{ \"action\": { \"type\": \"click\", \"x\": 100, \"y\": 200, \"button\": \"left\" } }`",
    "  • `{ \"action\": { \"type\": \"move\", \"x\": 548, \"y\": 1016 } }`",
    "  • `{ \"action\": { \"type\": \"type\", \"text\": \"hello\\n\" } }`",
    "  • `{ \"action\": { \"type\": \"key\", \"keys\": [\"CTRL\", \"C\"] } }`  (alias: `keypress`)",
    "  • `{ \"action\": { \"type\": \"scroll\", \"x\": 760, \"y\": 770, \"scroll_x\": 0, \"scroll_y\": 200 } }`",
    "  • `{ \"action\": { \"type\": \"drag\", \"path\": [ { \"x\": 350, \"y\": 300 }, { \"x\": 500, \"y\": 400 } ] } }`",
    "  • `{ \"action\": { \"type\": \"wait\", \"ms\": 1500 } }`  (`ms` optional)",
].join("\n");
function maybeInjectComputerHint(tool, model) {
    if (!tool || tool.name !== COMPUTER_TOOL_NAME) return tool.description;
    if (!model || model.id !== "gpt-5.5") return tool.description;
    const baseDesc = tool.description || "";
    if (baseDesc.includes("FORMAT NOTE — `action` is a discriminated-union")) {
        return baseDesc;
    }
    return baseDesc + WEAVEBENCH_COMPUTER_HINT;
}

export function convertResponsesTools(tools, options) {
    const strict = options?.strict === undefined ? false : options.strict;
    const model = options?.model;
    // WeaveBench 2026-05-07 (final): every GUI model sees the same single tool —
    // `__computer__` (fat function tool: accepts single `action` or batched
    // `actions[]`, ALWAYS returns a fresh screenshot). All other openclaw
    // built-ins (read/write/edit/grep/find/ls/exec/process/apply_patch/
    // image/pdf/web_fetch) cross the wire as standard function tools.
    return tools.flatMap((tool) => {
        // ---- __computer__ -----------------------------------------------
        if (isComputerTool(tool)) {
            return [{
                type: "function",
                name: tool.name,
                description: maybeInjectComputerHint(tool, model),
                parameters: tool.parameters,
                strict,
            }];
        }
        // ---- All other openclaw built-ins -------------------------------
        return [{
            type: "function",
            name: tool.name,
            description: maybeInjectComputerHint(tool, model),
            parameters: tool.parameters,
            strict,
        }];
    });
}
// =============================================================================
// Stream processing
// =============================================================================
export async function processResponsesStream(openaiStream, output, stream, model, options) {
    let currentItem = null;
    let currentBlock = null;
    const blocks = output.content;
    const blockIndex = () => blocks.length - 1;
    for await (const event of openaiStream) {
        if (event.type === "response.output_item.added") {
            const item = event.item;
            if (item.type === "reasoning") {
                currentItem = item;
                currentBlock = { type: "thinking", thinking: "" };
                output.content.push(currentBlock);
                stream.push({ type: "thinking_start", contentIndex: blockIndex(), partial: output });
            }
            else if (item.type === "message") {
                currentItem = item;
                currentBlock = { type: "text", text: "" };
                output.content.push(currentBlock);
                stream.push({ type: "text_start", contentIndex: blockIndex(), partial: output });
            }
            else if (item.type === "function_call") {
                currentItem = item;
                currentBlock = {
                    type: "toolCall",
                    id: `${item.call_id}|${item.id}`,
                    name: item.name,
                    arguments: {},
                    partialJson: item.arguments || "",
                };
                output.content.push(currentBlock);
                stream.push({ type: "toolcall_start", contentIndex: blockIndex(), partial: output });
            }
        }
        else if (event.type === "response.reasoning_summary_part.added") {
            if (currentItem && currentItem.type === "reasoning") {
                currentItem.summary = currentItem.summary || [];
                currentItem.summary.push(event.part);
            }
        }
        else if (event.type === "response.reasoning_summary_text.delta") {
            if (currentItem?.type === "reasoning" && currentBlock?.type === "thinking") {
                currentItem.summary = currentItem.summary || [];
                const lastPart = currentItem.summary[currentItem.summary.length - 1];
                if (lastPart) {
                    currentBlock.thinking += event.delta;
                    lastPart.text += event.delta;
                    stream.push({
                        type: "thinking_delta",
                        contentIndex: blockIndex(),
                        delta: event.delta,
                        partial: output,
                    });
                }
            }
        }
        else if (event.type === "response.reasoning_summary_part.done") {
            if (currentItem?.type === "reasoning" && currentBlock?.type === "thinking") {
                currentItem.summary = currentItem.summary || [];
                const lastPart = currentItem.summary[currentItem.summary.length - 1];
                if (lastPart) {
                    currentBlock.thinking += "\n\n";
                    lastPart.text += "\n\n";
                    stream.push({
                        type: "thinking_delta",
                        contentIndex: blockIndex(),
                        delta: "\n\n",
                        partial: output,
                    });
                }
            }
        }
        else if (event.type === "response.content_part.added") {
            if (currentItem?.type === "message") {
                currentItem.content = currentItem.content || [];
                // Filter out ReasoningText, only accept output_text and refusal
                if (event.part.type === "output_text" || event.part.type === "refusal") {
                    currentItem.content.push(event.part);
                }
            }
        }
        else if (event.type === "response.output_text.delta") {
            if (currentItem?.type === "message" && currentBlock?.type === "text") {
                if (!currentItem.content || currentItem.content.length === 0) {
                    continue;
                }
                const lastPart = currentItem.content[currentItem.content.length - 1];
                if (lastPart?.type === "output_text") {
                    currentBlock.text += event.delta;
                    lastPart.text += event.delta;
                    stream.push({
                        type: "text_delta",
                        contentIndex: blockIndex(),
                        delta: event.delta,
                        partial: output,
                    });
                }
            }
        }
        else if (event.type === "response.refusal.delta") {
            if (currentItem?.type === "message" && currentBlock?.type === "text") {
                if (!currentItem.content || currentItem.content.length === 0) {
                    continue;
                }
                const lastPart = currentItem.content[currentItem.content.length - 1];
                if (lastPart?.type === "refusal") {
                    currentBlock.text += event.delta;
                    lastPart.refusal += event.delta;
                    stream.push({
                        type: "text_delta",
                        contentIndex: blockIndex(),
                        delta: event.delta,
                        partial: output,
                    });
                }
            }
        }
        else if (event.type === "response.function_call_arguments.delta") {
            if (currentItem?.type === "function_call" && currentBlock?.type === "toolCall") {
                currentBlock.partialJson += event.delta;
                currentBlock.arguments = parseStreamingJson(currentBlock.partialJson);
                stream.push({
                    type: "toolcall_delta",
                    contentIndex: blockIndex(),
                    delta: event.delta,
                    partial: output,
                });
            }
        }
        else if (event.type === "response.function_call_arguments.done") {
            if (currentItem?.type === "function_call" && currentBlock?.type === "toolCall") {
                currentBlock.partialJson = event.arguments;
                currentBlock.arguments = parseStreamingJson(currentBlock.partialJson);
            }
        }
        else if (event.type === "response.output_item.done") {
            const item = event.item;
            if (item.type === "reasoning" && currentBlock?.type === "thinking") {
                currentBlock.thinking = item.summary?.map((s) => s.text).join("\n\n") || "";
                currentBlock.thinkingSignature = JSON.stringify(item);
                stream.push({
                    type: "thinking_end",
                    contentIndex: blockIndex(),
                    content: currentBlock.thinking,
                    partial: output,
                });
                currentBlock = null;
            }
            else if (item.type === "message" && currentBlock?.type === "text") {
                currentBlock.text = item.content.map((c) => (c.type === "output_text" ? c.text : c.refusal)).join("");
                currentBlock.textSignature = encodeTextSignatureV1(item.id, item.phase ?? undefined);
                stream.push({
                    type: "text_end",
                    contentIndex: blockIndex(),
                    content: currentBlock.text,
                    partial: output,
                });
                currentBlock = null;
            }
            else if (item.type === "function_call") {
                const args = currentBlock?.type === "toolCall" && currentBlock.partialJson
                    ? parseStreamingJson(currentBlock.partialJson)
                    : parseStreamingJson(item.arguments || "{}");
                const toolCall = {
                    type: "toolCall",
                    id: `${item.call_id}|${item.id}`,
                    name: item.name,
                    arguments: args,
                };
                currentBlock = null;
                stream.push({ type: "toolcall_end", contentIndex: blockIndex(), toolCall, partial: output });
            }
        }
        else if (event.type === "response.completed") {
            const response = event.response;
            if (response?.usage) {
                const cachedTokens = response.usage.input_tokens_details?.cached_tokens || 0;
                // extract reasoning_tokens from output_tokens_details (gpt-5.x reasoning models).
                // Both some OpenAI-compatible /v1/responses gateways return
                // this nested field; preserving it here lets eval/agent_judge/token_stats.py
                // surface agent_reasoning_tokens in score.json. Note that response.usage.output_tokens
                // already EXCLUDES reasoning_tokens (per OpenAI Responses spec), so storing
                // reasoning as a sibling field (not summed into output) keeps the schema clean.
                const reasoningTokens = response.usage.output_tokens_details?.reasoning_tokens || 0;
                output.usage = {
                    // OpenAI includes cached tokens in input_tokens, so subtract to get non-cached input
                    input: (response.usage.input_tokens || 0) - cachedTokens,
                    output: response.usage.output_tokens || 0,
                    reasoning: reasoningTokens,
                    cacheRead: cachedTokens,
                    cacheWrite: 0,
                    totalTokens: response.usage.total_tokens || 0,
                    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
                };
            }
            calculateCost(model, output.usage);
            if (options?.applyServiceTierPricing) {
                const serviceTier = response?.service_tier ?? options.serviceTier;
                options.applyServiceTierPricing(output.usage, serviceTier);
            }
            // Map status to stop reason
            output.stopReason = mapStopReason(response?.status);
            if (output.content.some((b) => b.type === "toolCall") && output.stopReason === "stop") {
                output.stopReason = "toolUse";
            }
        }
        else if (event.type === "error") {
            throw new Error(`Error Code ${event.code}: ${event.message}` || "Unknown error");
        }
        else if (event.type === "response.failed") {
            // WeaveBench 2026-05-06: surface the actual Azure failure reason instead of
            // collapsing to "Unknown error". Azure /responses sends an `error`
            // object on the response in the response.failed event.
            const r = event.response || {};
            const err = r.error || {};
            const detail = err.message || err.code || (r.incomplete_details?.reason) ||
                           (typeof err === 'string' ? err : '') ||
                           JSON.stringify(err).slice(0, 240) ||
                           "no detail";
            throw new Error(`response.failed: ${detail} (status=${r.status || 'unknown'})`);
        }
    }
}
function mapStopReason(status) {
    if (!status)
        return "stop";
    switch (status) {
        case "completed":
            return "stop";
        case "incomplete":
            return "length";
        case "failed":
        case "cancelled":
            return "error";
        // These two are wonky ...
        case "in_progress":
        case "queued":
            return "stop";
        default: {
            const _exhaustive = status;
            throw new Error(`Unhandled stop reason: ${_exhaustive}`);
        }
    }
}
//# sourceMappingURL=openai-responses-shared.js.map
