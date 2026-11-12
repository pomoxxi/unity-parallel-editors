"""The layered merge: step 3 of the flow in DESIGN.md.

A task branch is merged onto a candidate branch `upe-merge/<task-id>`, made
from the target in its own worktree. The target itself moves only when the
merge lands (after the checks and the council), by fast-forward.

Layers, in order, for each conflicted file:
1. Plain git merge (renames off, so `.meta` pairs are not mangled).
2. `.unity` and `.prefab`: UnityYAMLMerge, headless, `-p --fallback none`.
   `.meta`: refused outright if the two sides gave the asset different
   GUIDs; otherwise UnityYAMLMerge.
3. `Packages/packages-lock.json` is deleted and left for Unity to
   regenerate. `Packages/manifest.json` is merged key by key. The tag,
   layer and build scene lists in ProjectSettings merge as a union
   (`project_settings.py`).
4. Anything still unresolved goes to builder replay if a builder owns the
   file; otherwise the merge stops and reports it.

Every decision is written to `MergeReport.steps`, which the merge council
reads.
"""

from __future__ import annotations

import difflib
import json
from collections import Counter
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .project_settings import RULES as SETTINGS_RULES
from .project_settings import SettingsConflict, merge_settings
from .safegit import SAFE_GIT

Runner = Callable[..., subprocess.CompletedProcess]
# (base, theirs, mine, output) -> exit code; 0 means fully merged.
YamlMerger = Callable[[Path, Path, Path, Path], int]

YAML_MERGEABLE = (".unity", ".prefab")
LOCK_FILE = "Packages/packages-lock.json"
MANIFEST = "Packages/manifest.json"
CANDIDATE_PREFIX = "upe-merge/"


def _branch_ref(name: str) -> str:
    """A branch by its full name, so git never reads a tag of the same name (git takes `refs/tags/X` before
    `refs/heads/X`). A name that is already a ref is kept."""
    return name if name.startswith("refs/") else "refs/heads/" + name

_GUID = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)


class MergeError(Exception):
    pass


@dataclass(frozen=True)
class Step:
    path: str
    layer: str  # "git", "unityyamlmerge", "meta-guid", "packages-lock", "manifest", "project-settings", "replay", "unresolved"
    outcome: str  # "merged", "regenerate", "replay", "conflict"
    detail: str = ""


@dataclass
class MergeReport:
    task_id: str
    branch: str
    target: str
    candidate: str
    base: str = ""
    steps: list[Step] = field(default_factory=list)
    regenerate: list[str] = field(default_factory=list)  # files Unity must rewrite during the check
    replays: list[str] = field(default_factory=list)  # builder methods to re-run on the merged project
    committed: str = ""  # candidate commit, once everything resolved

    @property
    def unresolved(self) -> list[Step]:
        return [s for s in self.steps if s.outcome == "conflict"]

    @property
    def ok(self) -> bool:
        return bool(self.committed) and not self.unresolved

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id, "branch": self.branch, "target": self.target,
                "candidate": self.candidate, "base": self.base, "committed": self.committed,
                "regenerate": self.regenerate, "replays": self.replays,
                "steps": [s.__dict__ for s in self.steps],
            },
            indent=2,
        )


def program_there(path: Path) -> bool:
    """Whether `path` names a program file: itself, or with `.exe` added when it has no extension, as Windows starts it."""
    return path.is_file() or (bool(path.name) and not path.suffix and path.with_name(path.name + ".exe").is_file())


def unity_yaml_merge(exe: Path | Callable[[], Path], extra: tuple[str, ...] = (), runner: Runner = subprocess.run) -> YamlMerger:
    """A YamlMerger that runs UnityYAMLMerge. Its argument order is base, theirs, mine, output.

    `exe` may be a function, called at the first merge, so the tool is looked for only when a merge needs it.
    """

    def merge(base: Path, theirs: Path, mine: Path, out: Path) -> int:
        tool = exe() if callable(exe) else exe
        if not program_there(Path(tool)):
            raise MergeError(f"UnityYAMLMerge is not at {tool}: name it as yaml_merge in upe.toml, or check "
                             f"unity_editor (`upe setup --check` reads both)")
        argv = [str(tool), "merge", "-h", "-p", "--fallback", "none", *extra, str(base), str(theirs), str(mine), str(out)]
        return runner(argv, capture_output=True, text=True, errors="replace").returncode

    return merge


def yaml_merge_flags(project: Path) -> tuple[str, ...]:
    """Flags that keep UnityYAMLMerge's output formatted like the project's own saves.

    UNVERIFIED: that `m_SerializeInlineMappingsOnOneLine: 0` is the setting
    `--nomappinginoneline` must match (research/03 A4).
    """
    settings = project / "ProjectSettings" / "EditorSettings.asset"
    if settings.exists() and re.search(r"m_SerializeInlineMappingsOnOneLine:\s*0", settings.read_text(errors="replace")):
        return ("--nomappinginoneline",)
    return ()


def merge_manifest(base: str | None, ours: str, theirs: str) -> tuple[str | None, list[str]]:
    """Three-way merge of manifest.json by key. Returns (merged text or None, conflicting keys)."""
    b: dict[str, Any] = json.loads(base) if base else {}
    o: dict[str, Any] = json.loads(ours)
    t: dict[str, Any] = json.loads(theirs)
    conflicts: list[str] = []

    def three_way(bm: Mapping[str, Any], om: Mapping[str, Any], tm: Mapping[str, Any], prefix: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        missing = object()
        keys = list(om) + [k for k in tm if k not in om] + [k for k in bm if k not in om and k not in tm]
        for key in keys:
            bv, ov, tv = bm.get(key, missing), om.get(key, missing), tm.get(key, missing)
            if key == "dependencies" and all(isinstance(v, dict) or v is missing for v in (bv, ov, tv)):
                out[key] = three_way(
                    {} if bv is missing else bv, {} if ov is missing else ov, {} if tv is missing else tv, "dependencies."
                )
                continue
            if ov == tv:
                chosen = ov
            elif ov == bv:
                chosen = tv
            elif tv == bv:
                chosen = ov
            else:
                conflicts.append(prefix + key)
                chosen = ov
            if chosen is not missing:
                out[key] = chosen
        return out

    merged = three_way(b, o, t, "")
    if conflicts:
        return None, conflicts
    return json.dumps(merged, indent=2) + "\n", []


class Merger:
    def __init__(
        self,
        repo: Path | str,
        yaml_merger: YamlMerger | None = None,
        builders: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        """`builders` maps a project path (normalized, lower case) to the builder method that writes it."""
        self.repo = Path(repo)
        self.yaml_merger = yaml_merger
        self.builders = {k.replace("\\", "/").lower(): v for k, v in (builders or {}).items()}
        self._runner = runner

    def _git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        completed = self._runner(
            ["git", *SAFE_GIT, "-C", str(cwd), "-c", "merge.renames=false", "-c", "core.autocrlf=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if check and completed.returncode != 0:
            raise MergeError(f"git {' '.join(args)} failed: {(completed.stderr or completed.stdout or '').strip()}")
        return completed

    def _stage(self, cwd: Path, n: int, path: str) -> str | None:
        shown = self._git(cwd, "show", f":{n}:{path}", check=False)
        return shown.stdout if shown.returncode == 0 else None

    # The merge -----------------------------------------------------------

    def merge(
        self,
        task_id: str,
        branch: str,
        target: str,
        worktree: Path | str,
        before_merge: Callable[[Path], None] | None = None,
    ) -> MergeReport:
        """Merge `branch` onto a fresh candidate from `target`, in the worktree at `worktree`.

        A new worktree is made there; an existing one (a reused slot) must
        already have the candidate branch checked out at `target`.
        `before_merge` runs in the worktree while it still holds only the
        target, for example to record problems the target already has.
        """
        worktree = Path(worktree)
        candidate = CANDIDATE_PREFIX + task_id
        report = MergeReport(task_id, branch, target, candidate)
        # Git reads by their full names (`refs/heads/...`): a short name is a tag first when a tag has it. The record
        # and the messages keep the short names (pinned by `test_merge.CleanMerge`; `Merger.land` and the flow read the
        # record's target as `refs/heads/{target}`).
        names = (branch, target)
        branch, target = _branch_ref(branch), _branch_ref(target)
        report.base = self._git(self.repo, "merge-base", target, branch).stdout.strip()
        if worktree.exists():
            # The branch by its full name. `rev-parse --abbrev-ref HEAD` (and `symbolic-ref --short`) print
            # `heads/<name>` when a tag has the branch's name, which stopped every land in that slot; a detached HEAD
            # gives "" here (exit 1).
            on = self._git(worktree, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
            at = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            if on != "refs/heads/" + candidate or at != self._git(self.repo, "rev-parse", target).stdout.strip():
                on = on.removeprefix("refs/heads/") or "a detached HEAD"
                raise MergeError(f"{worktree} is on {on} at {at[:12]}, not on {candidate} at {names[1]}")
        else:
            self._git(self.repo, "worktree", "add", "-b", candidate, str(worktree), target)
        if before_merge is not None:
            # Try the merge first: `before_merge` can take most of an hour, and a conflict needs none of it.
            trial = MergeReport(task_id, *names, candidate, report.base)
            self._attempt(worktree, branch, target, trial)
            if trial.unresolved:
                return trial
            self._git(worktree, "merge", "--abort", check=False)
            self._git(worktree, "reset", "-q", "--hard", "HEAD")
            before_merge(worktree)

            # Unity rewrites and makes files of its own, and git refuses to merge over them. Only those in the
            # way go: `git clean` would also take the empty folders Unity keeps for the folder .metas git tracks.
            self._git(worktree, "reset", "-q", "--hard", "HEAD")
            coming = self._git(worktree, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", f"HEAD...{branch}").stdout
            untracked = self._git(worktree, "ls-files", "-z", "--others").stdout
            for path in set(coming.split("\0")) & set(untracked.split("\0")) - {""}:
                (worktree / path).unlink()

        self._attempt(worktree, branch, target, report)
        if report.unresolved:
            return report  # left uncommitted in the worktree, for a person or a later layer

        message = f"Merge {names[0]} into {names[1]} (candidate for {task_id})"
        self._git(worktree, "commit", "--no-verify", "-q", "-m", message)
        report.committed = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
        return report

    def _attempt(self, worktree: Path, branch: str, target: str, report: MergeReport) -> None:
        """Merge in the worktree and resolve what the layers can. Commits nothing."""
        result = self._git(worktree, "merge", "--no-ff", "--no-commit", branch, check=False)
        conflicted = [
            p for p in self._git(worktree, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if p
        ]
        if result.returncode != 0 and not conflicted:
            raise MergeError(f"git merge failed without conflicts: {(result.stderr or result.stdout).strip()}")

        for path in conflicted:
            report.steps.append(self._resolve(worktree, path, report))

        if LOCK_FILE not in report.regenerate and self._changed_on_both(report.base, target, branch, LOCK_FILE):
            report.steps.append(self._drop_lock(worktree, report))

    def _changed_on_both(self, base: str, target: str, branch: str, path: str) -> bool:
        def changed(rev: str) -> bool:
            return bool(self._git(self.repo, "diff", "--name-only", base, rev, "--", path).stdout.strip())

        return changed(target) and changed(branch)

    def _resolve(self, wt: Path, path: str, report: MergeReport) -> Step:
        lower = path.lower()
        if path == LOCK_FILE:
            return self._drop_lock(wt, report)
        if path == MANIFEST:
            return self._manifest(wt, path)
        if lower.startswith("projectsettings/") and Path(path).name.lower() in SETTINGS_RULES:
            step = self._settings(wt, path)
            return step if step.outcome == "merged" else self._replay_or_stop(wt, path, report, step.detail)
        if lower.endswith(".meta"):
            ours, theirs = self._stage(wt, 2, path), self._stage(wt, 3, path)
            og, tg = _guid(ours), _guid(theirs)
            if og and tg and og != tg:
                return Step(path, "meta-guid", "conflict", f"GUID {og} on the target, {tg} on the branch")
            step = self._yaml(wt, path)
            return step if step.outcome == "merged" else self._replay_or_stop(wt, path, report, step.detail)
        if lower.endswith(YAML_MERGEABLE):
            step = self._yaml(wt, path)
            return step if step.outcome == "merged" else self._replay_or_stop(wt, path, report, step.detail)
        return self._replay_or_stop(wt, path, report, "git could not merge it")

    def _yaml(self, wt: Path, path: str) -> Step:
        if self.yaml_merger is None:
            return Step(path, "unityyamlmerge", "conflict", "no UnityYAMLMerge configured")
        stages = {n: self._stage(wt, n, path) for n in (1, 2, 3)}
        if any(v is None for v in stages.values()):
            return Step(path, "unityyamlmerge", "conflict", "added or deleted on one side; nothing to three-way merge")
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            # UnityYAMLMerge chooses how to merge by the extension; without one it refuses every file.
            ext = Path(path).suffix
            base, mine, theirs, out = (t / f"{name}{ext}" for name in ("base", "mine", "theirs", "out"))
            for p, n in ((base, 1), (mine, 2), (theirs, 3)):
                p.write_text(stages[n], encoding="utf-8", newline="")
            code = self.yaml_merger(base, theirs, mine, out)
            if code != 0 or not out.exists():
                both = changed_on_both(stages[1], stages[2], stages[3])
                where = f": changed on both sides: {', '.join(both)}" if both else ""
                return Step(path, "unityyamlmerge", "conflict", f"UnityYAMLMerge exited {code}{where}")
            merged = restore_unchanged(stages[2], out.read_bytes().decode("utf-8", errors="replace"))
            lost = lost_changes(stages[1], stages[2], stages[3], merged)
            if lost:
                return Step(path, "unityyamlmerge", "conflict", "; ".join(lost))
            (wt / path).write_bytes(merged.encode("utf-8"))
        self._git(wt, "add", "--", path)
        return Step(path, "unityyamlmerge", "merged")

    def _manifest(self, wt: Path, path: str) -> Step:
        base, ours, theirs = (self._stage(wt, n, path) for n in (1, 2, 3))
        if ours is None or theirs is None:
            return Step(path, "manifest", "conflict", "deleted on one side")
        try:
            merged, keys = merge_manifest(base, ours, theirs)
        except json.JSONDecodeError as exc:
            return Step(path, "manifest", "conflict", f"not valid JSON: {exc}")
        if merged is None:
            return Step(path, "manifest", "conflict", "both sides changed " + ", ".join(keys))
        (wt / path).write_text(merged, encoding="utf-8", newline="\n")
        self._git(wt, "add", "--", path)
        return Step(path, "manifest", "merged")

    def _settings(self, wt: Path, path: str) -> Step:
        base, ours, theirs = (self._stage(wt, n, path) for n in (1, 2, 3))
        if base is None or ours is None or theirs is None:
            return Step(path, "project-settings", "conflict", "added or deleted on one side; nothing to three-way merge")
        try:
            merged = merge_settings(Path(path).name, base, ours, theirs)
        except SettingsConflict as exc:
            return Step(path, "project-settings", "conflict", str(exc))
        (wt / path).write_text(merged, encoding="utf-8", newline="\n")
        self._git(wt, "add", "--", path)
        return Step(path, "project-settings", "merged", "tags, layers and build scenes merged as a union")

    def _drop_lock(self, wt: Path, report: MergeReport) -> Step:
        self._git(wt, "rm", "-q", "--force", "--ignore-unmatch", "--", LOCK_FILE)
        report.regenerate.append(LOCK_FILE)
        return Step(LOCK_FILE, "packages-lock", "regenerate", "deleted; Unity rewrites it from the merged manifest")

    def _replay_or_stop(self, wt: Path, path: str, report: MergeReport, why: str) -> Step:
        builder = self.builders.get(path.lower().removesuffix(".meta"))
        if builder is None:
            return Step(path, "unresolved", "conflict", why)
        # Keep the target's version; the builder re-applies the task's change on top.
        if self._stage(wt, 2, path) is None:
            self._git(wt, "rm", "-q", "--force", "--", path)
        else:
            self._git(wt, "checkout", "--ours", "--", path)
            self._git(wt, "add", "--", path)
        if builder not in report.replays:
            report.replays.append(builder)
        return Step(path, "replay", "replay", f"target's version kept; {builder} re-runs on the merged project ({why})")

    # Landing -------------------------------------------------------------

    def land(self, report: MergeReport) -> None:
        """Fast-forward the target to the candidate. Only after the checks and a council GO.

        If the target is checked out in some worktree, the fast-forward runs
        there, so that checkout's files move with the branch.
        """
        if not report.ok:
            raise MergeError("the merge has unresolved conflicts or no candidate commit")
        target_ref = f"refs/heads/{report.target}"
        current = self._git(self.repo, "rev-parse", target_ref).stdout.strip()
        if self._git(self.repo, "merge-base", "--is-ancestor", current, report.committed, check=False).returncode != 0:
            raise MergeError(f"{report.target} moved since the merge; merge again")
        checkout = self._checked_out(target_ref)
        if checkout is not None:
            self._git(checkout, "merge", "--ff-only", "-q", report.committed)
        else:
            self._git(self.repo, "update-ref", target_ref, report.committed, current)

    def discard(self, report: MergeReport, worktree: Path | str) -> None:
        """Throw a candidate away: abort any half-done merge, drop its worktree and branch."""
        worktree = Path(worktree)
        if worktree.exists():
            self._git(worktree, "merge", "--abort", check=False)
            self._git(self.repo, "worktree", "remove", "--force", str(worktree))
        self._git(self.repo, "branch", "-D", report.candidate, check=False)

    def _checked_out(self, ref: str) -> Path | None:
        listing = self._git(self.repo, "worktree", "list", "--porcelain").stdout
        path = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
            elif line == f"branch {ref}" and path is not None:
                return path
        return None


_DOCUMENT = re.compile(r"(?m)^--- !u!\d+ &(-?\d+)[^\n]*$")
# An object's own line as Unity writes it; `_logical` keeps any other as written.
_OBJECT_LINE = re.compile(r"^--- !u!\d+ &-?\d+(?: stripped)?$")
# A YAML document's start or end marker: inside an object it starts a new document there.
_DOCUMENT_MARK = re.compile(r"^(?:---|\.\.\.)(?:[ \t]|$)")
_NAME = re.compile(r"(?m)^\s*m_Name: (.+)$")


def _chunks(text: str) -> tuple[str, list[tuple[str, str]]]:
    """What comes before the first object, then each object's fileID and full text, in order."""
    marks = list(_DOCUMENT.finditer(text))
    if not marks:
        return text, []
    ends = [m.start() for m in marks[1:]] + [len(text)]
    return text[:marks[0].start()], [(m.group(1), text[m.start():end]) for m, end in zip(marks, ends)]


_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
            "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}
_HEX = {"x": 2, "u": 4, "U": 8}


def _unescape(body: str) -> str | None:
    """A double-quoted value's text, its escapes read as YAML reads them; None if an escape is not YAML's."""
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        e = body[i + 1:i + 2]
        if e in _ESCAPES:
            out.append(_ESCAPES[e])
            i += 2
        elif e in _HEX:
            digits = body[i + 2:i + 2 + _HEX[e]]
            if len(digits) != _HEX[e] or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                return None
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                return None
            out.append(chr(code))
            i += 2 + _HEX[e]
        else:
            return None
    return "".join(out)


def _canonical(text: str) -> str:
    """One way to write a text: double-quoted, with only `\\`, `"` and characters YAML cannot hold raw escaped.

    Every value read as text is written this way, so two lines are equal exactly when they hold the
    same text. A value that is not read as text stays as written, and never has this form (see _value_text).
    """
    out = []
    for c in text:
        o = ord(c)
        if c in '\\"':
            out.append("\\" + c)
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        elif c in "\x85\u2028\u2029":
            out.append(f"\\u{o:04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


# YAML 1.1 (2005-01-18), production [27] b-char, without [22] b-line-feed (LF, the line break `_logical` splits on):
# [23] b-carriage-return (a lone CR), [24] b-next-line (U+0085), [25] b-line-separator (U+2028),
# [26] b-paragraph-separator (U+2029). Line breaks to YAML 1.1 (Unity's files say `%YAML 1.1`); to 1.2 only the CR is.
_ODD_BREAKS = "\r\x85\u2028\u2029"
_SINGLE = re.compile(r"'((?:[^']|'')*)'", re.DOTALL)
_DOUBLE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)


def _value_text(raw: str) -> str | None:
    """The text a whole value holds, or None when it is not simply a text, or not surely one.

    Single quotes: `''` is a quote. Double quotes: YAML's escapes. Plain: the text as it stands, only
    when YAML would read it that way: not starting with an indicator (`{`, `[`, `&`, `!`, `#`, `|`,
    `>`, `%`, `@`, a quote, or `-`, `?`, `:` before a space), with no `: ` or ` #` in it, and no line break.
    Values are compared as characters: a plain `1`, `true` or `~` equals a quoted `'1'`, `'true'` or `'~'`,
    and an empty quoted text equals an empty value. YAML would type the plain ones, but Unity reads a value
    by its field's type; single quotes were read this way before double quotes were.
    A value left as written (None) cannot equal a `_canonical` one: if it starts with `"` and is not
    the whole of a valid double-quoted text, it is not what `_canonical` writes.
    """
    if raw[0] == "'":
        m = _SINGLE.fullmatch(raw)
        return m.group(1).replace("''", "'") if m else None
    if raw[0] == '"':
        m = _DOUBLE.fullmatch(raw)
        return _unescape(m.group(1)) if m else None
    if raw[0] in ",[]{}#&*!|>%@`" or raw[0].isspace() or (raw[0] in "-?:" and (len(raw) == 1 or raw[1].isspace())):
        return None
    if ": " in raw or ":\t" in raw or " #" in raw or "\t#" in raw or raw.endswith(":") or "\n" in raw:
        return None
    return raw


# One item of a flow sequence and what ends it: a quoted text whole (a `,` or `]` inside it is text), or a
# plain one up to the first `,` (a plain item holds no `,[]{}`). Only spaces, tabs and line breaks are space
# here: a no-break space is text, so `\s` is not used.
_FLOW_ITEM = re.compile(r"""[ \t\n]*("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^ \t\n,\[\]{}'"][^,\[\]{}]*?)[ \t\n]*(,|\Z)""", re.DOTALL)


def _flow_text(raw: str) -> str | None:
    """A flow sequence of texts (`[a, "b"]`) written one way, or None when it is not surely one.

    Each item is read as a whole value is (`_value_text`) and written as `_canonical` writes it, so
    `[a, 'b']` and `["a", b]` read the same. The whole value stays as written (None) when any item is
    not surely a text: a nested `[...]` or `{...}`, a pair (`a: b`), an empty item or a trailing comma,
    an anchor, alias or tag, a comment, a plain item over a blank line, an escape YAML has not. A
    `{...}` mapping is never read: Unity's `{fileID: ...}` references stay as written. The result
    starts with `[`, so it cannot equal a whole text, which `_canonical` starts with `"`; and read
    again it gives itself, so a value left as written cannot equal it either.
    """
    if raw[0] != "[" or raw[-1] != "]":
        return None
    inner, pos, items = raw[1:-1], 0, []
    if inner.strip(" \t\n"):
        while True:
            item = _FLOW_ITEM.match(inner, pos)
            text = _value_text(item.group(1)) if item else None
            if text is None:
                return None
            if item.group(1)[0] not in "'\"" and (item.group(1)[0] in "?:" or any(c in text for c in "'\"\t")):
                # A plain item starting with `?` or `:`: YAML readers differ (libyaml and ruamel read `[?a]`
                # as a pair `{a: null}`), so in doubt it stays as written; so does one holding a tab (YAML
                # 1.2 allows it, ruamel does not). A plain item holding a quote is where a quote's state is
                # easiest to misread; it stays as written too.
                return None
            items.append(_canonical(text))
            if not item.group(2):  # the end of the sequence; after a `,` an item must follow
                break
            pos = item.end()
    return "[" + ", ".join(items) + "]"


# A quoted key that reads the same bare: `'m_MeshMetrics[0]': 1`. One with `:`, `#` or a space at
# an end would not, so it keeps its quotes.
_QUOTED_KEY = re.compile(r"^(\s*(?:- )?)'([^\s'\"#:,\[\]{}&*!|>%@`-](?:[^':#]|'')*?(?<=\S))':(?= |$)")
# A key (or a list item's dash) and the value after it, the value whole. A `- ` is a list item's dash,
# never the start of a key, so `- 'a: b'` is an item holding the text `a: b`, not a key `- 'a`; after a
# dash a quoted key is read whole (`- 'a: b': c`), as the dash read it before as the key's first character.
# A dash may be followed by more than one space (`-  k: v`), as in `_LINE`; `- : a` has an empty key.
_ENTRY = re.compile(r"^(\s*(?:- +)+(?:'(?:[^'\n]|'')*'|\"(?:[^\"\\\n]|\\[^\n])*\")[ \t]*: "
                    r"|\s*(?:- +)*(?!- )[^\s'\"\n][^:\n]*: "
                    r"|\s*(?:- +)+: "
                    r"|\s*- +)(\S.*)$", re.DOTALL)


# A dash ending its line is a list item whose value is below; a dash may be followed by more than one space.
_LINE = re.compile(r"^(\s*)((?:-(?: +|$))*)(.*)$")
# A plain key may hold a `:` with no space after it (`a:b: c`): only `: ` or a `:` ending the line ends a key.
_KEY = re.compile(r"^('(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\"|[^\s'\"#\-?:,\[\]{}](?:[^:]|:(?=\S))*?|-[^\s:](?:[^:]|:(?=\S))*?):(?: (.*)|$)")


class _Doubt(Exception):
    """A value whose quotes YAML readers read in different ways: its object is compared exactly as written."""


def _scan(value: str) -> tuple[str | None, int, bool, int | None]:
    """At the end of a value's text so far: the quote it is inside (or None), how many `{`/`[` are open,
    whether it is inside a comment, and where its first node ended (the index in `value` just after the quote
    or bracket that closed it at depth 0, or None while it is open or when the value is plain).

    In YAML a quote or a bracket opens only at the start of a value, or of an entry inside a `{`/`[`
    collection: after `[`, `{` or `,`, or after a `:` that is a value indicator. In a flow, a quote
    straight after a `:` (no space between) is read one of three ways, by what comes before the `:`:
    - a character `str.isalnum()` accepts, or `_` (glued, `[a:"b]`): the `:` and the quote are text.
      ruamel and PyYAML read one plain item `a:"b`; older libyaml rejects the form; no reader reads a pair;
    - a closing quote, `]` or `}` (`{"a":"b"}`): the `:` is a value indicator, and the quote opens;
    - anything else (`{:"b"}`, `{a: 1, :"b"}`, `{k :"b"}`, `{?:"b"}`, `[a.:"b]`): readers differ (ruamel reads
      `{k :"b"}` and `{?:"b"}` as a pair, `[a :"b"]` as one plain item), so this raises `_Doubt` and
      the object is compared exactly as written.
    In a flow a quote straight after a `?` (an explicit key, `[? "b"]`, `[a,? # c` then `"b"`) raises
    `_Doubt` too, and so do a `#` straight after a `?` or after a `:` that is not glued (`[a, ?# c`, `{"k":# c`: a
    comment to ruamel, though no space is before it) and an anchor or a tag
    at a node's start (`[a, &x "b`, `{"a": !t "b`: the quote after it opens in YAML). (A value that starts with an
    anchor or a tag, `&x "b`, never reaches here: `_named` doubts it first.) A quote after `: ` (a space after
    the `:`) opens. Elsewhere quotes and brackets are text: in a plain value (`Slot [1`, `Rock 'n roll`). Inside a
    flow a `#` after space, or straight after `[`, `{` or `,` (ruamel and libyaml read `[a,#c` as a comment),
    starts a comment, which runs to the line's end; `_logical` keeps that line break. So does a `#` once the first
    node has closed at depth 0 (`{fileID: 0} # [`, and `'a'#[`, a comment to ruamel with no space before it).

    For the next author: a branch that reads a form as text names the forms it reads,
    and its last branch (the `else`) raises `_Doubt`; a set of characters comes from the spec's production, named
    by its number, not from the cases seen so far: a branch whose `else` reads a form as text lets through a form
    nobody listed. `_value_text` is not built this way on purpose: it takes any
    plain text, and a list of allowed texts there would stop every unusual value.
    """
    stripped = value.lstrip()
    offset, value = len(value) - len(stripped), stripped
    if not value or value[0] not in "'\"{[":
        return None, 0, False, None
    depth, quote, comment, i, end = 0, None, False, 0, None
    last, spaced, colon = "", False, ""  # the last mark outside quotes and comments; space after it; the last `:`'s kind
    closer = False  # whether `last` is a quote that closed a quoted text (not a quote read as text, `[-k"`)
    while i < len(value):
        c = value[i]
        if comment:
            comment = c != "\n"
        elif quote == "'":
            if c == "'":
                if value[i + 1:i + 2] == "'":
                    i += 1
                else:
                    quote, last, spaced, closer = None, "'", False, True
                    end = i + 1 + offset if end is None and depth == 0 else end
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote, last, spaced, closer = None, '"', False, True
                end = i + 1 + offset if end is None and depth == 0 else end
        elif c in " \t\n":
            spaced = True
        elif c == "#" and depth > 0 and not spaced and (last == "?" or (last == ":" and colon != "glued")):
            raise _Doubt(value)  # `[a, ?# c`, `{"k":# c`: a comment to ruamel with no space before it, in doubt
        elif c == "#" and depth > 0 and (spaced or last in ("[", "{", ",")):
            comment = True
        elif c == "#" and depth == 0 and end is not None:  # after the first node closed (`{fileID: 0} # [`)
            comment = True  # a comment (to ruamel with no space before it too), so a `[` opens nothing
        elif c in "&!" and depth > 0 and last in ("[", "{", ",", "?", ":"):  # a property at a node's start
            raise _Doubt(value)
        elif c in "'\"" and depth > 0 and last == "?":  # an explicit key in a flow (`[? "b`): in doubt
            raise _Doubt(value)
        elif c in "'\"" and (i == 0 or (depth > 0 and (last in ("[", "{", ",") or (last == ":" and (spaced or colon != "glued"))))):
            if last == ":" and not spaced and colon == "other":
                raise _Doubt(value)
            quote = c
        else:
            if depth > 0 and last == ":" and not spaced and colon == "other":
                raise _Doubt(value)  # `{? :#`, `[:a`: a mark straight after a `:` that is not glued; readers differ
            if c == ":":
                if spaced or last in ("", "[", "{", ",", "?"):
                    colon = "other"  # after space (`k :`, `"a" :`), at an item's start, or after `?` (`{?:"b`)
                elif last in ("]", "}") or (last in ("'", '"') and closer):
                    colon = "closed"
                elif last.isalnum() or last == "_":
                    colon = "glued"  # plain text (`a:"b`): the named set; a mark the corpus needs is named here
                else:
                    colon = "other"  # any other mark (`.`, `#`, `!`, a CR): not named, so in doubt
            depth += 1 if c in "{[" else -1 if c in "}]" else 0
            if c in "}]" and depth == 0 and end is None:
                end = i + 1 + offset
            last, spaced, closer = c, False, False
        i += 1
    return quote, depth, comment, end


def _state(value: str) -> tuple[str | None, int]:
    """At the end of a value's text so far: the quote it is inside (or None), and how many `{`/`[` are open."""
    return _scan(value)[:2]


def _open(value: str) -> bool:
    """Whether a value is cut off at the end of its line: inside quotes, or a `{`/`[` not closed."""
    quote, depth = _state(value)
    return quote is not None or depth > 0


# YAML 1.2.2 [22] c-indicator, less what `_LINE`, `_KEY` and `_scan` read: at a line's start (after its dashes), and at
# a value's start (where `?`, `:` and `-` may begin a plain text, `-1`, `:a`).
_NOT_A_LINE_START = tuple("?:,]}#&*!|>%@`")
_NOT_A_VALUE_START = tuple(",]}#&*!|>%@`")


def _named(start: str, value: str) -> None:
    """Raise `_Doubt` for a line whose start `_logical` does not read, so its object is compared as
    written. `start` is the line after its dashes, `value` the value `_logical` reads from it.

    `_LINE` and `_KEY` read a line as `key: value` or as a lone value, and nothing else. They name these forms:
    a key `_KEY` takes, then its value; a lone value. Every other start is in doubt, by YAML 1.2.2's [22]
    `c-indicator`, less the ones read (`-` as a dash or in a key, `[`, `{`, `'`, `"`):
    - a line that starts, after its dashes, with `?` (an explicit key), `:` (an empty key, `- : a`, or a key starting
      with `:`), `,`, `]`, `}`, `#`, `&` or `!` (a property, or `&x {a` read as a key), `*` (an alias), `|` or `>` (a
      block scalar, or `| # c:` read as a key), `%`, `@` or a backtick; or with a dash then a tab (`-<TAB>"b`);
    - a value that starts (past spaces) with one of `,]}#&*!|>%@` or a backtick: a property before the node (its
      quote opens in YAML), a comment (the value is on the next line), an alias, or a block scalar (`|`, `>`).
    Every block scalar is in doubt (none is in a large project's thousands of YAML files), which closes how its
    header, its lines' end spaces, its `+` chomping, a header alone on its line and `- |` are read. The empty key is
    in doubt too: with `%YAML 1.1`, as every Unity file starts, ruamel rejects it, and its tail was not read whole.
    The note of `_scan` for the next author holds here too: a form the code does not name raises `_Doubt`.
    """
    if start[:1] in _NOT_A_LINE_START or start.startswith("-\t"):
        raise _Doubt(start)
    if value.lstrip(" \t")[:1] in _NOT_A_VALUE_START:  # `k:  &x "a`: a space more is still the value's start
        raise _Doubt(value)


def _whole_node(value: str) -> None:
    """Raise `_Doubt` when a lone value (no key read) is a key `_KEY` does not read: one that starts
    with a quote or a bracket and is followed, once it closes, by anything but a comment (`"k" : v`, `[a]: v`,
    `{a}: v`, and `"b" [c`, which opens a bracket), or a plain one holding a `:` before a space or a
    tab or at its end (`-: v`: a key `-`). YAML reads a closed node followed by text only as a key, or as the next
    line of a plain text above it: Unity folds a list item's text at the item's own column (`- Root ... part.01`
    then `    [part.03]#7/Part column#0`), and `_logical` reads such a line before it comes here (`_as_written`)."""
    if value[:1] not in ("'", '"', "[", "{"):
        if re.search(r":(?:[ \t]|$)", value):
            raise _Doubt(value)
        return
    quote, depth, _, end = _scan(value)
    if end is not None:  # the first node closed; what follows may still open a bracket (`"k" : "{a`)
        tail = value[end:]
        if tail.strip(" \t") and not (tail[0] in " \t" and tail.lstrip(" \t").startswith("#")):
            raise _Doubt(value)


def _escapes_the_break(line: str) -> bool:
    """Whether a line ends in an odd number of backslashes: the last one escapes the line break."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _as_written(line: str) -> bool:
    """Whether `_plain` keeps a line exactly as written: it rewrites a line only through `_QUOTED_KEY` (a quoted key)
    and `_ENTRY` (a key's or a dash's value). Read with its line break kept and nothing joined to it, such a line
    is text to the check as to YAML (a plain text's next line), so a change to it shows as a change."""
    return not (_QUOTED_KEY.match(line) or _ENTRY.match(line))


def _logical(chunk: str) -> list[str]:
    """An object's lines, with a value the Editor or UnityYAMLMerge folded onto more lines joined.

    UnityYAMLMerge folds long values (a `{fileID, guid, type}` reference, a long name) at another
    column than the Editor, and joins lines the Editor folded. A line continues the one before only
    when that one's value is cut off (open quotes or brackets), or when it has a value and this line
    is deeper than its key: then YAML reads the break and the indentation as one space. Inside double
    quotes, a `\\` ending a line escapes the break, which then reads as nothing, and each blank line
    after it as one line feed. A new key, or a list item at the key's depth, is never joined, so
    structure that means something stays. A block scalar (`|`, `>`) is in doubt (`_named`).

    The object's own line (`--- !u!114 &20`, maybe `stripped`) is read apart and nothing is joined to it; any other
    form of it, and a `---` or `...` line inside the object, raise `_Doubt`. A root that is no
    mapping is then read as any list or text is (` - "b\\ ` / `   c"` is a list item; `x` then `- 'b'` a text
    whose next line, past column -1, is a dash's: in doubt).

    A plain text with no key on its line (`- Root ... part.01`, or a text below its key or below a dash alone) goes
    on, to YAML, on every later line deeper than the node that holds it: its dash, a dash alone on the line above, or its key; the line at the text's own column too. Unity folds list items so
    (`    [part.03]#7/Part column#0`). Such a line is kept as written, its break kept and nothing joined to it (no quote
    or bracket in it opens), when `_plain` would not rewrite it (`_as_written`); a line it would rewrite, a dash's or
    a key's (`    - 'b'`), raises `_Doubt`.
    """
    out: list[str] = []
    key_col, has_value, cut, blanks, value, lone, owner_col = -1, False, False, 0, "", False, -1
    text_zone: tuple[int, int] | None = None  # the columns where a line still goes on a plain text above it
    for whole in chunk.split("\n"):
        line = whole.rstrip(" \t")  # YAML trims only spaces and tabs; a no-break space is text
        # Inside double quotes `\ ` or `\<tab>` at a line's end is an escaped space or tab, which stays;
        # anywhere else whitespace at a line's end is nothing, even after a `\`.
        escaped_end = whole[len(line)] if line != whole and _escapes_the_break(line) else ""

        def ends_in_double_quotes(text: str) -> bool:
            return bool(escaped_end) and _state(text)[0] == '"'

        if not line:  # inside a value a blank line is a line break; which, the next line tells
            blanks += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not out:  # the object's own line: `--- !u!114 &20`, maybe `stripped`, and nothing else
            if not _OBJECT_LINE.match(line):
                raise _Doubt(line)
            out.append(line)
            continue
        if _DOCUMENT_MARK.match(line):  # a `---` or `...` inside the object starts a YAML document there
            raise _Doubt(line)
        if cut or (has_value and indent > key_col):
            if cut and _state(value)[0] == '"' and _escapes_the_break(out[-1]):
                # A `\` ending a line in double quotes: the break is nothing, and each blank line after it is
                # one line feed (YAML 1.2, 7.3.1, s-double-escaped: `\`, the break, then l-empty lines).
                out[-1], value = out[-1][:-1], value[:-1]
                joined = "\n" * blanks + line.strip(" \t")
            elif cut and _scan(value)[2]:  # a comment ends at its line's end: the break stays, so it still ends there
                joined = "\n" * max(blanks, 1) + line.strip(" \t")
            else:
                joined = "\n" * blanks + (" " if not blanks else "") + line.strip(" \t")
            if ends_in_double_quotes(value + joined):
                joined += escaped_end
            out[-1] += joined
            value += joined  # the value read from its own start, so a `: ` inside it changes nothing
            blanks = 0
            if cut:
                cut = _open(value)
            if lone and not cut:
                _whole_node(value)
            continue
        out.extend([""] * blanks)
        blanks = 0
        out.append(line)
        lead, dashes, rest = _LINE.match(line).groups()
        if text_zone and text_zone[0] < indent:  # a next line of a plain text above, to YAML
            if not _as_written(line):
                raise _Doubt(line)
            # Kept as written, and nothing joined to it: every deeper line goes on the same text
            # (`    -` then `      'b'` was joined, and the joined line rewritten)
            key_col, has_value, cut, value, lone = indent, False, False, rest, False
            continue
        parent_col = len(lead) + len(dashes.rstrip()) - 1 if dashes else owner_col
        key_col = len(lead) + len(dashes)
        key = _KEY.match(rest)
        value = (key.group(2) or "") if key else rest
        lone = not key  # a node with no key: after it the line holds nothing but a comment
        _named(rest, value)
        if lone:
            _whole_node(value)
        if ends_in_double_quotes(value):
            out[-1] += escaped_end
            value += escaped_end
        has_value = bool(value)
        cut = _open(value)
        plain = lone and value[:1] not in ("", "'", '"', "[", "{")
        text_zone = (parent_col, key_col) if plain else None
        # The column a node below this line must be deeper than: a dash alone's own column (`-` then `    x`: the text
        # there goes on at any column past the dash), else the key's or the item's
        owner_col = len(lead) + len(dashes.rstrip()) - 1 if dashes and not rest else key_col
    return out


def _plain(chunk: str) -> str:
    """An object without what UnityYAMLMerge rewrites: folding, how a value or a key is quoted, and
    trailing space.

    Each whole value that is a text is written one way (`_canonical`), so `Boss Room`, `'Boss Room'`
    and `"Boss Room"` read the same, and `'Boss '` and `Boss` do not, since YAML keeps the space only
    inside quotes. A value that is not surely a text (a `{...}` reference, a plain value with `: `
    or ` #` in it, an escape YAML has not) stays as written and cannot equal a text. A `[...]` of texts
    is read item by item (`_flow_text`), so `[a, 'b']` and `["a", b]` read the same. Quotes are read
    once per joined line, so text inside a folded quoted value is never read as a key. Formatting
    this does not cover shows as a change, which can stop a merge but cannot lose anything.

    An object holding a lone CR (U+000D), U+0085, U+2028 or U+2029 as written is compared exactly as
    written. A lone CR is a line break to YAML 1.1 and 1.2 alike, but only `\\r\\n` is made `\\n` here
    and lines are split on `\\n`; the other three are line breaks to YAML 1.1 (Unity's files say
    `%YAML 1.1`) and characters to 1.2. Either way how the object's lines fold and join is in doubt.
    `_canonical` writes all four escaped, so no other object's reading holds one. (A large project's thousands of
    YAML files hold none; `\\r`, `\\N`, `\\L`, `\\P` escapes are read as usual.) An object with a quote
    in doubt (`_scan` raises `_Doubt`) is compared as written too: its reading is never computed, and
    a reading adds no such quote that its own text did not hold.
    """
    as_written = chunk[:-1] if chunk.endswith("\n") else chunk  # its blank lines at the end kept (a `|+` text's)
    if any(c in chunk for c in _ODD_BREAKS) or any(c.isspace() and c not in " \t\n" for c in chunk):
        # A space YAML reads as text (U+00A0, U+3000, U+2003, a form feed) is one to Python's `\s`,
        # `strip()` and `lstrip()`, which read indentation, keys and quotes here. Unity's files hold none.
        return as_written
    try:
        logical = _logical(chunk)
    except _Doubt:  # a quote YAML readers read in different ways (see _scan): compared as written too
        return as_written
    lines = []
    for line in logical:
        key = _QUOTED_KEY.match(line)
        if key:
            line = key.group(1) + key.group(2).replace("''", "'") + line[key.end() - 1:]
        entry = _ENTRY.match(line)
        flow = _flow_text(entry.group(2)) if entry else None
        text = _value_text(entry.group(2)) if entry and flow is None else None
        if flow is not None:
            line = entry.group(1) + flow
        elif text is not None:
            line = entry.group(1) + _canonical(text) if text else entry.group(1).rstrip()
        lines.append(line)
    return "\n".join(lines).strip("\n")  # lines are right-stripped already; a quoted value's end space stays


def _documents(text: str) -> dict[str, str]:
    """A scene or prefab's objects by fileID, without their formatting.

    The `--- !u!` line is kept: its class and its `stripped` flag (a fragment of a prefab
    instance, not a full object) are part of the object, and UnityYAMLMerge can drop the flag.
    """
    return {object_id: _plain(chunk) for object_id, chunk in _chunks(text.replace("\r\n", "\n"))[1]}


def restore_unchanged(mine: str, merged: str) -> str:
    """Give each object the merge did not change its exact text from the target.

    UnityYAMLMerge rewrites formatting across the whole file: 700 changed lines in a project's
    largest scene for a two-line merge. Without them the diff the council reads is the real one.
    """
    ours = dict(_chunks(mine.replace("\r\n", "\n"))[1])
    head, chunks = _chunks(merged.replace("\r\n", "\n"))
    return head + "".join(ours[i] if i in ours and _plain(ours[i]) == _plain(chunk) else chunk for i, chunk in chunks)


def _label(object_id: str, *texts: str) -> str:
    for text in texts:
        pattern = rf"(?m)^--- !u!\d+ &{re.escape(object_id)}[^\n]*\n(?:(?!^--- ).*\n)*?\s*m_Name: (.+)$"
        match = re.search(pattern, text.replace("\r\n", "\n"))
        if match and match.group(1).strip():
            return f"object {object_id} ({match.group(1).strip()})"
    return f"object {object_id}"


def changed_on_both(base: str, mine: str, theirs: str) -> list[str]:
    """The objects both sides changed, each its own way: where a stopped merge needs a person's eye."""
    was, ours, other = _documents(base), _documents(mine), _documents(theirs)
    return [_label(i, base, mine, theirs) for i in sorted(set(was) | set(ours) | set(other))
            if ours.get(i) != was.get(i) and other.get(i) != was.get(i) and ours.get(i) != other.get(i)]


def _moved(before: str, side: str) -> set[str]:
    """The lines, each found once in the base and the side, in a pair the side put in the other order.

    Both lines of a swap count, so it does not matter which of them the matcher calls moved.
    """
    counts = [Counter(text.split("\n")) for text in (before, side)]
    once = {line for line in counts[0] if all(c[line] == 1 for c in counts)}
    was, now = ([line for line in text.split("\n") if line in once] for text in (before, side))
    if was == now:
        return set()
    kept = set()
    for block in difflib.SequenceMatcher(None, was, now, autojunk=False).get_matching_blocks():
        kept.update(was[block.a:block.a + block.size])
    at_was, at_now = ({line: i for i, line in enumerate(seq)} for seq in (was, now))
    involved = set()
    for moved in (line for line in now if line not in kept):
        crossed = {other for other in now if (at_was[moved] < at_was[other]) != (at_now[moved] < at_now[other])}
        involved |= crossed | ({moved} if crossed else set())
    return involved


def _order_kept(before: str, side: str, merged: str) -> bool:
    """Whether a side that put lines in a new order still has that order in the merged object.

    Compared pair by pair: every two lines the side put in the other order must be in its order
    in the result, so two reorders elsewhere in one object, one on each side, can both be kept.
    Only lines found once in the base, the side and the result are compared, so a move among
    repeated lines (`0, 1, 0` made `1, 0, 0`) is not caught.
    """
    counts = [Counter(text.split("\n")) for text in (before, side, merged)]
    once = {line for line in counts[0] if all(c[line] == 1 for c in counts)}
    was, now, out = ([line for line in text.split("\n") if line in once] for text in (before, side, merged))
    if was == now:
        return True
    # The lines outside the longest run kept in order are the ones the side moved; any pair it
    # put in a new order has one of them in it.
    kept = set()
    for block in difflib.SequenceMatcher(None, was, now, autojunk=False).get_matching_blocks():
        kept.update(was[block.a:block.a + block.size])
    at_was, at_now, at_out = ({line: i for i, line in enumerate(seq)} for seq in (was, now, out))
    for moved in (line for line in now if line not in kept):
        for other in now:
            if other != moved and (at_was[moved] < at_was[other]) != (at_now[moved] < at_now[other]):
                if (at_out[moved] < at_out[other]) != (at_now[moved] < at_now[other]):
                    return False
    return True


def _edits(before: list[str], side: list[str], from_end: bool) -> list[tuple[int, int, list[str]]]:
    """A side's edits to the base as (start, end, new lines), aligned from the start or the end.

    In a run of equal lines the alignment is a guess: at the end of an object, `0, 0` made `5, 0`
    reads from the start as an insert and a delete of the last `0`, and from the end as the first
    slot changed. Both are true edit scripts, so the caller tries each.
    """
    if not from_end:
        matcher = difflib.SequenceMatcher(None, before, side, autojunk=False)
        return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]
    n, m = len(before), len(side)
    matcher = difflib.SequenceMatcher(None, before[::-1], side[::-1], autojunk=False)
    return [(n - i2, n - i1, side[m - j2:m - j1]) for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()) if tag != "equal"]


def _placements(before: list[str], edits: list[tuple[int, int, list[str]]], limit: int = 16) -> list[list]:
    """The same edit script with each insert or delete of copies of one line moved to either end
    of the run of that line it sits in: deleting one `1` from `1, 1, 1`, or adding one, reads
    the same wherever it was, and the two ends are the readings that differ against the other
    side's edit. A replaced line's place is known and stays.
    """
    choices = []
    for start, end, new in edits:
        lines = new if start == end else before[start:end] if not new else []
        if not lines or len(set(lines)) != 1:
            choices.append([(start, end, new)])
            continue
        line, size = lines[0], end - start
        low, high = start, end
        while low > 0 and before[low - 1] == line:
            low -= 1
        while high < len(before) and before[high] == line:
            high += 1
        options = {(start, end), (low, low + size), (high - size, high)}
        choices.append([(s, e, new) for s, e in sorted(options)])
    scripts = [[]]
    for options in choices:
        # A side's own edits always have an unchanged line between them; a placement without one
        # is not a reading of that side.
        scripts = [script + [option] for script in scripts for option in options
                   if not script or script[-1][1] < option[0]][:limit]
    return scripts or [list(edits)]


def _layout(before: list[str], a: list[tuple[int, int, list[str]]], b: list[tuple[int, int, list[str]]]) -> list:
    """The merged object as a line merge predicts it: each side's edit where it made it, and a `_Clash`
    where the two sides' edits overlap or touch.

    Edits apart from each other have one right result, so this can see a lost move among
    repeated lines or a swap the other side's edit undid, which counting lines cannot.
    """
    edits = sorted([(*e, 0) for e in a] + [(*e, 1) for e in b], key=lambda e: (e[0], e[1]))
    groups: list[list] = []
    for edit in edits:
        if groups:
            end = max(e[1] for e in groups[-1])
            # Overlapping edits clash, and so does an insert at the edge of the other side's edit:
            # UnityYAMLMerge puts it on either side. Two replaces side by side do not clash.
            at_edge = edit[0] == end and (edit[0] == edit[1] or any(e[0] == e[1] == end for e in groups[-1]))
            if edit[0] < end or at_edge:
                groups[-1].append(edit)
                continue
        groups.append([edit])
    out, at = [], 0
    for group in groups:
        start, end = min(e[0] for e in group), max(e[1] for e in group)
        out += before[at:start]
        out += group[0][2] if len(group) == 1 else [_Clash(before, start, end, group)]
        at = end
    return out + before[at:]


class _Clash:
    """Where the two sides' edits overlap, or both insert at one place: the merger may order the
    lines any way, and each line's count is the base's there plus what each side added or removed,
    a change both sides made counting once or twice. A base line one side deleted and the other
    replaced with something has no result that keeps both, so that reading allows nothing."""

    def __init__(self, before: list[str], start: int, end: int, group: list):
        deleted, replaced = (set(), set()), (set(), set())
        deltas: tuple[dict[str, int], dict[str, int]] = ({}, {})
        for s, e, new, side in group:
            (replaced if new else deleted)[side].update(range(s, e))
            for line in new:
                deltas[side][line] = deltas[side].get(line, 0) + 1
            for line in before[s:e]:
                deltas[side][line] = deltas[side].get(line, 0) - 1
        self.possible = not (deleted[0] & replaced[1] or deleted[1] & replaced[0])
        self.ranges = {}
        base = Counter(before[start:end])
        for line in set(base) | set(deltas[0]) | set(deltas[1]):
            was, d1, d2 = base[line], deltas[0].get(line, 0), deltas[1].get(line, 0)
            if d1 * d2 < 0:
                low = high = was + d1 + d2
            elif d1 >= 0 and d2 >= 0:
                low, high = was + max(d1, d2), was + d1 + d2
            else:
                low, high = max(0, was + d1 + d2), was + min(d1, d2)
            if high < 0 or low > high:
                self.possible = False
            self.ranges[line] = (low, high)

    def holds(self, lines: list[str]) -> bool:
        counts = Counter(lines)
        return self.possible and all(line in self.ranges for line in counts) and \
            all(low <= counts[line] <= high for line, (low, high) in self.ranges.items())



def _fits(layout: list, merged: list[str]) -> bool:
    """Whether the merged lines match a layout, each clash standing for a run of the lines it allows."""
    parts, clashes = [[]], []
    for line in layout:
        if isinstance(line, _Clash):
            clashes.append(line)
            parts.append([])
        else:
            parts[-1].append(line)
    if not clashes:
        return merged == parts[0]
    first, *middle, last = parts
    if merged[:len(first)] != first or len(merged) < len(first) + len(last):
        return False
    if last and merged[len(merged) - len(last):] != last:
        return False
    at, stop, spans = len(first), len(merged) - len(last), []
    for part in middle:  # the leftmost place for each part leaves the most room for the rest
        found = next((i for i in range(at, stop - len(part) + 1) if merged[i:i + len(part)] == part), None)
        if found is None:
            return False
        spans.append(merged[at:found])
        at = found + len(part)
    spans.append(merged[at:stop])
    return all(clash.holds(span) for clash, span in zip(clashes, spans))


def _both_kept(before: str | None, a: str, b: str, merged: str | None) -> bool:
    """Whether an object both sides changed has each side's changes, counted line by line.

    For every line, the merged object must have the base's count plus what each side added or
    removed. Both sides making the same change counts once or twice; one side adding a line the
    other removed is a clash. A side that reordered lines (children, components) must have its
    order in the merged object. Anything this cannot account for stops the merge, which cannot
    lose a change.
    """
    if merged is None:
        return False
    base_lines, out_lines = (before or "").split("\n"), merged.split("\n")
    scripts = [[placed for from_end in (False, True) for placed in _placements(base_lines, _edits(base_lines, side.split("\n"), from_end))]
               for side in (a, b)]
    fitting = [layout for layout in (_layout(base_lines, ea, eb) for ea in scripts[0] for eb in scripts[1])
               if _fits(layout, out_lines)]
    if not fitting:
        return False
    # A result that fits a reading with no clash is that reading's line merge: both sides' edits,
    # each where it was made. The counts below are for where the merger had a choice.
    if any(not any(isinstance(line, _Clash) for line in layout) for layout in fitting):
        return True
    if not all(_order_kept(before or "", side, merged) for side in (a, b)):
        return False
    was, ours, other, out = (Counter((text or "").split("\n")) for text in (before, a, b, merged))
    # A line one side moved and the other removed or rewrote: no result keeps both.
    if any(other[line] < was[line] for line in _moved(before or "", a)) or \
            any(ours[line] < was[line] for line in _moved(before or "", b)):
        return False
    for line in set(was) | set(ours) | set(other) | set(out):
        d1, d2 = ours[line] - was[line], other[line] - was[line]
        if d1 * d2 < 0:
            return False
        lo = max(was[line] + d1, was[line] + d2) if d1 >= 0 and d2 >= 0 else min(was[line] + d1, was[line] + d2)
        hi = was[line] + d1 + d2
        if not min(lo, hi) <= out[line] <= max(lo, hi):
            return False
    return True


def lost_changes(base: str, mine: str, theirs: str, merged: str) -> list[str]:
    """What one side changed in a scene or prefab that the merged file does not have.

    UnityYAMLMerge exits 0 with one side's rename gone when the other side deleted the object
    (research/05, "Merging real files"), so its exit code does not show that both sides were kept.
    In an object both sides changed, each side's changed lines must be in the merged object
    (`_both_kept`).
    """
    was, ours, other, out = _documents(base), _documents(mine), _documents(theirs), _documents(merged)
    lost = []

    def label(object_id: str) -> str:
        return _label(object_id, mine, theirs, base)

    for object_id in sorted(set(was) | set(ours) | set(other)):
        before, a, b = was.get(object_id), ours.get(object_id), other.get(object_id)
        if a is None and b is None:
            if object_id in out:
                lost.append(f"{label(object_id)} deleted on both sides and still in the merged file")
        elif a != before and b != before:
            if (a is None) != (b is None):
                lost.append(f"{label(object_id)} deleted on one side and changed on the other")
            elif a is not None and not _both_kept(before, a, b, out.get(object_id)):
                lost.append(f"{label(object_id)}: changed on both sides, and a change is not in the merged object")
        elif a != before or b != before:
            wanted = a if a != before else b
            if out.get(object_id) != wanted:
                lost.append(f"{label(object_id)}: one side's change is not in the merged file")
        elif out.get(object_id) != before:
            lost.append(f"{label(object_id)}: changed or removed in the merged file, though neither side changed it")
    for object_id in sorted(set(out) - set(was) - set(ours) - set(other)):
        lost.append(f"{_label(object_id, merged)}: in the merged file, though neither side has it")
    lost.extend(_head_lost(base, mine, theirs, merged))
    return lost


def _head(text: str) -> str:
    """What a file holds before its first object (`%YAML 1.1`, `%TAG !u! ...`), as written, its line ends made LF."""
    return _chunks(text.replace("\r\n", "\n"))[0]


def _head_lost(base: str, mine: str, theirs: str, merged: str) -> list[str]:
    """The text before the first object, compared as written as an object is (`_documents`
    reads only the objects):
    `_documents` reads only the objects, so a side's change there (a `%TAG` line, a document or a comment added) was
    lost with no stop. Two different changes are counted line by line as an object's are (`_both_kept`): a merge that
    took one side's head stops.

    A file that holds no `--- !u!` object is compared whole, as written, the same way. The common one on this path is
    a `.meta` file, which `Merger._yaml` sends to UnityYAMLMerge and then here; before this, nothing in a `.meta`
    merge was compared. (On `.meta` files with one line changed on one
    side or on each, UnityYAMLMerge exits 1 and writes no merged file, so none reaches this today.)"""
    was, a, b, out = _head(base), _head(mine), _head(theirs), _head(merged)
    objects = any(_chunks(text)[1] for text in (base, mine, theirs, merged))
    where = "the text before the first object" if objects else "the file (it holds no object)"
    if a != was and b != was:
        if a == b:
            return [] if out == a else [f"{where}: both sides' change is not in the merged file"]
        return [] if _both_kept(was, a, b, out) else \
            [f"{where}: changed on both sides, and a change is not in the merged file"]
    if a != was or b != was:
        wanted = a if a != was else b
        return [] if out == wanted else [f"{where}: one side's change is not in the merged file"]
    return [] if out == was else [f"{where}: changed in the merged file, though neither side changed it"]


def _guid(meta: str | None) -> str | None:
    if not meta:
        return None
    match = _GUID.search(meta)
    return match.group(1).lower() if match else None
