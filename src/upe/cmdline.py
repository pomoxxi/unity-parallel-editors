"""Command lines that grow with a land.

Windows refuses a command line past 32,767 characters (WinError 206). Every list a land can make long (changed paths,
test classes, GUIDs, files to restore) goes onto a command line through `capped`, in groups, or is a measured
`BOUNDED` entry of `tests/test_argv_capped.py`, which reads `src` and fails on a new one. It lives in a module of its
own because `checks.py` and `workspace.py` need it and cannot import `flow.py` (which imports both).
"""

from __future__ import annotations

import subprocess
from typing import Iterator, Sequence

# The characters of paths or patterns one call is given: the rest of the call (the program, its options, the folder, a
# log path) stays far under 8,000.
ARGV_CAP = 24_000


def cost(item: str) -> int:
    """What one item adds to a Windows command line: its text as `subprocess.list2cmdline` writes it (an item with a
    space or a tab is quoted, +2; a quote or the backslashes before one are escaped), and one separator. Equal to
    `len(item) + 1` for an item with none of those, so a `";".join` of a group is at most the cap."""
    return len(subprocess.list2cmdline([item])) + 1


def capped(items: Sequence[str], cap: int = ARGV_CAP) -> Iterator[list[str]]:
    """`items` in order, in groups whose costs add up to at most `cap`; an item longer than the cap is a group of its
    own."""
    group: list[str] = []
    size = 0
    for item in items:
        step = cost(item)
        if group and size + step > cap:
            yield group
            group, size = [], 0
        group.append(item)
        size += step
    if group:
        yield group
