"""Options for every git command the tool runs.

The `.git` folder is shared by the user's checkout and every agent's slot, and
any agent can write to it. Hooks, an fsmonitor command or symlinks planted
there would run, or be followed, with the tool. So the tool's own git runs no
hooks, no fsmonitor and makes no symlinks (DESIGN.md, "Security").
"""

from __future__ import annotations

import os

# Git's pathspec variables: they change what a pathspec matches, and git refuses `--literal-pathspecs` beside
# `GIT_GLOB_PATHSPECS` or `GIT_ICASE_PATHSPECS`. A call whose pathspecs a check rests on, or that passes
# `--literal-pathspecs`, runs without them.
PATHSPEC_VARIABLES = ("GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_LITERAL_PATHSPECS", "GIT_ICASE_PATHSPECS")


def pathspec_free_env() -> dict[str, str]:
    """This process's environment without git's pathspec variables."""
    return {k: v for k, v in os.environ.items() if k.upper() not in PATHSPEC_VARIABLES}


SAFE_GIT = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-c", "core.symlinks=false")
