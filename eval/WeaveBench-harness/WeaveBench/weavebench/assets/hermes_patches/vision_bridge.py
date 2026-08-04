#!/usr/bin/env python3
"""WeaveBench vision-bridge patch for /opt/hermes (hermes 0.14.0).

Idempotent. Re-runs safely. Writes a marker file to avoid double-patching.

See weavebench/agents/hermes_agent.py:_render_vision_bridge_patch docstring for
the full rationale. tl;dr: hermes 0.14.0 has two design gaps that prevent
MCP-returned screenshots from reaching the LLM:

  1. _model_supports_vision() (run_agent.py:3185) returns False for
     any model not in models.dev metadata.
  2. _tool_result_content_for_active_model() (run_agent.py:3327) only
     recognises dict-shaped multimodal envelopes. MCP image blocks are
     cached to disk and the path is returned as a JSON STRING
     `{"result": "MEDIA:/root/.hermes/image_cache/img_xxx.png"}` —
     which never gets converted to image_url parts.

This script patches both methods in-place inside the VM, guarded by
env var WEAVEBENCH_HERMES_VISION_BRIDGE=1 (set by the run script).
"""
import os
import re
import sys

MARKER = "/opt/hermes/.weavebench_vision_bridge_patched"
RUN_AGENT = "/opt/hermes/run_agent.py"

if os.path.exists(MARKER):
    print("vision-bridge: already patched (marker present), skip")
    sys.exit(0)

if not os.path.isfile(RUN_AGENT):
    print(f"PATCH_FAIL: missing {RUN_AGENT}")
    sys.exit(1)

src = open(RUN_AGENT, encoding="utf-8").read()

# ---------------------------------------------------------------------
# Patch 1: _model_supports_vision — force True when opted in via env.
# Insert env-var early-return at top of the method body (preserves the
# original logic as fallback for the unset case).
# ---------------------------------------------------------------------
needle1 = (
    "    def _model_supports_vision(self) -> bool:\n"
    "        \"\"\"Return True if the active provider+model reports native vision.\n"
)
inject1 = (
    "    def _model_supports_vision(self) -> bool:\n"
    "        # vision-bridge: force True when opted in.\n"
    "        # Reason: many gateway-routed models are not in\n"
    "        # models.dev metadata, so the original lookup always returns\n"
    "        # False and the agent gets a blind chat history.\n"
    "        if os.environ.get('WEAVEBENCH_HERMES_VISION_BRIDGE') == '1':\n"
    "            return True\n"
    "        \"\"\"Return True if the active provider+model reports native vision.\n"
)
if needle1 not in src:
    print("PATCH_FAIL: marker for _model_supports_vision not found")
    sys.exit(2)
src = src.replace(needle1, inject1, 1)

# ---------------------------------------------------------------------
# Patch 2: _tool_result_content_for_active_model — detect MEDIA: in
# string results from MCP tools and convert to OpenAI-style content
# parts (text + image_url(data:URL)).
# ---------------------------------------------------------------------
needle2 = (
    "    def _tool_result_content_for_active_model(self, tool_name: str, result: Any) -> Any:\n"
    "        \"\"\"Return the tool message content"
)
# Note: re module is already imported at run_agent.py top; we use _re/_b64
# aliases to be paranoid about overshadowing.
inject2_lines = [
    "    def _tool_result_content_for_active_model(self, tool_name: str, result: Any) -> Any:",
    "        # vision-bridge: detect MEDIA:<path> in STRING",
    "        # tool results (returned by MCP servers whose ImageContent blocks",
    "        # are cached via tools/mcp_tool.py:_cache_mcp_image_block) and",
    "        # convert to OpenAI-style content parts so the downstream vision",
    "        # path fires.",
    "        if (",
    "            os.environ.get('WEAVEBENCH_HERMES_VISION_BRIDGE') == '1'",
    "            and isinstance(result, str)",
    "            and 'MEDIA:' in result",
    "        ):",
    "            import re as _re, base64 as _b64, os as _os",
    "            _media_paths = []",
    "            try:",
    "                _media_paths = _re.findall(",
    "                    r'MEDIA:([^\\s\"\\\\]+\\.(?:png|jpg|jpeg|gif|webp))',",
    "                    result,",
    "                )",
    "            except Exception:",
    "                pass",
    "            _image_parts = []",
    "            for _p in _media_paths:",
    "                try:",
    "                    if _os.path.isfile(_p):",
    "                        with open(_p, 'rb') as _f:",
    "                            _b = _f.read()",
    "                        _ext = _os.path.splitext(_p)[1].lower().lstrip('.')",
    "                        _mime = 'image/jpeg' if _ext in ('jpg', 'jpeg') else ('image/' + _ext)",
    "                        _data_url = ('data:' + _mime + ';base64,' +",
    "                                     _b64.b64encode(_b).decode('ascii'))",
    "                        _image_parts.append({",
    "                            'type': 'image_url',",
    "                            'image_url': {'url': _data_url},",
    "                        })",
    "                except Exception:",
    "                    continue",
    "            if _image_parts:",
    "                _text_only = _re.sub(",
    "                    r'MEDIA:[^\\s\"\\\\]+', '[image-attached-below]', result,",
    "                ).strip()",
    "                if not _text_only:",
    "                    _text_only = '(tool returned image content; see below)'",
    "                _parts = [{'type': 'text', 'text': _text_only}] + _image_parts",
    "                if self._model_supports_vision():",
    "                    return _parts",
    "                # else: fall through to original (text-only) behaviour",
    "        \"\"\"Return the tool message content",
]
inject2 = "\n".join(inject2_lines)

if needle2 not in src:
    print("PATCH_FAIL: marker for _tool_result_content_for_active_model not found")
    sys.exit(4)
src = src.replace(needle2, inject2, 1)

# ---------------------------------------------------------------------
# Write back atomically, then compile to verify no syntax errors.
# ---------------------------------------------------------------------
tmp = RUN_AGENT + ".weavebench-patching"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(src)
os.replace(tmp, RUN_AGENT)

import py_compile
try:
    py_compile.compile(RUN_AGENT, doraise=True)
except Exception as exc:  # noqa: BLE001
    print(f"PATCH_FAIL: syntax error after patch: {exc}")
    sys.exit(6)

with open(MARKER, "w") as f:
    f.write("patched by wcb hermes_agent vision-bridge v1\n")

print("vision-bridge: patched OK (2 inserts), marker written")
