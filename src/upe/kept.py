"""Kept test results, one per commit, and the guard that says which of a merge's failed tests are known.

Before each merge, `land` needs to know which of the tests that fail on the merge also fail on the target. A full
EditMode run on the target costs half a land, so a commit's full result is kept (`test-baselines/<commit>.json`), and
a target with none is judged from the nearest ancestor's full result plus a run of only the failed tests' classes on
the target (`decide`). A class-only run is a `ClassRun`, a different type from a `Kept`: `KeptStore.put` takes only
a `Kept`, so a part is never stored as a commit's whole.

The format (2):

    {"format": 2, "scope": "full", "cases": 2007, "failed": [...], "flaky": [...], "unconfirmed": [...]}

`failed`: failed on every attempt (the CLI's retry, or the tool's re-run, failed it again). `flaky`: failed, then
passed on an attempt. `unconfirmed`: failed, and never seen failing twice. An old file is a JSON list of every test
that failed even once (format 1): it serves its own commit (case 1) as before, and is never an ancestor's result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .checks import SceneRun, TestRun
from .claims import write_atomic

FORMAT = 2
COMMIT_FILE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # a SHA-1 or SHA-256 commit id; `.lock` files are not read

# How each known test was decided, as `checks.md` names it.
BY_OWN = "own"
BY_CLASSES = "classes"
BY_FULL_RUN = "full"


@dataclass(frozen=True)
class Kept:
    """One commit's full-suite result."""
    failed: frozenset[str] = frozenset()  # failed on every attempt
    flaky: frozenset[str] = frozenset()  # failed, then passed on an attempt
    unconfirmed: frozenset[str] = frozenset()  # failed, never seen failing twice
    cases: int = 0
    format: int = FORMAT

    @property
    def seen(self) -> frozenset[str]:
        """Every test that failed even once: what the commit's own result excuses (case 1)."""
        return self.failed | self.flaky | self.unconfirmed

    @classmethod
    def of(cls, run: TestRun, cases: int) -> "Kept":
        failed = run.failed or set()
        return cls(frozenset(run.twice & failed), frozenset(run.flaky), frozenset(failed - run.twice), cases)

    def to_json(self) -> str:
        return json.dumps({"format": FORMAT, "scope": "full", "cases": self.cases, "failed": sorted(self.failed),
                           "flaky": sorted(self.flaky), "unconfirmed": sorted(self.unconfirmed)}, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Kept | None":
        """A kept file's result; None when it does not read as either format."""
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, list) and all(isinstance(n, str) for n in data):
            return cls(unconfirmed=frozenset(data), format=1)
        if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("scope") != "full":
            return None
        lists = [data.get(key) for key in ("failed", "flaky", "unconfirmed")]
        if not all(isinstance(v, list) and all(isinstance(n, str) for n in v) for v in lists):
            return None
        cases = data.get("cases")
        return cls(*(frozenset(v) for v in lists), cases=cases if isinstance(cases, int) else 0)


class KeptStore:
    """`test-baselines/<commit>.json`: the only reader and writer of kept full results."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def path(self, commit: str) -> Path:
        return self.folder / f"{commit}.json"

    def get(self, commit: str) -> Kept | None:
        """The commit's own kept result, of either format; None when there is none or it does not read."""
        try:
            return Kept.from_json(self.path(commit).read_text(encoding="utf-8"))
        except OSError:
            return None

    def put(self, commit: str, kept: Kept, *, refine: bool = False) -> bool:
        """Keep a full result for `commit`, never over a file already there. Returns whether it wrote.

        `refine`: the same run's result after its re-run, written over the one kept before it. The caller
        holds the commit's lock, and the re-run only moves tests between the three lists, so `seen` stays the same."""
        if not isinstance(kept, Kept) or kept.format != FORMAT:
            raise TypeError("only a full result in the new format is kept for a commit")
        path = self.path(commit)
        if path.exists() and not refine:
            return False
        write_atomic(path, kept.to_json())
        return True

    def nearest(self, commit: str, distance: Callable[[str], int | None]) -> tuple[str, Kept, int] | None:
        """The nearest ancestor of `commit` with a kept full result in the new format: (its commit, its result, how many
        commits before `commit` it is). `distance(c)` gives that count, or None when `c` is not an ancestor."""
        best = None
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            other = path.stem
            if other == commit or not COMMIT_FILE.match(other):
                continue
            kept = self.get(other)
            if kept is None or kept.format != FORMAT:
                continue
            far = distance(other)
            if far is not None and (best is None or far < best[2]):
                best = (other, kept, far)
        return best


@dataclass(frozen=True)
class ClassRun:
    """A run of some test classes only, on the target, with no retry. Never a commit's result: see `KeptStore.put`."""
    commit: str
    classes: tuple[str, ...]
    failed: frozenset[str] | None  # None: it left no report
    ran: frozenset[str] = frozenset()

    @property
    def finished(self) -> bool:
        return self.failed is not None


@dataclass
class Decision:
    """Which of the merge's failed tests are known, and how; which are new; which need the target's full run."""
    known: dict[str, str] = field(default_factory=dict)  # test -> BY_OWN, BY_CLASSES or BY_FULL_RUN
    new: set[str] = field(default_factory=set)
    need_full: set[str] = field(default_factory=set)
    not_compared: bool = False  # the class-only run left no report: no test was compared


def decide(failed: set[str], own: Kept | None, ancestor: tuple[str, Kept, int] | None,
           alone: ClassRun | None) -> Decision:
    """The guard, for each test that failed on the merge (after the retries):

    a. the target's own kept result lists it (failed even once): known;
    b. no result of its own, and the class-only run did not finish (or none ran): new, not compared;
    c. the class-only run did not fail it (it passed, or is not there: a class the target does not have): new;
    d. it failed there, and the ancestor's result lists it as failed on every attempt: known;
    e. it failed there, and the ancestor's result does not (flaky only, unconfirmed, absent): the target's full run.

    With no result of its own and no ancestor's, every test is new with no baseline."""
    decision = Decision()
    for name in sorted(failed):
        if own is not None:
            if name in own.seen:
                decision.known[name] = BY_OWN
            else:
                decision.new.add(name)
        elif ancestor is None:
            decision.new.add(name)
        elif alone is None or not alone.finished:
            decision.new.add(name)
            decision.not_compared = True
        elif name not in alone.failed:
            decision.new.add(name)
        elif name in ancestor[1].failed:
            decision.known[name] = BY_CLASSES
        else:
            decision.need_full.add(name)
    return decision


# Kept full scene results ----------------------------------------------------------------------------------
#
# `scene-baselines/<commit>.<slot>.json`, a folder of its own beside `test-baselines/`, so neither store's reader ever
# meets the other's files. A run of some paths, or one that did not complete, is a `SceneRun` and never a `FullScenes`:
# `SceneStore.put` takes only a `FullScenes`, and `FullScenes.of` makes one only from a complete full run.

SCENE_FORMAT = 1


@dataclass(frozen=True)
class FullScenes:
    """One commit's complete check of every scene and prefab: its problems as (path, kind, where, detail)."""
    problems: frozenset[tuple[str, str, str, str]]
    scenes: int
    prefabs: int

    @staticmethod
    def why_not(run: SceneRun) -> str:
        """Why a run is not a commit's full result; "" when it is: a full run (no `-upePaths`) that wrote its report in
        this run, ended 0 or 1, holds no `exception` row, had no licensing failure or compile error, and loaded at least
        one scene or prefab."""
        if not isinstance(run, SceneRun):
            return "not a scene run"
        if run.paths:
            return f"a check of {len(run.paths)} paths, not of every scene and prefab"
        if run.problems is None:
            return run.why or "no report"
        if run.why:
            return run.why
        if run.exit_code not in (0, 1):
            return f"the Editor exited {run.exit_code}"
        if any(kind == "exception" for _, kind, _, _ in run.problems):
            return "the check threw (an exception row): it stopped part way"
        if run.scenes < 0 or run.prefabs < 0 or run.scenes + run.prefabs <= 0:
            return "it loaded no scene and no prefab"
        return ""

    @classmethod
    def of(cls, run: SceneRun) -> "FullScenes | None":
        if cls.why_not(run):
            return None
        return cls(frozenset(run.problems), run.scenes, run.prefabs)

    def to_json(self) -> str:
        problems = [{"path": p, "kind": k, "where": w, "detail": d} for p, k, w, d in sorted(self.problems)]
        return json.dumps({"format": SCENE_FORMAT, "kind": "scene-check", "scope": "full", "scenes": self.scenes,
                           "prefabs": self.prefabs, "problems": problems}, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "FullScenes | None":
        """A kept file's result; None when it is not one: another format, kind or scope, a problem that is not four
        strings, an `exception` row, counts that are not whole numbers or add up to none."""
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if (not isinstance(data, dict) or data.get("format") != SCENE_FORMAT or data.get("kind") != "scene-check"
                or data.get("scope") != "full"):
            return None
        counts = [data.get("scenes"), data.get("prefabs")]
        if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts) or sum(counts) <= 0:
            return None
        rows = data.get("problems")
        if not isinstance(rows, list):
            return None
        problems = set()
        for row in rows:
            keys = ("path", "kind", "where", "detail")
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in keys):
                return None
            problems.add(tuple(row[k] for k in keys))
        if any(kind == "exception" for _, kind, _, _ in problems):
            return None
        return cls(frozenset(problems), *counts)


SLOT_NAME = re.compile(r"^s[1-9][0-9]*$")  # a slot's folder name, as `workspace.SLOT`
LIBRARY_ID = re.compile(r"^[0-9a-f]{32}$")  # a slot's Library as made, as `workspace.SLOT_ID`


class SceneStore:
    """`scene-baselines/<commit>.<slot>.json`: the only reader and writer of kept full scene results.

    Keyed by the slot too: one commit's full check gave one number of problems in slot s2 and another in slot s3 (why is
    not known: the slot, or the time between the runs, or the git-ignored local copies an
    acquire puts back), so a result is read only by a land whose check runs in the slot that made it, the
    pairing the baseline and the merge's check have. And by the Library that made it: a
    slot's name is given again to a new slot once the old one is removed, so the file names the id of the Library
    (`library`, `Workspaces.slot_id`) and `get` reads it only for that id."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def path(self, commit: str, slot: str) -> Path:
        if not COMMIT_FILE.match(commit):
            raise ValueError(f"not a commit id: {commit!r}")
        if not SLOT_NAME.match(slot):
            raise ValueError(f"not a slot name: {slot!r}")
        return self.folder / f"{commit}.{slot}.json"

    def get(self, commit: str, slot: str, library: str) -> FullScenes | None:
        """The commit's full result kept in `slot` by its Library `library`; None when there is none, another Library
        made it, or it does not read as one."""
        try:
            text = self.path(commit, slot).read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or data.get("library") != library:
            return None
        return FullScenes.from_json(text)

    def put(self, commit: str, slot: str, library: str, full: FullScenes) -> bool:
        """Keep a full result for `commit` made in `slot` by its Library `library`. Never over one that reads for that
        Library; over a file that does not read, or another Library's, yes (else that file would cost a full run in
        every land for ever). Returns whether it wrote."""
        if not isinstance(full, FullScenes):
            raise TypeError("only a complete full scene check is kept for a commit")
        if not LIBRARY_ID.match(library):
            raise ValueError(f"not a Library id: {library!r}")
        path = self.path(commit, slot)
        if self.get(commit, slot, library) is not None:
            return False
        data = json.loads(full.to_json())
        data["library"] = library
        write_atomic(path, json.dumps(data, indent=2) + "\n")
        return True
