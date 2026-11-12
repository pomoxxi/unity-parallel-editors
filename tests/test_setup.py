import io
import json
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from upe import __main__ as cli
from upe.flow import Config
from upe.setup import EXAMPLE, SetupError, check, interactive, render, write

VERSION = "6000.3.0f1"


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
                   check=True, capture_output=True)


class Machine:
    """A Unity project in git, an Editor with UnityYAMLMerge beside it, and a unity CLI, all in a temp folder."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.repo = tmp / "game"
        (self.repo / "ProjectSettings").mkdir(parents=True)
        (self.repo / "ProjectSettings" / "ProjectVersion.txt").write_text(f"m_EditorVersion: {VERSION}\n")
        (self.repo / "Assets" / "Paid").mkdir(parents=True)
        (self.repo / "Assets" / "Paid" / "A.cs").write_text("class A {}")
        (self.repo / "Assets" / "Mine.cs").write_text("class M {}")
        (self.repo / ".gitignore").write_text("Assets/Paid/\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "first")
        self.editor = tmp / "Unity" / VERSION / "Editor" / "Unity.exe"
        (self.editor.parent / "Data" / "Tools").mkdir(parents=True)
        self.editor.write_text("")
        (self.editor.parent / "Data" / "Tools" / "UnityYAMLMerge.exe").write_text("")
        self.cli = tmp / "cli" / "unity.exe"
        self.cli.parent.mkdir()
        self.cli.write_text("")
        self.config = tmp / "tool" / "upe.toml"
        self.config.parent.mkdir()

    def answers(self, *, target="upe/test-base", copies="Assets/Paid; Assets/Mine.cs; Assets/Gone"):
        return [str(self.repo), str(self.tmp / "work"), "", target, str(self.editor), str(self.cli), copies]


class Script:
    def __init__(self, answers):
        self.answers, self.asked, self.said = list(answers), [], []

    def ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0)

    def say(self, text):
        self.said.append(text)

    @property
    def text(self):
        return "\n".join(self.said)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self._tmp.name))
        git(self.m.repo, "branch", "--no-track", "upe/test-base", "main")

    def tearDown(self):
        self._tmp.cleanup()

    def run_setup(self, answers):
        script = Script(answers)
        written = interactive(self.m.config, ask=script.ask, say=script.say)
        self.assertEqual(script.answers, [], "every answer was asked for")
        return written, script

    def test_writes_a_config_the_tool_reads(self):
        written, script = self.run_setup(self.m.answers())
        self.assertEqual(written, self.m.config)
        config = Config.load(written)
        self.assertEqual((config.repo, config.target), (self.m.repo.resolve(), "upe/test-base"))
        self.assertEqual(config.golden, (self.m.tmp / "work-golden").resolve())
        self.assertEqual((config.unity_editor, config.unity_cli), (self.m.editor.resolve(), self.m.cli.resolve()))
        self.assertIsNone(config.yaml_merge, "the one beside the Editor is used; nothing to name")
        # Only the git-ignored path that is there is kept; the tracked and the missing ones are said and left out.
        self.assertEqual(config.local_copies, ("Assets/Paid",))
        self.assertIn("Assets/Mine.cs has files in git", script.text)
        self.assertIn("Assets/Gone is not in", script.text)
        self.assertIn(f"Unity {VERSION}", script.text)

    def test_a_root_the_printed_steps_cannot_quote_is_asked_again(self):
        # The printed steps put a slot's path in double quotes, which Git Bash, PowerShell and cmd read alike only
        # without these characters. Without the check the root was taken.
        answers = self.m.answers()
        answers.insert(1, str(self.m.tmp / "wo$rk"))
        written, script = self.run_setup(answers)
        self.assertIn("holds a dollar sign ($)", script.text)
        self.assertEqual(Config.load(written).root, (self.m.tmp / "work").resolve())

    def test_the_example_comments_are_kept(self):
        written, _ = self.run_setup(self.m.answers())
        text = written.read_text(encoding="utf-8")
        self.assertIn("# Threads per batch Editor", text)
        self.assertIn("# yaml_merge = ", text)
        self.assertEqual([l for l in text.splitlines() if "<" in l and not l.startswith("#")], [])
        self.assertTrue(text.startswith("# Written by `upe setup`"))

    def test_an_existing_config_is_not_replaced(self):
        self.run_setup(self.m.answers())
        before = self.m.config.read_text(encoding="utf-8")
        git(self.m.repo, "branch", "--no-track", "upe/other", "main")
        written, script = self.run_setup(["", "", "", "upe/other", "", "", ""])
        self.assertEqual(written.name, "upe.toml.new")
        self.assertEqual(self.m.config.read_text(encoding="utf-8"), before)
        self.assertIn('+target = "upe/other"', script.text)
        self.assertEqual(Config.load(written).local_copies, ("Assets/Paid",), "Enter keeps the earlier answers")

    def test_a_branch_someone_works_on_is_refused(self):
        answers = self.m.answers(target="main")
        answers[4:4] = ["upe/test-base"]
        written, script = self.run_setup(answers)
        self.assertIn("main is checked out in", script.text)
        self.assertEqual(Config.load(written).target, "upe/test-base")

    def test_a_missing_branch_is_written_only_when_the_user_says(self):
        answers = self.m.answers(target="upe/new")
        answers[4:4] = ["y"]
        written, script = self.run_setup(answers)
        self.assertIn("branch --no-track upe/new main", script.text)
        self.assertEqual(Config.load(written).target, "upe/new")
        git(self.m.repo, "rev-parse", "--verify", "upe/test-base")  # setup made no branch
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.m.repo), "rev-parse", "--verify", "--quiet", "upe/new"],
                                           capture_output=True).returncode, 0)

    def test_paths_inside_the_project_or_relative_are_asked_again(self):
        answers = self.m.answers()
        answers[1:1] = ["work", str(self.m.repo / "slots")]
        _, script = self.run_setup(answers)
        self.assertIn("give the whole path", script.text)
        self.assertIn("not inside the project", script.text)

    def test_an_editor_of_another_version_needs_a_yes(self):
        other = self.m.tmp / "Unity" / "6000.4.0f1" / "Editor" / "Unity.exe"
        other.parent.mkdir(parents=True)
        other.write_text("")
        answers = self.m.answers()
        answers[4:5] = [str(other), "n", str(self.m.editor)]
        written, _ = self.run_setup(answers)
        self.assertEqual(Config.load(written).unity_editor, self.m.editor.resolve())

    def test_no_yaml_merge_beside_the_editor_is_asked_for(self):
        (self.m.editor.parent / "Data" / "Tools" / "UnityYAMLMerge.exe").unlink()
        merge = self.m.tmp / "UnityYAMLMerge.exe"
        merge.write_text("")
        answers = self.m.answers()
        answers[5:5] = [str(merge)]
        written, _ = self.run_setup(answers)
        self.assertEqual(Config.load(written).yaml_merge, merge.resolve())

    def test_render_adds_a_key_the_template_does_not_have(self):
        text = render({"repo": Path("D:/g"), "unity_cli": Path("C:/u.exe")}, 'repo = "x"\n\n[builders]\n')
        self.assertEqual(text, 'repo = "D:/g"\n\nunity_cli = "C:/u.exe"\n\n[builders]\n')

    def test_render_escapes_quotes(self):
        self.assertIn(r'repo = "D:/a \"b\""', render({"repo": 'D:/a "b"'}, EXAMPLE.read_text(encoding="utf-8")))

    def test_render_without_a_builders_table_keeps_every_key(self):
        text = render({"repo": "D:/g", "unity_cli": "C:/u.exe", "builders": {"Assets/A.unity": "G.B.Build"}}, 'repo = "x"\n')
        self.assertEqual(tomllib.loads(text), {"repo": "D:/g", "unity_cli": "C:/u.exe", "builders": {"Assets/A.unity": "G.B.Build"}})

    def test_a_hand_written_config_is_read_and_rebuilt(self):
        # Several-line arrays, a commented line above the real one, no [builders], comments at line ends:
        # what the old README's "fill it in by hand" made.
        self.m.config.write_text(
            f'# target = "old"\nrepo = "{self.m.repo.as_posix()}"  # the game\nroot = "{(self.m.tmp / "work").as_posix()}"\n'
            f'golden = "{(self.m.tmp / "gold").as_posix()}"\ntarget = "upe/test-base"\njob_workers = 2\n'
            f'local_copies = [\n  "Assets/Paid",\n]\nshared = [\n  "ProjectSettings/TagManager.asset",\n]\n'
            f'unity_editor = "{self.m.editor.as_posix()}"\nunity_cli = "{self.m.cli.as_posix()}"\n'
            f'protect_checked_out_target = false\n', encoding="utf-8")
        written, script = self.run_setup([""] * 7)
        config = Config.load(written)
        self.assertEqual(written.name, "upe.toml.new")
        self.assertEqual((config.target, config.job_workers, config.local_copies), ("upe/test-base", 2, ("Assets/Paid",)))
        self.assertEqual((config.shared, config.protect_checked_out_target), (("ProjectSettings/TagManager.asset",), False))
        self.assertEqual(config.golden, (self.m.tmp / "gold").resolve())
        self.assertIn("comments of\nyour own are not in it", script.text)

    def test_a_yaml_merge_of_an_old_editor_is_left_out(self):
        old_merge = self.m.tmp / "old" / "UnityYAMLMerge.exe"
        old_merge.parent.mkdir()
        old_merge.write_text("")
        self.run_setup(self.m.answers())
        self.m.config.write_text(f'yaml_merge = "{old_merge.as_posix()}"\n' + self.m.config.read_text(encoding="utf-8"),
                                 encoding="utf-8")
        written, script = self.run_setup([""] * 7)
        self.assertIsNone(Config.load(written).yaml_merge)
        self.assertIn("is left out: the one beside this Editor is used", script.text)

    def test_a_folder_ignored_by_its_contents_is_a_local_copy(self):
        (self.m.repo / "Assets" / "Store" / "Pack").mkdir(parents=True)
        (self.m.repo / "Assets" / "Store" / "Pack" / "P.cs").write_text("class P {}")
        (self.m.repo / ".gitignore").write_text("Assets/Paid/\nAssets/Store/*\n")
        written, _ = self.run_setup(self.m.answers(copies="Assets/Paid; Assets/Store/"))
        self.assertEqual(Config.load(written).local_copies, ("Assets/Paid", "Assets/Store"))

    def test_local_copies_outside_the_project_are_left_out(self):
        _, script = self.run_setup(self.m.answers(copies=f"{self.m.cli}; ../tool; ."))
        self.assertEqual(script.text.count("is not a path inside the project"), 3)

    def test_a_dash_clears_the_local_copies(self):
        self.run_setup(self.m.answers())
        written, _ = self.run_setup(["", "", "", "", "", "", "-"])
        self.assertEqual(Config.load(written).local_copies, ())

    def test_golden_and_slots_inside_each_other_are_asked_again(self):
        answers = self.m.answers()
        answers[2:3] = [str(self.m.tmp / "work" / "golden"), str(self.m.tmp / "work"),str(self.m.tmp / "gold")]
        written, script = self.run_setup(answers)
        self.assertEqual(script.text.count("not inside the slots' folder"), 2)
        self.assertEqual(Config.load(written).golden, (self.m.tmp / "gold").resolve())

    def test_slots_around_the_config_are_asked_again(self):
        answers = self.m.answers()
        answers[1:1] = [str(self.m.config.parent)]
        _, script = self.run_setup(answers)
        self.assertIn("the config", script.text)

    def test_a_tracked_folder_typed_in_other_case_is_left_out(self):
        (self.m.repo / "Assets" / "Shared").mkdir()
        (self.m.repo / "Assets" / "Shared" / "S.cs").write_text("class S {}")
        git(self.m.repo, "add", "Assets/Shared")
        git(self.m.repo, "commit", "-q", "-m", "shared")
        written, script = self.run_setup(self.m.answers(copies="assets/shared; Assets/Paid"))
        self.assertEqual(Config.load(written).local_copies, ("Assets/Paid",))
        self.assertIn("assets/shared has files in git", script.text)

    def test_an_untracked_path_that_is_not_ignored_is_left_out(self):
        (self.m.repo / "Assets" / "Loose.cs").write_text("class L {}")
        written, script = self.run_setup(self.m.answers(copies="Assets/Loose.cs"))
        self.assertEqual(Config.load(written).local_copies, ())
        self.assertIn("is not shown as git-ignored", script.text)

    def test_a_config_inside_the_project_is_asked_again_at_once(self):
        self.m.config = self.m.repo / "upe.toml"
        answers = self.m.answers()
        answers[1:1] = [str(self.m.tmp / "elsewhere")]  # never a project: asked again, then refused again
        script = Script([str(self.m.repo)])
        with self.assertRaises(IndexError):  # it keeps asking for another project rather than going on
            interactive(self.m.config, ask=script.ask, say=script.say)
        self.assertIn("the config", script.text)
        self.assertFalse(self.m.config.exists())

    def test_a_target_checked_out_under_root_but_not_in_a_slot_is_refused(self):
        other = self.m.tmp / "work" / "elsewhere"
        git(self.m.repo, "worktree", "add", "-q", str(other), "upe/test-base")
        git(self.m.repo, "branch", "--no-track", "upe/free", "main")
        answers = self.m.answers()
        answers[4:4] = ["upe/free"]
        written, script = self.run_setup(answers)
        self.assertIn("upe/test-base is checked out in", script.text)
        self.assertEqual(Config.load(written).target, "upe/free")

    def test_a_non_ascii_worktree_path_is_read(self):
        git(self.m.repo, "worktree", "add", "-q", str(self.m.tmp / "wtŞĞ"), "upe/test-base")
        answers = self.m.answers()
        git(self.m.repo, "branch", "--no-track", "upe/free", "main")
        answers[4:4] = ["upe/free"]
        _, script = self.run_setup(answers)
        self.assertIn("wtŞĞ", script.text)

    def test_an_earlier_new_file_survives_a_refused_text(self):
        self.run_setup(self.m.answers())
        self.run_setup([""] * 7)
        new = self.m.config.with_name("upe.toml.new")
        earlier = new.read_text(encoding="utf-8")
        with self.assertRaises(SetupError) as ctx:
            write(self.m.config, 'repo = "x"\n', lambda _: None)
        self.assertIn(str(self.m.config), str(ctx.exception))
        self.assertNotIn(".trying", str(ctx.exception), "the trial file is gone; name the config")
        self.assertEqual(new.read_text(encoding="utf-8"), earlier)
        self.assertEqual([p.name for p in self.m.config.parent.iterdir()], ["upe.toml", "upe.toml.new"])

    def test_an_old_config_not_in_utf8_still_gets_its_difference(self):
        self.run_setup(self.m.answers())
        self.m.config.write_bytes(self.m.config.read_bytes() + "# caf\xe9\n".encode("cp1252"))
        written, script = self.run_setup(self.m.answers())
        self.assertEqual(written.name, "upe.toml.new")
        self.assertIn("does not read as TOML", script.text)

    def test_old_values_of_the_wrong_kind_are_left_out(self):
        self.run_setup(self.m.answers())
        text = self.m.config.read_text(encoding="utf-8").replace('local_copies = ["Assets/Paid"]', 'local_copies = "Assets/Paid"')
        self.m.config.write_text('builders = "x"\n' + text.replace("\n[builders]\n", "\n"), encoding="utf-8")
        written, script = self.run_setup(["y"] + [""] * 7)  # said before the first question, and asked
        self.assertIn("does not load as it is", script.text)
        self.assertEqual(Config.load(written).builders, {})
        self.assertEqual(Config.load(written).local_copies, ("Assets/Paid",), "a lone string is offered as one path")

    def test_faults_the_questions_fix_do_not_cost_the_other_settings(self):
        self.run_setup(self.m.answers())
        text = self.m.config.read_text(encoding="utf-8").replace('local_copies = ["Assets/Paid"]', 'local_copies = "Assets/Paid"')
        text = "\n".join(l for l in text.splitlines() if not l.startswith("golden =")).replace("test_retries = 1", "test_retries = 5")
        self.m.config.write_text('protected_paths = ["Tools/"]\n' + text, encoding="utf-8")
        written, script = self.run_setup(["", "", str(self.m.tmp / "gold"), "", "", "", ""])
        self.assertNotIn("does not load as it is", script.text)
        config = Config.load(written)
        self.assertEqual((config.test_retries, config.protected_paths[-1], config.local_copies), (5, "Tools/", ("Assets/Paid",)))

    def test_an_old_config_that_does_not_load_stops_unless_the_user_says(self):
        self.run_setup(self.m.answers())
        self.m.config.write_text('protect_checked_out_target = "false"\n' + self.m.config.read_text(encoding="utf-8"),
                                 encoding="utf-8")
        script = Script(["n"])
        with self.assertRaises(SetupError) as ctx:
            interactive(self.m.config, ask=script.ask, say=script.say)
        self.assertEqual(len(script.asked), 1, "stopped before any path was asked")
        self.assertIn("must be true or false", script.text)
        self.assertIn("fix", str(ctx.exception))
        self.assertFalse(self.m.config.with_name("upe.toml.new").exists())

    def test_render_puts_extra_keys_above_the_builders_comments(self):
        text = render({"protect_checked_out_target": False, "my key": 1}, EXAMPLE.read_text(encoding="utf-8"))
        self.assertIn('protect_checked_out_target = false\n"my key" = 1\n\n# Builders:', text)
        self.assertIn("(it must be safe to run twice).\n[builders]", text)
        self.assertEqual(tomllib.loads(text)["my key"], 1)

    def test_render_escapes_a_delete_character(self):
        self.assertEqual(tomllib.loads(render({"repo": "a\x7fb"}, 'repo = "x"\n'))["repo"], "a\x7fb")

    def test_a_broken_project_version_is_asked_again(self):
        other = self.m.tmp / "broken"
        (other / "ProjectSettings").mkdir(parents=True)
        (other / ".git").mkdir()
        (other / "ProjectSettings" / "ProjectVersion.txt").write_text("nothing here\n")
        answers = self.m.answers()
        answers[0:0] = [str(other)]
        _, script = self.run_setup(answers)
        self.assertIn("no m_EditorVersion", script.text)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self._tmp.name))
        git(self.m.repo, "branch", "--no-track", "upe/test-base", "main")
        interactive(self.m.config, ask=Script(self.m.answers()).ask, say=lambda _: None)

    def tearDown(self):
        self._tmp.cleanup()

    def levels(self, env=None):
        return {c.name: c.level for c in check(self.m.config, env=env or {})}

    def test_all_named_paths_pass(self):
        levels = self.levels()
        self.assertEqual({k: v for k, v in levels.items() if v == "fail"}, {})
        self.assertEqual(levels["unity_editor"], "ok")
        self.assertEqual(levels["golden"], "warn", "not made yet: golden --create")

    def test_a_moved_editor_and_a_deleted_branch_fail(self):
        self.m.editor.unlink()
        git(self.m.repo, "branch", "-D", "upe/test-base")
        levels = self.levels()
        self.assertEqual((levels["unity_editor"], levels["yaml_merge"], levels["target"]), ("fail", "ok", "fail"))

    def test_a_new_unity_version_is_said(self):
        (self.m.repo / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.4.0f1\n")
        self.assertEqual(self.levels()["unity_editor"], "warn")

    def test_a_missing_local_copy_fails(self):
        (self.m.repo / "Assets" / "Paid" / "A.cs").unlink()
        (self.m.repo / "Assets" / "Paid").rmdir()
        self.assertEqual(self.levels()["local_copies: Assets/Paid"], "fail")

    def test_no_config(self):
        self.m.config.unlink()
        [only] = check(self.m.config, env={})
        self.assertEqual((only.level, "upe setup" in only.message), ("fail", True))

    def test_the_config_wins_over_the_variable_as_in_the_tool(self):
        elsewhere = self.m.tmp / "elsewhere.exe"
        elsewhere.write_text("")
        self.m.editor.unlink()
        levels = self.levels({"UPE_UNITY_EDITOR": str(elsewhere), "UPE_UNITY_CLI": str(elsewhere)})
        self.assertEqual(levels["unity_editor"], "fail", "the tool runs the one in upe.toml, which is gone")

    def test_a_broken_project_version_fails_without_a_crash(self):
        (self.m.repo / "ProjectSettings" / "ProjectVersion.txt").write_text("nothing\n")
        self.assertEqual(self.levels()["repo"], "fail")

    def test_a_wrongly_typed_value_fails_without_a_crash(self):
        self.m.config.write_text(self.m.config.read_text(encoding="utf-8").replace("max_high = 1", 'max_high = "x"'),
                                 encoding="utf-8")
        self.assertEqual(self.levels(), {"config": "fail"})

    def test_a_checked_out_target_passes_when_the_config_allows_it(self):
        git(self.m.repo, "checkout", "-q", "upe/test-base")
        self.assertEqual(self.levels()["target"], "fail")
        self.m.config.write_text("protect_checked_out_target = false\n" + self.m.config.read_text(encoding="utf-8"),
                                 encoding="utf-8")
        self.assertEqual(self.levels()["target"], "ok")

    def test_a_yaml_merge_named_without_exe_passes_as_the_merge_takes_it(self):
        bare = self.m.editor.parent / "Data" / "Tools" / "UnityYAMLMerge"
        self.m.config.write_text(f'yaml_merge = "{bare.as_posix()}"\n' + self.m.config.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(self.levels()["yaml_merge"], "ok")

    def test_settings_of_the_wrong_kind_fail(self):
        text = self.m.config.read_text(encoding="utf-8")
        for bad in ('protect_checked_out_target = "false"\n', 'local_copies = "Assets/Paid"\n'):
            key = bad.split(" ")[0]
            kept = "\n".join(l for l in text.splitlines() if not l.startswith(key))
            self.m.config.write_text(bad + kept, encoding="utf-8")
            self.assertEqual(self.levels(), {"config": "fail"}, bad)

    def test_a_local_copy_outside_the_project_fails(self):
        text = self.m.config.read_text(encoding="utf-8").replace('local_copies = ["Assets/Paid"]', 'local_copies = ["../tool"]')
        self.m.config.write_text(text, encoding="utf-8")
        self.assertEqual(self.levels()["local_copies: ../tool"], "fail")

    def test_a_test_folder_is_checked_at_the_target(self):
        # `start` names each to every agent, so one with no tracked file at the target fails.
        text = self.m.config.read_text(encoding="utf-8")
        self.assertIn("test_folders = []", text, "setup keeps the example's line")
        self.m.config.write_text(text.replace("test_folders = []", 'test_folders = ["Assets/", "Assets/None/"]'),
                                 encoding="utf-8")
        levels = self.levels()
        self.assertEqual((levels["test_folders: Assets/"], levels["test_folders: Assets/None/"]), ("ok", "fail"))

    def test_the_tests_line_names_the_folders_before_any_claim(self):
        # The agent reads where the tests live before `start`, from `setup --check`'s `tests` line.
        def tests_line():
            [line] = [c for c in check(self.m.config, env={}) if c.name == "tests"]
            return line

        self.assertEqual(tests_line().level, "warn")  # no folder named Test or Tests at the target, none named
        (self.m.repo / "Assets" / "Game" / "Tests" / "Editor").mkdir(parents=True)
        (self.m.repo / "Assets" / "Game" / "Tests" / "Editor" / "Ünï.cs").write_text("[Test]\n", encoding="utf-8")
        git(self.m.repo, "add", "-A")
        git(self.m.repo, "commit", "-q", "-m", "tests")
        git(self.m.repo, "branch", "-f", "upe/test-base", "main")
        self.assertEqual((tests_line().level, tests_line().message.split(" (")[0]), ("ok", "Assets/Game/Tests/"))
        text = self.m.config.read_text(encoding="utf-8")
        self.m.config.write_text(text.replace("test_folders = []", 'test_folders = ["Assets/Mine/"]'), encoding="utf-8")
        self.assertIn("Assets/Mine/ (test_folders in upe.toml)", tests_line().message)

    def test_a_root_the_printed_steps_cannot_quote_fails_the_check(self):
        # Each character, by `setup --check`. Without the check: `ok`.
        import json as _json
        import re as _re

        text = self.m.config.read_text(encoding="utf-8")
        for char, name in (("$", "a dollar sign"), ("`", "a backtick"), ("'", "a single quote"), ('"', "a double quote"),
                           ("%", "a percent sign")):
            with self.subTest(char=char):
                root = (self.m.tmp / f"wo{char}rk").as_posix()
                self.m.config.write_text(_re.sub(r"(?m)^root = .*$", lambda _: f"root = {_json.dumps(root)}", text),
                                         encoding="utf-8")
                [line] = [c for c in check(self.m.config, env={}) if c.name == "root"]
                self.assertEqual(line.level, "fail")
                self.assertIn(f"holds {name} ({char})", line.message)

    def test_a_root_on_a_network_path_fails_the_check(self):
        # Git Bash's double quotes read `\\` as one backslash. With a stand-in for the config's load: a network path
        # is not resolved here, so no network is touched.
        from upe import setup as setupmod

        real = setupmod.Config.load
        for unc in (r"\\server\share\upe", "//server/share/upe"):
            with self.subTest(unc=unc):
                def load(path, unc=unc):
                    config = real(path)
                    config.root = Path(unc)
                    return config

                with mock.patch.object(setupmod.Config, "load", staticmethod(load)):
                    [line] = [c for c in check(self.m.config, env={}) if c.name == "root"]
                self.assertEqual(line.level, "fail")
                self.assertIn("a network path", line.message)
        self.assertIsNone(setupmod.root_problem(self.m.tmp / "work with spaces"))

    def test_the_folders_named_test_or_tests_any_case(self):
        from upe.flow import test_folders_in

        self.assertEqual(test_folders_in(["Assets/A/Tests/Editor/X.cs", "Assets/A/Tests/Y.cs", "Assets/b/test/Z.cs",
                                          "Assets/Contest/Q.cs", "Tests.cs", "Assets/TESTS/R/S.cs"]),
                         ["Assets/A/Tests/", "Assets/b/test/", "Assets/TESTS/"])

    def test_check_changes_nothing(self):
        before = {p: p.stat().st_mtime_ns for p in self.m.tmp.rglob("*") if ".git" not in p.parts}
        check(self.m.config, env={})
        after = {p: p.stat().st_mtime_ns for p in self.m.tmp.rglob("*") if ".git" not in p.parts}
        self.assertEqual(before, after)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name) / "upe.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--config", str(self.config), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_setup_without_a_person_refuses(self):
        code, _, err = self.main("setup")
        self.assertEqual(code, 2)
        self.assertIn("ask the user to run `upe setup`", err)
        self.assertFalse(self.config.exists())

    def test_setup_with_json_refuses_even_at_a_terminal(self):
        with mock.patch.object(cli, "at_a_terminal", return_value=True):
            code, _, err = self.main("--json", "setup")
        self.assertEqual((code, json.loads(err)["error"]), (2, "setup needs a person"))

    def test_any_command_without_a_config_says_to_run_setup(self):
        code, _, err = self.main("status")
        self.assertEqual(code, 2)
        self.assertIn("ask the user to run `upe setup`", err)

    def test_check_as_json(self):
        code, out, _ = self.main("--json", "setup", "--check")
        answer = json.loads(out)
        self.assertEqual((code, answer["ok"], answer["checks"][0]["name"]), (1, False, "config"))

    def test_a_person_who_stops_gets_nothing_written(self):
        with mock.patch.object(cli, "at_a_terminal", return_value=True), mock.patch("builtins.input", side_effect=EOFError):
            code, _, err = self.main("setup")
        self.assertEqual(code, 2)
        self.assertIn("nothing was written", err)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
