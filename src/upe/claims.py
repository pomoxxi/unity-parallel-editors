"""Claims: step 1 ("Claim") of the flow in DESIGN.md.

Before a task starts it declares the paths it will write. A claim that
overlaps a running task's claim is refused. At merge time the branch's
changed files are checked against its claim.

A claim entry is one of:
- a file path: `Assets/Scenes/Arena.unity`
- a folder, ending in `/`: `Assets/Features/Dash/`
- a glob: `Assets/Prefabs/Enemy*.prefab` (`*`, `?`, `[...]`, and `**` for any depth)

Paths are relative to the project root, compared with forward slashes and
case-insensitively (Windows). Claiming an asset also claims its `.meta`.
Glob-against-glob overlap is judged conservatively: if it cannot be ruled
out, it counts as an overlap.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

_WILDCARD = re.compile(r"[*?\[]")


class ClaimError(Exception):
    pass


def normalize(entry: str) -> str:
    """Forward slashes, lower case, no `./`, no `.meta` suffix."""
    text = entry.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or re.match(r"^[a-zA-Z]:", text) or ".." in text.split("/"):
        raise ClaimError(f"claim {entry!r} must be a path inside the project")
    if not text or text == "/":
        raise ClaimError("an empty claim would cover the whole project")
    text = text.lower()
    if text.endswith(".meta"):
        text = text[: -len(".meta")]
    return text


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                out.append("[" + pattern[i + 1 : end].replace("\\", "\\\\") + "]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class Entry:
    text: str  # normalized

    @property
    def is_folder(self) -> bool:
        return self.text.endswith("/")

    @property
    def is_glob(self) -> bool:
        return bool(_WILDCARD.search(self.text))

    @property
    def literal_prefix(self) -> str:
        match = _WILDCARD.search(self.text)
        return self.text if match is None else self.text[: match.start()]

    def covers(self, path: str) -> bool:
        """Whether this entry covers one concrete (normalized) path."""
        if self.is_glob:
            return bool(_glob_to_regex(self.text).match(path))
        if self.is_folder:
            return path.startswith(self.text)
        return path == self.text


def _overlap(a: Entry, b: Entry) -> bool:
    if not a.is_glob and not a.is_folder:
        return b.covers(a.text)
    if not b.is_glob and not b.is_folder:
        return a.covers(b.text)
    if a.is_folder and b.is_folder:
        return a.text.startswith(b.text) or b.text.startswith(a.text)
    # At least one glob: overlap unless their literal prefixes diverge.
    pa, pb = a.literal_prefix, b.literal_prefix
    return pa.startswith(pb) or pb.startswith(pa)


@dataclass(frozen=True)
class Claim:
    task_id: str
    entries: tuple[str, ...]  # normalized
    since: str = ""
    # Made by a `start` that holds the task's lock from before the claim to its end. Only such a claim
    # can be told dead: with no record and the lock free, its start is gone. A claim without it (made by an older
    # version) cannot be told, and is never freed by the tool. Written to the file only when true.
    locked: bool = False

    @classmethod
    def of(cls, task_id: str, entries: Iterable[str], *, locked: bool = False) -> "Claim":
        normalized = tuple(sorted({normalize(e) for e in entries}))
        if not normalized:
            raise ClaimError(f"task {task_id} claims nothing")
        return cls(task_id, normalized, datetime.now(timezone.utc).isoformat(timespec="seconds"), locked)

    def overlaps(self, other: "Claim") -> list[tuple[str, str]]:
        return [
            (a, b)
            for a in self.entries
            for b in other.entries
            if _overlap(Entry(a), Entry(b))
        ]

    def covers(self, path: str) -> bool:
        p = normalize(path)
        return any(Entry(e).covers(p) for e in self.entries)


def _folder_meta_on_the_way(path: str, claim: Claim) -> bool:
    """A folder's .meta above a claimed entry: Unity writes it when the task makes the folder.

    Two tasks making the same folder with different GUIDs is caught at merge
    time (the `.meta` GUID refusal), so these need no claim.
    """
    if not path.lower().endswith(".meta"):
        return False
    folder = normalize(path) + "/"
    return any(Entry(e).literal_prefix.startswith(folder) for e in claim.entries)


def unclaimed(changed_paths: Iterable[str], claim: Claim, shared: Iterable[str] = ()) -> list[str]:
    """Changed paths the claim does not cover. `shared` lists paths any task may write."""
    allowed = Claim("shared", tuple(normalize(s) for s in shared)) if shared else None
    return sorted(
        p for p in changed_paths
        if not claim.covers(p) and not (allowed and allowed.covers(p)) and not _folder_meta_on_the_way(p, claim)
    )


class ClaimRefused(ClaimError):
    def __init__(self, task_id: str, conflicts: dict[str, list[tuple[str, str]]], given: dict[str, str] | None = None):
        self.conflicts = conflicts
        given = given or {}
        lines = [f"claim for {task_id} refused:"]
        for other, pairs in conflicts.items():
            lines += [f"  {given.get(a, a)}  overlaps {other}'s  {b}" for a, b in pairs]
        lines.append("Paths are compared without case. A claim is freed when its task lands or is finished; "
                     f"`upe status <task>` shows where each is (here: {', '.join(conflicts)}). "
                     "Claim other paths, or start again then.")
        super().__init__("\n".join(lines))


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and a rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _try_lock(fd: int) -> None:
    """Lock the first byte of an open file, or raise OSError at once if another handle holds it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(lock_path: Path, error: type[Exception], timeout: float = 30.0, stale_after: float = 120.0, what: str = "") -> Iterator[None]:
    """Hold an OS lock on `lock_path` for the duration.

    The operating system drops the lock when its holder exits, so a crashed
    holder never blocks anyone and no lock is ever broken by age (`stale_after`
    is kept for callers and unused). A lock file left on disk means nothing.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    what = what or str(lock_path)
    deadline = time.monotonic() + timeout
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise error(f"{what} is locked by another process")
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def lock_held(lock_path: Path) -> bool:
    """Whether another handle holds the lock `file_lock` takes on `lock_path`, tried once, without waiting.

    A missing lock file is not held, and is not made: asking writes no file. When the lock is free it is
    held for an instant and freed; a `file_lock` that asks in that instant tries again 0.05 s later.
    OSError when the file cannot be opened for the lock (a folder without write rights, say).
    """
    try:
        fd = os.open(lock_path, os.O_RDWR)  # no O_CREAT
    except FileNotFoundError:
        return False
    try:
        try:
            _try_lock(fd)
        except OSError:
            return True
        _unlock(fd)
        return False
    finally:
        os.close(fd)


class ClaimRegistry:
    """Running tasks' claims in one JSON file, changed under a file lock."""

    def __init__(self, path: Path | str, lock_timeout: float = 30.0, stale_lock_after: float = 120.0):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout = lock_timeout
        self.stale_lock_after = stale_lock_after

    def _locked(self, timeout: float | None = None):
        return file_lock(self.lock_path, ClaimError, self.lock_timeout if timeout is None else timeout,
                         self.stale_lock_after, f"claims registry {self.path}")

    def _read(self) -> dict[str, Claim]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ClaimError(f"claims registry {self.path} does not read ({exc}); tell the user") from exc
        if not (isinstance(raw, dict) and all(
                isinstance(v, dict) and isinstance(v.get("entries"), list) and all(isinstance(e, str) for e in v["entries"])
                and isinstance(v.get("since", ""), str) and isinstance(v.get("locked", False), bool) for v in raw.values())):
            raise ClaimError(f"claims registry {self.path} does not read (a field of the wrong kind); tell the user")
        return {tid: Claim(tid, tuple(v["entries"]), v.get("since", ""), v.get("locked", False)) for tid, v in raw.items()}

    def _write(self, claims: dict[str, Claim]) -> None:
        data = {c.task_id: {"entries": list(c.entries), "since": c.since, **({"locked": True} if c.locked else {})}
                for c in claims.values()}
        write_atomic(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def all(self, timeout: float | None = None) -> dict[str, Claim]:
        """Every claim. `timeout` caps the wait for the lock (ClaimError after it); a read only needs a short one."""
        with self._locked(timeout):
            return self._read()

    def claim(self, task_id: str, entries: Iterable[str], *, locked: bool = False) -> Claim:
        """Record the claim, or raise ClaimRefused naming every overlap. Atomic. `locked`: the claim's `start` holds
        the task's lock (see Claim.locked)."""
        entries = list(entries)
        new = Claim.of(task_id, entries, locked=locked)
        with self._locked():
            claims = self._read()
            if task_id in claims:
                raise ClaimError(f"task {task_id} already holds a claim (since {claims[task_id].since}): it is being "
                                 f"started, or is running; `upe status {task_id}` says which")
            conflicts = {tid: pairs for tid, c in claims.items() if (pairs := new.overlaps(c))}
            if conflicts:
                raise ClaimRefused(task_id, conflicts, {normalize(e): e for e in entries})
            claims[task_id] = new
            self._write(claims)
        return new

    def release(self, task_id: str) -> bool:
        """Drop a task's claim when it lands or is abandoned. Returns False if it had none."""
        with self._locked():
            claims = self._read()
            if claims.pop(task_id, None) is None:
                return False
            self._write(claims)
            return True
