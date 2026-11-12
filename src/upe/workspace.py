"""Worktree and Library setup: step 2 ("Isolate") of the flow in DESIGN.md.

Each task gets a git worktree on its own branch, and a private `Library`.
The Library is copied, never linked: symlinked Libraries corrupt
(research/02). `Temp`, `Logs` and `UserSettings` are never copied; each
Editor makes its own.

Worktrees are slots that are reused (`<root>/slots/s1`, `s2`, ...). A
Library opened at a new path costs Unity a rescan, a recompile and a domain
reload, about 200 s for a large project, against 20 s at a path it has seen
(research/05). So a finished task's slot is parked, detached and clean, with
its Library kept, and the next task or merge candidate checks out its own
branch there; Unity then imports only the difference. A new slot is made,
with the golden Library copied in, only when every slot is busy.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
import subprocess
from dataclasses import dataclass
from pathlib import Path
import json
from typing import Callable, Sequence

from .claims import file_lock, write_atomic
from .cmdline import capped
from .safegit import SAFE_GIT

Runner = Callable[..., subprocess.CompletedProcess]

LOCKFILE = Path("Temp") / "UnityLockfile"
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
BRANCH_PREFIX = "upe/"
SLOT = re.compile(r"^s[1-9][0-9]*$")
# The id of a slot's `Library` as made, written when the golden's Library is copied in (a
# new slot, or a folder made again under a removed slot's name). A kept full scene result names the id of the
# Library that made it, so one made by a removed slot is never read by a new slot of the same name.
SLOT_ID_FILE = "upe-slot-id"
SLOT_ID = re.compile(r"^[0-9a-f]{32}$")


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class Workspace:
    task_id: str
    path: Path
    branch: str

    @property
    def library(self) -> Path:
        return self.path / "Library"


def is_link(path: Path) -> bool:
    """A symlink or an NTFS junction. Copying into or deleting through one reaches its target."""
    isjunction = getattr(os.path, "isjunction", lambda p: False)
    return os.path.islink(path) or isjunction(path)


def lock_state(project: Path) -> str:
    """Return "none", "held" (an Editor has the project open) or "stale".

    Unity holds Temp/UnityLockfile open while the project is open, so an
    exclusive open fails. A lockfile that opens is left over from a crash.
    UNVERIFIED on 6000.3: that Unity's handle denies our open on Windows.
    """
    lockfile = project / LOCKFILE
    if not lockfile.exists():
        return "none"
    try:
        with open(lockfile, "a"):
            pass
    except PermissionError:
        return "held"
    return "stale"


def clear_stale_lock(project: Path) -> bool:
    """Delete `Temp/` if its lock is stale. Returns True if it was cleared.

    Deleting only the lockfile is sometimes not enough (research/02), so the
    whole `Temp/` goes. Never touches a held lock.
    """
    if lock_state(project) != "stale":
        return False
    shutil.rmtree(project / "Temp")
    return True


class Workspaces:
    """Creates and removes task worktrees for one Unity project repository."""

    def __init__(
        self,
        repo: Path | str,
        root: Path | str,
        golden: Path | str,
        runner: Runner = subprocess.run,
        robocopy_threads: int = 16,
        local_copies: Sequence[str] = (),
    ) -> None:
        self.repo = Path(repo)
        self.root = Path(root)  # worktrees go here, one folder per task
        self.golden = Path(golden)  # a project whose Library is fully imported
        self._runner = runner
        self.robocopy_threads = robocopy_threads
        # Git-ignored files and folders the project needs to compile (an asset
        # store package kept out of git). Copied from the main checkout.
        self.local_copies = tuple(local_copies)

    # Git -----------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True, input: str | None = None) -> str:
        completed = self._runner(
            ["git", *SAFE_GIT, "-C", str(cwd or self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **({"input": input} if input is not None else {}),
        )
        if check and completed.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {(completed.stderr or '').strip()}")
        return completed.stdout or ""

    # Slots ---------------------------------------------------------------

    @property
    def slots_dir(self) -> Path:
        return self.root / "slots"

    @property
    def _map_path(self) -> Path:
        return self.root / ".upe" / "slots.json"

    def _read_map(self) -> dict[str, str | None]:
        if not self._map_path.exists():
            return {}
        try:
            slots = json.loads(self._map_path.read_text(encoding="utf-8"))
        except ValueError as exc:  # an error the tool names (exit 2), not a traceback
            raise WorkspaceError(f"{self._map_path} does not read ({exc}); tell the user") from exc
        if not isinstance(slots, dict) or not all(v is None or isinstance(v, str) for v in slots.values()):
            raise WorkspaceError(f"{self._map_path} does not read (not a map of slots to owners); tell the user")
        bad = [name for name in slots if not SLOT.match(name)]
        if bad:
            raise WorkspaceError(f"{self._map_path} names slots that are not slots: {', '.join(bad)}")
        return slots

    def _write_map(self, slots: dict[str, str | None]) -> None:
        write_atomic(self._map_path, json.dumps(slots, indent=2, sort_keys=True) + "\n")

    def _locked(self):
        return file_lock(self._map_path.with_suffix(".lock"), WorkspaceError, what=f"slot map {self._map_path}")

    def slots(self) -> dict[str, str | None]:
        """Each slot folder and who has it: a task id, `merge:<task>`, `base:<task>`, or None when parked."""
        return self._read_map()

    def path_of(self, owner: str) -> Path | None:
        for slot, held_by in self._read_map().items():
            if held_by == owner:
                return self.slots_dir / slot
        return None

    def acquire(self, owner: str, branch: str | None, base: str, reset_branch: bool = False) -> Path:
        """Check `branch` out from `base` in a parked slot, or a new one. Returns its path.

        `reset_branch` moves an existing branch (a merge candidate left from an
        earlier try); otherwise an existing branch is refused, as git refuses it.
        `branch` None checks `base` out detached, and makes no branch (the target's run of a land).
        """
        commit = self._git("rev-parse", "--verify", base + "^{commit}").strip()  # in the repo, not the slot
        existed = branch is None or bool(self._git("branch", "--list", branch).strip())  # None: no branch to delete
        if branch is None:
            reset_branch = False
        if branch is not None and existed and not reset_branch:
            raise WorkspaceError(f"branch {branch} already exists")
        with self._locked():
            slots = self._read_map()
            if owner in slots.values():
                raise WorkspaceError(f"{owner} already has slot {self.path_of(owner)}")
            parked = [name for name in sorted(slots, key=_slot_number)
                      if slots[name] is None and self._reusable(self.slots_dir / name)]
            name = parked[0] if parked else _next_slot(slots)
            slots[name] = owner
            self._write_map(slots)
        path = self.slots_dir / name
        made = False  # this call made the folder: only then is it removed again
        try:
            to = ("--detach",) if branch is None else ("-B" if reset_branch else "-b", branch)
            if parked:
                self._check_slot(path)
                self._scrub(path)
                self._git("checkout", "-q", *to, commit, cwd=path)
            else:
                self.slots_dir.mkdir(parents=True, exist_ok=True)
                made = not path.exists()
                self._git("worktree", "add", *to, str(path), commit)
            self.copy_local(path)
            if not parked:
                self.seed_library(Workspace(owner, path, branch or ""))
        except BaseException as exc:  # Ctrl-C too; a child the runner started is ended by `subprocess.run`
            try:
                if parked:
                    self._git("checkout", "-q", "--detach", cwd=path, check=False)
                # A folder that stays keeps its entry, the owner's: the next slot does not take its name,
                # and `finish` of the owner sees it.
                if parked:
                    self._give_back(name)
                elif made:
                    self._give_back(name, remove=True, owner=owner, begun=True)
                else:  # a folder of that name was there before (not in the tool's record): not ours to touch
                    self._drop_entry(name)
                if not existed:
                    self._git("branch", "-D", branch, check=False)
            except Exception as failed:  # the first error says why; this one says what is left
                exc.add_note(f"Giving slot {name} back failed too ({failed}): it is still {owner}'s")
            raise
        return path

    def _reusable(self, path: Path) -> bool:
        if not path.is_dir() or lock_state(path) == "held":
            return False
        return not self._has_changes(path)

    def _check_slot(self, path: Path) -> None:
        """Refuse a destructive git command anywhere but one of the tool's own slots."""
        if path.parent != self.slots_dir or not SLOT.match(path.name) or is_link(path) or not path.is_dir():
            raise WorkspaceError(f"{path} is not one of the tool's slots")
        top = Path(self._git("rev-parse", "--show-toplevel", cwd=path).strip())
        same = lambda a, b: os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
        if not same(top, path) or same(path, self.repo):
            raise WorkspaceError(f"{path} is not one of the tool's slots (git says its top is {top})")

    def _scrub(self, path: Path) -> None:
        """Clear what a slot's last owner could leave hidden from the dirty check.

        Index flags (skip-worktree, assume-unchanged), ignored files (an
        `Assets/Editor` script git ignores still runs in the next owner's
        Editor) and per-worktree git config. The Library stays; local copies
        and the tool's package are put back by the caller.
        """
        flagged = [line[2:] for line in self._git("ls-files", "-v", cwd=path).splitlines()
                   if line[:1] == "S" or line[:1].islower()]
        # One flag per call: git applies only the last of the two when given both. Each in groups capped by length,
        # every group of the first flag before the second.
        for flag in ("--no-skip-worktree", "--no-assume-unchanged"):
            for group in capped(flagged):
                self._git("update-index", flag, "--", *group, cwd=path)
        self._git("reset", "-q", "--hard", cwd=path)
        self._git("clean", "-q", "-ffdx", "-e", "/Library/", cwd=path)
        git_dir = Path(self._git("rev-parse", "--git-dir", cwd=path).strip())
        (git_dir if git_dir.is_absolute() else path / git_dir).joinpath("config.worktree").unlink(missing_ok=True)

    def release(self, owner: str, discard_changes: bool = False) -> None:
        """Park the owner's slot: detached at its commit, clean, Library kept.

        Refuses while an Editor has it open. Uncommitted work is refused too,
        unless `discard_changes` (a merge candidate being thrown away).
        """
        path = self.path_of(owner)
        if path is None:
            return
        if lock_state(path) == "held":
            raise WorkspaceError(f"{path}: an Editor still has it open")
        self._check_slot(path)
        if discard_changes:
            self._git("merge", "--abort", cwd=path, check=False)
            self._git("reset", "-q", "--hard", cwd=path)
            self._git("clean", "-q", "-fd", cwd=path)  # untracked only; the ignored Library stays
        elif self._has_changes(path):
            raise WorkspaceError(f"{path}: uncommitted work; commit or discard it first")
        self._git("checkout", "-q", "--detach", cwd=path)
        self._give_back(path.name)

    def _give_back(self, name: str, remove: bool = False, owner: str = "", begun: bool = False) -> None:
        """Park slot `name`, or, with `remove`, take its folder away and drop its entry. A folder that stays raises
        WorkspaceError and keeps its entry (see `_remove_folder`); `begun`: this process made the folder."""
        if remove:
            self._remove_folder(name, owner, begun=begun)
        with self._locked():
            slots = self._read_map()
            if remove:
                slots.pop(name, None)
            else:
                slots[name] = None
            self._write_map(slots)

    def _drop_entry(self, name: str) -> None:
        with self._locked():
            slots = self._read_map()
            slots.pop(name, None)
            self._write_map(slots)

    def _marker(self, name: str) -> Path:
        return self.root / ".upe" / "removing" / name

    def removal_begun(self, name: str, owner: str) -> bool:
        """Whether the tool began to remove slot `name` of `owner`, after it checked the slot held no work (or made it
        itself), and git stopped half way: git deletes the slot's `.git` first, so what is left reads as no worktree."""
        marker = self._marker(name)
        try:
            return marker.read_text(encoding="utf-8").strip() == owner
        except OSError:
            return False

    def _remove_folder(self, name: str, owner: str, *, begun: bool = False) -> None:
        """Remove slot folder `name` of `owner`, and git's record of it. The caller checked it holds no work, or made it.

        A marker under `.upe/removing/` is written first, so a removal git stops half way (a file held open, Ctrl-C:
        it has already deleted `.git`, so the rest no longer reads as a worktree) can be finished by a later call;
        a folder with no `.git` and no marker is not the tool's to delete (WorkspaceError, nothing done). The folder
        still there at the end: WorkspaceError, the marker kept.
        """
        path = self.slots_dir / name
        marker = self._marker(name)
        if path.exists() and not is_link(path):
            if begun or (path / ".git").exists():
                write_atomic(marker, owner + "\n")
                self._git("worktree", "remove", "--force", str(path), check=False)
            if path.exists():
                if not self.removal_begun(name, owner):
                    raise WorkspaceError(f"{path} is not a git worktree, and the tool did not begin removing it, so "
                                         f"it does not know what it holds; it is still {owner}'s")
                shutil.rmtree(path, ignore_errors=True)  # what git's removal left; a junction is not followed
        if path.exists():
            raise WorkspaceError(f"{path} could not be removed (a file in it is held open, or something still writes "
                                 f"in it); it is still {owner}'s")
        self._git("worktree", "prune", check=False)  # git's record of a worktree whose folder is gone
        marker.unlink(missing_ok=True)

    # Tasks ---------------------------------------------------------------

    @staticmethod
    def check_id(task_id: str) -> None:
        if not TASK_ID.match(task_id):
            raise WorkspaceError(f"task id {task_id!r} must be lowercase letters, digits and dashes")

    def workspace(self, task_id: str) -> Workspace:
        """The task's worktree. Raises if it has none."""
        self.check_id(task_id)
        path = self.path_of(task_id)
        if path is None:
            raise WorkspaceError(f"task {task_id} has no worktree")
        return Workspace(task_id, path, BRANCH_PREFIX + task_id)

    def create(self, task_id: str, base: str = "HEAD") -> Workspace:
        """Give the task a slot on a new branch `upe/<task>` from `base`."""
        self.check_id(task_id)
        self.acquire(task_id, BRANCH_PREFIX + task_id, base)
        return self.workspace(task_id)

    def is_dirty(self, ws: Workspace) -> bool:
        return self._has_changes(ws.path)

    def _has_changes(self, path: Path) -> bool:
        return bool(self.changes(path))

    def changes(self, path: Path) -> list[str]:
        """Paths with uncommitted content, and untracked files.

        Not `git status`: with autocrlf, a file Unity rewrote from CRLF to LF
        has a new size, and git reports it modified without comparing its
        content. The diff compares content (research/05).
        """
        modified = self._git("diff", "--name-only", "--no-renames", "-z", "HEAD", cwd=path).split("\0")
        untracked = self._git("ls-files", "-z", "--others", "--exclude-standard", cwd=path).split("\0")
        return [p for p in modified + untracked if p]

    # A `start` that died -------------------------------------

    def losses(self, path: Path) -> list[str]:
        """What taking the slot away would lose: tracked files whose content is not HEAD's, and files git does not
        track (the ignored ones, the Library above all, are not work).

        A checkout killed before it wrote its index (git writes the index last) leaves every file it wrote as
        untracked, and every tracked file as deleted. So with no index, a tracked file missing loses nothing (git
        has it), nor does a file git does not track whose path HEAD has and whose content is HEAD's. With an index,
        a tracked file missing is a deletion someone made, staged or not: it is work. The tool's own
        package is excluded in the slot, and the manifest and lock it changes are marked skip-worktree by
        `agent_package.install`: neither reads as work.
        """
        index = Path(self._git("rev-parse", "--git-path", "index", cwd=path).strip())
        indexed = (index if index.is_absolute() else path / index).exists()
        status = self._git("diff", "--name-status", "--no-renames", "-z", "HEAD", cwd=path).split("\0")
        lost = [name for kind, name in zip(status[0::2], status[1::2]) if name and (kind != "D" or indexed)]
        untracked = [p for p in self._git("ls-files", "-z", "--others", "--exclude-standard", cwd=path).split("\0") if p]
        if untracked:
            tree = {}
            for line in self._git("ls-tree", "-r", "-z", "HEAD", cwd=path).split("\0"):
                meta, _, name = line.partition("\t")
                if name and meta.split()[1:2] == ["blob"]:
                    tree[name] = meta.split()[2]
            known = [p for p in untracked if p in tree and "\n" not in p]
            hashes = (self._git("hash-object", "--stdin-paths", cwd=path, input="".join(f"{p}\n" for p in known)).split()
                      if known else [])
            same = {p for p, sha in zip(known, hashes) if sha == tree[p]}
            lost += [p for p in untracked if p not in same]
        return lost

    def recent_change(self, path: Path, quiet_s: float, now: float | None = None) -> str | None:
        """A file or folder under `path` (or `path` itself, as ".") written less than `quiet_s` seconds ago, or in
        the future, or gone while it was read; None when nothing was. The time can only make the caller refuse.

        A `start` killed while robocopy filled its Library leaves robocopy running (Windows does not end a killed
        process's children): the folders it fills are written as it goes, and so is the file it copies. Each time is
        read with `os.stat`, not from the folder's listing: on Windows the listing's copy of a file held open for
        writing keeps its old time until the file is closed.
        """
        since = (time.time() if now is None else now) - quiet_s
        try:
            if os.stat(path, follow_symlinks=False).st_mtime > since:
                return "."
        except FileNotFoundError:
            return None
        todo = [str(path)]
        while todo:
            folder = todo.pop()
            try:
                entries = list(os.scandir(folder))
            except FileNotFoundError:
                return os.path.relpath(folder, path)
            for entry in entries:
                rel = os.path.relpath(entry.path, path)
                try:
                    if os.stat(entry.path, follow_symlinks=False).st_mtime > since:
                        return rel
                    junction = getattr(entry, "is_junction", lambda: False)()
                    if entry.is_dir(follow_symlinks=False) and not junction:
                        todo.append(entry.path)
                except FileNotFoundError:
                    return rel
        return None

    def remove_slot(self, owner: str) -> str:
        """Take the owner's slot away, not parked: its folder (Library included) and its map entry. Returns the
        slot's name, or "" when the owner has none. The caller has checked that it holds no work.

        A slot whose folder is already gone loses only its map entry (and git's record of the worktree). A removal
        git stopped half way is finished (see `_remove_folder`). A folder still there at the end: WorkspaceError,
        the entry kept.
        """
        path = self.path_of(owner)
        if path is None:
            return ""
        if (path / ".git").exists():
            self._check_slot(path)
        self._remove_folder(path.name, owner)
        with self._locked():
            slots = self._read_map()
            if slots.get(path.name) == owner:
                slots.pop(path.name)
                self._write_map(slots)
        return path.name

    def remove(self, task_id: str) -> None:
        """Park a finished task's slot. Refuses if its Editor is open or work is uncommitted.

        The branch is kept; it is deleted only after it lands.
        """
        self.release(task_id)

    def copy_local(self, worktree: Path) -> None:
        """Copy `local_copies` from the main checkout into a new worktree.

        A git worktree has only tracked files. Without these the project does
        not compile, and a batch Editor with compile errors exits 1.
        """
        for rel in self.local_copies:
            source, dest = self.repo / rel, Path(worktree) / rel
            if source.is_dir():
                self.robocopy(source, dest)
            elif source.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            else:
                raise WorkspaceError(f"local copy {rel} not found in {self.repo}")

    # Library -------------------------------------------------------------

    def seed_library(self, ws: Workspace) -> None:
        """Copy the golden Library into the worktree with robocopy /MIR."""
        source = self.golden / "Library"
        if not source.is_dir():
            raise WorkspaceError(f"golden Library not found at {source}")
        if lock_state(self.golden) == "held":
            raise WorkspaceError(f"golden project {self.golden} is open in an Editor; close it before copying")
        if lock_state(ws.path) == "held":
            raise WorkspaceError(f"{ws.path} is open in an Editor")
        self.robocopy(source, ws.library)
        self._new_slot_id(ws.path)

    def slot_id(self, path: Path) -> str:
        """The id of the `Library` at `path` as made (`SLOT_ID_FILE`). A Library with none, or one that does not read
        (a slot made by an older version, a Library deleted by hand), is given a new one: a result kept before is then
        not read, which costs a full run and loses nothing."""
        try:
            text = (Path(path) / "Library" / SLOT_ID_FILE).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            text = ""
        return text if SLOT_ID.match(text) else self._new_slot_id(path)

    def _new_slot_id(self, path: Path) -> str:
        made = uuid.uuid4().hex
        (Path(path) / "Library").mkdir(parents=True, exist_ok=True)
        write_atomic(Path(path) / "Library" / SLOT_ID_FILE, made + "\n")
        return made

    def robocopy(self, source: Path, dest: Path, exclude_dirs: Sequence[str] = ()) -> int:
        """Mirror `source` to `dest`. Returns robocopy's exit code; 8 and above is failure.

        /MIR deletes whatever `dest` has that `source` lacks, so a `dest` that
        is, or lies under, a link inside the tool's folders is refused: the
        deletions would land on the link's target.
        """
        for p in (Path(dest), *Path(dest).parents):
            if p == self.root or self.root not in p.parents:
                break
            if is_link(p):
                raise WorkspaceError(f"{p} is a link; refusing to copy through it")
        argv = [
            "robocopy", str(source), str(dest), "/MIR",
            f"/MT:{self.robocopy_threads}", "/R:1", "/W:1",
            "/NFL", "/NDL", "/NP", "/NJH",
        ]
        if exclude_dirs:
            argv += ["/XD", *exclude_dirs]
        completed = self._runner(argv, capture_output=True, text=True, errors="replace")
        if completed.returncode >= 8:
            raise WorkspaceError(f"robocopy failed with {completed.returncode}: {(completed.stdout or '')[-2000:]}")
        return completed.returncode


def _slot_number(name: str) -> int:
    return int(name[1:]) if name[1:].isdigit() else 0


def _next_slot(slots: dict[str, str | None]) -> str:
    return f"s{max((_slot_number(n) for n in slots), default=0) + 1}"
