"""The states that hold work, swept.

Each state is made with real git for a dead start of `dash` (a marked claim, the lock free, the slot aged) by a
builder of `test_flow.DeadStartCase`, the same builders `DeadStartTests` uses. `finish dash` runs on each, and the
work must still be there after it: `finish` refused, or (state 8) the next `finish` ended the removal it marked. A
state the tool leaves open is in `KNOWN`, with its reason; the test prints the states it read and its
flags, and fails on a flag outside `KNOWN`.

It drives two of the code paths that remove a slot folder or delete a `upe/` branch, `_clear_start` and
`Workspaces.remove_slot`, through `finish`.

`TargetSlotSweep` covers the slot of a land's run on the target (`base:<task>`), taken by `acquire` (detached,
in a parked slot or a new one) and given back by `_target_slot` and `_discard_candidate`. Each state is made with real
git where that slot could meet it; the run on the target is driven through `Flow._target_slot` (acquire, then release)
and `Flow._discard_candidate`, and the work must still be there after. The others (a start undone, the candidate's own
acquire) are not read here.

`RecordedFinishSweep` covers a recorded task's `finish` (`Flow._finish`), which reads the slot's uncommitted
changes, inside and outside the claim, and parks only a clean slot.
"""

import sys
import unittest

from upe.flow import FlowError
from upe.workspace import WorkspaceError

from test_flow import DeadStartCase, Work, aged, asset, commit, git, write

KNOWN = {
    "a commit only in the slot's reflog": "a reused slot's reflog also holds old candidates' merges",
    "a commit on a parked slot's detached HEAD": (
        "a parked slot's detached HEAD holds the merge of a candidate a land threw away; "
        "`acquire` moves the slot off it, as the candidate's own acquire did, and the commit stays in "
        "the slot's reflog until git's reflog expiry (90 days for a reachable entry, 30 for an unreachable one, by "
        "default), or the slot's removal by a `finish` of a task that never recorded (a recorded task's `finish` parks "
        "its slot)"),
    "a commit only in the slot's reflog, before a recorded task's finish": (
        "the commit was reset away in the task's own slot before `finish` ran, so before it no "
        "ref and no worktree's HEAD has it (`git rev-list --all` does not list it). For a task not landed, `finish` "
        "parks the slot, moves no ref and leaves every reflog as it was; for a landed task it also deletes the branch "
        "`upe/<id>` and that branch's reflog. Either way the commit stays in the slot's own "
        "HEAD reflog, where git keeps an unreachable entry 30 days by default. The state shows that `finish` deletes "
        "nothing a ref has; it does not show the commit kept, which nothing in the tool can do once it is only in a "
        "reflog"),
}


class WorkStatesSweep(DeadStartCase):
    def states(self):
        states = [
            ("a commit on upe/<id>", self.a_commit_on_the_branch),
            ("a commit on the slot's detached HEAD", self.a_commit_on_the_detached_head),
            ("a tracked file deleted, staged", lambda flow: self.a_deletion(flow, staged=True)),
            ("a tracked file deleted, not staged", lambda flow: self.a_deletion(flow, staged=False)),
            ("a tracked file changed, staged", lambda flow: self.a_change(flow, staged=True)),
            ("a tracked file changed, not staged", lambda flow: self.a_change(flow, staged=False)),
            ("an untracked file", self.an_untracked_file),
            ("a slot another task owns", self.a_slot_another_task_owns),
            ("a commit only in the slot's reflog", self.a_commit_only_in_the_reflog),
        ]
        if sys.platform == "win32":  # POSIX deletes a file held open, so git's removal does not stop there
            states.insert(7, ("a removal git stops half way (a file held open)", self.a_removal_git_stops_half_way))
        return states

    def test_finish_keeps_the_work_of_every_state(self):
        read, flags = 0, []
        for i, (name, build) in enumerate(self.states()):
            if i:
                self.tearDown()
                self.setUp()
            flow = self.flow()
            work = build(flow)
            try:
                try:
                    flow.finish("dash")
                except FlowError:
                    pass
            finally:
                if work.held is not None:
                    work.held.close()
            if work.held is not None:
                aged(work.path)
                try:
                    flow.finish("dash")
                except FlowError:
                    pass
            read += 1
            if not work.kept():
                flags.append(name)
        outside = [name for name in flags if name not in KNOWN]
        print(f"\nwork states: {read} read; flagged: {flags}; outside KNOWN: {len(outside)}", file=sys.stderr)
        self.assertEqual(read, len(self.states()))
        self.assertEqual(outside, [])


class RecordedFinishSweep(DeadStartCase):
    """A recorded task's `finish`
    (`Flow._finish`), which now reads the slot's changes, names each by side of the claim, and parks only a clean
    slot. Each state is made with real git in the task's own slot (inside and outside its claim), `finish dash` runs
    on it, and the work must still be there after. A refusal is the way it keeps the work; for the commit states,
    `finish` parks the slot and the commit must still be reachable."""

    def states(self):
        states = [
            ("a commit on upe/<id>", self.recorded_commit_on_the_branch),
            ("a commit on the slot's detached HEAD", self.recorded_commit_on_the_detached_head),
            ("a commit only in the slot's reflog, before a recorded task's finish", self.recorded_commit_only_in_the_reflog),
            ("a slot another task owns", self.recorded_beside_a_slot_another_task_owns),
            ("a commit on a landed task's branch after the land", self.recorded_commit_after_the_land),
            ("the same, the slot's HEAD then detached off it", lambda f: self.recorded_commit_after_the_land(
                f, ["checkout", "-q", "--detach", "HEAD~1"])),
            ("the same, the slot then on another branch off it", lambda f: self.recorded_commit_after_the_land(
                f, ["checkout", "-q", "-b", "elsewhere", "HEAD~1"])),
        ]
        for inside in (True, False):
            side = "inside the claim" if inside else "outside the claim"
            states += [
                (f"a tracked file deleted, staged, {side}", lambda f, i=inside: self.recorded_deletion(f, True, i)),
                (f"a tracked file deleted, not staged, {side}", lambda f, i=inside: self.recorded_deletion(f, False, i)),
                (f"a tracked file changed, staged, {side}", lambda f, i=inside: self.recorded_change(f, True, i)),
                (f"a tracked file changed, not staged, {side}", lambda f, i=inside: self.recorded_change(f, False, i)),
                (f"an untracked file, {side}", lambda f, i=inside: self.recorded_new_file(f, False, i)),
                (f"a new file, staged, {side}", lambda f, i=inside: self.recorded_new_file(f, True, i)),
                (f"a staged change, the disk put back, {side}", lambda f, i=inside: self.recorded_index_only(f, "MM", i)),
                (f"a staged new file, deleted from disk, {side}", lambda f, i=inside: self.recorded_index_only(f, "AD", i)),
            ]
        return states

    def test_a_recorded_tasks_finish_keeps_the_work_of_every_state(self):
        read, flags, refused = 0, [], []
        for i, (name, build) in enumerate(self.states()):
            if i:
                self.tearDown()
                self.setUp()
            flow = self.flow()
            work = build(flow)
            try:
                flow.finish("dash")
            except FlowError:
                refused.append(name)
            read += 1
            if not work.kept():
                flags.append(name)
        outside = [name for name in flags if name not in KNOWN]
        print(f"\nrecorded finish states: {read} read; flagged: {flags}; outside KNOWN: {len(outside)}; refused: "
              f"{len(refused)}", file=sys.stderr)
        self.assertEqual(read, len(self.states()))
        self.assertEqual(outside, [])


class TargetSlotSweep(DeadStartCase):
    """The slot of a land's run on the target meets each state; a land of `kick` drives it."""

    LAND = "kick"

    def parked(self, flow, change):
        """A parked slot (owner none, detached, its Library kept) with `change(path)` made in it after it was parked."""
        flow.workspaces.create("jump", "main")
        path = flow.workspaces.path_of("jump")
        flow.workspaces.release("jump")
        git(self.repo, "branch", "-D", "upe/jump")
        change(path)
        return path

    def parked_change(self, flow, kind, staged):
        rel = "Assets/Scenes/Arena.unity"

        def change(path):
            if kind == "deleted":
                (path / rel).unlink()
            else:
                write(path, rel, "changed\n")
            if staged:
                git(path, "add", "-A", rel)

        path = self.parked(flow, change)
        if kind == "deleted":
            return Work(path, lambda: path.is_dir() and not (path / rel).exists())
        return Work(path, lambda: (path / rel).is_file() and (path / rel).read_text(encoding="utf-8") == "changed\n")

    def parked_untracked(self, flow):
        rel = "Assets/New.cs"
        path = self.parked(flow, lambda p: write(p, rel, "new\n"))
        return Work(path, lambda: (path / rel).is_file())

    def parked_detached_commit(self, flow):
        """A commit on a parked slot's detached HEAD: what a land refused or in conflict leaves (its candidate's merge,
        the candidate branch deleted). `acquire` checks the slot out at the target, as the candidate's acquire does
        today, so that commit is then only in the slot's reflog: a KNOWN state, not a new one."""
        made = []

        def change(path):
            asset(path, "Assets/Features/Jump/Jump.cs", "class Jump {}\n")
            commit(path, "a thrown-away candidate's merge")
            made.append(git(path, "rev-parse", "HEAD").strip())

        path = self.parked(flow, change)
        return Work(path, lambda: self.reachable(made[0]))

    def parked_reflog_commit(self, flow):
        made = []

        def change(path):
            asset(path, "Assets/Features/Jump/Jump.cs", "class Jump {}\n")
            commit(path, "reset away")
            made.append(git(path, "rev-parse", "HEAD").strip())
            git(path, "reset", "-q", "--hard", "HEAD~1")

        path = self.parked(flow, change)
        return Work(path, lambda: self.reachable(made[0]))

    def held_by_an_editor(self, flow):
        """Windows: a parked slot an Editor has open (its lockfile held with no sharing): not reusable."""
        from test_workspace import held_exclusively

        path = self.parked(flow, lambda p: (p / "Temp").mkdir())
        head = git(path, "rev-parse", "HEAD").strip()
        held = held_exclusively(path / "Temp" / "UnityLockfile")
        held.__enter__()

        class Closer:
            def close(self):
                held.__exit__(None, None, None)

        # Kept: the Editor's project was not checked out elsewhere under it, nor its Temp cleared.
        return Work(path, lambda: (path / "Temp" / "UnityLockfile").exists()
                    and git(path, "rev-parse", "HEAD").strip() == head, Closer())

    def states(self):
        # (name, builder, the KNOWN name it reads as when flagged)
        states = [
            ("a commit on upe/<id>", self.a_commit_on_the_branch, None),
            ("a commit on a parked slot's detached HEAD", self.parked_detached_commit, None),
            ("a commit only in the slot's reflog", self.parked_reflog_commit, None),
            ("a tracked file deleted, staged, in a parked slot", lambda f: self.parked_change(f, "deleted", True), None),
            ("a tracked file deleted, not staged, in a parked slot", lambda f: self.parked_change(f, "deleted", False),
             None),
            ("a tracked file changed, staged, in a parked slot", lambda f: self.parked_change(f, "changed", True), None),
            ("a tracked file changed, not staged, in a parked slot", lambda f: self.parked_change(f, "changed", False),
             None),
            ("an untracked file in a parked slot", self.parked_untracked, None),
            ("a slot another task owns", self.a_slot_another_task_owns, None),
        ]
        if sys.platform == "win32":
            states.append(("a removal git stops half way (a file held open)", self.a_removal_git_stops_half_way, None))
            states.append(("a parked slot an Editor has open", self.held_by_an_editor, None))
        return states

    def test_the_run_on_the_target_keeps_the_work_of_every_state(self):
        read, flags, took = 0, [], []
        for i, (name, build, known_as) in enumerate(self.states()):
            if i:
                self.tearDown()
                self.setUp()
            flow = self.flow()
            work = build(flow)
            half_way = name.startswith("a removal git stops half way")
            if half_way:
                try:
                    flow.finish("dash")  # stops half way: the slot keeps its owner and its marker
                except FlowError:
                    pass
            asset(self.repo, "Assets/Features/Kick/Kick.cs", "class Kick {}\n")  # the target, past every parked slot
            commit(self.repo, "kick's target")
            tip = git(self.repo, "rev-parse", "main").strip()
            notes = []
            try:
                with flow._target_slot(self.LAND, tip, notes) as slot:
                    took.append((name, slot == work.path))
                    self.assertEqual(git(slot, "rev-parse", "HEAD").strip(), tip)
                flow.workspaces.acquire("base:" + self.LAND, None, tip)  # a land killed in that run leaves it
                flow._discard_candidate(self.LAND)
                self.assertNotIn("base:" + self.LAND, flow.workspaces.slots().values())
            finally:
                if work.held is not None:
                    work.held.close()
            if half_way:
                aged(work.path)
                try:
                    flow.finish("dash")
                except FlowError:
                    pass
            read += 1
            if not work.kept():
                flags.append(known_as or name)
        outside = [name for name in flags if name not in KNOWN]
        reused = [name for name, same in took if same]
        print(f"\ntarget slot states: {read} read; flagged: {flags}; outside KNOWN: {len(outside)}; the run took the "
              f"state's own slot in: {reused}", file=sys.stderr)
        self.assertEqual(read, len(self.states()))
        self.assertEqual(outside, [])

    def test_a_run_on_the_target_that_fails_to_make_its_slot_leaves_no_slot_and_deletes_no_branch(self):
        # `acquire`'s except path for a detached slot: the folder it made is removed, its entry dropped, and no
        # `branch -D` runs (there is no branch of its own). Robocopy failing is a failure git cannot be made to give:
        # with a stand-in for robocopy only; git is real.
        import subprocess as sp

        flow = self.flow()
        branches = git(self.repo, "for-each-ref", "--format=%(refname)")
        real = flow.workspaces._runner

        def robocopy_fails(argv, **kw):
            if argv[0] == "robocopy":
                return sp.CompletedProcess(argv, 16, "robocopy: no space", "")
            return real(argv, **kw)

        flow.workspaces._runner = robocopy_fails
        tip = git(self.repo, "rev-parse", "main").strip()
        with self.assertRaises(WorkspaceError) as ctx:
            with flow._target_slot(self.LAND, tip, []):
                pass
        self.assertIn("robocopy failed with 16", str(ctx.exception))
        self.assertEqual(flow.workspaces.slots(), {})
        self.assertEqual(list(flow.workspaces.slots_dir.iterdir()) if flow.workspaces.slots_dir.exists() else [], [])
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)"), branches)


if __name__ == "__main__":
    unittest.main()
