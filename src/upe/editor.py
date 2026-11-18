"""Batch runs of the Unity Editor itself, for work the `unity` CLI does not cover.

Used for `-executeMethod` entry points in the tool's package (the scene
check) and for letting Unity rewrite `packages-lock.json` after a merge.
These runs need no Pipeline package and no Editor already open. Like the
CLI, they refuse a project whose Editor is open.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .workspace import lock_state

Runner = Callable[..., subprocess.CompletedProcess]

# How long a batch Editor may run. A run past it is ended with every process it started (`run_tree`).
BATCH_TIMEOUT_S = 3600.0


class EditorError(Exception):
    pass


# Shared by every Editor of the user, and left running by a normal quit too: a tree kill spares it.
SHARED_PROCESSES = frozenset({"unity.licensing.client.exe"})
KILL_WAIT_S = 60.0  # how long a killed Editor gets to be gone before the timeout is raised anyway


def tree(root: int, table: Sequence[tuple[int, int, str]], created: Callable[[int], int | None]) -> list[int]:
    """`root` and every process it started, root first, from a table of (pid, parent pid, exe name). A process
    older than the parent it names is not its child: Windows gave a dead parent's id to a new process."""
    found = [root]
    added = True
    while added:
        added = False
        for pid, parent, name in table:
            if pid in found or parent not in found or name.lower() in SHARED_PROCESSES:
                continue
            born, parent_born = created(pid), created(parent)
            if born is not None and parent_born is not None and born < parent_born:
                continue
            found.append(pid)
            added = True
    return found


def _process_table() -> list[tuple[int, int, str]]:
    """Every process now: (pid, parent pid, exe name). Empty off Windows or when the snapshot fails."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    table = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while more:
            table.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            more = kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return table


def kill_tree(root: int) -> list[int]:
    """End `root` and every process it started (asset import workers, the shader compiler, the package manager),
    but the shared ones. Killing `root` alone leaves its children running: Windows does not end them with it.
    Returns the ids it tried to end."""
    from .flow import process_created  # flow imports this module

    pids = tree(root, _process_table(), process_created)
    for pid in pids:  # root first, so it starts no new child while the rest are ended
        try:
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        except OSError:
            pass  # already gone
    return pids


def run_tree(argv: Sequence[str], *, timeout: float | None = None, **kw) -> subprocess.CompletedProcess:
    """`subprocess.run` for a batch Editor. Past `timeout`, the Editor and every process it started are ended,
    the wait for them is bounded by KILL_WAIT_S, and TimeoutExpired is raised. Any other exception in the wait
    (Ctrl-C too) ends them the same way, as `subprocess.run` ended its child, so no Editor keeps the slot."""
    proc = subprocess.Popen(argv, **kw)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _end_tree(proc)
        raise subprocess.TimeoutExpired(argv, timeout) from None
    except BaseException:
        _end_tree(proc)
        raise
    return subprocess.CompletedProcess(argv, code)


def _end_tree(proc: subprocess.Popen) -> None:
    kill_tree(proc.pid)
    proc.kill()  # if the walk could not see it
    try:
        proc.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def project_version(project: Path) -> str:
    text = (project / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8")
    match = re.search(r"^m_EditorVersion:\s*(\S+)", text, re.MULTILINE)
    if not match:
        raise EditorError(f"no m_EditorVersion in {project}/ProjectSettings/ProjectVersion.txt")
    return match.group(1)


def find_editor(project: Path, env: Mapping[str, str] | None = None) -> Path:
    """The Editor the user named in UPE_UNITY_EDITOR. The tool looks nowhere the user has not named."""
    env = os.environ if env is None else env
    if env.get("UPE_UNITY_EDITOR"):
        return Path(env["UPE_UNITY_EDITOR"])
    raise EditorError(f"no Unity Editor named: the project needs Unity {project_version(project)}; give its "
                      f"Unity.exe as unity_editor in upe.toml, or set UPE_UNITY_EDITOR")


def yaml_merge_for(editor: Path) -> Path:
    """UnityYAMLMerge as it ships with an Editor: <Editor folder>/Data/Tools/UnityYAMLMerge.exe."""
    return Path(editor).parent / "Data" / "Tools" / "UnityYAMLMerge.exe"


@dataclass(frozen=True)
class EditorRun:
    argv: tuple[str, ...]
    exit_code: int
    log: Path


class Editor:
    def __init__(self, exe: Path | str, runner: Runner = run_tree, job_workers: int | None = 4):
        self.exe = Path(exe)
        self._runner = runner
        self.job_workers = job_workers  # caps threads per Editor when several run at once

    def batch(
        self,
        project: Path | str,
        log: Path | str,
        *args: str,
        quit: bool = True,
        graphics: bool = False,
        timeout: float = BATCH_TIMEOUT_S,
    ) -> EditorRun:
        """Run a batch Editor. `graphics` keeps a GPU device; loading scenes needs one."""
        project = Path(project)
        if lock_state(project) == "held":
            raise EditorError(f"{project} is open in an Editor")
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [str(self.exe), "-batchmode", *([] if graphics else ["-nographics"]), "-accept-apiupdate",
                "-projectPath", str(project), "-logFile", str(log)]
        if self.job_workers:
            argv += ["-job-worker-count", str(self.job_workers)]
        argv += list(args)
        if quit:
            argv.append("-quit")
        # No output pipes: Unity writes to -logFile, and a pipe a child of the Editor keeps open would hold the wait.
        completed = self._runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=timeout)
        return EditorRun(tuple(argv), completed.returncode, log)

    def execute_method(
        self, project: Path | str, method: str, log: Path | str, args: Sequence[str] = (),
        graphics: bool = False, timeout: float = BATCH_TIMEOUT_S,
    ) -> EditorRun:
        """Run one static method. The method ends the Editor itself with EditorApplication.Exit."""
        return self.batch(project, log, "-executeMethod", method, *args, quit=False, graphics=graphics, timeout=timeout)

    def refresh(self, project: Path | str, log: Path | str, timeout: float = BATCH_TIMEOUT_S) -> EditorRun:
        """Open and close the project once, so Unity imports and rewrites derived files."""
        return self.batch(project, log, quit=True, timeout=timeout)


def compile_errors(log_text: str) -> list[str]:
    """Compiler errors from an Editor log, one line each."""
    return sorted(set(re.findall(r"^.*error CS\d{4}:.*$", log_text, re.MULTILINE)))
