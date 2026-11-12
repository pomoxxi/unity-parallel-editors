"""Automated checks: step 4 of the flow in DESIGN.md.

Static checks, run in Python on the merged worktree, no Unity needed:
- no conflict markers left in any changed text file
- every scene, prefab and YAML asset still parses as Unity YAML
- no fileID appears twice in one file
- every same-file `{fileID: n}` reference points at an object in the file
- every `{guid: g}` reference points at an asset in the project or its
  packages (built-in GUIDs are skipped)
- Transform parents and children agree (`m_Father` and `m_Children`)
- every asset under Assets/ has a `.meta`, every `.meta` has its asset,
  and no GUID is used by two `.meta` files

Then the Unity checks: a batch Editor loads the scenes and prefabs and
reports missing scripts, prefabs and references (`scene_checks`), and the
project compiles and passes its EditMode tests through the `unity`
CLI wrapper (`unity_checks`).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .cmdline import ARGV_CAP, capped
from .editor import Editor, EditorError, compile_errors
from .unity_cli import ExitCode, UnityCli

# The caps of the two Unity lines that grow with a land, read when a run starts (a test sets them small).
SCENE_PATHS_CAP = ARGV_CAP  # the characters of `-upePaths` one batch Editor is given
FILTER_CAP = ARGV_CAP  # the characters of the `--filter` class list one `unity test` is given

UNITY_YAML = (".unity", ".prefab", ".asset", ".mat", ".controller", ".anim", ".overrideController", ".playable", ".mask")
TEXT_SUFFIXES = UNITY_YAML + (".meta", ".cs", ".json", ".asmdef", ".asmref", ".shader", ".hlsl", ".uss", ".uxml")
TRANSFORM_CLASSES = {"4", "224"}  # Transform, RectTransform

_HEADER = re.compile(r"^--- !u!(\d+) &(-?\d+)( stripped)?\s*$", re.MULTILINE)
_LOCAL_REF = re.compile(r"\{fileID: (-?\d+)\}")
_GUID_REF = re.compile(r"\{fileID: (-?\d+), guid: ([0-9a-fA-F]{32}), type: \d+\}")
_META_GUID = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
META_GUID = _META_GUID  # a .meta's GUID line, for the flow's GUID rule
_META_GUID_LINE = re.compile(r"^[ \t]*guid[ \t]*:", re.MULTILINE)  # any line that names a GUID, well formed or not
_MARKER = re.compile(r"^(<{7}|>{7})( |$)|^={7}$", re.MULTILINE)
_FATHER = re.compile(r"^  m_Father: \{fileID: (-?\d+)\}", re.MULTILINE)
_CHILDREN = re.compile(r"^  m_Children:\s*\n((?:  - \{fileID: -?\d+\}\n)*)", re.MULTILINE)
BUILTIN_GUID_PREFIX = "0000000000000000"


@dataclass(frozen=True)
class Finding:
    path: str
    check: str
    message: str


@dataclass
class CheckReport:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # passed, skipped or not-checked items, for the council
    tests: "TestRun | None" = None  # the EditMode run `unity_checks` judged (after its re-run), when it ran one
    scene: "SceneRun | None" = None  # the scene check `scene_checks` ran, when it ran one

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, path: str, check: str, message: str) -> None:
        self.findings.append(Finding(path, check, message))


@dataclass(frozen=True)
class Document:
    class_id: str
    file_id: str
    stripped: bool
    body: str


def parse_unity_yaml(text: str) -> list[Document]:
    """Split a Unity YAML file into its documents. Raises ValueError if it is not Unity YAML."""
    if not text.startswith("%YAML 1.1"):
        raise ValueError("does not start with %YAML 1.1")
    if "%TAG !u! tag:unity3d.com,2011:" not in text[:200]:
        raise ValueError("missing the !u! tag line")
    headers = list(_HEADER.finditer(text))
    docs = []
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        docs.append(Document(h.group(1), h.group(2), bool(h.group(3)), text[h.end() : end]))
    stray = re.search(r"^--- (?!!u!\d+ &-?\d+( stripped)?\s*$).*$", text, re.MULTILINE)
    if stray:
        raise ValueError(f"malformed document header: {stray.group(0)[:80]!r}")
    return docs


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def guid_index(root: Path, extra_roots: Iterable[Path] = ()) -> tuple[dict[str, list[str]], set[str]]:
    """Map GUID -> .meta paths under Assets/ and Packages/, plus GUIDs from `extra_roots` (package cache)."""
    own: dict[str, list[str]] = {}
    for top in ("Assets", "Packages"):
        base = root / top
        if not base.is_dir():
            continue
        for meta in base.rglob("*.meta"):
            match = _META_GUID.search(meta.read_text(encoding="utf-8", errors="replace"))
            if match:
                own.setdefault(match.group(1).lower(), []).append(_rel(root, meta))
    return own, _external_guids(extra_roots)


def _external_guids(extra_roots: Iterable[Path]) -> set[str]:
    """The GUIDs of the `.meta` files under `extra_roots` (the package cache)."""
    external: set[str] = set()
    for extra in extra_roots:
        if extra.is_dir():
            for meta in extra.rglob("*.meta"):
                match = _META_GUID.search(meta.read_text(encoding="utf-8", errors="replace"))
                if match:
                    external.add(match.group(1).lower())
    return external


FOLDER_ASSET = "^folderAsset: yes"  # a folder .meta's line; `Flow._meta_tree`'s grep and `disk_meta_tree` read the same
_FOLDER_ASSET = re.compile(FOLDER_ASSET, re.MULTILINE)
META_KINDS = {
    "orphan": "orphan .meta: its asset is missing",
    "no-meta": "asset has no .meta, so its GUID will change",
    "folder-over-file": "a folder .meta (folderAsset: yes) over a file",
    "file-over-folder": "a file .meta over a folder: it has no `folderAsset: yes`",
    "no-guid": "no GUID read in the .meta, so Unity will give its asset a new one",
}


@dataclass(frozen=True)
class MetaTree:
    """What the `.meta` check reads of one project: every file (`files`, repo-relative, `/`-separated), the
    `.meta` files whose text has `folderAsset: yes` (`folder_metas`), and GUID -> the `.meta` paths under `Assets/` and
    `Packages/` that hold it (`guids`). `dirs`: folders known apart from the files' parents (a disk's empty folders; a
    git tree has none). The flow reads it from a commit through git (`Flow._meta_tree`); `disk_meta_tree` reads a
    folder, for `static_checks(root)` given no tree."""

    files: frozenset[str]
    folder_metas: frozenset[str]
    guids: dict[str, tuple[str, ...]]
    dirs: frozenset[str] = frozenset()


def disk_meta_tree(root: Path) -> MetaTree:
    """A `MetaTree` of the files and folders on disk under `Assets/` and `Packages/`."""
    files: set[str] = set()
    dirs: set[str] = set()
    folders: set[str] = set()
    guids: dict[str, list[str]] = {}
    for top in ("Assets", "Packages"):
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            rel = _rel(root, path)
            if path.is_dir():
                dirs.add(rel)
                continue
            files.add(rel)
            if path.suffix == ".meta":
                text = path.read_text(encoding="utf-8", errors="replace")
                if _FOLDER_ASSET.search(text):
                    folders.add(rel)
                match = _META_GUID.search(text)
                if match:
                    guids.setdefault(match.group(1).lower(), []).append(rel)
    return MetaTree(frozenset(files), frozenset(folders), {g: tuple(m) for g, m in guids.items()}, frozenset(dirs))


def _hidden(rel: str) -> bool:
    return any(part.startswith(".") or part.endswith("~") for part in rel.split("/"))  # Unity ignores these


def meta_findings(tree: MetaTree) -> set[tuple[str, str, str]]:
    """The `.meta` rules over one tree, as `(path, kind, guid or "")`. Under `Assets/`, outside hidden
    paths (a part `.x` or `x~`): a file needs an `X.meta` and a folder (a parent of a file, or one in `tree.dirs`) a
    `D.meta`; `X.meta` over a file must not be a folder `.meta`, and over a folder must be one; with nothing at `X`,
    `X.meta` is valid only as a folder `.meta` (git tracks an empty folder only as its `.meta`), else an orphan. A
    GUID held by two `.meta` files is a finding at each (`duplicate`, under `Assets/` and `Packages/`), and so is a
    `.meta` there, outside hidden paths, whose GUID was not read (`no-guid`: Unity gives it a new one).

    A folder is every parent, outside hidden paths, of any file under `Assets/`, a hidden file too (a
    folder that holds only `.gitkeep` is imported by Unity, which writes its `.meta`)."""
    out: set[tuple[str, str, str]] = set()
    under = [p for p in tree.files if p.startswith("Assets/") and not _hidden(p)]
    dirs = {d for d in tree.dirs if d.startswith("Assets/") and not _hidden(d)}
    for path in tree.files:
        parts = path.split("/")
        if parts[0] != "Assets":
            continue
        for i in range(2, len(parts)):
            if _hidden(parts[i - 1]):
                break
            dirs.add("/".join(parts[:i]))
    assets = {p for p in under if not p.endswith(".meta")}
    for path in assets:
        if path + ".meta" not in tree.files:
            out.add((path, "no-meta", ""))
        elif path + ".meta" in tree.folder_metas:
            out.add((path + ".meta", "folder-over-file", ""))
    for path in dirs:
        if path + ".meta" not in tree.files:
            out.add((path, "no-meta", ""))
        elif path + ".meta" not in tree.folder_metas:
            out.add((path + ".meta", "file-over-folder", ""))
    for meta in under:
        if meta.endswith(".meta"):
            asset = meta[: -len(".meta")]
            if asset not in assets and asset not in dirs and meta not in tree.folder_metas:
                out.add((meta, "orphan", ""))
    for guid, metas in tree.guids.items():
        if len(metas) > 1:
            out.update((meta, "duplicate", guid) for meta in metas)
    read = {meta for metas in tree.guids.values() for meta in metas}
    for meta in tree.files:
        if meta.endswith(".meta") and meta.startswith(("Assets/", "Packages/")) and not _hidden(meta) and meta not in read:
            out.add((meta, "no-guid", ""))
    return out


def check_meta_files(tree: MetaTree, report: CheckReport, target: MetaTree | None = None) -> None:
    """Report the `.meta` findings of `tree`; given a `target`, only those whose key the same check on the target
    does not give (the merge's own, as the Unity checks compare with the target's baseline). A duplicate's
    key holds its GUID, so a path that held one duplicated GUID on the target and holds another on the merge is
    reported (a new holder of a GUID is reported for its new path, with or without the GUID)."""
    found = meta_findings(tree)
    if target is not None:
        found -= meta_findings(target)
    for path, kind, guid in sorted(found):
        if kind == "duplicate":
            report.add(path, "meta", f"GUID {guid} is also used by {', '.join(m for m in tree.guids[guid] if m != path)}")
        else:
            report.add(path, "meta", META_KINDS[kind])


# The checks that read files, not a Unity run. Only a finding of one of these can be called "not the branch's" by its
# path; a test or a scene problem may come from the branch's code wherever it is reported.
STATIC_CHECKS = frozenset({"meta", "yaml", "file-id", "local-ref", "guid-ref", "hierarchy", "conflict-marker",
                           "tool-namespace", "case-duplicate", "claim", "protected"})


def _key(path: str) -> str:
    key = path.replace("\\", "/").casefold().rstrip("/")
    return key[: -len(".meta")] if key.endswith(".meta") else key


def _related(a: str, b: str) -> bool:
    """One path is the other, or its `.meta`, or holds it (or its `.meta`) as a folder; compared without case."""
    a, b = _key(a), _key(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def findings_outside_branch(findings: Iterable[Finding], paths: Iterable[str]) -> bool:
    """Whether a land's findings are all static and none is in a path the branch changed (`paths`: the branch's and
    the merge's changed paths). True only then: no finding, or any other kind (a test, a scene problem), is False, the
    side that says "fix". A rename is a delete and an add (`--no-renames`), so both its paths are in `paths`."""
    findings, keys = list(findings), list(paths)
    if not findings or any(f.check not in STATIC_CHECKS for f in findings):
        return False
    return not any(_related(f.path, p) for f in findings for p in keys)


def check_yaml_file(root: Path, path: Path, report: CheckReport, guids: set[str] | None) -> None:
    rel = _rel(root, path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("%YAML"):
        return  # binary-serialized or not Unity YAML; nothing to check here
    try:
        docs = parse_unity_yaml(text)
    except ValueError as exc:
        report.add(rel, "yaml", str(exc))
        return

    ids: dict[str, int] = {}
    for d in docs:
        ids[d.file_id] = ids.get(d.file_id, 0) + 1
    for file_id, count in ids.items():
        if count > 1:
            report.add(rel, "file-id", f"fileID {file_id} appears {count} times")

    for d in docs:
        for match in _LOCAL_REF.finditer(d.body):
            ref = match.group(1)
            if ref != "0" and ref not in ids:
                report.add(rel, "local-ref", f"&{d.file_id} refers to fileID {ref}, which is not in the file")
        if guids is not None:
            for match in _GUID_REF.finditer(d.body):
                guid = match.group(2).lower()
                if not guid.startswith(BUILTIN_GUID_PREFIX) and guid not in guids:
                    report.add(rel, "guid-ref", f"&{d.file_id} refers to GUID {guid}, which no asset has")

    transforms = {d.file_id: d for d in docs if d.class_id in TRANSFORM_CLASSES and not d.stripped}
    stripped = {d.file_id for d in docs if d.stripped}
    for d in transforms.values():
        children = _CHILDREN.search(d.body)
        for child in _LOCAL_REF.findall(children.group(1)) if children else []:
            if child in stripped or child not in transforms:
                continue
            father = _FATHER.search(transforms[child].body)
            if not father or father.group(1) != d.file_id:
                report.add(rel, "hierarchy", f"&{d.file_id} lists child &{child}, whose m_Father is not &{d.file_id}")


def check_conflict_markers(root: Path, paths: Iterable[str], report: CheckReport) -> None:
    for rel in paths:
        path = root / rel
        if path.suffix in TEXT_SUFFIXES and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if _MARKER.search(text):
                report.add(rel, "conflict-marker", "merge conflict markers left in the file")


def static_checks(root: Path | str, changed: Iterable[str] | None = None, package_cache: Path | None = None,
                  tree: MetaTree | None = None, target: MetaTree | None = None) -> CheckReport:
    """Run the static checks. `changed` limits the per-file checks; None checks every YAML file.

    `tree`: the commit being checked as git holds it. Given, the `.meta` check and the GUIDs a YAML
    reference may name come from it (and the package cache), not from the disk, so an untracked or missing file in
    the slot changes no answer; `target` then limits the `.meta` findings to those the target's tree does not give.
    Not given, both read the disk under `root` (`disk_meta_tree`)."""
    root = Path(root)
    report = CheckReport()
    cache = package_cache if package_cache is not None else root / "Library" / "PackageCache"
    tree = tree if tree is not None else disk_meta_tree(root)
    check_meta_files(tree, report, target)
    external = _external_guids([cache])

    guids: set[str] | None = set(tree.guids) | external
    if not cache.is_dir():
        guids = None
        report.notes.append("GUID references not checked: no package cache to resolve package GUIDs against")

    if changed is None:
        files = [p for top in ("Assets", "Packages") if (root / top).is_dir() for p in (root / top).rglob("*") if p.suffix in UNITY_YAML]
        changed_rel = [_rel(root, p) for p in files]
    else:
        changed_rel = sorted(set(changed))
        files = [root / rel for rel in changed_rel if (root / rel).suffix in UNITY_YAML and (root / rel).is_file()]

    check_conflict_markers(root, changed_rel, report)
    for path in files:
        check_yaml_file(root, path, report, guids)
    report.notes.append(f"static checks covered {len(files)} YAML files")
    return report


# Files that steer agents or run as tools, not project content. A change to one is landed by a person.
PROTECTED = (
    ".claude/", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".codex/", ".cursor/", ".cursorrules",
    ".vscode/", ".github/", ".githooks/", ".gitattributes", ".gitmodules", "upe.toml",
)
TOOL_NAMESPACE = re.compile(r"namespace\s+Upe\.Agent\b")


def check_protected(changed: Iterable[str], patterns: Iterable[str], report: CheckReport) -> None:
    """A change to an agent's instructions or hooks, or to a tool the user runs, is not the council's to pass."""
    from .claims import Claim

    guard = Claim.of("protected", list(patterns))
    for path in changed:
        if guard.covers(path):
            report.add(path, "protected", "changes agent instructions, hooks or tooling; a person lands this by hand")


def check_tool_namespace(root: Path, changed: Iterable[str], report: CheckReport) -> None:
    """The branch may not define code in the tool's namespace: the checks call Upe.Agent methods by name."""
    for rel in changed:
        path = root / rel
        if path.suffix == ".cs" and path.is_file() and TOOL_NAMESPACE.search(path.read_text(encoding="utf-8", errors="replace")):
            report.add(rel, "tool-namespace", "defines code in namespace Upe.Agent, which the tool's own checks use")


def check_case_duplicates(tracked: Iterable[str], changed: Iterable[str], report: CheckReport) -> None:
    """Two tracked paths that differ only in case are one file on Windows: reviewers and Unity see only one."""
    by_lower: dict[str, set[str]] = {}
    for path in tracked:
        by_lower.setdefault(path.lower(), set()).add(path)
    for path in changed:
        twins = by_lower.get(path.lower(), set()) - {path}
        if twins:
            report.add(path, "case-duplicate", f"differs only in case from {', '.join(sorted(twins))}")


def failed_tests(results_xml: str) -> set[str]:
    """Full names of the failed test cases in an NUnit results report."""
    root = ET.fromstring(results_xml)
    return {case.get("fullname") or case.get("name") or "" for case in root.iter("test-case") if case.get("result") == "Failed"}


def read_report(results_xml: str) -> dict[str, tuple[str, str]]:
    """Each test of an NUnit report: full name -> (result, class). The class is the case's `classname` (with its
    namespace, as `--filter` takes it), else the full name cut at its first `(`, then at its last `.`.

    Two cases can have one full name (a `TestCaseSource` of objects with one `ToString`, or one `[TestCase]` written
    twice). A name is `Failed` when any of its cases failed, `Passed` only when every case passed, else the result of
    its first case that did not pass (the last case no longer hides a failure)."""
    cases: dict[str, tuple[str, str]] = {}
    for case in ET.fromstring(results_xml).iter("test-case"):
        name = case.get("fullname") or case.get("name") or ""
        result = case.get("result") or "Unknown"
        if name in cases:
            seen, cls = cases[name]
            if seen == "Failed" or seen != "Passed" and result != "Failed":
                continue
            cases[name] = (result, cls)
        else:
            cases[name] = (result, case.get("classname") or class_of(name))
    return cases


def class_of(fullname: str) -> str:
    """The test class of a full test name: `A.B.Test("x.y")` -> `A.B`."""
    return fullname.split("(", 1)[0].rsplit(".", 1)[0]


def test_counts(results_xml: str, classes: Sequence[str] = ()) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Results by outcome over the whole run, and for each named test class (`Class.` in a case's full name)."""
    root = ET.fromstring(results_xml)
    total: dict[str, int] = {}
    per_class: dict[str, dict[str, int]] = {name: {} for name in classes}
    for case in root.iter("test-case"):
        result = case.get("result") or "Unknown"
        total[result] = total.get(result, 0) + 1
        fullname = case.get("fullname") or case.get("name") or ""
        for name in classes:
            if fullname.startswith(name + ".") or f".{name}." in fullname:
                per_class[name][result] = per_class[name].get(result, 0) + 1
    return total, per_class


def _counted(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {result.lower()}" for result, n in sorted(counts.items())) or "none"


@dataclass
class TestRun:
    exit_code: int
    error_code: str | None
    failed: set[str] | None  # None when the run wrote no results report
    flaky: set[str] = field(default_factory=set)  # failed, then passed on a retry; not in `failed`
    # Of `failed`, the tests seen failing on every attempt: in the first report and failed in every
    # `<report>.attempt-N.xml` the CLI wrote, or failed again in the tool's re-run (`confirm_failures`). Never from
    # the summary's `failed` list: the CLI lists there the parameterised tests it never re-ran.
    twice: set[str] = field(default_factory=set)
    retried: set[str] = field(default_factory=set)  # every test named in an attempt report: the CLI re-ran these
    ran: set[str] = field(default_factory=set)  # every test the first report names
    classes: dict[str, str] = field(default_factory=dict)  # test -> its class, for each test the first report names
    passed: set[str] = field(default_factory=set)  # the tests whose every case the first report names `Passed`

    @property
    def finished(self) -> bool:
        return self.failed is not None

    @property
    def unconfirmed(self) -> set[str]:
        """Failed, but never seen failing twice: the CLI did not re-run it, or the re-run did not name it."""
        return (self.failed or set()) - self.twice


def run_counts(run: "TestRun", results: Path | str) -> int:
    """How many tests a finished run's report lists; 0 if it should not be believed: none, or a failing exit with no failures."""
    total = sum(test_counts(Path(results).read_text(encoding="utf-8", errors="replace"))[0].values())
    return 0 if not run.failed and run.exit_code != ExitCode.OK else total


def _flaky(summary: Path) -> set[str]:
    """The tests `unity test --retries` says passed only on a retry, from the summary it writes beside the report."""
    try:
        return {entry["test"] for entry in json.loads(summary.read_text(encoding="utf-8"))["flaky"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def run_tests(cli: UnityCli, project: Path | str, results: Path | str, timeout_s: int = 1800,
              extra: Sequence[str] = (), retries: int = 0) -> TestRun:
    """Compile the project and run its EditMode tests with `unity test`.

    `unity test` starts its own batch Editor (`-runTests`), so the project must
    not be open in another: with the project already open it exits 6 with
    COMMAND_FAILED (research/05). A compile error also ends without results.
    """
    results = Path(results)
    results.parent.mkdir(parents=True, exist_ok=True)
    summary = results.with_suffix(".retries.json")
    # The tool's own re-run of this report (`confirm_failures`) is stale too once this run starts.
    for stale in (results, summary, *results.parent.glob(f"{results.stem}.attempt-*"),
                  *results.parent.glob(f"{results.stem}.rerun*")):
        stale.unlink(missing_ok=True)
    retrying = ("--retries", str(retries)) if retries else ()
    # The report keeps the first attempt. Each retry opens an Editor of its own, under the same limit.
    tested = cli.test(project, "--mode", "EditMode", *extra, *retrying, "--output", str(results), cli_timeout_s=timeout_s,
                      timeout=(retries + 1) * timeout_s + 300)
    cases = None
    if results.exists():
        try:
            cases = read_report(results.read_text(encoding="utf-8", errors="replace"))
        except ET.ParseError:
            cases = None
    # No report means the tests did not finish, whatever the exit code: code in the branch can end
    # the Editor with exit 0 before any test runs (DESIGN.md, "Security").
    if cases is None:
        return TestRun(tested.exit_code, tested.error_code, None)
    failed = {name for name, (result, _) in cases.items() if result == "Failed"}
    flaky = _flaky(summary) & failed if failed else set()
    twice, retried = _attempts(results, failed - flaky)
    return TestRun(tested.exit_code, tested.error_code, failed - flaky, flaky, twice=twice, retried=retried,
                   ran=set(cases), classes={name: cls for name, (_, cls) in cases.items()},
                   passed={name for name, (result, _) in cases.items() if result == "Passed"})


def _attempts(results: Path, failed: set[str]) -> tuple[set[str], set[str]]:
    """Of `failed`, the tests failed in every attempt report the CLI wrote beside `results`; and every test those
    reports name. With no attempt report (no retries, or none written), none failed twice. A report that does not
    read counts as one in which nothing failed (never more "failed twice" than a report shows)."""
    reports = sorted(results.parent.glob(f"{results.stem}.attempt-*.xml"),
                     key=lambda p: int(m.group(1)) if (m := re.search(r"\.attempt-(\d+)\.xml$", p.name)) else 0)
    twice, retried = (set(failed) if reports else set()), set()
    for report in reports:
        try:
            cases = read_report(report.read_text(encoding="utf-8", errors="replace"))
        except ET.ParseError:
            cases = {}
        retried |= set(cases)
        twice &= {name for name, (result, _) in cases.items() if result == "Failed"}
    return twice, retried


def run_tests_by_classes(cli: UnityCli, project: Path | str, results: Path | str, timeout_s: int,
                         classes: Sequence[str]) -> TestRun:
    """The EditMode tests of `classes` only (`--filter`), once, with no retry. When the class list fits
    one command line (`FILTER_CAP`), the very call of before: one `run_tests` into `results`. Longer, one run per
    group of classes: group 1 into `results`, group k into `<stem>.part-k<suffix>`, read as one run:
    - a group that leaves no report makes the whole unfinished (`failed` None, its exit and error code), and no
      later group runs: a part never reads as the whole;
    - else `failed` is every name any group's report names `Failed`, and `passed` the names some group names
      `Passed` and none names `Failed` (a name failed in one report is failed, as `read_report` reads two cases of
      one name);
    - `ran` and `classes` the union of the reports; the exit code the first group's that was not OK, else group 1's.

    A split run is not one session: a test that fails only after another class's test ran before it can fail or pass
    differently."""
    results = Path(results)
    for stale in results.parent.glob(f"{results.stem}.part-*"):
        stale.unlink(missing_ok=True)
    groups = list(capped(list(classes), FILTER_CAP)) or [[]]
    if len(groups) == 1:
        return run_tests(cli, project, results, timeout_s, extra=("--filter", ";".join(groups[0])), retries=0)
    runs: list[TestRun] = []
    for k, group in enumerate(groups, 1):
        part = results if k == 1 else results.with_name(f"{results.stem}.part-{k}{results.suffix}")
        done = run_tests(cli, project, part, timeout_s, extra=("--filter", ";".join(group)), retries=0)
        if not done.finished:
            return TestRun(done.exit_code, done.error_code, None)
        runs.append(done)
    failed = set().union(*(r.failed or set() for r in runs))
    first = next((r for r in runs if r.exit_code != ExitCode.OK), runs[0])
    return TestRun(first.exit_code, first.error_code, failed, ran=set().union(*(r.ran for r in runs)),
                   classes={name: cls for r in runs for name, cls in r.classes.items()},
                   passed=set().union(*(r.passed for r in runs)) - failed)


def confirm_failures(cli: UnityCli, project: Path | str, results: Path | str, run: TestRun,
                     timeout_s: int = 1800) -> tuple[TestRun, str]:
    """One more attempt for the failed tests the CLI's retry did not re-run (it
    does not re-run parameterised tests, yet lists them under `failed`), by their classes, with no retry, under the
    report `<results>.rerun.xml`. Returns the run with each such test failed twice (it failed again), flaky (it
    passed) or left unconfirmed (the re-run did not name it, or did not finish), and a note for the council ("" when
    nothing was re-run). The caller calls it only with `test_retries` above 0: with none, the user asked for none."""
    results = Path(results)
    untried = sorted((run.failed or set()) - run.twice - run.retried)
    if not untried:
        return run, ""
    classes = sorted({run.classes.get(name) or class_of(name) for name in untried})
    rerun = results.with_name(f"{results.stem}.rerun.xml")
    again = run_tests_by_classes(cli, project, rerun, timeout_s, classes)
    groups = len(list(capped(classes, FILTER_CAP)))
    if groups > 1:  # the class list was longer than one command line takes
        rerun = f"{rerun} and {rerun.stem}.part-2 to -{groups}{rerun.suffix} ({groups} runs, one per group of classes)"
    if not again.finished:
        return run, (f"{len(untried)} failed tests the CLI did not retry (parameterised) were to run again, by class "
                     f"({', '.join(classes)}), but that run left no report (exit {again.exit_code}, {again.error_code}), "
                     f"so they count as failed once; results in {rerun}")
    failed_again = {name for name in untried if name in (again.failed or set())}
    # Only `Passed` is a pass: a test the re-run names Inconclusive or Skipped (an `Assert.Ignore` when its
    # class runs alone) stays failed once.
    passed = {name for name in untried if name in again.passed and name not in failed_again}
    failed = (run.failed or set()) - passed
    exit_code, error_code = run.exit_code, run.error_code
    if not failed and (exit_code == ExitCode.TESTS_FAILED or error_code == "TESTS_FAILED"):
        exit_code, error_code = ExitCode.OK, None  # the CLI's exit 8 was for these, which now passed
    confirmed = TestRun(exit_code, error_code, failed, run.flaky | passed, twice=run.twice | failed_again,
                        retried=run.retried, ran=run.ran, classes=run.classes, passed=run.passed | passed)
    missing = len(untried) - len(failed_again) - len(passed)  # not named, or named neither Failed nor Passed
    return confirmed, (f"{len(untried)} failed tests the CLI did not retry (parameterised) were run again, by class "
                       f"({', '.join(classes)}): {len(failed_again)} failed again, {len(passed)} passed (flaky)"
                       + (f", {missing} not in that run's report, or neither failed nor passed there (they count as "
                          "failed once)" if missing else "")
                       + f"; results in {rerun}")


def unity_checks(
    cli: UnityCli,
    project: Path | str,
    report: CheckReport | None = None,
    test_timeout_s: int = 1800,
    baseline: set[str] | None = None,
    results: Path | str | None = None,
    classes: Sequence[str] = (),
    retries: int = 0,
    rerun: "Callable[[TestRun], TestRun] | None" = None,
    known_for: "Callable[[TestRun], tuple[set[str], list[str]]] | None" = None,
) -> CheckReport:
    """Run the EditMode tests; a test that also fails on the target (`baseline`) is not new.

    Without a baseline every failed test counts. The notes say how many tests ran, and how many
    of each class in `classes` (the test classes the branch changed), so the council can see
    that the branch's own tests ran, not only that nothing failed.

    `rerun(run)`, when given, runs before the verdict on a finished run (the re-run of the failed tests
    the CLI did not retry). `known_for(run)`, when given, takes the place of `baseline`: it says which of
    `run.failed` are known, with notes for the council, and may run tests on the target to say it.
    """
    report = report or CheckReport()
    results = Path(results) if results else Path(project) / "Logs" / "upe-test-results.xml"  # Logs/ is never committed
    run = run_tests(cli, project, results, test_timeout_s, retries=retries)
    if rerun is not None and run.finished and run.failed:
        run = rerun(run)
    report.tests = run
    if run.flaky:
        report.notes.append(f"{len(run.flaky)} tests failed, then passed on a retry, so they do not stop the land: {', '.join(sorted(run.flaky))}")
    if run.finished:
        total, per_class = test_counts(results.read_text(encoding="utf-8", errors="replace"), classes)
        report.notes.append(f"tests: {_counted(total)}; results in {results}")
        for name, counts in per_class.items():
            report.notes.append(f"{name}, changed by the branch: {_counted(counts)}")
        if not total:
            report.add(str(project), "unity-tests", "the report lists no tests, so nothing was tested")
            return report
    tests_failed = run.exit_code == ExitCode.TESTS_FAILED or run.error_code == "TESTS_FAILED"
    if run.failed is None:
        if tests_failed:
            report.add(str(project), "unity-tests", "one or more tests failed, and no report names them")
        elif run.error_code == "TEST_TIMED_OUT":
            report.add(str(project), "unity-tests", f"the tests did not finish in {test_timeout_s} s (TEST_TIMED_OUT). "
                                                     "Not a finding about the code: the machine was busy, or a test hangs. "
                                                     "Land again when fewer Editors run, or raise test_timeout_s in upe.toml")
        else:
            report.add(str(project), "unity-tests", f"tests left no results report (exit {run.exit_code}, {run.error_code}); "
                                                     "compile error, licence, crash, or the Editor was ended early")
        return report
    if not run.failed and run.exit_code != ExitCode.OK:
        report.add(str(project), "unity-tests", f"the report lists no failures, but the run exited {run.exit_code} ({run.error_code})")
        return report
    if not run.failed:
        report.notes.append("compiled and passed its tests")
        return report
    if known_for is not None:
        known, notes = known_for(run)
        known &= run.failed
        report.notes += notes
    else:
        known = run.failed & (baseline or set())
    for name in sorted(run.failed - known):
        report.add(name, "unity-tests", "test failed")
    if known:
        report.notes.append(f"{len(known)} tests also fail on the target before the merge: {', '.join(sorted(known))}")
    if known == run.failed:
        report.notes.append("compiled, and no test fails that did not fail on the target")
    return report


SCENE_CHECK_METHOD = "Upe.Agent.SceneCheck.Run"


def scene_problems(report_json: str) -> set[tuple[str, str, str, str]]:
    """The problems in a SceneCheck report, as comparable tuples."""
    data = json.loads(report_json)
    return {(p.get("path") or "", p.get("kind") or "", p.get("where") or "", p.get("detail") or "") for p in data.get("problems", [])}


def _licensing_failed(log: Path) -> bool:
    """Without a licence Unity's packages go unentitled and the project fails to compile (research/05)."""
    return log.exists() and "Licensing initialization failed" in log.read_text(encoding="utf-8", errors="replace")


SCENE_SUFFIXES = (".unity", ".prefab")


@dataclass(frozen=True)
class SceneRun:
    """What one batch run of `SceneCheck.Run` ended with. Only `kept.FullScenes.of` turns one into a
    commit's full result, and only when it is complete.

    `paths`: the `-upePaths` it was given, after the `.unity`/`.prefab` filter; `()` is the full run, every scene and
    prefab. `problems`: the report's problems, None when it wrote no report. `why`: what this run itself shows that makes
    it incomplete ("" when nothing): no report, a stale report, a licensing failure, a compile error in its log.

    A split check (`_join_scene_runs`) with a group that left no report: `group` names it ("group 2 of 3"), and
    `partial` holds the problems the groups before it reported (they are shown, never lost)."""
    paths: tuple[str, ...]
    exit_code: int
    problems: frozenset | None
    scenes: int = 0
    prefabs: int = 0
    why: str = ""
    log: str = ""
    group: str = ""
    partial: frozenset = frozenset()


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def run_scene_check(editor: Editor, project: Path | str, paths: Iterable[str], out_dir: Path | str) -> SceneRun:
    """One scene check in a batch Editor: of `paths` (scenes and prefabs only), or of every scene and prefab when none
    is given. The caller decides which: a filter that leaves no path is a full run, so a caller that filters checks
    for none first. Its report is deleted before the run, so a report there afterwards is this run's; it must
    also be newer than the run's start (`why` "stale report").

    Paths longer than one command line takes (`SCENE_PATHS_CAP`) are checked in groups, one batch Editor
    each (`upe-scene-check.json` for group 1, `upe-scene-check-<k>.json` for group k), and read as one run by
    `_join_scene_runs`. The full run (no path) is never split. An Editor that cannot start for a group (its project
    still open, say) stops the run with an `EditorError` that names the group."""
    project, out_dir = Path(project), Path(out_dir)
    paths = tuple(p for p in paths if p.lower().endswith(SCENE_SUFFIXES))
    for stale in (*out_dir.glob("upe-scene-check-*.json"), *out_dir.glob("upe-scene-check-*.log")):
        stale.unlink(missing_ok=True)  # an earlier split's groups: not this run's
    groups = list(capped(paths, SCENE_PATHS_CAP))
    if len(groups) <= 1:
        return _one_scene_run(editor, project, paths, out_dir, "upe-scene-check")
    runs: list[SceneRun] = []
    for k, group in enumerate(groups, 1):
        try:
            run = _one_scene_run(editor, project, tuple(group), out_dir, "upe-scene-check" + (f"-{k}" if k > 1 else ""))
        except EditorError as exc:
            raise EditorError(f"scene check, group {k} of {len(groups)} ({len(group)} paths): {exc}; the land stops "
                              "here, nothing of the check counts. A check longer than one command line runs one Editor "
                              "a group, and Unity still held the project when this one started: once no Editor has "
                              f"{project} open, land again (the whole land runs again)") from exc
        runs.append(run)
        if run.problems is None:  # the whole is "did not run" whatever the others say: no group after it is started
            break
    return _join_scene_runs(paths, runs, len(groups))


def _join_scene_runs(paths: tuple[str, ...], runs: list[SceneRun], total: int) -> SceneRun:
    """The groups of one split scene check read as one run. `paths` is the whole list, never `()`: a split
    run is never a commit's full result (`kept.FullScenes.why_not`). No report from any group (or fewer runs than
    groups) is no report for the whole; else the problems are the union. Scenes and prefabs add up; the exit code is
    the first outside 0 and 1, else the largest; `why` the first group's that has one, named by its group; `log` the
    log of the first group with no report, else of the first with a `why`, else group 1's (`scene_checks` reads it)."""
    missing = len(runs) < total or any(r.problems is None for r in runs)
    problems = None if missing else frozenset().union(*(r.problems for r in runs))
    odd = [r.exit_code for r in runs if r.exit_code not in (0, 1)]
    why = next((f"group {k} of {total}: {r.why}" for k, r in enumerate(runs, 1) if r.why), "")
    log = next((r.log for r in runs if r.problems is None), "") or next((r.log for r in runs if r.why), runs[0].log)
    failed = next((k for k, r in enumerate(runs, 1) if r.problems is None), len(runs) + 1 if missing else 0)
    return SceneRun(paths, odd[0] if odd else max(r.exit_code for r in runs), problems,
                    sum(r.scenes for r in runs), sum(r.prefabs for r in runs), why, log,
                    group=f"group {failed} of {total}" if failed else "",
                    partial=frozenset().union(*(r.problems for r in runs if r.problems is not None)) if missing
                    else frozenset())


def _one_scene_run(editor: Editor, project: Path, paths: tuple[str, ...], out_dir: Path, name: str) -> SceneRun:
    """One batch Editor's scene check: of `paths`, or of every scene and prefab when none is given."""
    out, log = out_dir / f"{name}.json", out_dir / f"{name}.log"
    out.unlink(missing_ok=True)
    args = ["-upeOut", str(out)] + (["-upePaths", ";".join(paths)] if paths else [])
    started = time.time()
    # With -nographics, Unity 6000.3 crashes (0xC0000005) opening a scene with a
    # Terrain: Terrain.AwakeFromLoad renders its splat maps (research/05).
    run = editor.execute_method(project, SCENE_CHECK_METHOD, log, args, graphics=True)
    if not out.exists() and _licensing_failed(log):
        run = editor.execute_method(project, SCENE_CHECK_METHOD, log, args, graphics=True)  # it passes in a minute or two
    if not out.exists():
        why = "licensing failed, twice" if _licensing_failed(log) else "no report"
        return SceneRun(paths, run.exit_code, None, why=why, log=str(log))
    text = out.read_text(encoding="utf-8")
    data = json.loads(text)
    problems = frozenset(scene_problems(text))
    why = ""
    if out.stat().st_mtime < started - 2:
        why = "stale report: not written by this run"
    elif _licensing_failed(log):
        why = "licensing failed"
    elif log.exists() and compile_errors(log.read_text(encoding="utf-8", errors="replace")):
        why = "compile errors in its log"
    return SceneRun(paths, run.exit_code, problems, _count(data.get("scenes")), _count(data.get("prefabs")), why,
                    str(log))


def scene_checks(
    editor: Editor,
    project: Path | str,
    report: CheckReport | None = None,
    paths: Iterable[str] = (),
    baseline: set[tuple[str, str, str, str]] | None = None,
    out_dir: Path | str | None = None,
) -> CheckReport:
    """Load scenes and prefabs in a batch Editor and report missing scripts, prefabs and references.

    Needs the `com.upe.agent` package in the worktree (agent_package.install).
    Problems already in `baseline` (the target before the merge) are not new
    and are only noted. The run is `report.scene`.
    """
    report = report or CheckReport()
    project = Path(project)
    out_dir = Path(out_dir) if out_dir else project / "Logs"
    run = run_scene_check(editor, project, paths, out_dir)
    report.scene = run
    log = Path(run.log)

    if run.problems is None:
        # A split check names the group that left no report, and keeps what the groups before it found (each a finding
        # as below, the target's own only noted).
        where = f" ({run.group} of a check split by the length of its command line)" if run.group else ""
        if _licensing_failed(log):
            report.add(str(project), "scene-check", f"scene check did not run{where}: Unity's licensing did not start, "
                                                    "twice. Not a finding about the code: packages go missing and "
                                                    f"everything fails to compile. See {log}")
        else:
            errors = compile_errors(log.read_text(encoding="utf-8", errors="replace")) if log.exists() else []
            why = "; ".join(errors[:5]) if errors else f"Editor exited {run.exit_code} without a report; see {log}"
            report.add(str(project), "scene-check", f"scene check did not run{where}: {why}")
        partial = set(run.partial)
        for path, kind, place, detail in sorted(partial - (baseline or set())):
            report.add(path or str(project), kind, f"{place} {detail}".strip())
        if partial:
            report.notes.append(f"the groups before {run.group} found {len(partial)} scene problems; the paths after "
                                "them were not checked")
        return report

    found = set(run.problems)
    known = found & (baseline or set())
    for path, kind, where, detail in sorted(found - known):
        report.add(path or str(project), kind, f"{where} {detail}".strip())
    if known:
        report.notes.append(f"{len(known)} scene problems were already on the target before the merge")
    report.notes.append(f"scene check loaded {run.scenes} scenes and {run.prefabs} prefabs")
    return report


# No full scene check when the merge only adds files that cannot leave an existing reference dangling.
#
# The kinds a merge may add and still skip it (every other kind keeps it): passive assets, which existing files cannot
# refer to (a new file has a new GUID, and scenes and prefabs refer by GUID), and a `.cs` inside a test assembly the
# target already has, which names no hook the scan sees. Shaders are not passive: `Shader.Find` and `#include` find
# them by name, not by GUID.
PASSIVE = frozenset((
    ".png", ".jpg", ".jpeg", ".tga", ".psd", ".tif", ".tiff", ".exr", ".hdr", ".bmp", ".gif",
    ".wav", ".mp3", ".ogg", ".aif", ".aiff", ".mp4", ".mov", ".webm", ".ttf", ".otf",
    ".fbx", ".obj", ".blend", ".dae",
    ".mat", ".asset", ".anim", ".controller", ".overridecontroller", ".physicmaterial", ".mask", ".playable",
    ".txt", ".json", ".xml", ".csv", ".bytes", ".md",
))
ASSEMBLY_FILES = (".asmdef", ".asmref")
# A test-only setting of an `.asmdef`: the define the test framework sets, the old optional reference, or a reference
# to the test runner by name, as a render pipeline package's test assembly names it. A reference by GUID is not taken: no
# file of the project names the runner's GUIDs, so they could not be checked (an `.asmdef` that uses them keeps the check).
TEST_DEFINE = "UNITY_INCLUDE_TESTS"
TEST_REFERENCES = frozenset(("UnityEngine.TestRunner", "UnityEditor.TestRunner"))
# The hook scan: names that make code run on import, on load or on a scene's opening, flagged anywhere in the text (a
# base list, an attribute, an alias, `using static`, a comment, a string, behind any `#if`). A false hit only keeps
# the full check. `partial` too: a part of an existing class (its base list in another file) can add Editor callbacks.
HOOK_WORDS = (
    "AssetPostprocessor", "ScriptedImporter", "AssetImporter", "AssetModificationProcessor", "InitializeOnLoad",
    "DidReloadScripts", "ModuleInitializer", "Callbacks", "PostProcessScene", "ExecuteAlways", "ExecuteInEditMode",
    "OnValidate", "partial",
    # Unity's other import callback (`UnityEditor.AssetsModifiedProcessor`, and its `Experimental` twin).
    "AssetsModifiedProcessor", "OnAssetsModified", "InitializeOnEnterPlayMode",
)
# Code that runs when an instance is loaded: a new ScriptableObject class whose `.asset` the merge also adds runs its
# `OnEnable` or `Awake` on that asset's import. Scanned in an added script; not counted in the target's
# scripts that name a hook (most MonoBehaviours have them): the target's ScriptableObject classes are counted apart.
# Also Unity's serialization callbacks, which run when any asset that holds the class loads.
LOAD_WORDS = ("ScriptableObject", "OnEnable", "Awake", "ISerializationCallbackReceiver", "OnAfterDeserialize",
              "OnBeforeSerialize")
HOOK_METHODS = re.compile(r"On(?:Pre|Post)process\w*|OnWill(?:SaveAssets|CreateAsset|DeleteAsset|MoveAsset)"
                          r"|IsOpenForEdit|OnImportAsset")
# The same names as an extended regular expression for `git grep -E` over the target's scripts (no `\w`, no `(?:`).
HOOK_PATTERN = "|".join([*HOOK_WORDS, HOOK_METHODS.pattern.replace("(?:", "(").replace(r"\w", "[A-Za-z0-9_]")])
OPEN_HOOK_BASE = ("a class that derives from a hook through a base class of the project, or of a precompiled .dll, which "
                  "a text scan cannot see")
OPEN_EXISTING_HOOK = ("an existing hook of the target (an AssetPostprocessor, say) that reacts to the added files by "
                      "changing other assets")
OPEN_EXISTING_LOAD = ("existing code of the target that runs when an added file loads an instance of a class the "
                      "target has (OnEnable, Awake, OnValidate, the serialization callbacks, constructors and field "
                      "initializers, of a ScriptableObject, a StateMachineBehaviour or a playable, and of a "
                      "[SerializeReference] type it names) and changes other assets")
# The added files that hold instances of the project's classes: an `.asset` (a ScriptableObject), a
# `.controller` (its StateMachineBehaviours), a `.playable` (a TimelineAsset, its tracks and clips). And the names the
# count looks for in the target's scripts: at least that many, since a class derived through a base class of the
# project names none of them.
LOAD_SUFFIXES = (".asset", ".controller", ".playable")
LOAD_PATTERN = "ScriptableObject|StateMachineBehaviour|PlayableAsset|TrackAsset"


def hooks_in(raw: bytes) -> list[str]:
    """What the hook scan sees in a script's text: each hook name, a `\\u` escape (C# reads one inside an identifier),
    or "not UTF-8". Before matching, formatting characters (category Cf, such as U+200B, which C# drops from
    identifiers) are removed and the text is put in NFC, as C# compares identifiers."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ["not UTF-8 text"]
    text = unicodedata.normalize("NFC", "".join(c for c in text if unicodedata.category(c) != "Cf"))
    found = [word for word in (*HOOK_WORDS, *LOAD_WORDS) if word in text]
    found += sorted(set(HOOK_METHODS.findall(text)))
    if re.search(r"\\[uU]", text):
        found.append("a \\u escape")
    return found


def test_assembly(raw: bytes | None) -> bool:
    """Whether an `.asmdef`'s text has a test-only setting. Unreadable or unparsable: no."""
    if raw is None:
        return False
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False

    def strings(key: str) -> list[str]:
        value = data.get(key)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    return (TEST_DEFINE in strings("defineConstraints") or "TestAssemblies" in strings("optionalUnityReferences")
            or any(ref in TEST_REFERENCES for ref in strings("references")))


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _assembly_reason(path: str, assemblies: dict[str, list[str]], added: set[str],
                     read_target: Callable[[str], bytes | None]) -> str:
    """Why a new `.cs` keeps the full check, by the assembly it compiles into; "" when that is a test assembly the
    target already has. Unity's rule: the nearest folder up to `Assets` that holds an `.asmdef` or an `.asmref`."""
    folder = _folder(path)
    while folder.startswith("Assets"):
        found = assemblies.get(folder)
        if found:
            if len(found) > 1:
                return f"{path}: its folder {folder} holds {len(found)} assembly files"
            asm = found[0]
            if asm in added:
                return f"{path}: compiles into {asm}, which the merge adds"
            if asm.lower().endswith(".asmref"):
                return f"{path}: compiles through {asm}, an .asmref, which can point at any assembly"
            if not test_assembly(read_target(asm)):
                return f"{path}: compiles into {asm}, which has no test-only setting (or does not read)"
            return ""
        if folder == "Assets":
            break
        folder = _folder(folder)
    return f"{path}: a script outside any test assembly (it compiles into Assembly-CSharp or Assembly-CSharp-Editor)"


def scene_skip_reasons(
    changes: Sequence[tuple[str, str]],
    read_new: Callable[[str], bytes | None],
    target_paths: set[str],
    read_target: Callable[[str], bytes | None],
    guid_taken: Callable[[str, str], str] | None = None,
) -> list[str]:
    """The allow list: why the full scene check must run for a merge that changes no scene or
    prefab; an empty list means it can be skipped. `changes` are `git diff --name-status --no-renames` pairs against
    the target (a rename is a delete and an add); `read_new` gives an added file's text, `read_target` a target file's;
    `target_paths` is every file of the target's tree. `guid_taken(guid, meta)` names what else holds an added
    `.meta`'s GUID ("" when nothing): the merge's metas and the package cache. None leaves that rule out (before the
    merge, when the files are not in a worktree yet); the judgment after the merge always has it."""
    reasons: list[str] = []
    added = {path for status, path in changes if status == "A"}
    assemblies: dict[str, list[str]] = {}
    for path in sorted(target_paths | added):
        if path.lower().endswith(ASSEMBLY_FILES):
            assemblies.setdefault(_folder(path), []).append(path)
    for status, path in changes:
        lower = path.lower()
        if status != "A":
            reasons.append(f"{path}: {'deleted' if status == 'D' else 'changed'} (status {status}), not only added")
            continue
        if not path.startswith("Assets/"):
            reasons.append(f"{path}: outside Assets/")
            continue
        if lower.endswith(".meta"):
            asset = path[:-len(".meta")]
            if asset not in added and not any(p.startswith(asset + "/") for p in added):
                reasons.append(f"{path}: a .meta for an asset or folder the merge does not add (a GUID or import "
                               "settings change)")
                continue
            if guid_taken is not None:
                text = (read_new(path) or b"").decode("utf-8", errors="replace")
                lines = len(_META_GUID_LINE.findall(text))
                if lines > 1:  # which one Unity takes is not shown, and the rule reads one
                    reasons.append(f"{path}: {lines} guid lines")
                    continue
                found = _META_GUID.search(text)
                if not found:
                    reasons.append(f"{path}: no GUID")
                    continue
                other = guid_taken(found.group(1).lower(), path)
                if other:
                    reasons.append(f"{path}: its GUID {found.group(1).lower()} is also {other}")
            continue
        if lower.endswith(".cs"):
            why = _assembly_reason(path, assemblies, added, read_target)
            if why:
                reasons.append(why)
                continue
            raw = read_new(path)
            if raw is None:  # a file that does not read could hold anything
                reasons.append(f"{path}: a script that does not read")
                continue
            hooks = hooks_in(raw)
            if hooks:
                reasons.append(f"{path}: a script in a test assembly that names {', '.join(hooks)} (it runs in the "
                               "Editor like any other assembly)")
            continue
        suffix = "." + lower.rsplit(".", 1)[1] if "." in lower.rsplit("/", 1)[-1] else ""
        if suffix not in PASSIVE:
            reasons.append(f"{path}: a {suffix or 'file with no extension'}, not a kind the allow list takes")
    return reasons


@dataclass(frozen=True)
class ScenePlan:
    """Which scene check a merge needs: `paths`, the changed scenes and prefabs only;
    `full`, every scene and prefab, with `reasons`; `skip`, none, with the files the allow list took in `added`."""
    kind: str
    paths: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    added: tuple[str, ...] = ()


def scene_plan(
    changes: Sequence[tuple[str, str]],
    read_new: Callable[[str], bytes | None],
    target_paths: set[str],
    read_target: Callable[[str], bytes | None],
    guid_taken: Callable[[str, str], str] | None = None,
) -> ScenePlan:
    """A merge that changes a scene or prefab checks those paths (a scene plus code too).
    One that deletes a scene or prefab (a rename too: a delete and an add) checks every one: what referred to it is
    elsewhere, and its path is in no tree to load. Else the allow list decides between `full` and `skip`."""
    scenes = [(status, path) for status, path in changes if path.lower().endswith(SCENE_SUFFIXES)]
    deleted = [path for status, path in scenes if status == "D"]
    if deleted:
        return ScenePlan("full", reasons=tuple(f"{path}: a scene or prefab deleted (or renamed): what referred to it "
                                               "is in other files" for path in deleted))
    if scenes:
        return ScenePlan("paths", paths=tuple(path for _, path in scenes))
    reasons = scene_skip_reasons(changes, read_new, target_paths, read_target, guid_taken)
    if reasons:
        return ScenePlan("full", reasons=tuple(reasons))
    return ScenePlan("skip", added=tuple(path for _, path in changes))
