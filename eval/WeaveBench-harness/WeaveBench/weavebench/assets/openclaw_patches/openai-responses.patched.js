// ============================================================================
// WeaveBench patched copy of pi-ai/dist/providers/openai-responses.js
//
// HISTORY (2026-05-07): native OpenAI Computer-Use Tool support has been
// PERMANENTLY REMOVED. The pre-removal flavour (with WEAVEBENCH_CUA_INCREMENTAL,
// previous_response_id loop, store:true, wcbGet/Reset/IsIncrementalCuaState
// imports, error-driven state reset, etc.) is preserved verbatim in
// `openai-responses.legacy_native_cua.js` for archival reference. That
// file is NOT loaded at runtime.
//
// Diffs vs upstream (.original.js) — current production patches only:
//   1. Imports convertResponsesMessages / convertResponsesTools /
//      processResponsesStream from the patched openai-responses-shared.js
//      (no incremental-CUA helpers anymore).
//   2. buildParams: stateless — never sets store:true, never attaches
//      previous_response_id; every turn ships the full transformed
//      message slice as the upstream behaviour.
//   3. catch path: no per-session state to reset (incremental CUA gone).
// Behaviour with WeaveBench-only patches is byte-for-byte upstream behaviour
// PLUS the multi-image-split / debug-log changes from the shared patch.
// ============================================================================
import OpenAI from "openai";
import { getEnvApiKey } from "../env-api-keys.js";
import { supportsXhigh } from "../models.js";
import { AssistantMessageEventStream } from "../utils/event-stream.js";
import { buildCopilotDynamicHeaders, hasCopilotVisionInput } from "./github-copilot-headers.js";
import {
    convertResponsesMessages,
    convertResponsesTools,
    processResponsesStream,
} from "./openai-responses-shared.js";
import { buildBaseOptions, clampReasoning } from "./simple-options.js";
import * as wbFs from "fs";
function wcbDebugLog(label, obj) {
    try {
        wbFs.appendFileSync("/tmp/openclaw/weavebench_cua_debug.log",
            `${new Date().toISOString()} ${label} ${JSON.stringify(obj)}\n`);
    } catch (e) {}
}
const OPENAI_TOOL_CALL_PROVIDERS = new Set(["openai", "openai-codex", "opencode"]);
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
/**
 * Get prompt cache retention based on cacheRetention and base URL.
 * Only applies to direct OpenAI API calls (api.openai.com).
 */
function getPromptCacheRetention(baseUrl, cacheRetention) {
    if (cacheRetention !== "long") {
        return undefined;
    }
    if (baseUrl.includes("api.openai.com")) {
        return "24h";
    }
    return undefined;
}
/**
 * Generate function for OpenAI Responses API
 */
export const streamOpenAIResponses = (model, context, options) => {
    const stream = new AssistantMessageEventStream();
    // Start async processing
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
        // WeaveBench 2026-05-06: Azure /v1/responses gpt-5.5 deployment偶发会在 SSE 流
        // 中发 `response.failed` event 并且 error 字段为空 ({}). 实测原因是 Azure
        // 端 rate-limit "Too Many Requests" (gpt-5.5 deployment per-min RPM/TPM
        // quota) — 15 路并发会在峰值打到 ~16% 失败率. 加最多 5 次重试 + 指数
        // backoff (5s/15s/30s/60s for 429), 配合 8 路并发能把失败率降到 <2%.
        const MAX_RETRIES = 5;
        let attempt = 0;
        let lastError = null;
        while (attempt < MAX_RETRIES) {
            attempt++;
            // reset transient state on retry
            if (attempt > 1) {
                output.content = [];
                output.usage = {
                    input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
                };
                output.stopReason = "stop";
                output.errorMessage = undefined;
                try {
                    console.error(`[weavebench responses-retry] attempt ${attempt}/${MAX_RETRIES} model=${model.id} reason=${(lastError && lastError.message || '').slice(0, 200)}`);
                } catch (_) {}
                // small backoff
                await new Promise((r) => setTimeout(r, 1500));
            }
            try {
                // Create OpenAI client
                const apiKey = options?.apiKey || getEnvApiKey(model.provider) || "";
                const client = createClient(model, context, apiKey, options?.headers);
                let params = buildParams(model, context, options);
                const nextParams = await options?.onPayload?.(params, model);
                if (nextParams !== undefined) {
                    params = nextParams;
                }
                const openaiStream = await client.responses.create(params, options?.signal ? { signal: options.signal } : undefined);
                stream.push({ type: "start", partial: output });
                await processResponsesStream(openaiStream, output, stream, model, {
                    serviceTier: options?.serviceTier,
                    applyServiceTierPricing,
                    sessionId: options?.sessionId,
                });
                if (options?.signal?.aborted) {
                    throw new Error("Request was aborted");
                }
                if (output.stopReason === "aborted" || output.stopReason === "error") {
                    throw new Error("An unknown error occurred");
                }
                stream.push({ type: "done", reason: output.stopReason, message: output });
                stream.end();
                return; // success
            }
            catch (error) {
                for (const block of output.content)
                    delete block.index;
                lastError = error;
                const errMsg = error instanceof Error ? error.message : JSON.stringify(error);
                // Don't retry on user-aborted, refusal, or hard 4xx (size/auth/etc.)
                const isAborted = options?.signal?.aborted;
                const isRefusal = /refus|content_?filter|safety|jailbreak|policy/i.test(errMsg);
                // Treat 429 / Too Many Requests / rate_limit as RETRYABLE — Azure
                // gpt-5.5 deployment hits per-deployment RPM/TPM quota under
                // 15-way concurrency. Use exponential backoff so we don't immediately
                // re-trip the quota.
                const isRateLimit = /429|too[ _-]?many[ _-]?request|rate[ _-]?limit|too_many_requests/i.test(errMsg);
                // WeaveBench 2026-05-08: Azure "Duplicate item found with id rs_xxx"
                // is technically `invalid_request_error` (would be classified as
                // hard 4xx) but it's actually transient: the dedup pass in
                // openai-responses-shared.patched.js → convertResponsesMessages
                // strips the duplicate id, so a plain retry sends a clean
                // request. Without this carve-out the buggy turn permanently
                // blocks the task.
                const isDupItem = /duplicate item found with id/i.test(errMsg);
                const isHard4xx = !isRateLimit && !isDupItem &&
                    /\b40[0-9]\b|invalid_request|unauthorized|not_found/i.test(errMsg);
                const shouldRetry = !isAborted && !isRefusal && !isHard4xx && attempt < MAX_RETRIES;
                if (shouldRetry) {
                    // Exponential backoff: 5s, 15s, 30s, 60s for rate_limit; 1.5s for transient
                    if (isRateLimit) {
                        const waits = [5000, 15000, 30000, 60000];
                        const w = waits[Math.min(attempt - 1, waits.length - 1)];
                        try { console.error(`[weavebench responses-rate-limit-backoff] attempt ${attempt}/${MAX_RETRIES} sleep=${w}ms`); } catch (_) {}
                        await new Promise((r) => setTimeout(r, w));
                    }
                    continue; // loop will retry (next iteration also has the standard 1.5s if !rateLimit)
                }
                // Final failure path (matches original logic)
                output.stopReason = isAborted ? "aborted" : "error";
                output.errorMessage = errMsg;
                stream.push({ type: "error", reason: output.stopReason, error: output });
                stream.end();
                return;
            }
        }
    })();
    return stream;
};
export const streamSimpleOpenAIResponses = (model, context, options) => {
    const apiKey = options?.apiKey || getEnvApiKey(model.provider);
    if (!apiKey) {
        throw new Error(`No API key for provider: ${model.provider}`);
    }
    const base = buildBaseOptions(model, options, apiKey);
    const reasoningEffort = supportsXhigh(model) ? options?.reasoning : clampReasoning(options?.reasoning);
    return streamOpenAIResponses(model, context, {
        ...base,
        reasoningEffort,
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
        // WeaveBench patch: see openai-completions.patched.js for rationale.
        timeout: 1800000,
        // WeaveBench patch: enable SDK auto-retry (3) for ECONNRESET / 5xx / 408 / 429
        // — see openai-completions.patched.js for full justification.
        maxRetries: 3,
    });
}
function buildParams(model, context, options) {
    // WeaveBench 2026-05-07: stateless — native incremental CUA loop removed.
    // Always ship the full transformed message slice; no store:true,
    // no previous_response_id, no truncation:auto.
    const messages = convertResponsesMessages(model, context, OPENAI_TOOL_CALL_PROVIDERS, {
        sessionId: options?.sessionId,
    });
    const cacheRetention = resolveCacheRetention(options?.cacheRetention);
    const params = {
        model: model.id,
        input: messages,
        stream: true,
        prompt_cache_key: cacheRetention === "none" ? undefined : options?.sessionId,
        prompt_cache_retention: getPromptCacheRetention(model.baseUrl, cacheRetention),
        store: false,
    };
    if (options?.maxTokens) {
        params.max_output_tokens = options?.maxTokens;
    }
    if (options?.temperature !== undefined) {
        params.temperature = options?.temperature;
    }
    if (options?.serviceTier !== undefined) {
        params.service_tier = options.serviceTier;
    }
    if (context.tools) {
        // WeaveBench 2026-05-04: pass model so convertResponsesTools can model-gate
        // a per-tool description hint (e.g. inject __computer__ action format
        // examples for gpt-5.5 only — see openai-responses-shared.patched.js).
        params.tools = convertResponsesTools(context.tools, { model });
    }
    if (model.reasoning) {
        if (options?.reasoningEffort || options?.reasoningSummary) {
            params.reasoning = {
                effort: options?.reasoningEffort || "medium",
                summary: options?.reasoningSummary || "auto",
            };
            params.include = ["reasoning.encrypted_content"];
        }
        else {
            if (model.name.startsWith("gpt-5")) {
                // Jesus Christ, see https://community.openai.com/t/need-reasoning-false-option-for-gpt-5/1351588/7
                messages.push({
                    role: "developer",
                    content: [
                        {
                            type: "input_text",
                            text: "# Juice: 0 !important",
                        },
                    ],
                });
            }
        }
    }
    return params;
}
function getServiceTierCostMultiplier(serviceTier) {
    switch (serviceTier) {
        case "flex":
            return 0.5;
        case "priority":
            return 2;
        default:
            return 1;
    }
}
function applyServiceTierPricing(usage, serviceTier) {
    const multiplier = getServiceTierCostMultiplier(serviceTier);
    if (multiplier === 1)
        return;
    usage.cost.input *= multiplier;
    usage.cost.output *= multiplier;
    usage.cost.cacheRead *= multiplier;
    usage.cost.cacheWrite *= multiplier;
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite;
}
//# sourceMappingURL=openai-responses.js.map