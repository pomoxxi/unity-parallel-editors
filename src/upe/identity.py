"""Which agent is calling the tool, told apart by process.

Every agent runs as the same Windows user, so the tool cannot tell agents
apart by account. It can by process: an agent's app (claude.exe, codex.exe,
node.exe, ...) starts the shells that run `upe`. The nearest ancestor that is
not a shell, an interpreter or git counts as the agent.

The council uses this: a verdict from the same agent process as the task's
`land`, or as the other member, is refused. That is an agent reviewing its own
work, or a subagent it started inside its own process. It raises the bar
against an agent that votes on its own work by mistake or under prompt
injection; it is not a boundary against one that means to (DESIGN.md,
"Security").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable

# Processes between an agent and the tool: shells, interpreters, git, terminals' plumbing.
PLUMBING = {
    "python.exe", "pythonw.exe", "py.exe", "upe.exe", "uv.exe", "uvx.exe",
    "bash.exe", "sh.exe", "dash.exe", "zsh.exe", "env.exe", "timeout.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "openconsole.exe",
    "git.exe", "git-bash.exe", "mintty.exe", "winpty.exe", "winpty-agent.exe",
}

_QUERY = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,"
          "@{n='Created';e={$_.CreationDate.ToFileTimeUtc()}} | ConvertTo-Json -Compress")


def agent_process(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                  pid: int | None = None) -> dict | None:
    """The calling agent's process, as {"pid", "name", "created"}. None off Windows or if it cannot be told.

    A `--detach` job has no live parent chain; the command that started it
    puts its own answer in UPE_AGENT for it.
    """
    if pid is None and os.environ.get("UPE_AGENT"):
        return json.loads(os.environ["UPE_AGENT"])
    if sys.platform != "win32":
        return None
    completed = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
                       capture_output=True, text=True, errors="replace", timeout=60)
    if completed.returncode != 0:
        return None
    rows = json.loads(completed.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    by_pid = {int(r["ProcessId"]): r for r in rows}
    current, seen = by_pid.get(pid or os.getpid()), set()
    while current is not None and current["ProcessId"] not in seen:
        seen.add(current["ProcessId"])
        if (current.get("Name") or "").lower() not in PLUMBING:
            return {"pid": int(current["ProcessId"]), "name": current.get("Name") or "", "created": current.get("Created")}
        parent = by_pid.get(int(current.get("ParentProcessId") or 0))
        if parent is None or (parent.get("Created") or 0) > (current.get("Created") or 0):
            return None  # the parent exited and its id was reused: the chain is broken
        current = parent
    return None


def same(a: dict | None, b: dict | None) -> bool:
    return a is not None and b is not None and (a["pid"], a["created"]) == (b["pid"], b["created"])
