"""Tests for the four-harness max_steps alignment.

openclaw already had a real step-cap watchdog; codex/claudecode/hermes used to
accept max_steps but never enforce it. These tests assert each harness now
renders a runner script that actually limits steps:
  - claudecode: native `--max-turns N` flag on both call branches
  - codex/hermes: a background watchdog that counts steps and kills the CLI
    once it reaches max_steps * (1 + max_refusal_retries)
"""
from __future__ import annotations

from weavebench.agents.codex_agent import CodexAgent
from weavebench.agents.claudecode_agent import ClaudeCodeAgent
from weavebench.agents.cua_harness_claudecode_agent import CuaHarnessClaudeCodeAgent
from weavebench.agents.hermes_agent import HermesAgent


def _kw():
    return dict(model="gpt-5.5", litellm_base_url="http://x/v1",
                litellm_api_key="k")


# --- claudecode: native --max-turns -----------------------------------------

def test_claudecode_injects_max_turns_both_branches():
    a = ClaudeCodeAgent(max_steps=7, **_kw())
    s = a._render_run_script(None)
    # one for the first call, one for the resume branch
    assert s.count("--max-turns 7") == 2


def test_claudecode_writes_real_step_marker():
    a = ClaudeCodeAgent(max_steps=7, **_kw())
    s = a._render_run_script(None)
    # runner emits a real MAX_STEPS_REACHED marker derived from claude's result
    assert "MAX_STEPS_REACHED=$STEPS_CAPPED" in s
    assert "error_max_turns" in s
    assert "turns >= 7" in s


def test_claudecode_exports_disable_interleaved_thinking():
    a = ClaudeCodeAgent(max_steps=7, **_kw())
    s = a._render_run_script(None)
    assert "export DISABLE_INTERLEAVED_THINKING=1" in s
    assert "unset DISABLE_INTERLEAVED_THINKING" not in s


def test_cua_harness_claudecode_leaves_role_turns_unlimited():
    a = CuaHarnessClaudeCodeAgent(max_steps=7, **_kw())
    s = a._render_run_script(None)
    assert "--max-turns" not in s
    assert "export DISABLE_INTERLEAVED_THINKING=1" in s
    assert "MAX_STEPS_REACHED=0 max_turns=unlimited" in s
    assert "MAX_STEPS_REACHED=$STEPS_CAPPED" not in s


# --- codex: watchdog ---------------------------------------------------------

def test_codex_watchdog_cap_formula():
    # default max_refusal_retries=3 -> cap = 5 * (1+3) = 20
    a = CodexAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert "WATCHDOG_CAP=20" in s


def test_codex_counts_function_calls():
    a = CodexAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert '"type":"function_call"' in s
    assert "/root/.codex/sessions" in s


def test_codex_kills_codex_and_breaks_on_cap():
    a = CodexAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert "@openai/codex" in s          # pkill target
    # must stop the retry loop when the watchdog tripped (else OTHER_FAIL re-runs)
    assert '[ -f "$STEPS_CAPPED" ] && break' in s
    assert "kill $WATCH_PID" in s


def test_codex_emits_step_marker_to_log():
    a = CodexAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert "MAX_STEPS_REACHED=1 cap=" in s
    assert "MAX_STEPS_REACHED=0 cap=" in s


# --- hermes: watchdog counting request_dump files ---------------------------

def test_hermes_watchdog_cap_formula():
    a = HermesAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert "WATCHDOG_CAP=20" in s


def test_hermes_counts_assistant_in_session_json():
    a = HermesAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    # counts "role":"assistant" in the incrementally-written session_*.json —
    # same unit openclaw counts (verified on a real VM: hermes produces
    # session_*.json, NOT request_dump_*.json).
    assert '"role": *"assistant"' in s
    assert "session_*.json" in s
    assert "request_dump" not in s  # the disproven assumption must be gone


def test_hermes_kills_hermes_and_breaks_on_cap():
    a = HermesAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert "/opt/hermes/.venv/bin/hermes" in s   # pkill target
    assert '[ -f "$STEPS_CAPPED" ] && break' in s
    assert "kill $WATCH_PID" in s


def test_hermes_emits_step_marker_to_log():
    a = HermesAgent(max_steps=5, **_kw())
    s = a._render_run_script()
    assert "MAX_STEPS_REACHED=1 cap=" in s


# --- cross-harness: cap scales with max_steps -------------------------------

def test_cap_scales_with_max_steps():
    for C, render in ((CodexAgent, "_render_run_script"),
                      (HermesAgent, "_render_run_script")):
        a = C(max_steps=10, **_kw())  # default retries=3 -> cap=40
        s = getattr(a, render)()
        assert "WATCHDOG_CAP=40" in s
