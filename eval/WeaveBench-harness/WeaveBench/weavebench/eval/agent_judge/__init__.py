"""eval.agent_judge — Agent-as-Judge pipeline for WeaveBench.

Replaces the official grader step with a multi-turn OpenClaw judge agent
that uses tools (read_file, view_image, grep) to inspect rollout deliverables
and produce 8-dim + per-artifact_check scores.

Architecture:
  stage_case.py     — assemble _eval/<case_id>/ from rollout dir
  judge_runner.py   — invoke openclaw + parse score.json output
  prompt_template.txt — judge agent system prompt
  run_one_aj.py     — drop-in replacement of the orchestrator run_one with
                      grader call swapped for agent-judge call

The host machine must have:
  - node 22+
  - openclaw installed under the path given by $AJ_OPENCLAW_BIN
  - a template profile prepared under $AJ_TEMPLATE_PROFILE
  - an OpenAI-compatible multimodal LLM gateway reachable
    (defaults to OpenRouter; see docs/AGENT_JUDGE.md)
"""
__version__ = "0.1.0"
