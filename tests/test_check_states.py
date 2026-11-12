"""The slot states and git environments a land's checks read, swept.

The same four branches land in each state of the slot and of git's environment, and every state must give the same
`checks.md` (and the same outcome) as the reference state. A state that gives another is a flag; a flag the tool
leaves open would go in `KNOWN`, with its reason. `KNOWN` starts empty. The test prints
`check states: N read; differ: [...]; outside KNOWN: M` and fails on a flag outside `KNOWN`.

The branches (`build`), one flow, real git, the Editor and the `unity` CLI stand-ins of `test_flow`:
- `fx` (findings): one of each `.meta` rule broken (an asset with no `.meta`, a folder with no `.meta`, an orphan, a
  duplicate GUID, a folder `.meta` over a file, a file `.meta` over a folder) and a dangling GUID reference, under a
  folder with a non-ASCII name and a space; and a rename of an unclaimed asset into its claim, which
  the claim read (`Flow._changed`, `--no-renames`) must see as a change outside the claim;
- `nx` (named): brings back an asset whose GUID a scene of the target names: the full scene check, "named by", and
  the run on the target after the merge in the candidate's slot (`_candidate_at`);
- `cx` (clean): a passive asset only: the skip note, with the count of the target's hook scripts;
- `rx`: a copy of a changed asset and a rename under the non-ASCII folder, so the diffs meet a copy, a
  rename and a path git quotes, and the named-by read a non-ASCII path.
The git output a land parses, and what in the sweep reaches each: `grep` (the four reads of
`Flow._grep`: the attribute, colour, output and pattern states, and a non-ASCII path that names a GUID);
`diff --name-only -z` and `--name-status -z` (the copy, the rename and the non-ASCII path, and `fx`'s rename across
the claim's edge; `diff.renames=copies`);
`ls-tree -r --name-only -z` (the non-ASCII paths); `rev-parse HEAD` and the candidate branch's `symbolic-ref` (the
three tag states). Guards that reach no read today, kept so a new parse is caught: `core.quotePath` and `core.abbrev`
(every path is read with `-z` or `--null`, every commit by its full name), `core.excludesFile` (its one land read is
the dirty check of the task's slot, `ls-files --others --exclude-standard`, which no branch here meets with an
untracked file; a user's exclude file hides an untracked file there as `.gitignore` does, which an untracked file
cannot lose: a land merges commits only), `diff.noprefix` and `diff.mnemonicPrefix` (no patch is parsed), and the
`ls-files -z` reads (`check_case_duplicates`, and `_candidate_at`'s `--others`, which finds no file in these lands).

The states (`Sweep.states`): the slot as a new worktree (the reference), parked and scrubbed by the tool, as Unity
leaves it; an untracked `.gitattributes` in the slot (three), `info/attributes` (two); fifteen hostile config keys,
only through `GIT_CONFIG_COUNT`, in eight families (keys that cannot hide each other's effect) and all at once, with
`core.hooksPath` as a control (the tool's `-c` must win: a hook that runs is a flag); git's four pathspec variables; a
tag named like the candidate branch; and a tag named like the target, ahead of it, and tags named like the
task branches, where the land must refuse, naming the tag, and with the refusal switched off must still read the
branches by their full names and give the reference's checks: 25 states, 4 lands each. Disk states change
the candidate slot when the land first reads a tree (`SceneCheckCase.land_with_disk`); environment states are set in
this process's environment around the lands only, so the test's own git commands are not changed.

Each state reaches the code it names (`test_each_state_reaches_git`: a bare git read in the state
differs from the same read without it, for each config key on its own; a disk state counts the lands it changed), and
a planted change of the reads flags the state that names it (`test_a_planted_read_change_flags_its_state`).

Run it alone from the repo's root with both folders on the path (`test_flow` is imported from `tests`):
`PYTHONPATH="src;tests" python -m unittest test_check_states` (on POSIX, `src:tests`).

Runtime: every state stays in the default suite, and the time is accepted as measured. Measured only on a loaded
machine, never on a quiet one: 23 states took 315 to 952 s (with up to three other full suites beside them); about 117
git calls a land, a third of a land's time its own guard (`Flow._fingerprint`). `UPE_CHECK_STATES=quick` keeps one
state of each kind, for a local run; the default suite runs them all.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest import mock

from contextlib import nullcontext

from upe.flow import Flow, FlowError

import test_flow
from test_flow import MATERIAL, asset, commit, file_meta, folder_meta, git, write

KNOWN: dict[str, str] = {}

OLD_GUID = hashlib.md5(b"Assets/Art/Old.png.meta").hexdigest()
GONE_GUID = hashlib.md5(b"Assets/Art/Gone.png.meta").hexdigest()
DANGLING = "c" * 32
TASKS = ("fx", "nx", "cx", "rx")
OUTPUT_KEYS = "grep.lineNumber=true, grep.column=true, grep.fullName=true"

EXPECT = {
    "fx": [
        "- Assets/F/Bare.png [meta] asset has no .meta, so its GUID will change",
        "- Assets/F/Sub [meta] asset has no .meta, so its GUID will change",
        "- Assets/F/Ghost.png.meta [meta] orphan .meta: its asset is missing",
        f"- Assets/F/Copy.png.meta [meta] GUID {OLD_GUID} is also used by Assets/Art/Old.png.meta",
        "- Assets/F/Flip.png.meta [meta] a folder .meta (folderAsset: yes) over a file",
        "- Assets/F/Dir.meta [meta] a file .meta over a folder: it has no `folderAsset: yes`",
        f"- Assets/F/Ä c/Ref.mat [guid-ref] &2100000 refers to GUID {DANGLING}, which no asset has",
        "- Assets/Art/Moved.png [claim] changed outside the task's claim",
        "- Assets/Art/Moved.png.meta [claim] changed outside the task's claim",
    ],
    "nx": ["is also named by Assets/Scenes/Named.unity of the target"],
    "rx": ["Assets/Art/Old.png: changed (status M), not only added",
           "Assets/Ä b/x y.png: deleted (status D), not only added",
           "is also named by Assets/Ä b/x y.png.meta of the target"],
    "cx": ["(the target has 1 scripts that name a hook, by its scan)"],
}


def config(**keys: str) -> dict[str, str]:
    """Config keys through `GIT_CONFIG_COUNT` only (`core_quotePath` for `core.quotePath`)."""
    env = {"GIT_CONFIG_COUNT": str(len(keys))}
    for i, (key, value) in enumerate(keys.items()):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key.replace("_", "."), value
    return env


@dataclass
class State:
    name: str
    kind: str  # "slot", "attributes", "config", "pathspec", "ref"
    env: dict = field(default_factory=dict)  # this process's environment around the lands
    slot_file: tuple | None = None  # (path in the slot, text): an untracked file at the first tree read
    info: str | None = None  # the text of `info/attributes` in the repo's common git folder
    on_disk: Callable | None = None  # the slot's disk at the first tree read: returns the paths to take away
    before: Callable | None = None  # run on the test before the lands (the flow given)
    probe: tuple = ()  # the bare git read that shows the state reaches git: (args, folder under the probe repo)
    # A state the land must refuse, with this text in its `FlowError`. It is checked once with the refusal
    # on, then the lands run with the refusal off (`Flow._refuse_tags`), and must give the reference's checks.
    refusal: str = ""


class Sweep(test_flow.SceneCheckCase):
    def tmp(self) -> Path:
        return Path(self._tmp.name)

    # The branches --------------------------------------------------------------------------------------------------

    def build(self):
        write(self.repo, "Assets/Empty.meta", folder_meta("5" * 32))  # an empty folder's .meta: on disk, no folder
        write(self.repo, "Assets/Tool.cs", "class Tool {}\n")  # a script the target tracks no .meta for
        asset(self.repo, "Assets/Art/Old.png", "png texture of the old asset\n" * 4)  # rx copies it
        asset(self.repo, "Assets/Editor/Hook.cs", "class Hook : UnityEditor.AssetPostprocessor {}\n")
        asset(self.repo, "Assets/Scenes/Named.unity", f"--- !u!1 &1\n  m_Tex: {{fileID: 2800000, guid: {GONE_GUID}, type: 3}}\n")
        asset(self.repo, "Assets/Ä b/x y.png", "png")
        asset(self.repo, "Assets/Art/Moved.png", "png that fx moves into its claim\n")
        commit(self.repo, "the target of the sweep")
        flow = self.flow()
        flow.start("fx", "Findings.", ["Assets/F/"])
        wt = flow.workspaces.workspace("fx").path
        asset(wt, "Assets/F/Ä c/Ref.mat", MATERIAL + f"  m_Shader: {{fileID: 4800000, guid: {DANGLING}, type: 3}}\n")
        write(wt, "Assets/F/Bare.png", "png")
        write(wt, "Assets/F/Ghost.png.meta", file_meta("8" * 32))
        write(wt, "Assets/F/Copy.png", "png")
        write(wt, "Assets/F/Copy.png.meta", file_meta(OLD_GUID))
        write(wt, "Assets/F/Sub/In.png", "png")
        write(wt, "Assets/F/Sub/In.png.meta", file_meta("4" * 32))
        write(wt, "Assets/F/Flip.png", "png")
        write(wt, "Assets/F/Flip.png.meta", folder_meta("3" * 32))
        write(wt, "Assets/F/Dir.meta", file_meta("2" * 32))
        write(wt, "Assets/F/Dir/x.png", "png")
        write(wt, "Assets/F/Dir/x.png.meta", file_meta("1" * 32))
        # A rename from outside the claim into it. With git's default `diff.renames=true` a read without
        # `--no-renames` names only the new path, inside the claim, and the two claim lines go.
        git(wt, "mv", "Assets/Art/Moved.png", "Assets/F/Moved.png")
        git(wt, "mv", "Assets/Art/Moved.png.meta", "Assets/F/Moved.png.meta")
        commit(wt, "fx work")
        flow.start("nx", "Named.", ["Assets/Art/Gone.png"])
        asset(flow.workspaces.workspace("nx").path, "Assets/Art/Gone.png", "png")
        commit(flow.workspaces.workspace("nx").path, "nx work")
        # A copy of a changed file and a rename under the non-ASCII folder, so the diff reads (`_changed`,
        # `_changes`: `--no-renames -z`) meet what `diff.renames=copies`, `core.quotePath` and a lost `-z` would change.
        # A branch of its own: in `nx` they would make the plan before the merge a full one too, so `nx` would no
        # longer take the run on the target after the merge (`_candidate_at`), whose read the pathspec states must
        # reach.
        flow.start("rx", "Renames and copies.", ["Assets/Art/Old.png", "Assets/Art/Old copy.png", "Assets/Ä b/"])
        wt = flow.workspaces.workspace("rx").path
        asset(wt, "Assets/Art/Old copy.png", "png texture of the old asset\n" * 4)
        write(wt, "Assets/Art/Old.png", "png texture of the old asset\n" * 4 + "changed\n")
        git(wt, "mv", "Assets/Ä b/x y.png", "Assets/Ä b/x z.png")
        git(wt, "mv", "Assets/Ä b/x y.png.meta", "Assets/Ä b/x z.png.meta")
        commit(wt, "rx work")
        flow.start("cx", "Clean.", ["Assets/C/"])
        asset(flow.workspaces.workspace("cx").path, "Assets/C/Tex.png", "png")
        commit(flow.workspaces.workspace("cx").path, "cx work")
        return flow

    # The states ----------------------------------------------------------------------------------------------------

    def states(self) -> list[State]:
        binary = self.tmp() / "attributes-binary"
        binary.write_text("* binary\n")
        everything = self.tmp() / "excludes-everything"
        everything.write_text("*\n")
        hooks = self.tmp() / "hooks"
        hooks.mkdir(exist_ok=True)
        self.marker = self.tmp() / "a-hook-ran"
        for hook in ("post-checkout", "post-merge", "post-commit", "pre-commit", "reference-transaction"):
            (hooks / hook).write_text(f"#!/bin/sh\necho {hook} >> '{self.marker.as_posix()}'\n", newline="\n")
        grep = (("grep", "-e", "guid", "HEAD"), "")
        keys = [
            ("color.ui", "always", grep), ("color.grep", "always", grep), ("color.diff", "always", (("diff",), "")),
            ("grep.lineNumber", "true", grep), ("grep.column", "true", grep),
            ("grep.patternType", "fixed", (("grep", "-e", "^guid", "HEAD"), "")),
            ("grep.extendedRegexp", "true", (("grep", "-e", "gu(i)d", "HEAD"), "")),
            ("grep.fullName", "true", (("grep", "-e", "guid", "HEAD"), "Assets")),
            ("core.quotePath", "false", (("ls-files",), "")), ("diff.noprefix", "true", (("diff",), "")),
            ("diff.mnemonicPrefix", "true", (("diff",), "")),
            ("diff.renames", "copies", (("diff", "--name-status", "two~1", "two"), "")),
            ("core.abbrev", "4", (("log", "--oneline", "-1"), "")),
            ("core.attributesFile", str(binary), grep),
            ("core.excludesFile", str(everything), (("status", "--porcelain"), "")),
        ]
        # One probe per key (`test_each_state_reaches_git`); the sweep lands them in families, for the
        # runtime: each family keys that cannot hide each other's effect: `grep.patternType=fixed` and
        # `grep.extendedRegexp` apart, as the second changes the first's reading.
        self.key_probes = [State(f"{key}={value}", "config", env=config(**{key.replace(".", "_"): value}), probe=probe)
                           for key, value, probe in keys]
        families = [("color.ui", "color.grep", "color.diff"), ("grep.lineNumber", "grep.column", "grep.fullName"),
                    ("grep.patternType",), ("grep.extendedRegexp",), ("diff.noprefix", "diff.mnemonicPrefix", "diff.renames"),
                    ("core.quotePath", "core.abbrev"), ("core.attributesFile",), ("core.excludesFile",)]
        value = {key: v for key, v, _ in keys}
        assert sorted(k for f in families for k in f) == sorted(value), "each key in one family"
        states = [
            State("a new slot (the reference)", "slot"),
            State("a parked slot, scrubbed by the tool", "slot", before=self.park),
            State("the slot as Unity leaves it", "slot", on_disk=self.as_unity_leaves_it),
            *[State(f"an untracked .gitattributes in the slot: {text}", "attributes", slot_file=(".gitattributes", text + "\n"),
                    probe=grep) for text in ("* binary", "* -diff", "*.meta -text -diff")],
            *[State(f"info/attributes: {text}", "attributes", info=text + "\n", probe=grep)
              for text in ("* -diff", "* binary")],
            *[State(", ".join(f"{k}={value[k]}" for k in family), "config",
                    env=config(**{k.replace(".", "_"): value[k] for k in family})) for family in families],
            State("all the config keys at once", "config",
                  env=config(**{key.replace(".", "_"): value for key, value, _ in keys}), probe=grep),
            State("core.hooksPath (the tool's -c must win)", "config", env=config(core_hooksPath=str(hooks)),
                  probe=(("commit", "-q", "--allow-empty", "-m", "probe"), "")),
            *[State(f"{variable}=1", "pathspec", env={variable: "1"}, probe=(("ls-files", "--", pattern), ""))
              for variable, pattern in (("GIT_GLOB_PATHSPECS", "Assets/*.meta"), ("GIT_NOGLOB_PATHSPECS", "Assets/*.meta"),
                                        ("GIT_LITERAL_PATHSPECS", "Assets/*.meta"),
                                        ("GIT_ICASE_PATHSPECS", "assets/*.meta"))],
            State("a tag named like the candidate branch", "ref", before=self.tag_candidates,
                  probe=(("rev-parse", "--abbrev-ref", "HEAD"), "")),
            State("a tag named like the target, ahead of it", "ref", before=self.tag_target, refusal="refs/tags/main"),
            State("tags named like the task branches", "ref", before=self.tag_task_branches, refusal="refs/tags/upe/"),
        ]
        if os.environ.get("UPE_CHECK_STATES") == "quick":  # a local run: one state of each kind
            kinds: set[str] = set()
            states = [s for s in states if s.kind == "slot" or not (s.kind in kinds or kinds.add(s.kind))]
        return states

    def park(self, flow):
        """Three parked slots, made and given back by the tool, so the candidates are taken from them (scrubbed)."""
        for i in range(3):
            flow.workspaces.create(f"jump{i}", "main")
        for i in range(3):
            flow.workspaces.release(f"jump{i}")
            git(self.repo, "branch", "-D", f"upe/jump{i}")
        self.parked = {p.name for p in flow.workspaces.slots_dir.iterdir() if p.is_dir()}  # a candidate takes one

    def tag_candidates(self, flow):
        for task in TASKS:
            git(self.repo, "tag", f"upe-merge/{task}")
        return lambda: [git(self.repo, "tag", "-d", f"upe-merge/{task}") for task in TASKS]

    def tag_target(self, flow):
        """A tag `main` on a commit ahead of the target that holds a file outside
        every claim."""
        git(self.repo, "checkout", "-q", "-b", "side")
        asset(self.repo, "Assets/Evil/Evil.txt", "not reviewed\n")
        commit(self.repo, "a commit only a tag names")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "tag", "main", "side")
        git(self.repo, "branch", "-D", "side")
        return lambda: git(self.repo, "tag", "-d", "main")

    def tag_task_branches(self, flow):
        """A tag `upe/<id>` for each task, on the target (a commit before each task's work)."""
        for task in TASKS:
            git(self.repo, "tag", f"upe/{task}", "refs/heads/main")
        return lambda: [git(self.repo, "tag", "-d", f"upe/{task}") for task in TASKS]

    def as_unity_leaves_it(self, root: Path):
        """What Unity writes when it opens the project: each file here would hide a finding from a check of the disk."""
        (root / "Assets/Empty").mkdir(exist_ok=True)
        return [root / "Assets/Empty",
                *self.put(root, "Assets/Tool.cs.meta", file_meta("6" * 32)),
                *self.put(root, "Assets/Art/Stray.png", "png"), *self.put(root, "Assets/Art/Stray.png.meta", file_meta(OLD_GUID)),
                *self.put(root, "Assets/F/Bare.png.meta", file_meta("7" * 32)),
                *self.put(root, "Assets/F/Ghost.png", "png"),
                *self.put(root, "Assets/F/Sub.meta", folder_meta("9" * 32)),
                *self.put(root, f"Assets/F/Hidden.shader.meta", file_meta(DANGLING)),
                *self.put(root, "Temp/UnityTempFile", "x"), *self.put(root, "Logs/Editor.log", "x")]

    # One state's lands ---------------------------------------------------------------------------------------------

    def normalize(self, text: str) -> str:
        for root in {str(self.tmp()), self.tmp().as_posix(), str(self.tmp().resolve()), self.tmp().resolve().as_posix()}:
            text = text.replace(root, "<tmp>")
        text = re.sub(r"\b[0-9a-f]{40}\b", "<commit>", text)
        text = re.sub(r"\b[0-9a-f]{12}\b", "<commit12>", text)
        return re.sub(r"\bs[1-9][0-9]*\b", "<slot>", text)

    def land_state(self, flow, state: State, tasks=TASKS) -> dict[str, tuple]:
        """{task: (outcome, normalized checks.md)} of the tasks' lands in `state`. Kept results are cleared first, so
        each state's first land meets what the reference's did."""
        for folder in ("scene-baselines", "test-baselines"):
            shutil.rmtree(self.config.root / ".upe" / folder, ignore_errors=True)
        info = Path(git(self.repo, "rev-parse", "--git-common-dir").strip())
        info = (info if info.is_absolute() else self.repo / info) / "info" / "attributes"
        undo = state.before(flow) if state.before else None
        self.reached = 0

        def on_disk(root):
            self.reached += 1
            # Every state: a package cache, as a real slot's Library holds one (the test's robocopy copies none), so
            # the GUID references are checked.
            made = self.put(root, "Library/PackageCache/com.x/Pkg.mat.meta", file_meta("d" * 32))
            made += state.on_disk(root) if state.on_disk else []
            if state.slot_file:
                made += self.put(root, *state.slot_file)
            if state.before == self.park:
                self.assertIn(root.name, self.parked, "the candidate is in a parked slot")
            return made

        out = {}
        try:
            if state.info:
                info.parent.mkdir(exist_ok=True)
                info.write_text(state.info)
            if state.refusal:  # the refusal on: the land stops before it reads (a flag, under its own key, if not)
                try:
                    with mock.patch.dict(os.environ, state.env):
                        Flow.land(flow, tasks[0])
                    out["refusal"] = ("not refused",)
                except FlowError as exc:
                    if state.refusal not in str(exc):
                        out["refusal"] = ("refused for another reason", self.normalize(str(exc)))
            for task in tasks:
                (flow.run_dir(task) / "checks.md").unlink(missing_ok=True)
                try:
                    with mock.patch.dict(os.environ, state.env), \
                            mock.patch.object(Flow, "_refuse_tags", lambda *a: None) if state.refusal else nullcontext():
                        outcome = self.land_with_disk(flow, on_disk, task)
                    out[task] = (outcome.state, self.normalize(self.checks_md(flow, task)))
                except Exception as exc:  # FlowError, MergeError, WorkspaceError: a flag, not an error of the test
                    out[task] = ("raised", type(exc).__name__, self.normalize(str(exc)))
        finally:
            info.unlink(missing_ok=True)
            if callable(undo):
                undo()
        if all(o[0] != "raised" for o in out.values()):  # a land that raised is a flag of its own
            self.assertEqual(self.reached, len(tasks), f"{state.name}: the slot was changed at each land's first read")
        return out

    def first_difference(self, a: dict, b: dict) -> str:
        if "refusal" in b:
            return f"refusal: {b['refusal']}"[:600]
        for task in a:
            if a[task] != b.get(task) and "raised" in (a[task][0], b.get(task, ("",))[0]):
                return f"{task}: {a[task][0]!r} -> {b.get(task)!r}"[:600]
            if a[task] != b.get(task):
                lines_a, lines_b = "\n".join(map(str, a[task])).splitlines(), "\n".join(map(str, b.get(task, ()))).splitlines()
                for la, lb in zip(lines_a + [""] * len(lines_b), lines_b + [""] * len(lines_a)):
                    if la != lb:
                        return f"{task}: {la!r} -> {lb!r}"
        return ""

    # The tests -----------------------------------------------------------------------------------------------------

    def test_every_state_gives_the_same_checks(self):
        # Red with a read's option removed. Before tags were handled, "a tag named like the candidate branch" made every
        # land raise MergeError.
        began = time.monotonic()
        flow = self.build()
        states = self.states()
        reference = self.land_state(flow, states[0])
        for task, lines in EXPECT.items():  # a reference that read nothing would agree with every state
            self.assertNotEqual(reference[task][0], "raised", reference[task])
            for line in lines:
                self.assertIn(line, reference[task][1], f"{task}: the reference reads what its branch holds")
        self.assertEqual([reference[t][0] for t in TASKS], ["checks-failed", "awaiting-council", "awaiting-council", "awaiting-council"])
        read, differ = 1, []
        for state in states[1:]:
            got = self.land_state(flow, state)
            read += 1
            if got != reference:
                differ.append((state.name, self.first_difference(reference, got)))
            if self.marker.exists():
                differ.append((state.name, f"a hook ran: {self.marker.read_text().split()}"))
                self.marker.unlink()
        outside = [name for name, _ in differ if name not in KNOWN]
        print(f"\ncheck states: {read} read; differ: {differ}; outside KNOWN: {len(outside)}; "
              f"{time.monotonic() - began:.0f} s", file=sys.stderr)
        self.assertEqual(outside, [], "a state that gives another checks.md: mend the read, or name it in KNOWN")

    def test_each_state_reaches_git(self):
        # A bare git read (no `-c`, no fixed options) in each state of the environment differs from
        # the same read without it, so the state reaches git on this machine. A state that does not is a failure.
        repo = self.tmp() / "probe"
        write(repo, "Assets/Top.meta", "fileFormatVersion: 2\nguid: " + "a" * 32 + "\nline\nline\nline\n")
        write(repo, "Assets/Deep/Inner.meta", "guid: " + "b" * 32 + "\n")
        write(repo, "Assets/Ä.png", "png")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        commit(repo, "one")
        write(repo, "Assets/Copy.meta", (repo / "Assets/Top.meta").read_text() + "copied\n")
        write(repo, "Assets/Top.meta", (repo / "Assets/Top.meta").read_text() + "changed\n")
        commit(repo, "two: a copy")
        git(repo, "tag", "two")  # the probes name it: the hooksPath probe adds a commit
        write(repo, "Assets/Top.meta", (repo / "Assets/Top.meta").read_text() + "unstaged\n")
        write(repo, "Assets/new.txt", "untracked\n")
        import subprocess

        def bare(args, folder, env):
            done = subprocess.run(["git", "-C", str(repo / folder), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env={**os.environ, **env})
            return done.returncode, done.stdout

        for state in [*self.states(), *self.key_probes]:  # the families have no probe: each key has one
            if not state.probe:
                continue
            with self.subTest(state.name):
                args, folder = state.probe
                plain = bare(args, folder, {})
                undo = None
                if state.slot_file:
                    write(repo, *state.slot_file)
                if state.info:
                    (repo / ".git/info").mkdir(exist_ok=True)
                    (repo / ".git/info/attributes").write_text(state.info)
                if state.kind == "ref":
                    git(repo, "tag", "main")
                    undo = lambda: git(repo, "tag", "-d", "main")
                try:
                    got = bare(args, folder, state.env)
                finally:
                    (repo / ".gitattributes").unlink(missing_ok=True)
                    (repo / ".git/info/attributes").unlink(missing_ok=True)
                    if undo:
                        undo()
                if "hooksPath" in state.name:
                    self.assertTrue(self.marker.exists(), "the hook ran for a bare git")
                    self.marker.unlink()
                else:
                    self.assertNotEqual(plain, got, f"{state.name} does not change a bare read: it does not reach git")

    def test_a_planted_read_change_flags_its_state(self):
        # Each change of a read's argv, made from outside the code (the flow's and the
        # merger's runners, or `subprocess.Popen` for the streamed `_guids_named` read), flags the state that names it.
        # A change the reference's own branch already shows (a rename, a non-ASCII path, a full commit name) flags the
        # reference itself; the others must leave the reference as it was.
        flow = self.build()
        states = {s.name: s for s in self.states()}
        reference = "a new slot (the reference)"
        plain = {task: self.land_state(flow, states[reference], (task,)) for task in ("fx", "nx", "rx")}
        colours = "color.ui=always, color.grep=always, color.diff=always"
        diffs = "diff.noprefix=true, diff.mnemonicPrefix=true, diff.renames=copies"

        def drop(word, unless=None):
            return lambda a: [x for x in a if x != word] if unless is None or unless in a else a

        grep = "grep"
        plants = [  # (name, the read it changes, how, the state it flags, the task that lands)
            ("-a dropped", grep, drop("-a"), "an untracked .gitattributes in the slot: * binary", "fx"),
            ("--no-color dropped", grep, drop("--no-color"), colours, "fx"),
            ("--no-line-number dropped", grep, drop("--no-line-number"), OUTPUT_KEYS, "fx"),
            ("--no-column dropped", grep, drop("--no-column"), OUTPUT_KEYS, "fx"),
            ("-G dropped", grep, drop("-G"), "grep.patternType=fixed", "fx"),
            ("the pathspec variables kept", grep, None, "GIT_GLOB_PATHSPECS=1", "fx"),
            ("--no-color dropped from the streamed read", "popen", drop("--no-color"), colours, "nx"),
            ("--no-renames dropped from the diffs", "diff", drop("--no-renames"), diffs, "rx"),
            ("-z dropped from the diffs", "diff", drop("-z"), reference, "rx"),
            ("-z dropped from ls-tree", "ls-tree", drop("-z"), reference, "fx"),
            ("rev-parse HEAD read short", "rev-parse", lambda a: a[:-1] + ["--short", "HEAD"] if a[-1] == "HEAD" else a,
             reference, "fx"),
            ("the candidate branch read by its short name", "symbolic-ref", lambda a: a[:-1] + ["--short", "HEAD"],
             reference, "nx"),
        ]
        real, merger_real, real_popen = flow._runner, flow.merger._runner, subprocess.Popen
        for name, read, change, state, task in plants:
            with self.subTest(name):
                def planted(inner):
                    def run(argv, **kw):
                        if read in argv:
                            if change is None:
                                kw["env"] = None
                            else:
                                argv = change(list(argv))
                        return inner(argv, **kw)
                    return run

                def popen(argv, *a, **kw):
                    if read == "popen" and "grep" in argv and "-o" in argv and "-h" in argv:
                        argv = change(list(argv))
                    return real_popen(argv, *a, **kw)

                flow._runner, flow.merger._runner = (planted(real), planted(merger_real)) if read != "popen" else \
                    (real, merger_real)
                try:
                    with mock.patch("subprocess.Popen", popen):
                        planted_reference = self.land_state(flow, states[reference], (task,))
                        if name == "--no-renames dropped from the diffs":
                            # git's default `diff.renames=true` sees the rename: the reference flags too; the copy is
                            # seen only with `diff.renames=copies`, so that state differs from the planted reference.
                            self.assertNotEqual(planted_reference, plain[task], "the reference is flagged")
                            self.assertNotEqual(self.land_state(flow, states[state], (task,)), planted_reference,
                                                f"{state} reads the copy")
                        elif state != reference:
                            self.assertEqual(planted_reference, plain[task], "the reference is not flagged")
                            self.assertNotEqual(self.land_state(flow, states[state], (task,)), plain[task],
                                                f"{state} is flagged")
                        else:
                            self.assertNotEqual(planted_reference, plain[task], "the reference is flagged")
                finally:
                    flow._runner, flow.merger._runner = real, merger_real


if __name__ == "__main__":
    unittest.main()
