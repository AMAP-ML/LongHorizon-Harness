"""One declarative description of every agent backend the harness can drive.

Agent facts used to be spread across ``cli._AGENTS`` (choices and default
models), ``model_catalog`` (labels and availability), and a hardcoded fallback
list in the Web bundle.  Adding or correcting a backend meant editing all
three, and they drifted.  This module owns the facts; the others read them.

Availability is deliberately tri-state.  ``shutil.which`` answers "is there a
file with this name", which is not "can I drive this agent": a Microsoft Store
App Execution Alias resolves fine and fails on every real call.  An agent that
is on PATH but cannot run is worse than a missing one, because it looks healthy
while breaking every run, so it gets its own state instead of being folded into
either ``usable`` or ``missing``.
"""

from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from .types import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_DEEPSEEK_HARNESS_MODEL,
    DEFAULT_OPENCODE_MODEL,
)
from .utils.agent_cli import probe_agent_cli, resolve_agent_binary

Availability = Literal["usable", "found_but_broken", "missing"]


@dataclass(frozen=True)
class ReasoningSpec:
    """How one agent accepts a reasoning-effort selection.

    ``validation`` records what the backend does with a value it does not
    recognise, which is not cosmetic: Codex surfaces a provider 400 and the run
    fails with a readable reason, while Claude Code prints a warning and
    silently continues at its default effort.  The workbench must not promise
    the second case is verified.
    """

    transport: Literal["codex_config", "cli_flag"]
    flag: str
    scope: Literal["per_model", "per_agent"]
    source: Literal["model_catalog", "cli_help", "declared"]
    declared_choices: tuple[str, ...] = ()
    validation: Literal["provider_error", "silently_ignored"] = "provider_error"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    label: str
    binary: str
    default_model: str
    capabilities: frozenset[str]
    reasoning: ReasoningSpec | None = None


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="claude_code",
        label="Claude Code",
        binary="claude",
        default_model=DEFAULT_CLAUDE_MODEL,
        capabilities=frozenset({"cli", "gui", "mcp", "role_isolation"}),
        reasoning=ReasoningSpec(
            transport="cli_flag",
            flag="--effort",
            scope="per_agent",
            source="cli_help",
            declared_choices=("low", "medium", "high", "xhigh", "max"),
            validation="silently_ignored",
        ),
    ),
    AgentSpec(
        id="codex",
        label="Codex",
        binary="codex",
        default_model=DEFAULT_CODEX_MODEL,
        capabilities=frozenset({"cli", "gui", "mcp"}),
        reasoning=ReasoningSpec(
            transport="codex_config",
            flag="model_reasoning_effort",
            # Codex advertises the supported efforts per model, and the lists
            # are uneven: newer models add tiers older ones reject.
            scope="per_model",
            source="model_catalog",
            declared_choices=("minimal", "low", "medium", "high", "xhigh", "max"),
        ),
    ),
    AgentSpec(
        id="deepseek_harness",
        label="DeepSeek Harness (CLI)",
        binary="dsh",
        default_model=DEFAULT_DEEPSEEK_HARNESS_MODEL,
        capabilities=frozenset({"cli"}),
        reasoning=None,
    ),
    AgentSpec(
        id="opencode",
        label="OpenCode",
        binary="opencode",
        default_model=DEFAULT_OPENCODE_MODEL,
        capabilities=frozenset({"cli"}),
        reasoning=ReasoningSpec(
            transport="cli_flag",
            flag="--variant",
            scope="per_agent",
            # `opencode run --help` documents the variant as
            # provider-specific and only gives examples, so there is no
            # authoritative list to discover.
            source="declared",
            declared_choices=("minimal", "low", "medium", "high", "max"),
        ),
    ),
)

AGENT_IDS: tuple[str, ...] = tuple(spec.id for spec in AGENT_SPECS)
_SPECS_BY_ID = {spec.id: spec for spec in AGENT_SPECS}


def agent_spec(agent_id: str) -> AgentSpec:
    try:
        return _SPECS_BY_ID[agent_id]
    except KeyError:
        raise ValueError(f"Unknown agent: {agent_id}") from None


def supports_reasoning_effort(agent_id: str) -> bool:
    spec = _SPECS_BY_ID.get(agent_id)
    return spec is not None and spec.reasoning is not None


# The value reaches Codex as an inline TOML string and the other backends as an
# argv element.  Quoting is handled by the adapters; this rejects the
# characters that would end the TOML string or smuggle a newline regardless of
# quoting.  An allow-list of known tiers would be wrong: Codex accepts
# `ultra` client-side even though the API enumerates a shorter set, and
# operators must be able to pass values a future backend adds.
_EFFORT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def normalise_reasoning_effort(value: object, *, agent_id: str | None = None) -> str:
    """Validate a reasoning-effort value; ``""`` means follow the provider default."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("reasoning effort must be a string")
    effort = value.strip()
    if not effort:
        return ""
    if not _EFFORT_RE.match(effort):
        raise ValueError(
            "reasoning effort may only contain letters, digits, '.', '_', ':' or '-' "
            "and must be at most 64 characters"
        )
    if agent_id is not None and not supports_reasoning_effort(agent_id):
        raise ValueError(f"{agent_id} does not accept a reasoning effort")
    return effort


@dataclass(frozen=True)
class AgentProbe:
    agent_id: str
    availability: Availability
    binary: str = ""
    version: str = ""
    problem: str = ""
    # Efforts discovered from the CLI itself, when the spec says to look.
    discovered_efforts: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.availability == "usable"


# Installations do not change during a session, and probing runs a subprocess
# per agent, so this is cached far longer than the model catalogue.  The
# explicit refresh endpoint passes ``force``.
_PROBE_TTL_SECONDS = 300.0
_PROBE_TIMEOUT_SECONDS = 10
_probe_lock = Lock()
_probed_at = 0.0
_probed: dict[str, AgentProbe] | None = None
_probed_key: tuple[tuple[str, str], ...] | None = None


def probe_agents(
    *,
    force: bool = False,
    binaries: dict[str, str | None] | None = None,
) -> dict[str, AgentProbe]:
    """Probe every known agent CLI, in parallel, and cache the result.

    ``binaries`` overrides discovery per agent id.  Callers that already
    resolved a path (the Web API resolves once per request so its response is
    self-consistent) must be able to probe exactly that path; re-resolving here
    could otherwise report a different installation than the one a run uses.
    """

    global _probed, _probed_at, _probed_key
    overrides = {key: value for key, value in (binaries or {}).items() if key in _SPECS_BY_ID}
    targets = {
        spec.id: overrides.get(spec.id, _UNRESOLVED) if spec.id in overrides else _UNRESOLVED
        for spec in AGENT_SPECS
    }
    cache_key = tuple(
        (spec.id, "" if targets[spec.id] is _UNRESOLVED else str(targets[spec.id] or ""))
        for spec in AGENT_SPECS
    )
    now = time.monotonic()
    with _probe_lock:
        if (
            not force
            and _probed is not None
            and _probed_key == cache_key
            and now - _probed_at < _PROBE_TTL_SECONDS
        ):
            return dict(_probed)
        # Probing four CLIs serially is dominated by the slowest one, which is
        # long enough to be visible on the workbench's first paint.
        with ThreadPoolExecutor(max_workers=max(1, len(AGENT_SPECS))) as pool:
            results = list(
                pool.map(lambda spec: _probe_one(spec, targets[spec.id]), AGENT_SPECS)
            )
        probed = {probe.agent_id: probe for probe in results}
        _probed = probed
        _probed_key = cache_key
        _probed_at = time.monotonic()
        return dict(probed)


_UNRESOLVED = object()


def _probe_one(spec: AgentSpec, target: object = _UNRESOLVED) -> AgentProbe:
    try:
        if target is _UNRESOLVED:
            cli = probe_agent_cli(spec.binary, timeout=_PROBE_TIMEOUT_SECONDS)
        elif not target:
            return AgentProbe(spec.id, "missing", problem=f"`{spec.binary}` was not found")
        else:
            cli = probe_agent_cli(
                spec.binary, timeout=_PROBE_TIMEOUT_SECONDS, path=str(target)
            )
    except Exception as exc:  # probing must never break metadata for one agent
        return AgentProbe(spec.id, "missing", problem=f"probe failed: {exc}")
    if not cli.found:
        return AgentProbe(spec.id, "missing", problem=cli.problem)
    if not cli.usable:
        return AgentProbe(
            spec.id, "found_but_broken", binary=cli.path, problem=cli.problem
        )
    efforts: tuple[str, ...] = ()
    if spec.reasoning is not None and spec.reasoning.source == "cli_help":
        efforts = _efforts_from_cli_help(cli.path, spec.reasoning.flag)
    return AgentProbe(
        spec.id,
        "usable",
        binary=cli.path,
        version=cli.version,
        discovered_efforts=efforts,
    )


def reasoning_choices(spec: AgentSpec, probe: AgentProbe | None) -> tuple[str, ...]:
    """Resolve the effort choices to offer, preferring what the CLI reported."""

    if spec.reasoning is None:
        return ()
    if probe is not None and probe.discovered_efforts:
        return probe.discovered_efforts
    return spec.reasoning.declared_choices


# `claude --help` wraps the tier list onto a continuation line:
#     --effort <level>        Effort level for the current session
#                             (low, medium, high, xhigh, max)
# so the text after the flag is flattened before the parenthesised list is read.
_HELP_TIER_RE = re.compile(r"\(([^()]{2,200})\)")
_HELP_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _efforts_from_cli_help(binary: str, flag: str) -> tuple[str, ...]:
    help_text = _cli_help_text(binary)
    index = help_text.find(flag)
    if index < 0:
        return ()
    window = " ".join(help_text[index : index + 400].split())
    match = _HELP_TIER_RE.search(window)
    if not match:
        return ()
    values: list[str] = []
    for token in match.group(1).split(","):
        candidate = token.strip().lower()
        if _HELP_TOKEN_RE.match(candidate) and candidate not in values:
            values.append(candidate)
    # A single word in parentheses is prose ("(default)"), not a tier list.
    return tuple(values) if len(values) > 1 else ()


def _cli_help_text(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def resolve_agent_binary_for(agent_id: str) -> str | None:
    return resolve_agent_binary(agent_spec(agent_id).binary)
