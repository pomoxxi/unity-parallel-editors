"""What an agent could not read from the tool, closed in the tool's answers.

A spent id: `start` on a finished id names a free id, and the step it prints starts.
The tests: `start` names the project's test folders, or prints the git command that lists them.
A slot's uncommitted changes: `land` refuses those inside the claim and names the rest; `finish` names each and
prints the git steps that put back those outside the claim. Every step the tool prints is run here, on real git.
`next` after a false stop: "tell the user, do not fix" only when no finding is in a path the branch changed.

Each test runs on real git (`FlowCase`). A stand-in is used once, for a failure git cannot be made to give: the
`.meta` read that gives findings at paths the branch did not change (`FalseStopTests`, named there).
"""

import json
import os
import re
import shlex
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from upe import flow as flowmod
from upe.checks import MetaTree
from upe.flow import Config, Flow, FlowError
from upe.workspace import TASK_ID, WorkspaceError

from test_flow import FlowCase, asset, commit, git, write

CLAIM = ["Assets/Features/Dash/"]
EDITOR_FILE = "ProjectSettings/Packages/com.unity.probuilder/Settings.json"  # a file the Editor rewrites on its own


def posix_shell() -> str | None:
    """A real POSIX shell: Git for Windows' own `sh.exe` (beside its `cmd` folder; never WSL's `bash.exe`), or
    `/bin/sh`. None when there is none."""
    import shutil

    if os.name != "nt":
        return "/bin/sh" if Path("/bin/sh").exists() else None
    found = shutil.which("git")
    if not found:
        return None
    here = Path(found).resolve()  # Git\cmd\git.exe, or Git\mingw64\bin\git.exe from Git Bash
    for top in (here.parents[1], here.parents[2]):
        for sh in (top / "bin" / "sh.exe", top / "usr" / "bin" / "sh.exe"):
            if sh.exists():
                return str(sh)
    return None


def run_printed(command: str, shell: str = "sh") -> subprocess.CompletedProcess:
    """Run a command the tool printed, as printed, in a real shell: POSIX `sh` (Git Bash's), or `powershell`. With no
    such shell, word for word as POSIX reads it (`shlex`), which expands no `$`."""
    kw = dict(capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL)
    assert command.startswith("git "), command
    if shell == "powershell":
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], **kw)
    sh = posix_shell()
    if sh:
        return subprocess.run([sh, "-c", command], **kw)
    return subprocess.run(shlex.split(command), **kw)


def printed_steps(message: str) -> list[str]:
    """The git steps a refusal or a note prints, in order. Each is in backticks and followed by what it does; a
    backtick inside a path is part of the step (the step's words are single-quoted)."""
    return re.findall(r"`(git (?:--literal-pathspecs )?-C .*?)` (?:discards|deletes)", message)


def unrefused(call, *args):
    """`call(*args)`, where a refusal is a failure of the test, not an error (the mutation harness counts only a
    failure as red)."""
    try:
        return call(*args)
    except (FlowError, WorkspaceError) as exc:
        raise AssertionError(f"refused: {exc}") from None


def porcelain(path: Path) -> str:
    return git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")


def free_id_in(message: str) -> str:
    found = re.search(r"`upe start ([a-z0-9-]+) --brief", message)
    assert found, message
    return found.group(1)


# A spent id -------------------------------------------------------------------------------------------------------


class SpentIdTests(FlowCase):
    def finished(self, flow, task_id="dash"):
        flow.start(task_id, "Add a dash.", CLAIM)
        flow.finish(task_id)

    def refusal(self, flow, task_id="dash"):
        with self.assertRaises(FlowError) as ctx:
            flow.start(task_id, "Add a dash.", CLAIM)
        return str(ctx.exception)

    def started(self, flow, task_id):
        """The printed step, run: `upe start <id> --brief ... --claim <the claim finish freed>`."""
        task = flow.start(task_id, "Add a dash.", CLAIM)
        self.assertEqual(task.state, "working")
        self.assertTrue(flow.workspaces.workspace(task_id).path.is_dir())
        self.assertNotEqual(git(self.repo, "branch", "--list", f"upe/{task_id}").strip(), "")

    def test_start_on_a_finished_id_names_a_free_id_and_its_step_starts(self):
        # Before, the refusal named no id ("pick a new task id").
        flow = self.flow()
        self.finished(flow)
        said = self.refusal(flow)
        self.assertIn("Task dash is spent", said)
        self.assertIn("`upe start dash-2 --brief", said)
        self.assertIn("It is done: for new work, pick a new task id", said)  # the old sentence stays
        self.started(flow, free_id_in(said))

    def test_a_spent_id_with_a_number_counts_on_from_its_base(self):
        flow = self.flow()
        self.finished(flow)
        self.finished(flow, "dash-2")
        said = self.refusal(flow, "dash-2")
        self.assertEqual(free_id_in(said), "dash-3")
        self.started(flow, "dash-3")

    def taken_then(self, take):
        """`take(flow)` takes dash-2 one way; the printed id is then dash-3, and it starts."""
        flow = self.flow()
        self.finished(flow)
        take(flow)
        said = self.refusal(flow)
        self.assertEqual(free_id_in(said), "dash-3")
        self.started(flow, "dash-3")

    def test_a_record_takes_an_id(self):
        def landed_record(flow):
            record = json.loads((flow.state_dir / "tasks" / "dash.json").read_text(encoding="utf-8"))
            (flow.state_dir / "tasks" / "dash-2.json").write_text(
                json.dumps({**record, "task_id": "dash-2", "state": "landed"}), encoding="utf-8")
        self.taken_then(landed_record)

    def test_a_record_that_does_not_read_takes_an_id(self):
        self.taken_then(lambda flow: (flow.state_dir / "tasks" / "dash-2.json").write_text("{", encoding="utf-8"))

    def test_a_branch_takes_an_id(self):
        self.taken_then(lambda flow: git(self.repo, "branch", "upe/dash-2", "main"))

    def test_a_claim_takes_an_id(self):
        self.taken_then(lambda flow: flow.registry.claim("dash-2", ["Assets/Other/"], locked=True))

    def test_a_slot_owner_takes_an_id(self):
        self.taken_then(lambda flow: flow.workspaces.acquire("dash-2", None, "main"))

    def test_a_48_character_id_gives_a_free_id_that_is_a_task_id(self):
        flow = self.flow()
        for long in ("d" * 48, "a" * 45 + "-bb"):
            with self.subTest(long=long):
                self.finished(flow, long)
                free = free_id_in(self.refusal(flow, long))
                self.assertRegex(free, TASK_ID)
                self.assertLessEqual(len(free), 48)
                self.started(flow, free)
                flow.finish(free)

    def test_claims_that_do_not_read_name_no_id(self):
        flow = self.flow()
        self.finished(flow)
        (flow.state_dir / "claims.json").write_text("{", encoding="utf-8")
        said = self.refusal(flow)
        self.assertIn("under an id no task uses", said)
        self.assertNotIn("upe start dash-", said)

    def test_the_printed_id_is_a_hint_another_start_may_take_first(self):
        flow = self.flow()
        self.finished(flow)
        printed = free_id_in(self.refusal(flow))
        other = self.flow()  # a second agent's flow, on the same state
        other.start(printed, "Another task.", ["Assets/Other/"])
        with self.assertRaises(FlowError) as ctx:
            flow.start(printed, "Add a dash.", CLAIM)
        self.assertIn("already exists (working)", str(ctx.exception))  # its own refusal; nothing of `other` changed
        self.assertEqual(other.task(printed).brief, "Another task.")
        self.assertEqual(free_id_in(self.refusal(flow)), "dash-3")

    def test_an_id_in_use_or_unreadable_is_not_called_spent(self):
        # A spent id (a record that reads `finished`) is told apart from one whose record does not read.
        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        path = flow.state_dir / "tasks" / "dash.json"
        for state in ("working", "landed", "refused"):
            with self.subTest(state=state):
                path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "state": state}),
                                encoding="utf-8")
                self.assertNotIn("spent", self.refusal(flow))
        path.write_text("{not json", encoding="utf-8")
        said = self.refusal(flow)
        self.assertIn("its record does not read", said)
        self.assertNotIn("spent", said)


# Where the tests live -------------------------------------------------------------------------------------------


class TestFoldersTests(FlowCase):
    def test_upe_toml_names_the_test_folders_and_start_gives_them(self):
        flow = self.flow()
        self.config.test_folders = ("Assets/Other/Tests/",)
        flow.start("dash", "Add a dash.", CLAIM)
        answer = flow.tests_answer(flow.workspaces.workspace("dash").path)
        self.assertEqual(answer, {"folders": ["Assets/Other/Tests/"], "from": "upe.toml"})

    def test_with_none_named_start_prints_a_command_that_lists_the_test_folders(self):
        # The printed step, run as printed on real git in the slot.
        asset(self.repo, "Assets/Tests/EditMode/FooTests.cs", "[Test] public void Foo() {}\n")
        asset(self.repo, "Assets/Game/test/BarTest.cs", "[Test] public void Bar() {}\n")  # another case of the name
        asset(self.repo, "Assets/Scripts/Bar.cs", "class Test {}\n")  # the word, in no test folder
        commit(self.repo, "tests")
        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        answer = flow.tests_answer(flow.workspaces.workspace("dash").path)
        self.assertEqual((answer["folders"], answer["from"]), ([], "none"))
        done = run_printed(answer["find"])
        self.assertEqual(done.returncode, 0, done.stderr)
        listed = done.stdout.splitlines()
        self.assertIn("Assets/Tests/EditMode.meta", listed)
        self.assertIn("Assets/Game/test/BarTest.cs", listed)
        self.assertFalse([p for p in listed if p.startswith("Assets/Scripts/")], listed)

    def test_a_test_folder_outside_the_project_is_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upe.toml"
            for value, ok in (('["Assets/Tests/"]', True), ('["../x"]', False), ('["C:/x"]', False), ('[""]', False),
                              ('"Assets/Tests/"', False), ('["/Assets"]', False)):
                with self.subTest(value=value):
                    path.write_text(f'repo = "game"\nroot = "work"\ngolden = "golden"\ntest_folders = {value}\n')
                    if ok:
                        self.assertEqual(Config.load(path).test_folders, ("Assets/Tests/",))
                    else:
                        with self.assertRaises(FlowError):
                            Config.load(path)


# A slot's uncommitted changes -----------------------------------------------------------------------------------


class DirtCase(FlowCase):
    def setUp(self):
        super().setUp()
        write(self.repo, EDITOR_FILE, '{"a": 1}\n')
        write(self.repo, "ProjectSettings/TagManager.asset", "tags: []\n")
        write(self.repo, ".gitignore", "Library/\nTemp/\nLogs/\n*.tmp\n")
        asset(self.repo, "Assets/Features/Dash/Old.cs", "class Old {}\n")
        asset(self.repo, "Assets/Other/Keep.cs", "class Keep {}\n")
        commit(self.repo, "the target's files")

    def dash(self, flow, claim=CLAIM):
        flow.start("dash", "Add a dash.", claim)
        return self.work(flow, "dash", "Assets/Features/Dash/Dash.cs", "class Dash {}\n")

    @staticmethod
    def change(wt, rel, kind):
        """One uncommitted change of `kind` at `rel` in the slot `wt`."""
        if kind in ("changed, not staged", "changed, staged"):
            write(wt, rel, "changed\n")
            if kind.endswith(", staged"):
                git(wt, "add", "--", rel)
        elif kind in ("deleted, not staged", "deleted, staged"):
            (wt / rel).unlink()
            if kind.endswith(", staged"):
                git(wt, "rm", "-q", "--cached", "--", rel)
        elif kind == "untracked":
            write(wt, rel, "new\n")
        elif kind == "a new file, staged":
            write(wt, rel, "new\n")
            git(wt, "add", "--", rel)
        else:
            raise AssertionError(kind)

    def entries(self, flow):
        return list(flow.task("dash").history)


TRACKED_KINDS = ("changed, not staged", "changed, staged", "deleted, not staged", "deleted, staged")
NEW_KINDS = ("untracked", "a new file, staged")


class LandDirtTests(DirtCase):
    # Inside the claim: refused, every path named, nothing written (a refusal test: red with the split removed, that
    # is, with every path read as outside).

    def refused_naming(self, flow, rel):
        before = self.entries(flow)
        with self.assertRaises(FlowError) as ctx:
            Flow.land(flow, "dash")
        said = str(ctx.exception)
        self.assertIn(f'"{rel}"', said)
        self.assertIn("inside the claim", said)
        self.assertEqual(self.entries(flow), before, "a refusal writes no entry")
        return said

    def test_each_kind_of_change_inside_the_claim_stops_the_land_and_is_named(self):
        cases = [(kind, "Assets/Features/Dash/Old.cs") for kind in TRACKED_KINDS] + \
                [(kind, "Assets/Features/Dash/New.cs") for kind in NEW_KINDS]
        for i, (kind, rel) in enumerate(cases):
            with self.subTest(kind=kind):
                if i:
                    self.tearDown()
                    self.setUp()
                flow = self.flow()
                wt = self.dash(flow)
                self.change(wt, rel, kind)
                self.refused_naming(flow, rel)

    def test_a_meta_a_case_a_glob_and_a_shared_file_are_inside(self):
        cases = [
            ("a .meta beside a claimed file", ["Assets/Features/Dash/Dash.cs"], "Assets/Features/Dash/Dash.cs.meta"),
            ("the claim in another case", ["assets/features/dash/"], "Assets/Features/Dash/Old.cs"),
            ("a glob claim", ["Assets/Features/Dash/*.cs"], "Assets/Features/Dash/Old.cs"),
            ("a folder .meta on the way to the claim", CLAIM, "Assets/Features.meta"),
            ("a shared file", CLAIM, "ProjectSettings/TagManager.asset"),
        ]
        for i, (name, claim, rel) in enumerate(cases):
            with self.subTest(name):
                if i:
                    self.tearDown()
                    self.setUp()
                flow = self.flow()
                wt = self.dash(flow, claim)
                self.change(wt, rel, "changed, not staged")
                self.refused_naming(flow, rel)

    def test_a_path_in_another_case_on_disk_is_inside(self):
        # Windows reads `assets/features/dash/new.cs` as the claimed folder's file; so does the claim (no case).
        flow = self.flow()
        wt = self.dash(flow)
        write(wt, "assets/features/dash/new2.cs", "new\n")
        listed = git(wt, "ls-files", "--others", "--exclude-standard").split()
        self.assertTrue(listed)
        self.refused_naming(flow, listed[0])

    def test_inside_and_outside_together_are_refused_with_both_named(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, "Assets/Features/Dash/Old.cs", "changed, not staged")
        self.change(wt, EDITOR_FILE, "changed, not staged")
        said = self.refused_naming(flow, "Assets/Features/Dash/Old.cs")
        self.assertIn(f'Outside the claim, 1 more is uncommitted ("{EDITOR_FILE}")', said)
        self.assertIn("no land has them", said)

    def test_attributes_config_and_environment_do_not_hide_a_change_inside_the_claim(self):
        # The inputs that can change the read: git's attributes (an untracked `.gitattributes`
        # in the slot, `info/attributes`), the user's git config (GIT_CONFIG_GLOBAL), and git's pathspec variables.
        import tempfile

        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, "Assets/Features/Dash/Old.cs", "changed, not staged")
        write(wt, ".gitattributes", "* -text -diff binary\n")
        info = Path(git(wt, "rev-parse", "--git-common-dir").strip())
        info = info if info.is_absolute() else wt / info
        write(info, "info/attributes", "*.cs -diff binary\n")
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "gitconfig"
            ignore = Path(tmp) / "ignore"
            ignore.write_text("*.wip\n", encoding="utf-8")
            config.write_text("[diff]\n\trelative = true\n\tnoprefix = true\n[core]\n\tquotepath = true\n"
                              f"\texcludesFile = {ignore.as_posix()}\n[status]\n\tshowUntrackedFiles = no\n",
                              encoding="utf-8")
            env = {"GIT_CONFIG_GLOBAL": str(config), "GIT_LITERAL_PATHSPECS": "1", "GIT_ICASE_PATHSPECS": "1"}
            with mock.patch.dict(os.environ, env):
                said = self.refused_naming(flow, "Assets/Features/Dash/Old.cs")
        self.assertIn('".gitattributes"', said)  # the untracked file is named too, outside the claim

    def test_a_slot_git_cannot_read_stops_the_land_before_it_starts(self):
        # Real git failing: the slot's `.git` file names no repository. Raised before "land started".
        flow = self.flow()
        wt = self.dash(flow)
        before = self.entries(flow)
        (wt / ".git").unlink()  # Windows refuses to open the hidden file for writing
        (wt / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
        with self.assertRaises(FlowError):
            Flow.land(flow, "dash")
        self.assertEqual(self.entries(flow), before)

    # Outside the claim: named, and the land goes on (before, every uncommitted path was refused).

    def snapshot(self, wt, rels):
        return porcelain(wt), {rel: (wt / rel).read_bytes() if (wt / rel).exists() else None for rel in rels}

    def lands_naming(self, flow, wt, rel, said):
        before = self.snapshot(wt, [rel])
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "awaiting-council", outcome.message)
        self.assertIn(f'"{rel}"', outcome.message)
        self.assertIn("outside the claim stays in the slot", outcome.message)
        checks = (flow.run_dir("dash") / "checks.md").read_text(encoding="utf-8")
        self.assertIn(f'"{rel}"', checks)
        self.assertTrue(any(f'"{rel}"' in line for line in said), said)  # the job's log
        self.assertEqual(self.snapshot(wt, [rel]), before, "the land changed nothing in the slot")
        merge = flow.committed("dash")
        shown = subprocess.run(["git", "-C", str(self.repo), "show", f"{merge}:{rel}"], capture_output=True)
        committed = subprocess.run(["git", "-C", str(self.repo), "show", f"main:{rel}"], capture_output=True)
        self.assertEqual((shown.returncode, shown.stdout), (committed.returncode, committed.stdout),
                         "the merge holds the committed content, not the slot's")
        return outcome

    def test_each_kind_of_change_outside_the_claim_is_named_and_the_land_goes_on(self):
        cases = [(kind, EDITOR_FILE) for kind in TRACKED_KINDS] + [(kind, "Assets/Other/New.cs") for kind in NEW_KINDS]
        for i, (kind, rel) in enumerate(cases):
            with self.subTest(kind=kind):
                if i:
                    self.tearDown()
                    self.setUp()
                flow = self.flow()
                said = []
                flow._say = said.append
                wt = self.dash(flow)
                self.change(wt, rel, kind)
                self.lands_naming(flow, wt, rel, said)

    def test_what_would_land_unchecked_work_does_not(self):
        # The claim "a land merges only commits, so nothing uncommitted lands", and the cases tried to break it.
        flow = self.flow()
        said = []
        flow._say = said.append
        wt = self.dash(flow)
        write(wt, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Rewritten\n")  # a scene, outside
        write(wt, "Packages/manifest.json", '{"dependencies": {"com.evil": "1.0.0"}}\n')  # skip-worktree: not listed
        write(wt, ".gitattributes", "*.cs merge=ours\n")  # untracked attributes in the slot
        write(wt, "Assets/Features/Jump/Jump.cs", "class FromTheSlot {}\n")  # untracked, at a path the target adds
        asset(self.repo, "Assets/Features/Jump/Jump.cs", "class Jump {}\n")
        commit(self.repo, "jump landed meanwhile")
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "awaiting-council", outcome.message)
        merge = flow.committed("dash")
        files = git(self.repo, "ls-tree", "-r", "--name-only", merge).split("\n")
        self.assertNotIn(".gitattributes", files)
        self.assertEqual(git(self.repo, "show", f"{merge}:Assets/Features/Jump/Jump.cs"), "class Jump {}\n")
        self.assertNotIn("Rewritten", git(self.repo, "show", f"{merge}:Assets/Scenes/Arena.unity"))
        self.assertNotIn("com.evil", git(self.repo, "show", f"{merge}:Packages/manifest.json"))
        candidate = flow.candidate_path("dash")
        self.assertNotIn("Rewritten", (candidate / "Assets/Scenes/Arena.unity").read_text(encoding="utf-8"),
                         "the checks ran on the merge, not on the slot's disk")

    def test_an_outside_change_the_branch_also_commits_is_stopped_by_the_claim_check(self):
        flow = self.flow()
        wt = self.dash(flow)
        write(wt, "Assets/Other/Keep.cs", "class Keep { int committed; }\n")
        commit(wt, "outside the claim, committed")
        write(wt, "Assets/Other/Keep.cs", "class Keep { int dirty; }\n")
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "checks-failed")
        checks = (flow.run_dir("dash") / "checks.md").read_text(encoding="utf-8")
        self.assertIn("- Assets/Other/Keep.cs [claim]", checks)
        self.assertIn('"Assets/Other/Keep.cs"', checks)  # and named as uncommitted

    def test_a_file_git_ignores_in_the_claim_is_named_and_not_merged(self):
        # Ignored by the project (`.gitignore`) and by the user's own config (`core.excludesFile`): no commit holds
        # either, so neither lands, and the land names both. Before, it named neither.
        import tempfile

        flow = self.flow()
        wt = self.dash(flow)
        write(wt, "Assets/Features/Dash/scratch.tmp", "work?\n")
        write(wt, "Assets/Features/Dash/draft.wip", "work?\n")
        write(wt, "Assets/Other/elsewhere.tmp", "not the claim's\n")
        with tempfile.TemporaryDirectory() as tmp:
            ignore, config = Path(tmp) / "ignore", Path(tmp) / "gitconfig"
            ignore.write_text("*.wip\n", encoding="utf-8")
            config.write_text(f"[core]\n\texcludesFile = {ignore.as_posix()}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
                outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "awaiting-council", outcome.message)
        self.assertIn('"Assets/Features/Dash/scratch.tmp"', outcome.message)
        self.assertIn('"Assets/Features/Dash/draft.wip"', outcome.message)
        self.assertIn("ignored by git", outcome.message)
        self.assertNotIn("elsewhere.tmp", outcome.message)
        self.assertNotIn("Library", outcome.message)
        merge = flow.committed("dash")
        self.assertNotIn("scratch.tmp", git(self.repo, "ls-tree", "-r", "--name-only", merge))

    def test_a_clean_slot_says_nothing_of_it(self):
        flow = self.flow()
        self.dash(flow)
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "awaiting-council")
        self.assertNotIn("Also:", outcome.message)


class FinishDirtTests(DirtCase):
    def finish_refused(self, flow, state="working"):
        with self.assertRaises((FlowError, WorkspaceError)) as ctx:
            flow.finish("dash")
        self.assertEqual(flow.task("dash").state, state)
        self.assertIn("dash", flow.registry.all())
        return str(ctx.exception)

    def run_steps(self, said):
        steps = printed_steps(said)
        self.assertTrue(steps, said)
        for step in steps:
            done = run_printed(step)
            self.assertEqual(done.returncode, 0, f"{step}\n{done.stderr}")
        return steps

    def parks_after_the_steps(self, flow, said):
        self.run_steps(said)
        outcome = unrefused(flow.finish, "dash")
        self.assertEqual(outcome.state, "finished", outcome.message)
        self.assertNotIn("dash", flow.workspaces.slots().values())

    def test_after_a_land_finish_names_the_editors_file_and_its_printed_step_clears_it(self):
        # Before, the land refused, and `finish` said "uncommitted work" with no path and no step.
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, EDITOR_FILE, "changed, not staged")
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "awaiting-council", outcome.message)
        self.assertEqual(printed_steps(outcome.message), printed_steps(self.finish_refused(flow, "awaiting-council")))
        said = self.finish_refused(flow, "awaiting-council")
        self.assertIn(f'"{EDITOR_FILE}"', said)
        self.parks_after_the_steps(flow, said)

    def test_each_kind_outside_the_claim_is_cleared_by_the_printed_step(self):
        names = ["Assets/Other/a $b `c` d.txt", "Assets/Other/çé ü.txt", "Assets/Other/it's.txt", "Assets/Other/x[1].txt"]
        cases = [(kind, EDITOR_FILE) for kind in TRACKED_KINDS] + [(kind, "Assets/Other/New.cs") for kind in NEW_KINDS] \
            + [("untracked", name) for name in names] + [("a new file, staged", names[0])]
        for i, (kind, rel) in enumerate(cases):
            with self.subTest(kind=kind, rel=rel):
                if i:
                    self.tearDown()
                    self.setUp()
                flow = self.flow()
                wt = self.dash(flow)
                self.change(wt, rel, kind)
                said = self.finish_refused(flow)
                self.assertIn(rel, said)
                self.parks_after_the_steps(flow, said)
                self.assertEqual(porcelain(wt), "")

    def test_a_tracked_name_with_dollar_backtick_and_space_is_put_back_by_the_printed_step(self):
        rel = "Assets/Other/a $b `c` d.txt"
        write(self.repo, rel, "committed\n")
        commit(self.repo, "a tracked file with an odd name")
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, rel, "changed, staged")
        self.parks_after_the_steps(flow, self.finish_refused(flow))
        self.assertEqual((wt / rel).read_text(encoding="utf-8"), "committed\n")

    def test_the_same_step_runs_as_printed_in_powershell(self):
        import shutil

        if not shutil.which("powershell"):
            self.skipTest("no PowerShell on this machine")
        rel = "Assets/Other/a $b `c` d.txt"
        write(self.repo, rel, "committed\n")
        commit(self.repo, "a tracked file with an odd name")
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, rel, "changed, not staged")
        self.change(wt, "Assets/Other/$new `x`.txt", "untracked")
        for step in printed_steps(self.finish_refused(flow)):
            done = run_printed(step, "powershell")
            self.assertEqual(done.returncode, 0, f"{step}\n{done.stderr}")
        self.assertEqual(unrefused(flow.finish, "dash").state, "finished")
        self.assertEqual((wt / rel).read_text(encoding="utf-8"), "committed\n")
        self.assertFalse((wt / "Assets/Other/$new `x`.txt").exists())

    def test_a_record_claim_that_does_not_read_counts_every_change_as_inside(self):
        # Doubt is refusal: with no claim to judge by, no change is called the Editor's.
        flow = self.flow()
        wt = self.dash(flow)
        path = flow.state_dir / "tasks" / "dash.json"
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "claim": ["../outside"]}),
                        encoding="utf-8")
        self.change(wt, EDITOR_FILE, "changed, not staged")
        said = self.finish_refused(flow)
        self.assertIn("Inside the claim", said)
        self.assertEqual(printed_steps(said), [])

    def test_more_than_the_cap_takes_two_rounds_of_finish(self):
        flow = self.flow()
        wt = self.dash(flow)
        for n in range(flowmod.STEP_PATHS + 5):
            write(wt, f"Assets/Other/Gen{n:02}.txt", "new\n")
        said = self.finish_refused(flow)
        self.assertIn("again for the other 5", said)
        self.run_steps(said)
        said = self.finish_refused(flow)
        self.assertNotIn("again for the other", said)
        self.parks_after_the_steps(flow, said)

    def test_the_printed_step_matches_each_name_literally(self):
        # `[1]` is a glob character class in a pathspec: the step names `x[1].txt` only, and `x1.txt` stays.
        flow = self.flow()
        wt = self.dash(flow)
        write(wt, "Assets/Other/x[1].txt", "new\n")
        write(wt, "Assets/Other/x1.txt", "another\n")
        task = flow.task("dash")
        dirt = flow._slot_dirt(task, wt)
        dirt.outside = ["Assets/Other/x[1].txt"]  # the step for that one name alone
        for step in printed_steps(flow._restore_steps("dash", wt, dirt, dirt.outside) + " "):
            self.assertEqual(run_printed(step).returncode, 0)
        self.assertFalse((wt / "Assets/Other/x[1].txt").exists())
        self.assertTrue((wt / "Assets/Other/x1.txt").exists())

    def test_inside_the_claim_finish_prints_no_step_and_keeps_the_work(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, "Assets/Features/Dash/Old.cs", "changed, not staged")
        self.change(wt, "Assets/Features/Dash/New.cs", "untracked")
        said = self.finish_refused(flow)
        self.assertIn('"Assets/Features/Dash/Old.cs"', said)
        self.assertIn('"Assets/Features/Dash/New.cs"', said)
        self.assertIn("The tool gives no step that discards it", said)
        self.assertEqual(printed_steps(said), [])
        self.assertEqual((wt / "Assets/Features/Dash/Old.cs").read_text(encoding="utf-8"), "changed\n")
        self.assertTrue((wt / "Assets/Features/Dash/New.cs").exists())

    def test_a_manifest_change_inside_the_claim_is_refused_before_finish_puts_the_manifest_back(self):
        # `finish` once ran `agent_package.uninstall` (which checks the manifest out at HEAD) before the
        # dirty check: an uncommitted manifest change of a task that claimed it was lost. The read now comes first.
        flow = self.flow()
        flow.start("dash", "Add a package.", ["Packages/manifest.json"])
        wt = flow.workspaces.workspace("dash").path
        text = '{"dependencies": {"com.a": "1.0.0", "com.b": "2.0.0"}}\n'
        write(wt, "Packages/manifest.json", text)
        said = self.finish_refused(flow)
        self.assertIn('"Packages/manifest.json"', said)
        self.assertEqual((wt / "Packages/manifest.json").read_text(encoding="utf-8"), text)

    def test_the_tools_own_manifest_entry_is_not_the_tasks_change(self):
        # A task that claims the manifest has the tool's package in it from `start` on (`allow_manifest_edits` lifts
        # the mark): that is not the task's work, so `finish` parks the slot. Red with
        # `_only_the_tools_manifest` removed.
        flow = self.flow()
        flow.start("dash", "Add a package.", ["Packages/manifest.json"])
        wt = flow.workspaces.workspace("dash").path
        self.assertIn("Packages/manifest.json", git(wt, "diff", "--name-only", "HEAD"))
        self.assertEqual(unrefused(flow.finish, "dash").state, "finished")


# `next` after a false stop ---------------------------------------------------------------------------------------


class FalseStopTests(FlowCase):
    """A land whose findings are all static and none is in a path the branch changed says "tell the user, do not fix".

    With a stand-in: git cannot be made to give a `.meta` read with findings at paths the branch did not touch (its
    known cause is closed), so `_meta_tree` of the merge's commit is wrapped to add files with no `.meta`. The other
    tests here (the branch's own findings) run on real git, with no stand-in."""

    def ghosts(self, flow, paths):
        real = flow._meta_tree
        target = git(self.repo, "rev-parse", "main").strip()

        def with_ghosts(cwd, commit):
            tree = real(cwd, commit)
            if commit == target:
                return tree
            return MetaTree(tree.files | frozenset(paths), tree.folder_metas, tree.guids, tree.dirs)

        return mock.patch.object(flow, "_meta_tree", with_ghosts)

    def landed(self, flow, extra=()):
        flow.start("dash", "Add a dash.", CLAIM)
        self.work(flow, "dash", "Assets/Features/Dash/Dash.cs", "class Dash {}\n")
        ghosts = [f"Assets/Ghost/G{i:02}.cs" for i in range(24)] + list(extra)
        with self.ghosts(flow, ghosts):
            return Flow.land(flow, "dash")

    def test_findings_none_of_which_is_the_branchs_say_tell_the_user(self):
        # Before, the entry had no mark, and `next` said "fix in its slot".
        flow = self.flow()
        outcome = self.landed(flow)
        self.assertEqual(outcome.state, "checks-failed")
        task = flow.task("dash")
        self.assertTrue(task.history[-1].endswith(flowmod.NOT_IN_BRANCH), task.history[-1])
        step = flow.situation_of(task).step
        self.assertIn("none of the findings is in a path this branch changed", step)
        self.assertIn("do not fix them", step)
        self.assertIn("do not fix them", outcome.message)

    def test_one_finding_in_the_branchs_path_says_fix(self):
        flow = self.flow()
        self.landed(flow, ["Assets/Features/Dash/Ghost.cs"])
        task = flow.task("dash")
        self.assertFalse(task.history[-1].endswith(flowmod.NOT_IN_BRANCH))
        self.assertIn("fix in its slot", flow.situation_of(task).step)

    def test_a_failed_test_is_never_called_not_the_branchs(self):
        # A test named by its class, at no path: only the static checks can be judged by path.
        self.test_reports[:] = [[], ["Game.Tests.MoveTests.Breaks"], ["Game.Tests.MoveTests.Breaks"]]
        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        self.work(flow, "dash", "Assets/Features/Dash/Dash.cs", "class Dash {}\n")
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "checks-failed", outcome.message)
        self.assertIn("unity-tests", (flow.run_dir("dash") / "checks.md").read_text(encoding="utf-8"))
        task = flow.task("dash")
        self.assertFalse(task.history[-1].endswith(flowmod.NOT_IN_BRANCH))
        self.assertNotIn("do not fix", flow.situation_of(task).step)

    def own_findings(self, change):
        """A branch whose own change leaves a `.meta` finding, on real git: `next` says fix."""
        asset(self.repo, "Assets/Features/Dash/Old.cs", "class Old {}\n")
        commit(self.repo, "old")
        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        wt = flow.workspaces.workspace("dash").path
        change(wt)
        commit(wt, "the branch's change")
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "checks-failed", outcome.message)
        task = flow.task("dash")
        self.assertFalse(task.history[-1].endswith(flowmod.NOT_IN_BRANCH), task.history[-1])
        self.assertIn("fix in its slot", flow.situation_of(task).step)
        return (flow.run_dir("dash") / "checks.md").read_text(encoding="utf-8")

    def test_a_deleted_file_whose_meta_stays_is_the_branchs(self):
        checks = self.own_findings(lambda wt: (wt / "Assets/Features/Dash/Old.cs").unlink())
        self.assertIn("Assets/Features/Dash/Old.cs.meta [meta]", checks)

    def test_a_folder_the_branch_adds_without_its_meta_is_the_branchs(self):
        def add(wt):
            write(wt, "Assets/Features/Dash/New/A.cs", "class A {}\n")
            write(wt, "Assets/Features/Dash/New/A.cs.meta", "fileFormatVersion: 2\nguid: " + "a" * 32 + "\n")
        checks = self.own_findings(add)
        self.assertIn("Assets/Features/Dash/New [meta]", checks)

    def test_a_rename_that_leaves_the_old_meta_is_the_branchs(self):
        def rename(wt):
            git(wt, "mv", "Assets/Features/Dash/Old.cs", "Assets/Features/Dash/Renamed.cs")
        checks = self.own_findings(rename)
        self.assertIn("[meta]", checks)


# Edge cases of a slot's changes, a land and a finish -----------------------------------------------------------


class DirtEdgeTests(DirtCase):

    def refused(self, call, *args):
        with self.assertRaises((FlowError, WorkspaceError)) as ctx:
            call(*args)
        return str(ctx.exception)

    # A change outside the claim may be the task's own; no step discards it without saying so in its own line.

    def test_a_change_outside_the_claim_is_not_called_unitys_and_each_step_says_what_it_discards(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, EDITOR_FILE, "changed, not staged")
        self.change(wt, "Assets/Other/MyTests.cs", "untracked")  # the agent's own test, outside its claim
        outcome = unrefused(Flow.land, flow, "dash")
        said = self.refused(flow.finish, "dash")
        for text in (outcome.message, said):
            self.assertNotIn("Editor's", text)
            self.assertIn("If one is the task's work" if text is outcome.message else "if one is the task's work", text)
        steps = re.findall(r"`(git .*?)` ([^`]*?)(?: and |, and then|\. |$)", said)
        self.assertEqual(len(steps), 2, said)
        for step, what in steps:
            self.assertRegex(what, r"^(discards the uncommitted change|deletes the untracked file)", step)
            self.assertIn("lost if the task's work", what)

    # Rules no other test reaches.

    def test_a_finding_at_a_path_only_the_merge_changed_is_the_branchs(self):
        # A builder's replay adds `Built.asset` with no `.meta`: in the merge's paths, not the branch's.
        flow = self.flow(builders={"Assets/Scenes/Arena.unity": "Dash.Builder.Build"})
        flow.start("dash", "Add a dash.", ["Assets/Scenes/Arena.unity"])
        self.work(flow, "dash", "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Dash\n")
        write(self.repo, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Jump\n")
        commit(self.repo, "the target's scene")
        self.unity.builder_edit = ("Assets/Scenes/Built.asset", "a: 1\n")
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "checks-failed", outcome.message)
        self.assertIn("Assets/Scenes/Built.asset [meta]", (flow.run_dir("dash") / "checks.md").read_text(encoding="utf-8"))
        self.assertFalse(flow.task("dash").history[-1].endswith(flowmod.NOT_IN_BRANCH))

    def test_a_start_still_beginning_takes_an_id(self):
        from datetime import datetime, timezone

        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        flow.finish("dash")
        jobs = flow.state_dir / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        name = f"start-dash-2-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        (jobs / f"{name}.json").write_text(json.dumps({"job": name, "state": "started", "pid": os.getpid(),
                                                       "log": "nope"}), encoding="utf-8")
        self.assertEqual(flow.free_id("dash"), "dash-3")

    def test_a_path_the_claim_cannot_judge_is_inside(self):
        # With a stand-in: git gives no path `normalize` refuses, so `_dirt` is made to give one.
        flow = self.flow()
        self.dash(flow)
        with mock.patch.object(flow, "_dirt", lambda path: ({"Assets/../Odd.cs"}, set())):
            said = self.refused(Flow.land, flow, "dash")
        self.assertIn('"Assets/../Odd.cs"', said)
        self.assertIn("inside the claim", said)

    def test_the_tools_own_files_and_the_local_copies_are_not_named_as_ignored(self):
        flow = self.flow()
        flow.start("dash", "Add a dash.", [*CLAIM, "Packages/com.upe.agent/"])
        self.config.local_copies = ("Assets/Features/Dash/Paid/",)  # as copied in from the project (not done here)
        wt = self.work(flow, "dash", "Assets/Features/Dash/Dash.cs", "class Dash {}\n")
        info = Path(git(wt, "rev-parse", "--git-common-dir").strip())
        info = info if info.is_absolute() else wt / info
        with (info / "info" / "exclude").open("a", encoding="utf-8") as f:
            f.write("/Assets/Features/Dash/Paid/\n")
        write(wt, "Assets/Features/Dash/Paid/Asset.cs", "paid\n")
        write(wt, "Assets/Features/Dash/scratch.tmp", "work?\n")
        self.assertTrue((wt / "Packages" / "com.upe.agent").is_dir())
        self.assertEqual(flow._ignored_in_claim(flow.task("dash"), wt), ["Assets/Features/Dash/scratch.tmp"])

    def test_a_name_with_one_or_two_quotes_gets_the_git_bash_note_and_its_step_runs(self):
        for i, name in enumerate(("Assets/Other/it's.txt", "Assets/Other/it's Bob's.txt")):
            with self.subTest(name=name):
                if i:
                    self.tearDown()
                    self.setUp()
                flow = self.flow()
                wt = self.dash(flow)
                self.change(wt, name, "untracked")
                said = self.refused(flow.finish, "dash")
                self.assertIn("written for Git Bash", said)
                for step in printed_steps(said):
                    self.assertEqual(run_printed(step).returncode, 0)
                self.assertEqual(unrefused(flow.finish, "dash").state, "finished")

    def test_a_failed_listing_of_ignored_files_is_said(self):
        # With a stand-in: git is not made to fail this listing alone on demand.
        flow = self.flow()
        self.dash(flow)
        with mock.patch.object(flow, "_ignored_in_claim", mock.Mock(side_effect=FlowError("git ls-files failed: boom"))):
            outcome = unrefused(Flow.land, flow, "dash")
        self.assertIn("the files git ignores in the claim were not listed (git ls-files failed: boom)", outcome.message)

    def test_finish_says_what_git_ignores_in_the_claim_stays_until_the_next_task(self):
        flow = self.flow()
        wt = self.dash(flow)
        write(wt, "Assets/Features/Dash/scratch.tmp", "work?\n")
        outcome = unrefused(flow.finish, "dash")
        self.assertIn('until the next task takes it, which deletes it: "Assets/Features/Dash/scratch.tmp"', outcome.message)

    # A nested repository or a submodule gets no step (no loop of the same step).

    def test_a_nested_repository_or_a_submodule_gets_no_step_and_says_tell_the_user(self):
        for kind in ("an untracked nested repository", "a staged submodule"):
            with self.subTest(kind):
                self.tearDown()
                self.setUp()
                flow = self.flow()
                wt = self.dash(flow)
                inner = wt / "Assets/Other/Plugin"
                write(inner, "a.txt", "plugin\n")
                git(inner, "init", "-q")
                git(inner, "add", "-A")
                git(inner, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "plugin")
                if kind == "a staged submodule":
                    git(wt, "add", "Assets/Other/Plugin")
                said = self.refused(flow.finish, "dash")
                # One clear text for it alone: what it is, no step, tell the user, what the user decides.
                self.assertIn('"Assets/Other/Plugin', said)
                self.assertIn("is a nested repository or a submodule, which `git clean -f` and `git restore` leave as "
                              "it is, so the tool gives no step for it: tell the user, who decides whether to keep it",
                              said)
                self.assertNotIn("Once you know none is", said)
                self.assertEqual(printed_steps(said), [], said)
                # With a file beside it: the step for the file, then the sentence for the repository, apart.
                write(wt, "Assets/Other/Note.txt", "note\n")
                said = self.refused(flow.finish, "dash")
                self.assertRegex(said, r"Once you know none is, `git .*? clean -f -- 'Assets/Other/Note.txt'` deletes")
                self.assertIn("and then run `upe finish dash` again. \"Assets/Other/Plugin", said)

    # Names as they are, not octal-escaped.

    def test_the_tests_command_prints_a_non_ascii_name_as_it_is(self):
        asset(self.repo, "Assets/Tëst/Tests/Ünï.cs", "[Test] public void U() {}\n")
        commit(self.repo, "a non-ASCII test")
        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        done = run_printed(flow.tests_answer(flow.workspaces.workspace("dash").path)["find"])
        self.assertIn("Assets/Tëst/Tests/Ünï.cs", done.stdout.splitlines(), done.stdout)

    # The index is read too.

    def test_a_staged_change_put_back_on_disk_or_a_staged_new_file_deleted_is_named(self):
        for side, rel in (("inside", "Assets/Features/Dash/Old.cs"), ("outside", "Assets/Other/Keep.cs")):
            for kind in ("MM", "AD"):
                with self.subTest(side=side, kind=kind):
                    self.tearDown()
                    self.setUp()
                    flow = self.flow()
                    wt = self.dash(flow)
                    target = rel if kind == "MM" else rel.replace(".cs", "New.cs")
                    if kind == "MM":
                        before = (wt / target).read_text(encoding="utf-8")
                        write(wt, target, "staged\n")
                        git(wt, "add", "--", target)
                        write(wt, target, before)  # the disk as HEAD; the index holds "staged"
                    else:
                        write(wt, target, "staged new\n")
                        git(wt, "add", "--", target)
                        (wt / target).unlink()
                    said = self.refused(flow.finish, "dash")
                    self.assertIn(f'"{target}"', said)
                    if side == "outside":
                        for step in printed_steps(said):
                            self.assertEqual(run_printed(step).returncode, 0, step)
                        self.assertEqual(unrefused(flow.finish, "dash").state, "finished")

    # A landed task with a change inside the claim gets a step that ends it.

    def test_a_landed_tasks_change_inside_the_claim_gets_a_step_after_the_user_says(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.assertEqual(flow.land("dash").state, "landed")
        self.change(wt, "Assets/Features/Dash/Dash.cs", "changed, not staged")
        said = self.refused(flow.finish, "dash")
        self.assertIn("the task has landed", said)
        # The order and its condition, together.
        self.assertIn("so no land can take it now: tell the user. Once they say it is not wanted, `git ", said)
        steps = printed_steps(said)
        self.assertTrue(steps, said)
        for step in steps:
            self.assertEqual(run_printed(step).returncode, 0, step)
        self.assertEqual(unrefused(flow.finish, "dash").state, "finished")

    # A commit on the slot's detached HEAD is kept before the slot is parked.

    def test_a_commit_on_the_slots_detached_head_is_kept_by_the_printed_step(self):
        flow = self.flow()
        wt = self.dash(flow)
        git(wt, "checkout", "-q", "--detach")
        asset(wt, "Assets/Features/Dash/Detached.cs", "class Detached {}\n")
        commit(wt, "detached work")
        sha = git(wt, "rev-parse", "HEAD").strip()
        said = self.refused(flow.finish, "dash")
        self.assertIn("no branch, tag or remote has", said)
        found = re.search(r"`(git -C .*? branch upe-kept/\S+ [0-9a-f]+)`", said)
        self.assertTrue(found, said)
        self.assertEqual(run_printed(found.group(1)).returncode, 0)
        self.assertEqual(unrefused(flow.finish, "dash").state, "finished")
        self.assertIn(sha, git(self.repo, "rev-list", "--branches").split())

    # The free id keeps the whole id, and a git that fails still says "spent".

    def test_an_id_ending_in_a_number_of_its_own_keeps_it(self):
        flow = self.flow()
        flow.start("fix-issue-42", "Fix it.", CLAIM)
        flow.finish("fix-issue-42")
        with self.assertRaises(FlowError) as ctx:
            flow.start("fix-issue-42", "Fix it.", CLAIM)
        self.assertEqual(free_id_in(str(ctx.exception)), "fix-issue-42-2")

    def test_a_failed_branch_read_still_says_spent(self):
        # With a stand-in: `for-each-ref` is made to fail; git does not fail it on demand.
        flow = self.flow()
        flow.start("dash", "Add a dash.", CLAIM)
        flow.finish("dash")
        real = flow._git

        def failing(cwd, *args, **kw):
            if args[:1] == ("for-each-ref",):
                raise FlowError("git for-each-ref failed: boom")
            return real(cwd, *args, **kw)

        with mock.patch.object(flow, "_git", failing), self.assertRaises(FlowError) as ctx:
            flow.start("dash", "Add a dash.", CLAIM)
        self.assertIn("Task dash is spent", str(ctx.exception))
        self.assertIn("under an id no task uses", str(ctx.exception))

    # After a conflict, the land's printed steps clear what stops `git merge <target>`.

    def test_after_a_conflict_the_lands_steps_clear_what_stops_the_merge(self):
        flow = self.flow()
        flow.start("dash", "Add a dash.", ["Assets/Data/"])
        wt = self.work(flow, "dash", "Assets/Data/Items.asset", "a: 1\n")
        asset(self.repo, "Assets/Data/Items.asset", "a: 2\n")
        write(self.repo, "Assets/Other/Keep.cs", "class Keep { int target; }\n")
        commit(self.repo, "the target moves on both")
        write(wt, "Assets/Other/Keep.cs", "class Keep { int unity; }\n")  # outside the claim, uncommitted
        outcome = unrefused(Flow.land, flow, "dash")
        self.assertEqual(outcome.state, "conflict", outcome.message)
        merge = subprocess.run(["git", "-C", str(wt), "merge", "main"], capture_output=True, text=True)
        self.assertIn("would be overwritten", merge.stderr + merge.stdout)
        for step in printed_steps(outcome.message):
            self.assertEqual(run_printed(step).returncode, 0, step)
        merge = subprocess.run(["git", "-C", str(wt), "merge", "main"], capture_output=True, text=True)
        self.assertNotIn("would be overwritten", merge.stderr + merge.stdout)
        self.assertIn("CONFLICT", merge.stdout)

    # A root holding `$` or a backtick is refused by `setup`, and every printed slot path is in double quotes
    # (`ShellStepTests`).

    # A landed task's own branch is deleted by `finish`, so it does not keep a commit made after the land.

    def test_a_commit_on_a_landed_tasks_branch_after_the_land_is_kept_first(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.assertEqual(flow.land("dash").state, "landed")
        asset(wt, "Assets/Features/Dash/After.cs", "class After {}\n")
        commit(wt, "work after the land, on upe/dash")
        sha = git(wt, "rev-parse", "HEAD").strip()
        said = self.refused(flow.finish, "dash")
        self.assertIn("no branch, tag or remote has", said)
        found = re.search(r"`(git -C .*? branch upe-kept/\S+ [0-9a-f]+)`", said)
        self.assertTrue(found, said)
        self.assertEqual(run_printed(found.group(1)).returncode, 0)
        self.assertEqual(unrefused(flow.finish, "dash").state, "finished")
        self.assertNotIn("upe/dash", git(self.repo, "branch", "--list", "upe/dash"))
        self.assertIn(sha, git(self.repo, "rev-list", "--branches").split())

    # A shared file is named as shared.

    def test_a_shared_file_is_named_as_shared(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, "ProjectSettings/TagManager.asset", "changed, not staged")
        said = self.refused(Flow.land, flow, "dash")
        self.assertIn('Of these, "ProjectSettings/TagManager.asset" is not in the claim but shared', said)


# The printed steps in each shell, and a landed branch the slot's HEAD moved off ------------------------------


def run_in(shell: str, command: str) -> subprocess.CompletedProcess:
    """Run a printed step as printed in `sh` (Git Bash's), `powershell` or `cmd` (cmd.exe; the command line passed as
    it is, `/s /c "<step>"`, so no argument quoting of Python's stands between the step and cmd)."""
    if shell == "cmd":
        return subprocess.run(f'cmd /d /s /c "{command}"', capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL)
    return run_printed(command, shell)


class ShellStepTests(DirtCase):
    CLAIM = CLAIM  # for `DeadStartCase.dead_start`
    def refused(self, call, *args):
        with self.assertRaises((FlowError, WorkspaceError)) as ctx:
            call(*args)
        return str(ctx.exception)

    # A landed task's branch holds a commit after the land, and the slot's HEAD has moved off it.

    def test_a_commit_on_a_landed_tasks_branch_the_head_moved_off_is_kept_first(self):
        # Before, `finish` read only the slot's HEAD, then deleted `upe/dash`, and the commit was lost.
        for i, move in enumerate((["checkout", "-q", "--detach", "HEAD~1"], ["checkout", "-q", "-b", "elsewhere", "HEAD~1"])):
            with self.subTest(move=" ".join(move)):
                if i:
                    self.tearDown()
                    self.setUp()
                flow = self.flow()
                wt = self.dash(flow)
                self.assertEqual(flow.land("dash").state, "landed")
                asset(wt, "Assets/Features/Dash/After.cs", "class After {}\n")
                commit(wt, "work after the land, on upe/dash")
                sha = git(wt, "rev-parse", "HEAD").strip()
                git(wt, *move)
                said = self.refused(flow.finish, "dash")
                self.assertIn("its branch upe/dash has 1 commit no other branch, tag or remote has", said)
                found = re.search(r"`(git -C \".*?\" branch upe-kept/dash-[0-9a-f]{8} [0-9a-f]{40})`", said)
                self.assertTrue(found, said)
                self.assertIn(sha, found.group(1))
                self.assertEqual(run_printed(found.group(1)).returncode, 0)
                self.assertEqual(unrefused(flow.finish, "dash").state, "finished")
                self.assertEqual(git(self.repo, "branch", "--list", "upe/dash").strip(), "")
                self.assertIn(sha, git(self.repo, "rev-list", "--branches").split())

    # Every printed slot path is in double quotes, and runs as printed in sh, PowerShell and cmd.

    def test_the_keep_step_runs_as_printed_in_sh_powershell_and_cmd(self):
        import shutil

        shells = ["sh", "cmd"] + (["powershell"] if shutil.which("powershell") else [])
        for i, shell in enumerate(shells):
            with self.subTest(shell=shell):
                if i:
                    self.tearDown()
                    self.setUp()
                self.config.root = Path(self._tmp.name) / "work with spaces"
                flow = self.flow()
                wt = self.dash(flow)
                git(wt, "checkout", "-q", "--detach")
                asset(wt, "Assets/Features/Dash/Detached.cs", "class Detached {}\n")
                commit(wt, "detached work")
                sha = git(wt, "rev-parse", "HEAD").strip()
                said = self.refused(flow.finish, "dash")
                found = re.search(r"`(git -C \"[^\"]+\" branch upe-kept/dash-[0-9a-f]{8} [0-9a-f]{40})`", said)
                self.assertTrue(found, said)
                done = run_in(shell, found.group(1))
                self.assertEqual(done.returncode, 0, f"{shell}: {found.group(1)}\n{done.stderr}")
                self.assertEqual(unrefused(flow.finish, "dash").state, "finished")
                self.assertIn(sha, git(self.repo, "rev-list", "--branches").split())

    def test_a_dead_starts_step_runs_as_printed_in_sh_powershell_and_cmd(self):
        import shutil

        from upe import agent_package
        from test_flow import DeadStartCase, aged

        shells = ["sh", "cmd"] + (["powershell"] if shutil.which("powershell") else [])
        for i, shell in enumerate(shells):
            with self.subTest(shell=shell):
                if i:
                    self.tearDown()
                    self.setUp()
                self.config.root = Path(self._tmp.name) / "work with spaces"
                flow = self.flow()
                path = DeadStartCase.dead_start(self, flow)
                agent_package.install(path)
                manifest = json.loads((path / "Packages" / "manifest.json").read_text(encoding="utf-8"))
                manifest["dependencies"][agent_package.PIPELINE] = "1.0.0"
                write(path, "Packages/manifest.json", json.dumps(manifest, indent=2) + "\n")
                agent_package.allow_manifest_edits(path)
                aged(path)
                said = self.refused(flow.finish, "dash")
                found = re.search(r"`(git -C \"[^\"]+\" checkout -- Packages/manifest\.json)`", said)
                self.assertTrue(found, said)
                done = run_in(shell, found.group(1))
                self.assertEqual(done.returncode, 0, f"{shell}: {found.group(1)}\n{done.stderr}")
                aged(path)
                self.assertEqual(unrefused(flow.finish, "dash").state, "finished")

    def test_the_restore_and_tests_steps_put_the_slot_in_double_quotes(self):
        flow = self.flow()
        wt = self.dash(flow)
        self.change(wt, EDITOR_FILE, "changed, not staged")
        said = self.refused(flow.finish, "dash")
        self.assertIn(f'git --literal-pathspecs -C "{wt}" restore', said)
        self.assertIn(f"-- '{EDITOR_FILE}'", said)  # file names keep single quotes (Git Bash and PowerShell)
        self.assertTrue(flow.tests_answer(wt)["find"].startswith(f'git -c core.quotepath=off -C "{wt}" ls-files'))


if __name__ == "__main__":
    unittest.main()
