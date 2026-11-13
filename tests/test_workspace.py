import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from upe.workspace import LOCKFILE, WorkspaceError, Workspaces, clear_stale_lock, lock_state


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@contextmanager
def held_exclusively(path: Path):
    """Open a file with no sharing, the way an Editor holds its lockfile."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x40000000, 0, None, 4, 0x80, None)  # GENERIC_WRITE, no share, OPEN_ALWAYS
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "game"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / ".gitignore").write_text("Library/\nTemp/\n")
        (self.repo / "Assets").mkdir()
        (self.repo / "Assets" / "a.txt").write_text("a")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

        self.golden = self.tmp / "golden"
        (self.golden / "Library" / "Artifacts" / "00").mkdir(parents=True)
        (self.golden / "Library" / "ArtifactDB").write_bytes(b"db")
        (self.golden / "Library" / "Artifacts" / "00" / "x").write_bytes(b"x")
        self.ws = Workspaces(self.repo, self.tmp / "wt", self.golden, robocopy_threads=2)

    def tearDown(self):
        self._tmp.cleanup()


class LockTests(Base):
    def test_no_lock(self):
        self.assertEqual(lock_state(self.repo), "none")

    def test_stale_lock_is_cleared(self):
        (self.repo / "Temp").mkdir()
        (self.repo / LOCKFILE).write_text("")
        self.assertEqual(lock_state(self.repo), "stale")
        self.assertTrue(clear_stale_lock(self.repo))
        self.assertFalse((self.repo / "Temp").exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows share modes")
    def test_held_lock_is_left_alone(self):
        (self.repo / "Temp").mkdir()
        with held_exclusively(self.repo / LOCKFILE):
            self.assertEqual(lock_state(self.repo), "held")
            self.assertFalse(clear_stale_lock(self.repo))
            self.assertTrue((self.repo / LOCKFILE).exists())


class WorktreeTests(Base):
    def test_task_id_is_validated(self):
        for bad in ("", "Has Caps", "../escape", "a" * 49, "-lead"):
            with self.assertRaises(WorkspaceError):
                self.ws.workspace(bad)

    def test_create_gives_a_slot_on_the_task_branch(self):
        w = self.ws.create("feat-1")
        self.assertEqual(w.path, self.tmp / "wt" / "slots" / "s1")
        self.assertEqual(w.branch, "upe/feat-1")
        self.assertTrue((w.path / "Assets" / "a.txt").exists())
        self.assertEqual(git(w.path, "branch", "--show-current").strip(), "upe/feat-1")
        self.assertEqual(self.ws.slots(), {"s1": "feat-1"})
        self.assertEqual(self.ws.workspace("feat-1"), w)

    @unittest.skipUnless(sys.platform == "win32", "robocopy")
    def test_create_copies_ignored_local_files(self):
        (self.repo / ".gitignore").write_text("Library/\nTemp/\nAssets/Vendor/\nAssets/key.txt\n")
        git(self.repo, "commit", "-q", "-am", "ignore vendor")
        (self.repo / "Assets" / "Vendor" / "Core").mkdir(parents=True)
        (self.repo / "Assets" / "Vendor" / "Core" / "v.cs").write_text("v")
        (self.repo / "Assets" / "key.txt").write_text("k")
        ws = Workspaces(self.repo, self.tmp / "wt", self.golden, robocopy_threads=2,
                        local_copies=["Assets/Vendor", "Assets/key.txt"])
        w = ws.create("feat-1")
        self.assertEqual((w.path / "Assets" / "Vendor" / "Core" / "v.cs").read_text(), "v")
        self.assertEqual((w.path / "Assets" / "key.txt").read_text(), "k")
        self.assertFalse(ws.is_dirty(w))

    @unittest.skipUnless(sys.platform == "win32", "robocopy")
    def test_line_ending_rewrite_is_not_dirty(self):
        # Unity rewrites a CRLF checkout with LF: git status calls it modified, the content is the same.
        git(self.repo, "config", "core.autocrlf", "true")
        (self.repo / "Assets" / "q.asset").write_text("a: 1\nb: 2\n", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "settings")
        w = self.ws.create("feat-1")
        self.assertEqual((w.path / "Assets" / "q.asset").read_bytes(), b"a: 1\r\nb: 2\r\n")
        (w.path / "Assets" / "q.asset").write_bytes(b"a: 1\nb: 2\n")
        self.assertIn("q.asset", git(w.path, "status", "--porcelain"))
        self.assertFalse(self.ws.is_dirty(w))
        (w.path / "Assets" / "q.asset").write_bytes(b"a: 1\nb: 3\n")
        self.assertTrue(self.ws.is_dirty(w))

    def test_missing_local_copy_refused_and_the_slot_given_back(self):
        ws = Workspaces(self.repo, self.tmp / "wt", self.golden, local_copies=["Assets/Nope"])
        with self.assertRaises(WorkspaceError):
            ws.create("feat-1")
        self.assertEqual(ws.slots(), {})
        self.assertNotIn("upe/feat-1", git(self.repo, "branch"))

    def test_create_twice_refused(self):
        self.ws.create("feat-1")
        with self.assertRaises(WorkspaceError):
            self.ws.create("feat-1")

    def test_an_existing_branch_is_refused_and_kept(self):
        git(self.repo, "branch", "upe/feat-1")
        with self.assertRaises(WorkspaceError):
            self.ws.create("feat-1")
        self.assertIn("upe/feat-1", git(self.repo, "branch"))
        self.assertEqual(self.ws.slots(), {})

    def test_no_worktree_for_an_unknown_task(self):
        with self.assertRaises(WorkspaceError):
            self.ws.workspace("feat-9")
        self.assertIsNone(self.ws.path_of("feat-9"))

    @unittest.skipUnless(sys.platform == "win32", "robocopy")
    def test_create_copies_library(self):
        w = self.ws.create("feat-1")
        self.assertEqual((w.library / "ArtifactDB").read_bytes(), b"db")
        self.assertEqual((w.library / "Artifacts" / "00" / "x").read_bytes(), b"x")
        self.assertFalse(w.library.is_symlink())

    def test_missing_golden_library(self):
        ws = Workspaces(self.repo, self.tmp / "wt", self.tmp / "nowhere")
        with self.assertRaises(WorkspaceError):
            ws.create("feat-1")
        self.assertEqual(ws.slots(), {})

    @unittest.skipUnless(sys.platform == "win32", "Windows share modes")
    def test_golden_open_in_editor_refused(self):
        (self.golden / "Temp").mkdir()
        with held_exclusively(self.golden / LOCKFILE):
            with self.assertRaises(WorkspaceError):
                self.ws.create("feat-1")

    def test_remove_parks_the_slot_with_its_library(self):
        w = self.ws.create("feat-1")
        (w.library / "warm").write_text("imported here")
        head = git(w.path, "rev-parse", "HEAD").strip()
        self.ws.remove("feat-1")
        self.assertTrue((w.library / "warm").exists())
        self.assertEqual(git(w.path, "rev-parse", "--abbrev-ref", "HEAD").strip(), "HEAD")  # detached
        self.assertEqual(git(w.path, "rev-parse", "HEAD").strip(), head)
        self.assertIn("upe/feat-1", git(self.repo, "branch"))  # kept until it lands
        self.assertEqual(self.ws.slots(), {"s1": None})

    def test_a_parked_slot_is_reused_without_copying_the_library(self):
        w1 = self.ws.create("feat-1")
        (w1.library / "warm").write_text("imported here")
        self.ws.remove("feat-1")
        (self.repo / "Assets" / "b.txt").write_text("b")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "more")
        w2 = self.ws.create("feat-2", "HEAD")
        self.assertEqual(w2.path, w1.path)
        self.assertTrue((w2.library / "warm").exists())  # not mirrored over from the golden
        self.assertTrue((w2.path / "Assets" / "b.txt").exists())
        self.assertEqual(git(w2.path, "branch", "--show-current").strip(), "upe/feat-2")
        self.assertEqual(self.ws.slots(), {"s1": "feat-2"})

    def test_reuse_clears_what_the_last_owner_hid(self):
        w1 = self.ws.create("feat-1")
        (w1.path / "Temp").mkdir()
        (w1.path / "Temp" / "Planted.cs").write_text("// runs in the next owner's Editor")  # ignored
        git(w1.path, "update-index", "--skip-worktree", "Assets/a.txt")
        (w1.path / "Assets" / "a.txt").write_text("hidden edit")
        self.ws.remove("feat-1")  # the dirty check cannot see either
        w2 = self.ws.create("feat-2")
        self.assertEqual(w2.path, w1.path)
        self.assertFalse((w2.path / "Temp" / "Planted.cs").exists())
        self.assertEqual((w2.path / "Assets" / "a.txt").read_text(), "a")
        self.assertNotIn("S ", git(w2.path, "ls-files", "-v"))

    def test_reuse_clears_the_flags_of_thousands_of_files(self):
        # The scrub's `update-index` calls in groups capped by length. Real git. Red with one `update-index` of 1,200
        # paths, about 84,000 characters, which Windows refuses (WinError 206).
        names = [f"Assets/Generated/Level Data/Chunks/Region_{i // 100:02}/Chunk_{i:04}_baked.asset" for i in range(1200)]
        for name in names:
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text("baked")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "1,200 chunks")
        w1 = self.ws.create("feat-1")
        for i in range(0, len(names), 300):  # the test's own flags, set in small calls
            git(w1.path, "update-index", "--skip-worktree", *names[i:i + 300][:150])
            git(w1.path, "update-index", "--assume-unchanged", *names[i:i + 300][150:])
        for name in names:
            (w1.path / name).write_text("hidden edit")
        self.ws.remove("feat-1")  # the dirty check sees none of them
        calls, real = [], self.ws._runner

        def seen(argv, **kw):
            if "update-index" in argv:
                calls.append(list(argv))
            return real(argv, **kw)

        self.ws._runner = seen
        try:
            w2 = self.ws.create("feat-2")
        except OSError as exc:  # WinError 206: a failure of the scrub, not an error of the test
            self.fail(f"the scrub raised {exc!r}")
        self.assertEqual(w2.path, w1.path)
        flags = [line for line in git(w2.path, "ls-files", "-v").splitlines() if line[:1] == "S" or line[:1].islower()]
        self.assertEqual(flags, [], "no flag left")
        self.assertEqual(git(w2.path, "status", "--porcelain"), "")
        self.assertEqual((w2.path / names[0]).read_text(), "baked")
        self.assertGreater(len(calls), 2)
        self.assertEqual([c[c.index("update-index") + 1] for c in calls],
                         sorted([c[c.index("update-index") + 1] for c in calls], reverse=True),
                         "every group of --no-skip-worktree before --no-assume-unchanged")
        for argv in calls:
            self.assertLess(len(subprocess.list2cmdline(argv)), 32_767)

    def test_destructive_commands_only_in_slots(self):
        self.ws.create("feat-1")
        self.ws._write_map({"s1": "feat-1", "s2": None})
        with self.assertRaises(WorkspaceError):  # s2 is in the map but is no worktree of ours
            self.ws._check_slot(self.ws.slots_dir / "s2")
        with self.assertRaises(WorkspaceError):
            self.ws._check_slot(self.repo)
        (self.tmp / "wt" / ".upe" / "slots.json").write_text('{"..\\\\..\\\\game": "merge:x"}')
        with self.assertRaises(WorkspaceError):
            self.ws.slots()

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_copies_refuse_a_junction(self):
        w = self.ws.create("feat-1")
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "precious.txt").write_text("keep")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(w.path / "Vendor"), str(target)], check=True, capture_output=True)
        with self.assertRaises(WorkspaceError):
            self.ws.robocopy(self.golden / "Library", w.path / "Vendor")
        self.assertTrue((target / "precious.txt").exists())

    def test_busy_slots_are_not_shared(self):
        a = self.ws.create("feat-1")
        b = self.ws.create("feat-2")
        self.assertEqual((a.path.name, b.path.name), ("s1", "s2"))

    def test_a_dirty_parked_slot_is_skipped(self):
        w = self.ws.create("feat-1")
        self.ws.remove("feat-1")
        (w.path / "Assets" / "stray.txt").write_text("left behind")
        self.assertEqual(self.ws.create("feat-2").path.name, "s2")

    @unittest.skipUnless(sys.platform == "win32", "Windows share modes")
    def test_a_parked_slot_open_in_an_editor_is_skipped(self):
        w = self.ws.create("feat-1")
        self.ws.remove("feat-1")
        (w.path / "Temp").mkdir()
        with held_exclusively(w.path / LOCKFILE):
            self.assertEqual(self.ws.create("feat-2").path.name, "s2")

    def test_remove_refuses_uncommitted_work(self):
        w = self.ws.create("feat-1")
        (w.path / "Assets" / "new.txt").write_text("work")
        with self.assertRaises(WorkspaceError):
            self.ws.remove("feat-1")
        self.assertEqual(self.ws.slots(), {"s1": "feat-1"})

    def test_release_can_discard_a_candidate(self):
        path = self.ws.acquire("merge:feat-1", "upe-merge/feat-1", "HEAD")
        (path / "Assets" / "a.txt").write_text("half merged")
        (path / "Assets" / "new.txt").write_text("new")
        self.ws.release("merge:feat-1", discard_changes=True)
        self.assertEqual((path / "Assets" / "a.txt").read_text(), "a")
        self.assertFalse((path / "Assets" / "new.txt").exists())
        self.assertEqual(self.ws.slots(), {"s1": None})
        again = self.ws.acquire("merge:feat-1", "upe-merge/feat-1", "HEAD", reset_branch=True)
        self.assertEqual(again, path)


def aged(path: Path, seconds: float = 600) -> None:
    """Every file and folder under `path` last written `seconds` ago: a slot nothing writes in any more."""
    import os
    import time

    when = time.time() - seconds
    for folder, dirs, files in os.walk(path):
        for name in files + dirs:
            os.utime(os.path.join(folder, name), (when, when))
    os.utime(path, (when, when))


class DeadStartSlotTests(Base):
    """What a `start` that died leaves in its slot, and how the slot is taken away."""

    def interrupted_copy(self, argv, **kw):
        if argv[0] == "robocopy":  # Ctrl-C while the Library is copied: the folder is half made
            Path(argv[2]).mkdir(parents=True, exist_ok=True)
            (Path(argv[2]) / "half").write_text("")
            raise KeyboardInterrupt
        return subprocess.run(argv, **kw)

    def test_a_broken_slot_map_is_a_workspace_error(self):
        # Row 5 of the table: `main` raised JSONDecodeError, a traceback and exit 1 after the claim was made.
        (self.tmp / "wt" / ".upe").mkdir(parents=True)
        for text in ("{not json", "[]"):
            with self.subTest(text=text):
                (self.tmp / "wt" / ".upe" / "slots.json").write_text(text)
                with self.assertRaises(WorkspaceError) as ctx:
                    self.ws.slots()
                self.assertIn("does not read", str(ctx.exception))

    def test_ctrl_c_in_the_library_copy_gives_the_slot_back(self):
        ws = Workspaces(self.repo, self.tmp / "wt", self.golden, runner=self.interrupted_copy)
        with self.assertRaises(KeyboardInterrupt):
            ws.create("feat-1")
        self.assertEqual(ws.slots(), {})
        self.assertFalse((self.tmp / "wt" / "slots" / "s1").exists())
        self.assertNotIn("upe/feat-1", git(self.repo, "branch"))
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_what_a_slot_would_lose(self):
        # A tracked file missing loses nothing (git has it), nor does a file whose content is HEAD's, as a
        # checkout killed before it wrote its index leaves them (they read as untracked). Anything else is work.
        w = self.ws.create("feat-1")
        self.assertEqual(self.ws.losses(w.path), [])
        (w.path / "Assets" / "a.txt").unlink()
        self.assertEqual(self.ws.losses(w.path), ["Assets/a.txt"], "with an index, a tracked file missing is a deletion")
        git(w.path, "rm", "-q", "Assets/a.txt")
        self.assertEqual(self.ws.losses(w.path), ["Assets/a.txt"], "the same, staged")
        git(w.path, "reset", "-q", "--hard")
        index = Path(git(w.path, "rev-parse", "--absolute-git-dir").strip()) / "index"
        index.unlink()
        self.assertTrue(git(w.path, "ls-files", "--others", "--exclude-standard").strip(), "the probe: no index, so untracked")
        self.assertEqual(self.ws.losses(w.path), [], "no index: every file is HEAD's")
        (w.path / "Assets" / "a.txt").write_text("changed")
        self.assertEqual(self.ws.losses(w.path), ["Assets/a.txt"], "no index, and a file not as HEAD has it")
        git(w.path, "reset", "-q", "--hard")
        (w.path / "Assets" / "a.txt").write_text("changed")
        self.assertEqual(self.ws.losses(w.path), ["Assets/a.txt"], "a tracked file changed")
        git(w.path, "checkout", "--", "Assets/a.txt")
        (w.path / "Assets" / "new.txt").write_text("work")
        self.assertEqual(self.ws.losses(w.path), ["Assets/new.txt"], "a file git does not track")
        (w.path / "Assets" / "new.txt").unlink()
        (w.path / "Library" / "warm").write_text("ignored")
        self.assertEqual(self.ws.losses(w.path), [], "the ignored Library is not work")

    def test_a_slot_written_in_lately_is_named(self):
        # A killed start's robocopy may still write in the slot; a change in the last `quiet_s` is named.
        import os
        import time

        w = self.ws.create("feat-1")
        self.assertIsNotNone(self.ws.recent_change(w.path, 120), "a slot made just now")
        aged(w.path)
        self.assertIsNone(self.ws.recent_change(w.path, 120))
        (w.path / "Library" / "deep").mkdir()
        (w.path / "Library" / "deep" / "file").write_text("being copied")
        for old in (w.path / "Library" / "deep" / "file", w.path / "Library"):  # robocopy sets a file's time to the source's
            os.utime(old, (time.time() - 600, time.time() - 600))
        self.assertEqual(self.ws.recent_change(w.path, 120), os.path.join("Library", "deep"), "a folder a copy fills")
        aged(w.path)
        future = time.time() + 3600  # the clock set back since: a change in the future is not old
        os.utime(w.path / "Assets" / "a.txt", (future, future))
        self.assertEqual(self.ws.recent_change(w.path, 120), os.path.join("Assets", "a.txt"))

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_a_junction_is_not_walked_into(self):
        # What a junction points at is not the slot's; a file written there is no change in the slot.
        import time

        w = self.ws.create("feat-1")
        target = self.tmp / "elsewhere"
        target.mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(w.path / "Library" / "Linked"), str(target)], check=True,
                       capture_output=True)
        time.sleep(0.2)
        since = time.time()
        time.sleep(0.2)
        (target / "fresh.txt").write_text("written after `since`")
        self.assertIsNone(self.ws.recent_change(w.path, 0, now=since))

    def test_a_file_held_open_and_written_is_named(self):
        # The folder listing's time of a file held open for writing stays old on Windows; os.stat's does not.
        import os
        import time

        w = self.ws.create("feat-1")
        big = w.path / "Library" / "big.bin"
        big.write_bytes(b"")
        code = ("import sys, time\n"
                "with open(sys.argv[1], 'ab', buffering=0) as f:\n"
                "    print('open', flush=True)\n"
                "    for _ in range(600):\n"
                "        f.write(b'x' * 4096); time.sleep(0.1)\n")
        writer = subprocess.Popen([sys.executable, "-c", code, str(big)], stdout=subprocess.PIPE, text=True,
                                  stdin=subprocess.DEVNULL)
        try:
            self.assertEqual(writer.stdout.readline().strip(), "open")
            time.sleep(0.5)
            aged(w.path)  # every time, the open file's too, set ten minutes back; the writer goes on writing
            time.sleep(1.5)
            self.assertEqual(self.ws.recent_change(w.path, 60), os.path.join("Library", "big.bin"))
        finally:
            writer.kill()
            writer.wait()
            writer.stdout.close()

    def test_ctrl_c_with_a_folder_that_stays_keeps_its_entry(self):
        # The clean-up dropped the entry though the folder stayed, so the next slot took its name.
        held = []

        def copy_then_interrupt(argv, **kw):
            if argv[0] == "robocopy":
                Path(argv[2]).mkdir(parents=True, exist_ok=True)
                held.append(open(Path(argv[2]) / "open", "w"))  # a file git cannot delete while it is open
                raise KeyboardInterrupt
            return subprocess.run(argv, **kw)

        ws = Workspaces(self.repo, self.tmp / "wt", self.golden, runner=copy_then_interrupt)
        try:
            with self.assertRaises(KeyboardInterrupt) as ctx:
                ws.create("feat-1")
            self.assertTrue((self.tmp / "wt" / "slots" / "s1").exists())
            self.assertEqual(ws.slots(), {"s1": "feat-1"}, "the folder stays, and so does its entry")
            self.assertIn("still feat-1's", " ".join(getattr(ctx.exception, "__notes__", [])))
        finally:
            for f in held:
                f.close()
        self.assertEqual(self.ws.remove_slot("feat-1"), "s1", "the removal it began is finished later")
        self.assertEqual((self.ws.slots(), (self.tmp / "wt" / "slots" / "s1").exists()), ({}, False))

    def test_remove_slot_takes_the_slot_away_and_says_if_the_folder_stays(self):
        w = self.ws.create("feat-1")
        self.assertEqual(self.ws.remove_slot("feat-1"), "s1")
        self.assertEqual((self.ws.slots(), w.path.exists()), ({}, False))
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)
        self.assertEqual(self.ws.remove_slot("feat-1"), "", "no slot: nothing to do")
        # The folder already gone (a start killed in `worktree add`, or removed by hand): the entry is dropped.
        w = self.ws.create("feat-2")
        import shutil

        shutil.rmtree(w.path)
        self.assertEqual(self.ws.remove_slot("feat-2"), "s1")
        self.assertEqual(self.ws.slots(), {})
        git(self.repo, "branch", "-D", "upe/feat-2")  # git no longer holds it as checked out
        # A file held open (real git): git deletes `.git` first and stops; the folder and the entry stay,
        # and once the file is closed the next call finishes the removal it began.
        w = self.ws.create("feat-3")
        with open(w.path / "Library" / "held", "w"):
            with self.assertRaises(WorkspaceError) as ctx:
                self.ws.remove_slot("feat-3")
            self.assertIn("could not be removed", str(ctx.exception))
            self.assertTrue(w.path.exists())
            self.assertEqual(self.ws.slots(), {"s1": "feat-3"})
        self.assertEqual(self.ws.remove_slot("feat-3"), "s1")
        self.assertEqual((self.ws.slots(), w.path.exists()), ({}, False))
        # A folder with no `.git` that the tool did not begin to remove is not its to delete.
        w = self.ws.create("feat-4")
        (w.path / ".git").unlink()
        (w.path / "Assets" / "mine.txt").write_text("someone's")
        with self.assertRaises(WorkspaceError) as ctx:
            self.ws.remove_slot("feat-4")
        self.assertIn("did not begin removing it", str(ctx.exception))
        self.assertTrue((w.path / "Assets" / "mine.txt").exists())


@unittest.skipUnless(sys.platform == "win32", "robocopy is Windows'")
class SlotIdTests(Base):
    def test_each_new_slot_gets_an_id_of_its_own_when_the_golden_holds_one(self):
        # Real robocopy copies a golden's id file into each new slot; the id
        # written after the copy makes each slot's Library its own. Red with that write removed: both slots read the
        # golden's id.
        from upe.workspace import SLOT_ID_FILE

        (self.golden / "Library" / SLOT_ID_FILE).write_text("f" * 32 + "\n")
        a = self.ws.acquire("a", "upe/a", "main")
        b = self.ws.acquire("b", "upe/b", "main")
        self.assertTrue((a / "Library" / "ArtifactDB").exists(), "the golden's Library was copied")
        ids = [self.ws.slot_id(a), self.ws.slot_id(b)]
        self.assertNotIn("f" * 32, ids)
        self.assertNotEqual(ids[0], ids[1])


class RobocopyExitCodes(Base):
    def fake(self, code):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, code, "", "")

    def test_codes_below_8_are_success(self):
        for code in (0, 1, 3, 7):
            ws = Workspaces(self.repo, self.tmp / "wt", self.golden, runner=self.fake(code))
            self.assertEqual(ws.robocopy(self.golden / "Library", self.tmp / "out"), code)

    def test_8_and_above_fail(self):
        ws = Workspaces(self.repo, self.tmp / "wt", self.golden, runner=self.fake(8))
        with self.assertRaises(WorkspaceError):
            ws.robocopy(self.golden / "Library", self.tmp / "out")


if __name__ == "__main__":
    unittest.main()
