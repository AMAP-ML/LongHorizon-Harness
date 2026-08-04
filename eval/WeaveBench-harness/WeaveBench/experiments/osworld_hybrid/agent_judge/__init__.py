"""eval.agent_judge — Agent-as-Judge pipeline for EyesOn-Bench.

Replaces the official grader step with a multi-turn OpenClaw judge agent
that uses tools (read_file, view_image, grep) to inspect rollout deliverables
and produce 8-dim + per-artifact_check scores.

Architecture:
  stage_case.py         — assemble _eval/<case_id>/ from rollout dir
  judge_runner.py       — invoke openclaw + parse score.json output
  prompt_template.txt   — judge agent system prompt
  run_bench_gen_aj.py   — drop-in replacement of run_bench_gen.py with
                          grader call swapped for agent-judge call

The host machine must have:
  - node 22+
  - openclaw npm package installed in a profile (~/.openclaw-judge/)
  - cop-api 4141 (gpt-5.5 multimodal) reachable
"""
__version__ = "0.1.0"
