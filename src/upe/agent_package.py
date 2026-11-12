"""The tool's own Unity code, put into a worktree without ever reaching the project's history.

The project's repo is not changed for the tool (DESIGN.md decision 7). Two things
go into each worktree locally:

1. `com.upe.agent` (this repo's `unity/com.upe.agent`) is copied in as an
   embedded package under `Packages/`. Unity loads embedded packages without
   a manifest entry. The folder is listed in the repo's `info/exclude`, so
   git never sees it.
2. `com.unity.pipeline`, which the `unity` CLI needs to talk to an Editor,
   is added with `unity pipeline install`. That edits `manifest.json`.

Unity also writes both packages into `packages-lock.json`. Both files are
then marked skip-worktree in that worktree's own index, so its local edits
are never staged or committed. `scrub_lock` removes the two entries from a
lock file that is about to be committed (after a merge regenerates it).

Unity's IDE integration writes two more files on every open, named after the
worktree's folder: `.vscode/settings.json` gets `"dotnet.defaultSolution":
"<folder>.slnx"`, and `<folder>.slnx` is written next to it. A project may track
its own `.slnx` files, so only this folder's one is excluded, and the
settings file is skip-worktree like the package files (research/05).

UNVERIFIED on a real machine: what `unity pipeline install` writes, and that
the lock gets an entry for the embedded package (a project's lock lists its other
embedded packages).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .unity_cli import UnityCli
from .safegit import SAFE_GIT

Runner = Callable[..., subprocess.CompletedProcess]

PACKAGE_NAME = "com.upe.agent"
PIPELINE = "com.unity.pipeline"
LOCAL_ONLY = ("Packages/manifest.json", "Packages/packages-lock.json", ".vscode/settings.json")
PACKAGE_SOURCE = Path(__file__).resolve().parents[2] / "unity" / PACKAGE_NAME
EXCLUDE_LINE = f"/Packages/{PACKAGE_NAME}/"


def exclude_lines(worktree: Path) -> list[str]:
    """What the worktree's local files need in `info/exclude`, which every worktree of the repo shares."""
    return [EXCLUDE_LINE, f"/{worktree.name}.slnx"]


class AgentPackageError(Exception):
    pass


def _git(worktree: Path, *args: str, runner: Runner = subprocess.run) -> str:
    completed = runner(["git", *SAFE_GIT, "-C", str(worktree), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise AgentPackageError(f"git {' '.join(args)} failed: {(completed.stderr or '').strip()}")
    return completed.stdout or ""


def exclude_file(worktree: Path) -> Path:
    common = Path(_git(worktree, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = worktree / common
    return common / "info" / "exclude"


def install(worktree: Path | str, cli: UnityCli | None = None, source: Path = PACKAGE_SOURCE) -> None:
    """Put the helper package (and, given a CLI, the Pipeline package) into one worktree."""
    worktree = Path(worktree)
    if not source.is_dir():
        raise AgentPackageError(f"package source not found at {source}")

    exclude = exclude_file(worktree)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    missing = [line for line in exclude_lines(worktree) if line not in lines]
    if missing:
        exclude.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")

    dest = worktree / "Packages" / PACKAGE_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)

    present = [p for p in LOCAL_ONLY if (worktree / p).exists()]
    if present:
        _git(worktree, "update-index", "--skip-worktree", "--", *present)

    if cli is not None and PIPELINE not in _dependencies(worktree / "Packages" / "manifest.json"):
        result = cli.pipeline_install(worktree)
        if not result.ok:
            raise AgentPackageError(f"unity pipeline install failed: {result.error_code or result.exit_code}")


def uninstall(worktree: Path | str) -> None:
    """Take both packages out again and put the local-only files back to the committed version."""
    worktree = Path(worktree)
    dest = worktree / "Packages" / PACKAGE_NAME
    if dest.exists():
        shutil.rmtree(dest)
    tracked = [p for p in LOCAL_ONLY if _git(worktree, "ls-files", "--", p).strip()]
    if tracked:
        _git(worktree, "update-index", "--no-skip-worktree", "--", *tracked)
        _git(worktree, "checkout", "HEAD", "--", *tracked)


def local_edits_hidden(worktree: Path | str) -> list[str]:
    """The local-only files whose local edits git is currently told to ignore."""
    out = _git(Path(worktree), "ls-files", "-v", "--", *LOCAL_ONLY)
    return [line[2:] for line in out.splitlines() if line.startswith("S ")]


def scrub_lock(lock_text: str, committed_manifest: str) -> str:
    """Drop the tool's packages, and anything only they pulled in, from a lock file.

    Keeps every entry reachable from the project's committed manifest or from
    the project's own embedded packages.
    """
    lock = json.loads(lock_text)
    deps: dict = lock.get("dependencies", {})
    roots = set(_dependencies_text(committed_manifest))
    roots |= {n for n, e in deps.items() if e.get("source") == "embedded" and n != PACKAGE_NAME}
    reachable, todo = set(), [r for r in roots if r in deps]
    while todo:
        name = todo.pop()
        if name in reachable:
            continue
        reachable.add(name)
        todo += [d for d in (deps[name].get("dependencies") or {}) if d in deps]
    lock["dependencies"] = {n: e for n, e in deps.items() if n in reachable}
    return json.dumps(lock, indent=2) + "\n"


def allow_manifest_edits(worktree: Path | str) -> None:
    """For a task that claims manifest.json: lift the mark so its package changes get committed.

    The tool's own manifest entry goes along with them; `strip_tool_packages`
    takes it out again when the branch is merged.
    """
    _git(Path(worktree), "update-index", "--no-skip-worktree", "--", LOCAL_ONLY[0])


def strip_tool_packages(manifest_text: str, target_manifest_text: str) -> str | None:
    """The manifest without the tool's packages, unless the target has them too. None if none are there."""
    data = json.loads(manifest_text)
    deps = data.get("dependencies", {})
    keep = _dependencies_text(target_manifest_text)
    tool = [n for n in (PIPELINE, PACKAGE_NAME) if n in deps and n not in keep]
    if not tool:
        return None
    data["dependencies"] = {n: v for n, v in deps.items() if n not in tool}
    return json.dumps(data, indent=2) + "\n"


def _dependencies(manifest: Path) -> dict:
    return _dependencies_text(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}


def _dependencies_text(text: str) -> dict:
    return json.loads(text).get("dependencies", {}) if text.strip() else {}
