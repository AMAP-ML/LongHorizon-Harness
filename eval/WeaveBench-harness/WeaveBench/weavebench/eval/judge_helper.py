"""Shared VLM/LLM judge helper for task graders.

Used by graders to evaluate visual/creative outputs via OpenRouter VLM.
Env vars (auto-set by weavebench-run):
  OPENROUTER_BASE_URL, OPENROUTER_API_KEY, JUDGE_MODEL

Optional protocol switch (2026-05-11):
  WEAVEBENCH_JUDGE_PROTOCOL = "responses" (default) | "chat"
    - "responses": uses OpenAI /v1/responses (works for gateways that
      expose the Responses API; useful for gpt-5.x-style deployments)
    - "chat": uses OpenAI /v1/chat/completions (works for any
      OpenAI-compatible gateway, including local GitHub Copilot
      endpoints that only expose the chat path; e.g. claude-opus-4
      as judge)
"""
import os, json, base64, logging
from pathlib import Path

log = logging.getLogger("ds_judge")


def _b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def _judge_protocol() -> str:
    return (os.environ.get("WEAVEBENCH_JUDGE_PROTOCOL") or "responses").strip().lower()


def _call_judge_vlm(client, model, content_responses, max_tokens=2048):
    """Dispatch a multimodal judge call to /responses or /chat/completions.

    `content_responses` is the responses-API content list (uses
    {"type":"input_image","image_url":...} and {"type":"input_text","text":...}).
    For chat-protocol mode it's transformed to chat content
    ({"type":"image_url","image_url":{"url":...}} and {"type":"text","text":...}).
    """
    if _judge_protocol() == "chat":
        # Transform to chat-style content
        chat_content = []
        for c in content_responses:
            t = c.get("type")
            if t == "input_image":
                chat_content.append({"type": "image_url",
                                     "image_url": {"url": c["image_url"]}})
            elif t == "input_text":
                chat_content.append({"type": "text", "text": c["text"]})
            else:
                chat_content.append(c)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": chat_content}],
            max_completion_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    # Default: /responses
    resp = client.responses.create(
        model=model,
        input=[{"role": "user", "content": content_responses}],
        max_output_tokens=max_tokens,
    )
    return (resp.output_text or "").strip()


def vlm_score_rubric(image_paths, rubric: dict, instruction: str = "", max_retries: int = 3) -> dict:
    """Score a rubric dict {key: criterion_text} against image(s).

    Returns dict {key: 0.0|1.0, "judge_method": "vlm"|"failed", "judge_error": str|None}.
    """
    out = {k: 0.0 for k in rubric}
    out["judge_method"] = "failed"
    out["judge_error"] = None
    if not rubric:
        return out
    try:
        from openai import OpenAI
    except Exception as e:
        out["judge_error"] = f"openai-import:{e}"
        return out
    if isinstance(image_paths, (str, Path)):
        image_paths = [image_paths]
    image_paths = [Path(p) for p in image_paths if Path(p).exists()]
    if not image_paths:
        out["judge_error"] = "no-images"
        return out
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL", "openai/gpt-5.5")
    print(f"[judge-vlm] base_url={base_url} model={model} key_prefix={api_key[:8]} n_images={len(image_paths)} protocol={_judge_protocol()}", flush=True)
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        out["judge_error"] = f"client:{e}"
        return out
    rubric_list = "\n".join(f"  - {k}: {v}" for k, v in rubric.items())
    prompt = (
        (instruction + "\n\n" if instruction else "") +
        "请逐条评估以下 rubric，每条返回 1（满足）或 0（不满足）。如不确定一律给 0。\n\n"
        f"Rubric:\n{rubric_list}\n\n"
        "严格按 JSON 返回（不要 markdown 代码块、不要其他文字）：\n"
        "{\"<rubric_id>\": 0_or_1, ...}\n"
        "确保所有 rubric_id 都作为键出现。"
    )
    content = []
    for p in image_paths[:6]:
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{_b64(p)}"})
    content.append({"type": "input_text", "text": prompt})
    last_err = None
    for attempt in range(max_retries):
        try:
            raw = _call_judge_vlm(client, model, content, max_tokens=2048)
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(raw)
            for k in rubric:
                out[k] = float(max(0, min(1, int(data.get(k, 0)))))
            out["judge_method"] = "vlm"
            out["judge_error"] = None
            return out
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[judge-vlm] attempt {attempt+1} failed: {last_err}", flush=True)
            log.warning("VLM attempt %d failed: %s", attempt + 1, last_err)
    out["judge_error"] = last_err
    return out


def llm_score_text(text: str, rubric: dict, instruction: str = "", max_retries: int = 3) -> dict:
    """Text-only LLM judge (e.g., for analysis.md, README.md content quality)."""
    out = {k: 0.0 for k in rubric}
    out["judge_method"] = "failed"
    out["judge_error"] = None
    if not rubric or not text:
        out["judge_error"] = "no-input"
        return out
    try:
        from openai import OpenAI
    except Exception as e:
        out["judge_error"] = f"openai-import:{e}"
        return out
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL", "openai/gpt-5.5")
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        out["judge_error"] = f"client:{e}"
        return out
    rubric_list = "\n".join(f"  - {k}: {v}" for k, v in rubric.items())
    prompt = (
        (instruction + "\n\n" if instruction else "") +
        f"待评估文本（节选 4000 字符）：\n---\n{text[:4000]}\n---\n\n"
        f"Rubric:\n{rubric_list}\n\n严格 JSON：{{\"<id>\":0_or_1,...}}"
    )
    last_err = None
    for attempt in range(max_retries):
        try:
            raw = _call_judge_vlm(
                client, model,
                [{"type": "input_text", "text": prompt}],
                max_tokens=1024,
            )
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(raw)
            for k in rubric:
                out[k] = float(max(0, min(1, int(data.get(k, 0)))))
            out["judge_method"] = "llm"
            out["judge_error"] = None
            return out
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[judge-llm] attempt {attempt+1} failed: {last_err}", flush=True)
    out["judge_error"] = last_err
    return out


def audit_chat_jsonl_for_banned(banned_substrings, chat_path=None):
    """Scan the agent's openclaw chat.jsonl for banned tool-call patterns.

    Each banned_substring is checked as a literal substring of the JSON-
    serialized `arguments` of every assistant `toolCall` event.

    Returns dict {
        "any_banned": bool,
        "matches": [<substring that matched>, ...],
        "total_tool_calls": int,
        "computer_screenshots": int,   # number of __computer__ screenshot calls
        "banned_examples": [str, ...]  # short snippets where banned appeared
    }
    """
    import json
    from pathlib import Path
    if chat_path is None:
        chat_path = Path("/home/user/.openclaw/agents/main/sessions/chat.jsonl")
    else:
        chat_path = Path(chat_path)
    out = {"any_banned": False, "matches": [], "total_tool_calls": 0,
           "computer_screenshots": 0, "banned_examples": []}
    if not chat_path.exists():
        out["error"] = "no-chat-jsonl"
        return out
    try:
        text = chat_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        out["error"] = f"read:{e}"
        return out
    for ln in text.splitlines():
        if not ln.strip(): continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if ev.get("type") != "message":
            continue
        m = ev.get("message", {})
        if m.get("role") != "assistant":
            continue
        for x in m.get("content", []) or []:
            if not isinstance(x, dict) or x.get("type") != "toolCall":
                continue
            out["total_tool_calls"] += 1
            name = x.get("name", "")
            args = x.get("arguments", {})
            args_blob = json.dumps(args, ensure_ascii=False)
            if name == "__computer__":
                act = args.get("action", {}) if isinstance(args, dict) else {}
                if isinstance(act, dict) and act.get("type") == "screenshot":
                    out["computer_screenshots"] += 1
            for sub in banned_substrings:
                if sub in args_blob:
                    if sub not in out["matches"]:
                        out["matches"].append(sub)
                    out["banned_examples"].append(args_blob[:200])
                    out["any_banned"] = True
    return out
