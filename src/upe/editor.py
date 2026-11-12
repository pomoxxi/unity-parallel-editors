"""Batch runs of the Unity Editor itself, for work the `unity` CLI does not cover.

Used for `-executeMethod` entry points in the tool's package (the scene
check) and for letting Unity rewrite `packages-lock.json` after a merge.
These runs need no Pipeline package and no Editor already open. Like the
CLI, they refuse a project whose Editor is open.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .workspace import lock_state

Runner = Callable[..., subprocess.CompletedProcess]


class EditorError(Exception):
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
    def __init__(self, exe: Path | str, runner: Runner = subprocess.run, job_workers: int | None = 4):
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
        timeout: float = 3600.0,
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
        completed = self._runner(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
        return EditorRun(tuple(argv), completed.returncode, log)

    def execute_method(
        self, project: Path | str, method: str, log: Path | str, args: Sequence[str] = (),
        graphics: bool = False, timeout: float = 3600.0,
    ) -> EditorRun:
        """Run one static method. The method ends the Editor itself with EditorApplication.Exit."""
        return self.batch(project, log, "-executeMethod", method, *args, quit=False, graphics=graphics, timeout=timeout)

    def refresh(self, project: Path | str, log: Path | str, timeout: float = 3600.0) -> EditorRun:
        """Open and close the project once, so Unity imports and rewrites derived files."""
        return self.batch(project, log, quit=True, timeout=timeout)


def compile_errors(log_text: str) -> list[str]:
    """Compiler errors from an Editor log, one line each."""
    return sorted(set(re.findall(r"^.*error CS\d{4}:.*$", log_text, re.MULTILINE)))
