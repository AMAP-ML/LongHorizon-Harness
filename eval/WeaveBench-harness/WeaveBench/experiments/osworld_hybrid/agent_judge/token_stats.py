"""Aggregate agent-side token usage from openclaw chat.jsonl.

OpenClaw writes one line per record to chat.jsonl. Each record of type
`message` carries a `message.usage` dict with this shape:

    {
      "input": <int>,            # uncached prompt tokens for THIS call
      "output": <int>,           # generated tokens
      "cacheRead": <int>,        # tokens served from prompt cache
      "cacheWrite": <int>,       # tokens written to cache
      "totalTokens": <int>,      # total billable tokens for THIS call
      "cost": {"input":..,"output":..,"cacheRead":..,"cacheWrite":..,"total":..}
    }

NOTE: the totalTokens field is OpenClaw's notion of billable for the call
and equals input + output + cacheRead + cacheWrite (rough). Cost is 0 for
custom endpoints (no pricing table). We sum each separately so downstream
tools can recompute cost if a pricing table is supplied later.

We also track:
  - n_calls: number of message records carrying usage
  - reasoning_tokens: if openclaw exposes a "reasoning" usage subkey (some
    Responses-API models report this; we look for both "reasoningTokens"
    and the OpenAI-style "reasoning_tokens" within message.usage).
  - thinking_blocks / thinking_chars: number and total length of
    `content[type=thinking]` blocks. Anthropic Claude models with
    extended thinking enabled put their full reasoning text into these
    blocks; the upstream API does NOT report a separate token count for
    them (thinking is folded into output_tokens), so the literal text
    length is the only reasoning-effort signal we can recover. Useful as
    a proxy for Claude reasoning even though `reasoning` (token count)
    will be 0 for Anthropic.
  - by_model: per-model breakdown if more than one model used."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("agent_judge.token_stats")


def _coerce_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def aggregate_chat_jsonl(chat_path: str | Path) -> Dict[str, Any]:
    """Parse a chat.jsonl and return aggregated agent token usage.

    Returns a dict with keys:
      ok, error, n_calls, input, output, cache_read, cache_write,
      reasoning, total_tokens, by_model, sample_models,
      first_call_at, last_call_at.

    Always returns ok=True with zeros if the file doesn't exist (so the
    caller can treat it as "no data" without raising).
    """
    p = Path(chat_path)
    out: Dict[str, Any] = {
        "ok": True, "error": None,
        "n_calls": 0,
        "input": 0, "output": 0,
        "cache_read": 0, "cache_write": 0,
        "reasoning": 0,
        "thinking_blocks": 0,
        "thinking_chars": 0,
        "total_tokens": 0,
        "by_model": {},
        "sample_models": [],
        "first_call_at": None,
        "last_call_at": None,
    }
    if not p.exists():
        out["error"] = "chat.jsonl missing"
        return out
    try:
        size = p.stat().st_size
        if size == 0:
            out["error"] = "chat.jsonl empty"
            return out
    except Exception:
        pass

    seen_models: list[str] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "message":
                    continue
                msg = rec.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                inp = _coerce_int(usage.get("input"))
                outp = _coerce_int(usage.get("output"))
                cr = _coerce_int(usage.get("cacheRead") or usage.get("cache_read"))
                cw = _coerce_int(usage.get("cacheWrite") or usage.get("cache_write"))
                tot = _coerce_int(usage.get("totalTokens") or usage.get("total_tokens"))
                rea = _coerce_int(
                    # OpenClaw chat.jsonl normalized field (preferred — written by
                    # wcb_assets/openclaw_patches/openai-responses-shared.patched.js).
                    usage.get("reasoning")
                    or usage.get("reasoningTokens")
                    or usage.get("reasoning_tokens")
                    # OpenAI Responses raw API nested field, in case a caller forwards it
                    # without going through the openclaw normalizer (typo fixed:
                    # was "output_details" which never matched the real API key).
                    or (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
                )

                if inp == 0 and outp == 0 and cr == 0 and cw == 0 and tot == 0:
                    continue

                # Count Claude / Anthropic-style thinking blocks. The Anthropic
                # Messages API returns extended-thinking reasoning text as
                # `content[type=thinking]` blocks. The token count is folded
                # into output_tokens (no separate breakdown), so the literal
                # text length is the only quantitative reasoning-effort signal
                # we can recover for Claude models. Counting also catches the
                # `redacted_thinking` path which openclaw normalizes to
                # `type=thinking` with body "[Reasoning redacted]".
                think_blocks_in_msg = 0
                think_chars_in_msg = 0
                content_blocks = msg.get("content")
                if isinstance(content_blocks, list):
                    for blk in content_blocks:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "thinking":
                            txt = blk.get("thinking") or blk.get("text") or ""
                            think_blocks_in_msg += 1
                            think_chars_in_msg += len(txt) if isinstance(txt, str) else 0

                out["n_calls"] += 1
                out["input"] += inp
                out["output"] += outp
                out["cache_read"] += cr
                out["cache_write"] += cw
                out["reasoning"] += rea
                out["thinking_blocks"] += think_blocks_in_msg
                out["thinking_chars"] += think_chars_in_msg
                out["total_tokens"] += tot if tot else (inp + outp + cr + cw)

                model = msg.get("model") or msg.get("provider") or "unknown"
                if model not in seen_models:
                    seen_models.append(model)
                bm = out["by_model"].setdefault(
                    model, {"n_calls": 0, "input": 0, "output": 0,
                            "cache_read": 0, "cache_write": 0,
                            "reasoning": 0, "thinking_blocks": 0,
                            "thinking_chars": 0, "total_tokens": 0})
                bm["n_calls"] += 1
                bm["input"] += inp
                bm["output"] += outp
                bm["cache_read"] += cr
                bm["cache_write"] += cw
                bm["reasoning"] += rea
                bm["thinking_blocks"] += think_blocks_in_msg
                bm["thinking_chars"] += think_chars_in_msg
                bm["total_tokens"] += tot if tot else (inp + outp + cr + cw)

                ts = msg.get("timestamp") or rec.get("timestamp")
                if ts:
                    if out["first_call_at"] is None:
                        out["first_call_at"] = ts
                    out["last_call_at"] = ts
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["sample_models"] = seen_models[:8]
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: token_stats.py <chat.jsonl>")
        sys.exit(2)
    print(json.dumps(aggregate_chat_jsonl(sys.argv[1]), indent=2,
                     ensure_ascii=False))
