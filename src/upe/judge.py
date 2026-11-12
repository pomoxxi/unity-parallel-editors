"""The priority judge (DESIGN.md decision 6): shares the machine's CPU among the running Editors.

What it reads:
- each session's declared activity (`upe activity <task> <kind>`), since only
  the agent knows it is about to render or enter PlayMode;
- every Unity process on the machine, with the project it has open, its
  memory and its CPU use, children (import workers, shader compilers)
  counted with their Editor;
- the machine's total and free memory and its core count.

How it decides: fixed rules (`Judge`), not a model, give each session a
priority, `high`, `normal` or `low`, with a reason. Heavy work is high, at
most `max_high` sessions at once.

How a decision is applied: the Windows priority class of every process of
the session. `normal` is `editor_priority`, BELOW_NORMAL by default, so the
tool's Editors give way to the user's own programs; `high` is one class
above it and `low` one below (NORMAL and IDLE by default). That only
matters when the CPU is contended, which is when it should.
HIGH and REALTIME are never used. Memory is not capped: a capped Editor
crashes. Only processes of the tool's own worktrees are touched; an Editor
the user opened on another project is reported and left alone.

The numbers (how many may be high, when memory is short) are config values,
with defaults from research/05-measurements.md.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from .workspace import TASK_ID
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

ACTIVITIES = ("idle", "editing", "importing", "compiling", "testing", "playmode", "rendering", "lighting", "building")
HEAVY = ("playmode", "rendering", "lighting", "building")
PRIORITIES = ("high", "normal", "low")

# Windows priority classes, lowest first. HIGH and REALTIME are left out on purpose.
IDLE, BELOW_NORMAL, NORMAL, ABOVE_NORMAL = 0x40, 0x4000, 0x20, 0x8000
LADDER = (IDLE, BELOW_NORMAL, NORMAL, ABOVE_NORMAL)
BASES = {"below_normal": BELOW_NORMAL, "normal": NORMAL}


def priority_classes(base: str = "normal") -> dict[str, int]:
    """The class for each priority: `normal` is the base, `high` one step up, `low` one step down.

    With the base at below_normal (the default, `editor_priority`), the tool's
    Editors never outrank the user's own programs, and still rank among
    themselves.
    """
    i = LADDER.index(BASES[base])
    return {"high": LADDER[i + 1], "normal": LADDER[i], "low": LADDER[i - 1]}


PRIORITY_CLASS = priority_classes("normal")  # ABOVE_NORMAL, NORMAL, BELOW_NORMAL

class JudgeError(Exception):
    pass


# Activity ---------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def declare(state_dir: Path, session: str, activity: str, note: str = "") -> dict[str, str]:
    """Record what a session is doing. Agents call this before and after heavy work."""
    if activity not in ACTIVITIES:
        raise JudgeError(f"activity must be one of {', '.join(ACTIVITIES)}")
    if not TASK_ID.match(session):  # it names a file in the tool's state
        raise JudgeError(f"session {session!r} is not a task id")
    record = {"activity": activity, "note": note, "since": _now()}
    path = state_dir / "activity" / f"{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return record


def activities(state_dir: Path) -> dict[str, dict[str, str]]:
    folder = state_dir / "activity"
    if not folder.is_dir():
        return {}
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))}


# Processes --------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    pid: int
    parent: int
    name: str
    command_line: str
    rss: int  # bytes
    cpu_s: float  # total kernel + user time


_PROJECT_PATH = re.compile(r"-projectpath\s+(?:\"([^\"]+)\"|(\S+))", re.IGNORECASE)


def project_of(command_line: str) -> Path | None:
    match = _PROJECT_PATH.search(command_line or "")
    return Path(match.group(1) or match.group(2)) if match else None


_PS_QUERY = (
    "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine,"
    "WorkingSetSize,KernelModeTime,UserModeTime | ConvertTo-Json -Compress"
)


def list_processes(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[Proc]:
    """Every process on the machine (Windows, via CIM)."""
    completed = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_QUERY],
                       capture_output=True, text=True, errors="replace", timeout=60)
    if completed.returncode != 0:
        raise JudgeError(f"process listing failed: {(completed.stderr or '').strip()[:500]}")
    rows = json.loads(completed.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    return [
        Proc(int(r["ProcessId"]), int(r.get("ParentProcessId") or 0), r.get("Name") or "", r.get("CommandLine") or "",
             int(r.get("WorkingSetSize") or 0), (int(r.get("KernelModeTime") or 0) + int(r.get("UserModeTime") or 0)) / 1e7)
        for r in rows
    ]


def editor_trees(procs: Sequence[Proc]) -> dict[Path, list[Proc]]:
    """Each open project and the processes that belong to it: its Editors and all their children."""
    children: dict[int, list[Proc]] = {}
    for p in procs:
        children.setdefault(p.parent, []).append(p)
    trees: dict[Path, list[Proc]] = {}
    for p in procs:
        project = project_of(p.command_line) if p.name.lower() == "unity.exe" else None
        if project is None:
            continue
        key = _norm(project)
        tree = trees.setdefault(key, [])
        todo = [p]
        while todo:
            q = todo.pop()
            if all(q.pid != t.pid for t in tree):
                tree.append(q)
                todo += children.get(q.pid, [])
    return trees


def _norm(path: Path | str) -> Path:
    return Path(os.path.normcase(os.path.abspath(str(path))))


# Machine ----------------------------------------------------------------


@dataclass(frozen=True)
class Machine:
    cores: int
    total_ram: int
    free_ram: int


def machine() -> Machine:
    if sys.platform != "win32":
        return Machine(os.cpu_count() or 1, 0, 0)

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return Machine(os.cpu_count() or 1, status.ullTotalPhys, status.ullAvailPhys)


# Snapshot ---------------------------------------------------------------


@dataclass
class Session:
    session: str  # task id, "merge:<task id>", "base:<task id>" (a land's run on the target) or "golden"
    project: str
    activity: str
    note: str
    since: str
    pids: list[int]
    rss_mb: int
    cpu_percent: float  # of one core; 400 means four cores busy


@dataclass
class Snapshot:
    taken: str
    cores: int
    total_ram_mb: int
    free_ram_mb: int
    sessions: list[Session]
    other_editors: list[str] = field(default_factory=list)  # projects the tool does not own; never touched

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def snapshot(
    known: Mapping[str, Path],
    declared: Mapping[str, Mapping[str, str]],
    lister: Callable[[], list[Proc]],
    machine_info: Callable[[], Machine] = machine,
    interval_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Snapshot, dict[str, list[int]]]:
    """Two process samples `interval_s` apart, matched to the tool's sessions by project path."""
    first = {p.pid: p.cpu_s for p in lister()}
    sleep(interval_s)
    procs = lister()
    trees = editor_trees(procs)
    by_path = {_norm(path): name for name, path in known.items()}
    m = machine_info()
    sessions, pids, others = [], {}, []
    for project, tree in sorted(trees.items(), key=lambda kv: str(kv[0])):
        name = by_path.get(project)
        if name is None:
            others.append(str(project))
            continue
        cpu = sum(max(0.0, p.cpu_s - first.get(p.pid, p.cpu_s)) for p in tree) / interval_s * 100
        act = declared.get(name, {})
        default = "importing" if name == "golden" else "testing" if name.startswith(("merge:", "base:")) else "editing"
        sessions.append(Session(name, str(project), act.get("activity", default), act.get("note", ""),
                                act.get("since", ""), sorted(p.pid for p in tree),
                                sum(p.rss for p in tree) // 2**20, round(cpu, 1)))
        pids[name] = sorted(p.pid for p in tree)
    snap = Snapshot(_now(), m.cores, m.total_ram // 2**20, m.free_ram // 2**20, sessions, others)
    return snap, pids


# Decision ---------------------------------------------------------------


@dataclass
class Decision:
    priorities: dict[str, str]
    reasons: dict[str, str]
    summary: str = ""


class Judge:
    """Rules, not a model: the tool calls no model (DESIGN.md decision 11).

    - A session doing heavy Editor work (playmode, rendering, lighting,
      building) is high, because someone is waiting on it. At most `max_high`
      are; among more, the one that declared first wins.
    - While a session is heavy, idle and background sessions (importing,
      compiling, testing) are low, and editing sessions normal.
    - With no heavy session, every session is normal, except that idle ones
      are low when memory is short (free memory below `memory_short_mb`).
    """

    def __init__(self, max_high: int = 1, memory_short_mb: int = 4096):
        self.max_high, self.memory_short_mb = max_high, memory_short_mb

    def decide(self, snap: Snapshot) -> Decision:
        if not snap.sessions:
            return Decision({}, {}, "no sessions running")
        decision = Decision({}, {})
        heavy = sorted((s for s in snap.sessions if s.activity in HEAVY), key=lambda s: s.since or "~")
        high = {s.session for s in heavy[:self.max_high]}
        short = snap.free_ram_mb < self.memory_short_mb
        for s in snap.sessions:
            if s.session in high:
                priority, reason = "high", f"{s.activity}: someone is waiting on it"
            elif s.activity in HEAVY:
                priority, reason = "normal", f"{s.activity}, but at most {self.max_high} high; it declared later"
            elif heavy and s.activity != "editing":
                priority, reason = "low", f"{s.activity} gives way to the heavy session"
            elif short and s.activity == "idle":
                priority, reason = "low", f"idle, and memory is short ({snap.free_ram_mb} MB free)"
            else:
                priority, reason = "normal", s.activity
            decision.priorities[s.session], decision.reasons[s.session] = priority, reason
        decision.summary = (f"{len(heavy)} heavy, {len(high)} high" + ("; memory short" if short else ""))
        return decision


# Applying ---------------------------------------------------------------


def set_priority_class(pid: int, priority_class: int) -> None:
    if sys.platform != "win32":
        raise JudgeError("process priorities are set only on Windows")
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0200 | 0x1000, False, pid)  # PROCESS_SET_INFORMATION | QUERY_LIMITED
    if not handle:
        raise JudgeError(f"cannot open process {pid} (error {kernel32.GetLastError()})")
    try:
        if not kernel32.SetPriorityClass(handle, priority_class):
            raise JudgeError(f"cannot set the priority of process {pid} (error {kernel32.GetLastError()})")
    finally:
        kernel32.CloseHandle(handle)


def apply(decision: Decision, pids: Mapping[str, Sequence[int]],
          setter: Callable[[int, int], None] = set_priority_class,
          classes: Mapping[str, int] = PRIORITY_CLASS) -> list[str]:
    """Set every process of each session to its priority. Returns the failures; a process may have exited."""
    failures = []
    for name, priority in decision.priorities.items():
        for pid in pids.get(name, ()):
            try:
                setter(pid, classes[priority])
            except JudgeError as exc:
                failures.append(str(exc))
    return failures
