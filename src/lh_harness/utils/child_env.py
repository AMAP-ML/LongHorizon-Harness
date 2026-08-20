"""Environment variables that must agree with a child's working directory.

``cwd=`` sets the real working directory of a spawned process, but part of the
world reads ``PWD`` instead of calling ``getcwd``.  A shell used to hide this:
``sh -c`` rewrites ``PWD`` from ``getcwd()`` when it starts, so the old
``cd <workspace> && ...`` command strings kept the two in agreement for free.
Executing argv directly removes that shell, and the launcher's ``PWD`` is then
inherited unchanged -- pointing at wherever the operator typed ``lh-harness``.

This is not cosmetic.  OpenCode resolves the directory its tools work in from
``PWD``, so a stale value sends an agent's writes outside the workspace the run
promised to contain them in, while every path in the transcript still looks
plausible.  ``OLDPWD`` is dropped for the same reason: a ``cd -`` in an agent's
shell would jump to a directory this run never chose.
"""

from __future__ import annotations

import os


def apply_working_directory(env: dict[str, str], cwd: str | os.PathLike[str] | None) -> dict[str, str]:
    """Make ``env`` describe ``cwd``, the directory the child is really started in.

    A ``cwd`` of ``None`` leaves ``env`` alone: the child then genuinely
    inherits ours, and the inherited ``PWD`` is already the right answer.
    """

    if cwd is None:
        return env
    env["PWD"] = os.path.abspath(os.fspath(cwd))
    # Removed rather than blanked, or `cd -` in an agent's shell would fail
    # confusingly instead of behaving like a fresh shell.
    env.pop("OLDPWD", None)
    return env
