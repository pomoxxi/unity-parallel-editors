import io
import itertools
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from upe.editor import yaml_merge_for
from upe.merge import LOCK_FILE, MergeError, Merger, changed_on_both, lost_changes, restore_unchanged, merge_manifest, program_there, unity_yaml_merge, yaml_merge_flags

GUID_A = "a" * 32
GUID_B = "b" * 32


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "core.autocrlf=false", *args], check=True, capture_output=True, text=True
    ).stdout


def write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def union_merger(calls):
    """A stand-in for UnityYAMLMerge: keeps every object of both sides, in order."""

    def merge(base, theirs, mine, out):
        calls.append((base.name, theirs.name, mine.name, out.name))
        seen, blocks = set(), []
        for src in (mine, theirs):
            for block in re.split(r"(?m)^(?=--- )", src.read_text()):
                if block not in seen:
                    seen.add(block)
                    blocks.append(block)
        out.write_text("".join(blocks))
        return 0

    return merge


def failing_merger(base, theirs, mine, out):
    return 1


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        write(self.repo, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Arena\n")
        write(self.repo, "Packages/manifest.json", json.dumps({"dependencies": {"com.a": "1.0.0"}}, indent=2) + "\n")
        write(self.repo, LOCK_FILE, '{"dependencies": {}}\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "branch", "upe/feat")
        self.feat = self.tmp / "feat"
        git(self.repo, "worktree", "add", "-q", str(self.feat), "upe/feat")
        self.candidate_wt = self.tmp / "candidate"

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, root, message="change"):
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", message)

    def merge(self, merger=None, builders=None):
        return Merger(self.repo, yaml_merger=merger, builders=builders).merge(
            "feat", "upe/feat", "main", self.candidate_wt
        )


class CleanMerge(RepoCase):
    def test_disjoint_changes_merge_and_land(self):
        write(self.feat, "Assets/Features/Dash/Dash.cs", "class Dash {}\n")
        self.commit(self.feat)
        write(self.repo, "Assets/Features/Jump/Jump.cs", "class Jump {}\n")
        self.commit(self.repo)

        merger = Merger(self.repo)
        report = merger.merge("feat", "upe/feat", "main", self.candidate_wt)
        self.assertTrue(report.ok, report.steps)
        self.assertEqual(report.steps, [])
        # The target does not move until land().
        self.assertFalse((self.repo / "Assets/Features/Dash/Dash.cs").exists())
        merger.land(report)
        self.assertTrue((self.repo / "Assets/Features/Dash/Dash.cs").exists())
        self.assertEqual(git(self.repo, "rev-parse", "main").strip(), report.committed)

    def test_the_record_and_the_merge_message_keep_the_short_names(self):
        # Git reads the branches by their full names, while the record keeps the short ones, which `Merger.land` and
        # the flow read as `refs/heads/{report.target}`, and so does the merge commit's message. Both the record of a
        # merge that stops and the final one. Red with either given the full names.
        write(self.feat, "Assets/Features/Dash/Dash.cs", "class Dash {}\n")
        self.commit(self.feat)
        merger = Merger(self.repo)
        report = merger.merge("feat", "upe/feat", "main", self.candidate_wt, before_merge=lambda wt: None)
        self.assertEqual((report.branch, report.target), ("upe/feat", "main"))
        self.assertEqual(git(self.candidate_wt, "log", "-1", "--format=%s").strip(),
                         "Merge upe/feat into main (candidate for feat)")
        merger.land(report)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main").strip(), report.committed)

    def test_a_merge_that_stops_keeps_the_short_names(self):
        # The record a merge that stops returns (the flow writes it as merge.json).
        write(self.feat, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Dash\n")
        self.commit(self.feat)
        write(self.repo, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Jump\n")
        self.commit(self.repo)
        stopped = Merger(self.repo, yaml_merger=failing_merger).merge("feat", "upe/feat", "main", self.candidate_wt,
                                                                      before_merge=lambda wt: None)
        self.assertFalse(stopped.ok)
        self.assertEqual((stopped.branch, stopped.target), ("upe/feat", "main"))

    def test_land_refused_if_target_moved(self):
        write(self.feat, "Assets/A.cs", "a\n")
        self.commit(self.feat)
        merger = Merger(self.repo)
        report = merger.merge("feat", "upe/feat", "main", self.candidate_wt)
        write(self.repo, "Assets/Late.cs", "late\n")
        self.commit(self.repo)
        with self.assertRaises(MergeError):
            merger.land(report)


class SceneConflicts(RepoCase):
    def make_scene_conflict(self):
        write(self.feat, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Arena\n--- !u!1 &2\nGameObject:\n  m_Name: Dash\n")
        self.commit(self.feat)
        write(self.repo, "Assets/Scenes/Arena.unity", "--- !u!1 &1\nGameObject:\n  m_Name: Arena\n--- !u!1 &3\nGameObject:\n  m_Name: Jump\n")
        self.commit(self.repo)

    def test_unityyamlmerge_resolves_and_gets_base_theirs_mine_order(self):
        self.make_scene_conflict()
        calls = []
        report = self.merge(merger=union_merger(calls))
        self.assertTrue(report.ok, report.steps)
        self.assertEqual(calls, [("base.unity", "theirs.unity", "mine.unity", "out.unity")])
        self.assertEqual([(s.path, s.layer, s.outcome) for s in report.steps],
                         [("Assets/Scenes/Arena.unity", "unityyamlmerge", "merged")])
        merged = (self.candidate_wt / "Assets/Scenes/Arena.unity").read_text()
        self.assertIn("Dash", merged)
        self.assertIn("Jump", merged)

    def test_the_merger_gets_files_with_the_assets_extension(self):
        names = []

        def merger(base, theirs, mine, out):
            names.extend(p.suffix for p in (base, theirs, mine, out))
            out.write_text("merged")
            return 0

        self.make_scene_conflict()
        self.merge(merger)
        self.assertEqual(set(names), {".unity"})  # UnityYAMLMerge refuses a file with no extension

    def test_a_conflict_stops_before_the_long_work(self):
        ran = []
        self.make_scene_conflict()
        report = Merger(self.repo, yaml_merger=failing_merger).merge(
            "dash", "upe/feat", "main", self.candidate_wt, before_merge=lambda wt: ran.append(wt))
        self.assertFalse(report.ok)
        self.assertEqual(ran, [])

    def test_the_long_work_sees_only_the_target(self):
        seen = []
        self.make_scene_conflict()

        def before(wt):
            seen.append((subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout,
                         (wt / "Assets/Scenes/Arena.unity").read_text()))

        report = Merger(self.repo, yaml_merger=union_merger([])).merge("dash", "upe/feat", "main", self.candidate_wt, before_merge=before)
        self.assertTrue(report.ok, report.steps)
        self.assertEqual(seen[0][0], "")
        self.assertNotIn("Dash", seen[0][1])  # the trial merge was undone before the long work
        self.assertIn("Dash", (self.candidate_wt / "Assets/Scenes/Arena.unity").read_text())

    def test_what_the_long_work_rewrites_does_not_stop_the_merge(self):
        write(self.feat, "Packages/manifest.json", json.dumps({"dependencies": {"com.a": "1.0.0", "com.b": "1.0.0"}}, indent=2) + "\n")
        self.commit(self.feat)

        def unity_rewrites(wt):
            write(wt, "Packages/manifest.json", json.dumps({"dependencies": {"com.a": "1.0.0"}}) + "\n")

        report = Merger(self.repo).merge("dash", "upe/feat", "main", self.candidate_wt, before_merge=unity_rewrites)
        self.assertTrue(report.ok, report.steps)
        self.assertIn("com.b", (self.candidate_wt / "Packages/manifest.json").read_text())

    def test_a_file_the_long_work_makes_does_not_stop_the_merge(self):
        write(self.feat, "ProjectSettings/New.asset", "from the branch\n")
        self.commit(self.feat)

        def unity_makes_it(wt):
            write(wt, "ProjectSettings/New.asset", "made by Unity\n")

        report = Merger(self.repo).merge("dash", "upe/feat", "main", self.candidate_wt, before_merge=unity_makes_it)
        self.assertTrue(report.ok, report.steps)
        self.assertEqual((self.candidate_wt / "ProjectSettings/New.asset").read_text(), "from the branch\n")

    def test_a_file_both_sides_added_survives_the_long_work(self):
        write(self.feat, "Assets/Both.cs.meta", "guid: 1\n")
        self.commit(self.feat, "the branch's")
        write(self.repo, "Assets/Both.cs.meta", "guid: 1\n")
        self.commit(self.repo, "the target's")
        report = Merger(self.repo).merge("dash", "upe/feat", "main", self.candidate_wt, before_merge=lambda wt: None)
        self.assertTrue(report.ok, report.steps)
        self.assertTrue((self.candidate_wt / "Assets/Both.cs.meta").exists())

    def test_folders_and_files_the_long_work_leaves_are_kept(self):
        write(self.feat, "Assets/Feature.cs", "class Feature {}\n")
        self.commit(self.feat)

        def unity_keeps_a_folder(wt):
            (wt / "Assets/EmptyFolder").mkdir(parents=True)  # its .meta is tracked; git does not track the folder
            write(wt, "Assets/Generated.txt", "made by Unity\n")

        report = Merger(self.repo).merge("dash", "upe/feat", "main", self.candidate_wt, before_merge=unity_keeps_a_folder)
        self.assertTrue(report.ok, report.steps)
        self.assertTrue((self.candidate_wt / "Assets/EmptyFolder").is_dir())
        self.assertTrue((self.candidate_wt / "Assets/Generated.txt").exists())

    def test_a_non_ascii_scene_reaches_the_yaml_layer(self):
        scene = "Assets/Scenes/Sc\u00e8ne_\u015f.unity"
        write(self.repo, scene, "--- !u!1 &1\nGameObject:\n  m_Name: Arena\n")
        self.commit(self.repo)
        git(self.feat, "merge", "-q", "main")
        write(self.feat, scene, "--- !u!1 &1\nGameObject:\n  m_Name: Arena\n--- !u!1 &2\nGameObject:\n  m_Name: Dash\n")
        self.commit(self.feat)
        write(self.repo, scene, "--- !u!1 &1\nGameObject:\n  m_Name: Arena\n--- !u!1 &3\nGameObject:\n  m_Name: Jump\n")
        self.commit(self.repo)
        report = self.merge(merger=union_merger([]))
        self.assertEqual([(s.path, s.layer) for s in report.steps], [(scene, "unityyamlmerge")])

    def test_unityyamlmerge_failure_stops_and_is_not_committed(self):
        self.make_scene_conflict()
        merger = Merger(self.repo, yaml_merger=failing_merger)
        report = merger.merge("feat", "upe/feat", "main", self.candidate_wt)
        self.assertFalse(report.ok)
        self.assertEqual(report.committed, "")
        self.assertEqual(report.unresolved[0].detail, "UnityYAMLMerge exited 1")
        merger.discard(report, self.candidate_wt)
        self.assertFalse(self.candidate_wt.exists())
        self.assertNotIn("upe-merge/feat", git(self.repo, "branch"))

    def test_builder_replay_keeps_target_and_lists_builder(self):
        self.make_scene_conflict()
        report = self.merge(merger=failing_merger, builders={"Assets/Scenes/Arena.unity": "Dash.Builder.Build"})
        self.assertTrue(report.ok, report.steps)
        self.assertEqual(report.replays, ["Dash.Builder.Build"])
        self.assertEqual(report.steps[0].outcome, "replay")
        self.assertIn("Jump", (self.candidate_wt / "Assets/Scenes/Arena.unity").read_text())

    def test_other_text_conflict_without_builder_stops(self):
        write(self.feat, "Assets/Data/Items.asset", "a: 1\n")
        self.commit(self.feat)
        write(self.repo, "Assets/Data/Items.asset", "a: 2\n")
        self.commit(self.repo)
        report = self.merge(merger=union_merger([]))
        self.assertFalse(report.ok)
        self.assertEqual(report.unresolved[0].layer, "unresolved")


class LostChanges(unittest.TestCase):
    BASE = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!1 &20\nGameObject:\n  m_Name: B\n"

    def lost(self, mine, theirs, merged):
        return lost_changes(self.BASE, mine, theirs, merged)

    def test_both_sides_kept(self):
        mine = self.BASE.replace("m_Name: A", "m_Name: A2")
        theirs = self.BASE.replace("m_Name: B", "m_Name: B2")
        self.assertEqual(self.lost(mine, theirs, mine.replace("m_Name: B", "m_Name: B2")), [])

    def test_deleted_on_one_side_and_changed_on_the_other(self):
        deleted = self.BASE.split("--- !u!1 &20")[0]
        renamed = self.BASE.replace("m_Name: B", "m_Name: B2")
        found = self.lost(deleted, renamed, deleted)
        self.assertEqual(found, ["object 20 (B2) deleted on one side and changed on the other"])

    def test_one_sides_change_missing_from_the_result(self):
        renamed = self.BASE.replace("m_Name: A", "m_Name: A2")
        self.assertEqual(self.lost(renamed, self.BASE, self.BASE), ["object 10 (A2): one side's change is not in the merged file"])

    def test_an_added_object_missing_from_the_result(self):
        added = self.BASE + "--- !u!1 &30\nGameObject:\n  m_Name: C\n"
        self.assertEqual(self.lost(added, self.BASE, self.BASE), ["object 30 (C): one side's change is not in the merged file"])

    def test_line_endings_and_spacing_are_not_changes(self):
        self.assertEqual(self.lost(self.BASE, self.BASE, self.BASE.replace("\n", "\r\n")), [])

    def test_quotes_are_formatting_but_a_space_inside_them_is_not(self):
        quoted = self.BASE.replace("m_Name: B", "m_Name: 'B'")
        self.assertEqual(self.lost(self.BASE, quoted, self.BASE), [])
        self.assertEqual(self.lost(self.BASE, self.BASE.replace("m_Name: B", "m_Name: ''"),
                                   self.BASE.replace("m_Name: B", "m_Name:")), [])
        spaced = self.BASE.replace("m_Name: B", "m_Name: 'B '")
        renamed = self.BASE.replace("m_Name: A", "m_Name: A2")
        kept = renamed.replace("m_Name: B", "m_Name: 'B '")
        self.assertEqual(self.lost(renamed, spaced, kept), [])
        self.assertEqual(self.lost(renamed, spaced, renamed), ["object 20 (B): one side's change is not in the merged file"])
        self.assertNotEqual(restore_unchanged(renamed, kept), renamed)  # the target's `B` does not replace `'B '`

    def test_an_object_neither_side_changed_must_come_through(self):
        renamed = self.BASE.replace("m_Name: A", "m_Name: A2")
        changed = renamed.replace("m_Name: B", "m_Name: Z")
        self.assertEqual(self.lost(renamed, self.BASE, changed),
                         ["object 20 (B): changed or removed in the merged file, though neither side changed it"])
        dropped = renamed.split("--- !u!1 &20")[0]
        self.assertEqual(self.lost(renamed, self.BASE, dropped),
                         ["object 20 (B): changed or removed in the merged file, though neither side changed it"])

    def test_a_line_folded_at_another_column_is_not_a_change(self):
        # What UnityYAMLMerge does to long references and names in scenes.
        base = ("%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!1001 &20\nPrefabInstance:\n"
                "  m_Source: {fileID: 164696360095427260, guid: 3ccd65918d2f84f48b355c31f6301a1d,\n    type: 3}\n"
                "  id: 10:Stage/9:Lamp\n    5, out/1:Lamp, right\n  'm_MeshMetrics[0]': 1\n")
        renamed = base.replace("m_Name: A", "m_Name: A2")
        rewrapped = (renamed.replace("3ccd65918d2f84f48b355c31f6301a1d,\n    type: 3}", "3ccd65918d2f84f48b355c31f6301a1d, type: 3}")
                     .replace("9:Lamp\n    5, out", "9:Lamp 5,\n    out").replace("'m_MeshMetrics[0]'", "m_MeshMetrics[0]"))
        self.assertEqual(lost_changes(base, renamed, base, rewrapped), [])
        self.assertEqual(restore_unchanged(renamed, rewrapped), renamed)  # the council's diff keeps the target's text
        changed = rewrapped.replace("type: 3}", "type: 2}")
        self.assertEqual(lost_changes(base, renamed, base, changed),
                         ["object 20: changed or removed in the merged file, though neither side changed it"])

    def test_reading_folds_does_not_erase_structure(self):
        # A change of structure must not read as a fold.
        base = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n{body}\n"
        for name, before, after in (
            ("list items joined", "  lines:\n  - Hello\n  - world", "  lines:\n  - Hello - world"),
            ("key moved into a mapping", "  settings:\n    speed: 1\n  range: 2", "  settings:\n    speed: 1\n    range: 2"),
            ("block scalar's lines", "  t: |\n    a\n    b", "  t: |\n    a b"),
            ("a key-like line inside a folded text", "  m_text: \"a, then\n    'Jump': press\"", "  m_text: \"a, then\n    Jump: press\""),
            # A bracket or quote inside a plain value opens nothing.
            ("list after a plain [", "  label: Slot [1\n  names:\n  - Hello\n  - world", "  label: Slot [1\n  names:\n  - Hello - world"),
            ("key moved after a plain quote", "  label: Rock 'n roll\n  data:\n    x: 1\n  y: 2", "  label: Rock 'n roll\n  data:\n    x: 1\n    y: 2"),
        ):
            with self.subTest(name):
                was = base.replace("{body}", before)
                mine = was.replace("m_Name: A", "m_Name: A2")
                theirs = was.replace(before, after)
                merged = mine.replace(before, after)
                self.assertEqual(lost_changes(was, mine, theirs, merged), [])
                self.assertEqual(restore_unchanged(mine, merged), merged)  # the target's text does not come back over it
                self.assertEqual(lost_changes(was, mine, theirs, mine), ["object 20: one side's change is not in the merged file"])

    def test_a_quoted_list_item_refolded_over_many_lines_is_not_a_change(self):
        words = " ".join(f"word{i}" for i in range(40))
        base = f"%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  notes:\n  - 'Note: {words}'\n  - x\n"
        folded = base.replace(f"'Note: {words}'", "'Note: " + " ".join(f"word{i}" for i in range(13)) + "\n    "
                              + " ".join(f"word{i}" for i in range(13, 26)) + "\n    " + " ".join(f"word{i}" for i in range(26, 40)) + "'")
        renamed = base.replace("m_Name: A", "m_Name: A2")
        self.assertEqual(lost_changes(base, renamed, base, folded.replace("m_Name: A", "m_Name: A2")), [])

    def test_an_object_on_neither_side_must_not_appear(self):
        renamed = self.BASE.replace("m_Name: A", "m_Name: A2")
        self.assertEqual(self.lost(renamed, self.BASE, renamed + "--- !u!1 &40\nGameObject:\n  m_Name: D\n"),
                         ["object 40 (D): in the merged file, though neither side has it"])

    OBJECT = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n  m_Layer: 0\n  m_IsActive: 1\n"
    BOTH = "object 10 (A): changed on both sides, and a change is not in the merged object"

    def test_one_object_two_fields_both_kept(self):
        mine = self.OBJECT.replace("m_Layer: 0", "m_Layer: 5")
        theirs = self.OBJECT.replace("m_Name: A", "m_Name: A2")
        self.assertEqual(lost_changes(self.OBJECT, mine, theirs, mine.replace("m_Name: A", "m_Name: A2")), [])

    def test_one_object_one_sides_field_dropped(self):
        mine = self.OBJECT.replace("m_Layer: 0", "m_Layer: 5")
        theirs = self.OBJECT.replace("m_Name: A", "m_Name: A2")
        for merged in (theirs, mine):
            self.assertEqual(lost_changes(self.OBJECT, mine, theirs, merged), [self.BOTH])

    def test_one_object_one_field_two_ways(self):
        mine = self.OBJECT.replace("m_Layer: 0", "m_Layer: 5")
        theirs = self.OBJECT.replace("m_Layer: 0", "m_Layer: 6")
        self.assertEqual(lost_changes(self.OBJECT, mine, theirs, theirs), [self.BOTH])

    def test_one_object_the_same_change_on_both_sides(self):
        both = self.OBJECT.replace("m_Layer: 0", "m_Layer: 5").replace("m_IsActive: 1", "m_IsActive: 0")
        mine = both
        theirs = both.replace("m_Layer: 5\n", "m_Layer: 5\n  m_TagString: x\n")
        self.assertEqual(lost_changes(self.OBJECT, mine, theirs, theirs), [])

    def test_one_object_both_add_list_entries(self):
        base = "--- !u!1 &10\nGameObject:\n  m_Name: A\n  m_Component:\n  - component: {fileID: 1}\n"
        mine = base + "  - component: {fileID: 2}\n"
        theirs = base + "  - component: {fileID: 3}\n"
        self.assertEqual(lost_changes(base, mine, theirs, mine + "  - component: {fileID: 3}\n"), [])
        self.assertEqual(lost_changes(base, mine, theirs, mine), [self.BOTH])

    def test_one_object_both_changed_and_gone_from_the_result(self):
        mine = self.OBJECT.replace("m_Layer: 0", "m_Layer: 5")
        theirs = self.OBJECT.replace("m_Name: A", "m_Name: A2")
        self.assertEqual(len(lost_changes(self.OBJECT, mine, theirs, "%YAML 1.1\n")), 1)

    def test_a_dropped_stripped_flag_is_a_lost_change(self):
        base = "--- !u!4 &4\nTransform:\n  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
        stripped = base.replace("&4\n", "&4 stripped\n")
        moved = base.replace("x: 0", "x: 3")
        self.assertEqual(lost_changes(base, stripped, moved, moved), ["object 4: changed on both sides, and a change is not in the merged object"])
        self.assertEqual(lost_changes(base, stripped, base, base), ["object 4: one side's change is not in the merged file"])
        self.assertEqual(lost_changes(base, stripped, moved, stripped.replace("x: 0", "x: 3")), [])

    CHILDREN = "--- !u!4 &4\nTransform:\n  m_Children:\n  - {fileID: 10}\n  - {fileID: 11}\n  m_Father: {fileID: 0}\n"

    def test_a_reorder_lost_to_the_other_sides_addition(self):
        swapped = self.CHILDREN.replace("10}\n  - {fileID: 11}", "11}\n  - {fileID: 10}")
        added = self.CHILDREN.replace("11}\n", "11}\n  - {fileID: 12}\n")
        self.assertEqual(lost_changes(self.CHILDREN, swapped, added, added),
                         ["object 4: changed on both sides, and a change is not in the merged object"])
        both = swapped.replace("10}\n", "10}\n  - {fileID: 12}\n")
        self.assertEqual(lost_changes(self.CHILDREN, swapped, added, both), [])
        self.assertEqual(lost_changes(self.CHILDREN, added, swapped, both), [])

    def test_two_reorders_in_one_object_one_on_each_side(self):
        base = "--- !u!114 &50\nMonoBehaviour:\n  listA:\n  - 10\n  - 11\n  - 12\n  - 13\n  listB:\n  - 20\n  - 21\n"
        mine = base.replace("10\n  - 11", "11\n  - 10")
        theirs = base.replace("20\n  - 21", "21\n  - 20")
        both = mine.replace("20\n  - 21", "21\n  - 20")
        self.assertEqual(lost_changes(base, mine, theirs, both), [])
        self.assertEqual(lost_changes(base, mine, theirs, theirs),
                         ["object 50: changed on both sides, and a change is not in the merged object"])

    LIST = "--- !u!114 &50\nMonoBehaviour:\n  values:\n{}  m_Enabled: 1\n"

    def items(self, *values):
        return self.LIST.format("".join(f"  - {v}\n" for v in values))

    def test_a_move_among_repeated_lines_lost_to_the_other_sides_addition(self):
        base, mine, theirs = self.items(0, 1, 0), self.items(1, 0, 0), self.items(0, 1, 0, 2)
        self.assertEqual(len(lost_changes(base, mine, theirs, self.items(0, 1, 0, 2))), 1)
        self.assertEqual(lost_changes(base, mine, theirs, self.items(1, 0, 0, 2)), [])

    def test_two_edits_that_write_the_same_line(self):
        base, mine, theirs = self.items(0, 0, 0), self.items(1, 0, 0), self.items(0, 0, 1)
        self.assertEqual(len(lost_changes(base, mine, theirs, self.items(1, 0, 0))), 1)
        self.assertEqual(lost_changes(base, mine, theirs, self.items(1, 0, 1)), [])

    def test_a_swap_lost_to_the_other_side_rewriting_a_swapped_line(self):
        base, mine, theirs = self.items(10, 20, 30), self.items(20, 10, 30), self.items(99, 20, 30)
        self.assertEqual(len(lost_changes(base, mine, theirs, self.items(99, 20, 30))), 1)

    def test_two_slots_of_a_repeated_list_that_ends_the_object(self):
        def last(*values):
            return "--- !u!114 &50\nMonoBehaviour:\n  m_Enabled: 1\n  weights:\n" + "".join(f"  - {v}\n" for v in values)
        for n in (2, 4):
            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    zeros = [0] * n
                    mine, theirs, both = list(zeros), list(zeros), list(zeros)
                    mine[i] = both[i] = 5
                    theirs[j] = both[j] = 9
                    with self.subTest(n=n, i=i, j=j):
                        self.assertEqual(lost_changes(last(*zeros), last(*mine), last(*theirs), last(*both)), [])
                        self.assertEqual(lost_changes(last(*zeros), last(*theirs), last(*mine), last(*both)), [])

    def test_a_wrong_arrangement_of_a_list_that_ends_the_object(self):
        def last(*values):
            return "--- !u!114 &50\nMonoBehaviour:\n  m_Enabled: 1\n  refs:\n" + "".join(f"  - {v}\n" for v in values)
        self.assertEqual(len(lost_changes(last(0, 0), last(5, 0), last(0, 9), last(9, 5))), 1)
        self.assertEqual(len(lost_changes(last(0, 0, 0), last(5, 0, 0), last(0, 0, 9), last(0, 5, 9))), 1)
        # both sides wrote the same line in different slots: counting allows one, only the place shows two
        self.assertEqual(len(lost_changes(last(0, 0, 0, 0), last(1, 0, 0, 0), last(0, 0, 1, 0), last(1, 0, 0, 0))), 1)
        self.assertEqual(lost_changes(last(0, 0, 0, 0), last(1, 0, 0, 0), last(0, 0, 1, 0), last(1, 0, 1, 0)), [])

    def test_a_reading_that_makes_a_clash_does_not_excuse_a_loss(self):
        def last(*values):
            return "--- !u!114 &50\nMonoBehaviour:\n  m_Enabled: 1\n  refs:\n" + "".join(f"  - {v}\n" for v in values)
        # adjacent slots at the end of the object, one write lost or the two swapped
        self.assertEqual(len(lost_changes(last(0, 0, 0), last(5, 0, 0), last(0, 5, 0), last(5, 0, 0))), 1)
        self.assertEqual(lost_changes(last(0, 0, 0), last(5, 0, 0), last(0, 5, 0), last(5, 5, 0)), [])
        # `0, 1, 0` also reads as a `1` inserted after the first slot, beside the other side's
        # replace of it: UnityYAMLMerge puts such an insert on either side, so either order is kept
        self.assertEqual(lost_changes(last(0, 0, 0), last(5, 0, 0), last(0, 1, 0), last(1, 5, 0)), [])
        self.assertEqual(len(lost_changes(last(0, 0, 0), last(5, 0, 0), last(0, 1, 0), last(1, 0, 0))), 1)
        self.assertEqual(lost_changes(last(0, 0, 0), last(5, 0, 0), last(0, 1, 0), last(5, 1, 0)), [])
        self.assertEqual(len(lost_changes(last(0, 0, 0, 0), last(1, 0, 0, 0), last(0, 1, 0, 0), last(1, 0, 0, 0))), 1)

    def test_a_deletion_slid_onto_the_other_sides_replace_is_still_lost(self):
        def children(*ids):
            return "--- !u!4 &4\nTransform:\n  m_Children:\n" + "".join(f"  - {{fileID: {i}}}\n" for i in ids) + "  m_Father: {fileID: 0}\n"
        base, mine, theirs = children(0, 0, 0, 0), children(5, 0, 0, 0), children(0, 0, 0)
        self.assertEqual(len(lost_changes(base, mine, theirs, mine)), 1)
        self.assertEqual(lost_changes(base, mine, theirs, children(5, 0, 0)), [])

    def test_overlapping_edits_of_different_lengths_both_kept(self):
        def children(*ids):
            return "--- !u!4 &4\nTransform:\n  m_Children:\n" + "".join(f"  - {{fileID: {i}}}\n" for i in ids) + "  m_Father: {fileID: 0}\n"
        base = children(10, 11, 12, 13)
        self.assertEqual(lost_changes(base, children(12, 13), children(10, 13), children(13)), [])
        self.assertEqual(lost_changes(base, children(10, 12, 13), children(12, 13), children(12, 13)), [])
        self.assertEqual(lost_changes(base, children(10, 51, 12, 13), children(50, 51, 12, 13), children(50, 51, 12, 13)), [])
        self.assertEqual(len(lost_changes(base, children(12, 13), children(10, 13), children(12, 13))), 1)
        fields = "--- !u!1 &1\nGameObject:\n  m_Layer: 0\n  m_Name: A\n  m_TagString: Untagged\n"
        layer = fields.replace("m_Layer: 0", "m_Layer: 5")
        both = layer.replace("m_Name: A", "m_Name: B")
        self.assertEqual(lost_changes(fields, layer, both, both), [])
        self.assertEqual(len(lost_changes(fields, layer, both, layer)), 1)

    def test_far_apart_edits_must_be_where_each_side_made_them(self):
        base = self.items(1, 2, 3, 4, 5, 6, 7, 8)
        mine, theirs = self.items(2, 1, 3, 4, 5, 6, 7, 8), self.items(1, 2, 3, 4, 5, 6, 8, 7)
        self.assertEqual(lost_changes(base, mine, theirs, self.items(2, 1, 3, 4, 5, 6, 8, 7)), [])
        self.assertEqual(len(lost_changes(base, mine, theirs, self.items(2, 1, 3, 5, 4, 6, 8, 7))), 1)

    def test_an_object_both_sides_deleted_must_be_gone(self):
        deleted = self.BASE.split("--- !u!1 &20")[0]
        self.assertEqual(lost_changes(self.BASE, deleted, deleted, deleted), [])
        self.assertEqual(lost_changes(self.BASE, deleted, deleted, self.BASE), ["object 20 (B) deleted on both sides and still in the merged file"])

    def test_a_file_that_is_not_unity_yaml_is_compared_whole(self):
        # A text with no `--- !u!` object once had nothing to compare, so `d` passed though it holds
        # neither side's change. It is compared whole now, as written: this stops (safe; such a file reaches
        # `lost_changes` only after UnityYAMLMerge wrote it, and the common one is a `.meta`, on which the real tool
        # exits 1 and writes nothing today).
        self.assertEqual(lost_changes("a", "b", "c", "d"),
                         ["the file (it holds no object): changed on both sides, and a change is not in the merged file"])
        self.assertEqual(lost_changes("a", "a", "a", "a"), [])


class RestoreUnchanged(unittest.TestCase):
    TARGET = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: 'a [b]'\n--- !u!1 &20\nGameObject:\n  m_Name: C\n"

    def test_formatting_goes_back_and_real_changes_stay(self):
        merged = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: a [b]\n--- !u!1 &20\nGameObject:\n  m_Name: D\n--- !u!1 &30\nGameObject:\n  m_Name: E\n"
        self.assertEqual(restore_unchanged(self.TARGET, merged),
                         "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: 'a [b]'\n--- !u!1 &20\nGameObject:\n  m_Name: D\n--- !u!1 &30\nGameObject:\n  m_Name: E\n")

    def test_an_object_gone_from_the_merge_stays_gone(self):
        merged = "%YAML 1.1\n--- !u!1 &20\nGameObject:\n  m_Name: C\n"
        self.assertEqual(restore_unchanged(self.TARGET, merged), merged)

    def test_a_file_with_no_objects_is_left_alone(self):
        self.assertEqual(restore_unchanged("a: 1\n", "a: 2\n"), "a: 2\n")

    def test_a_change_to_spaces_inside_a_value_survives(self):
        base = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: Boss Room\n--- !u!1 &20\nGameObject:\n  m_Name: Other\n"
        mine = base.replace("m_Name: Other", "m_Name: Other2")
        theirs = base.replace("Boss Room", "BossRoom")
        merged = mine.replace("Boss Room", "BossRoom")
        self.assertEqual(restore_unchanged(mine, merged), merged)
        self.assertEqual(lost_changes(base, mine, theirs, merged), [])
        self.assertEqual(lost_changes(base, mine, theirs, mine), ["object 10 (Boss Room): one side's change is not in the merged file"])

    def test_a_change_to_a_quote_inside_a_value_survives(self):
        base = "--- !u!1 &10\nGameObject:\n  m_Name: 'it''s'\n"
        renamed = base.replace("it''s", "its")
        self.assertEqual(restore_unchanged(base, renamed), renamed)

    def test_an_empty_quoted_value_is_an_empty_value(self):
        self.assertEqual(lost_changes("--- !u!1 &1\na: ''\n", "--- !u!1 &1\na: ''\n", "--- !u!1 &1\na: 1\n", "--- !u!1 &1\na: 1\n"), [])
        self.assertEqual(restore_unchanged("--- !u!1 &1\na: ''\n", "--- !u!1 &1\na: \n"), "--- !u!1 &1\na: ''\n")

    def test_double_quotes_rewritten_as_plain_or_single_are_not_a_change(self):
        # research/05: UnityYAMLMerge rewrites some double-quoted values; that stopped a merge that lost nothing.
        base = '%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m_Text: "Boss \\"Room\\" \\u00e9"\n  m_Empty: ""\n'
        renamed = base.replace("m_Name: A", "m_Name: A2")
        for rewrite in ("m_Text: 'Boss \"Room\" \u00e9'", 'm_Text: Boss "Room" \u00e9'):
            with self.subTest(rewrite):
                merged = renamed.replace('m_Text: "Boss \\"Room\\" \\u00e9"', rewrite).replace('m_Empty: ""', "m_Empty:")
                self.assertEqual(lost_changes(base, renamed, base, merged), [])
                self.assertEqual(restore_unchanged(renamed, merged), renamed)  # the target's own text comes back

    def test_a_real_change_inside_double_quotes_still_stops(self):
        base = '%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m_Text: "Boss "\n'
        renamed = base.replace("m_Name: A", "m_Name: A2")
        for wrong in ("m_Text: Boss", "m_Text: 'Boss'", 'm_Text: "Bos "'):
            with self.subTest(wrong):
                self.assertEqual(lost_changes(base, renamed, base, renamed.replace('m_Text: "Boss "', wrong)),
                                 ["object 20: changed or removed in the merged file, though neither side changed it"])

    UNTOUCHED = ["object 20: changed or removed in the merged file, though neither side changed it"]

    def value_kept(self, before: str, after: str) -> list[str]:
        """lost_changes when one side renames object 10 and the merge writes object 20's value `before` as `after`."""
        base = f'%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m_Text: {before}\n  m_X: 1\n'
        renamed = base.replace("m_Name: A", "m_Name: A2")
        return lost_changes(base, renamed, base, renamed.replace(f"m_Text: {before}\n", f"m_Text: {after}\n"))

    def test_the_same_text_written_another_way_passes(self):
        for before, after in (('"a\\x01"', '"a\\u0001"'), ('"\\t"', "'\t'"), ('"\\/"', "/"), ("'it''s'", '"it\'s"'),
                              ('"abc\\\n    def"', "abcdef"), ('"abc\\ \n    def"', '"abc  def"'), ('"abc\\\t\n    def"', '"abc\\t def"'),
                              ("'Boss\n\n    Room'", '"Boss\\nRoom"'),
                              ('"a\n    b\\ \n    c"', '"a b  c"'), ("a\\ ", "'a\\'"), ("'a\\ \n    b'", "'a\\ b'")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), [])

    def test_an_escaped_break_then_blank_lines_folds_as_yaml_does(self):
        # YAML 1.2, 7.3.1 (s-double-escaped): inside double quotes `\` and the line break
        # are nothing, and each empty line after them is one line feed (l-empty: a line of only spaces or tabs
        # is empty too). Before, a blank line after the `\` kept the `\` and the value stayed raw.
        for before, after in (('"a\\\n    b"', '"ab"'),                   # no blank line (as before)
                              ('"a\\\n\n    b"', '"a\\nb"'),               # one
                              ('"a\\\n\n\n    b"', '"a\\n\\nb"'),          # two
                              ('"a\\\n   \n    b"', '"a\\nb"'),            # one, of spaces
                              ('"a\\\n \t \n    b"', '"a\\nb"'),           # one, of spaces and a tab
                              ('"a\\\n\n    b"', '"a\n\n    b"'),          # the same text with no `\`
                              ('"a\\ \n\n    b"', '"a \\nb"'),             # an escaped space, then a blank line
                              ('"a\\\n\n    b\\\n    c"', '"a\\nbc"'),     # a second escaped break after it
                              ('"a\\\n\n    "', '"a\\n"')):                # the closing quote after the blank line
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), [])

    def test_a_flow_sequence_of_texts_written_two_ways_is_one_value(self):
        # An escaped break inside a flow joined right already; what stopped is that a
        # `[...]` was never read as texts, so its items written two ways stayed two. Each item is now read
        # as a whole value is (`_value_text`).
        for before, after in (('[a, "b"]', '[a, b]'),
                              ("[a, 'b']", '[a, "b"]'),
                              ('[a, "b\\x63"]', '[a, bc]'),
                              ('[a, "b\\\n    c"]', '[a, bc]'),           # an escaped break, against plain
                              ('[a, "b\\\n\n    c"]', '[a, "b\\nc"]'),    # case 1 inside a flow
                              ('[a,b]', '[ a , b ]'),
                              ('[a,\n    b]', '[a, b]'),                  # over two lines
                              ('[a,\n\n    b]', '[a, b]'),                # a blank line between items is space
                              ('[a, "b,c"]', "[a, 'b,c']"),               # a comma inside quotes
                              ('[a, "b]"]', "[a, 'b]']"),                 # a bracket inside quotes
                              ("[1, '~', true]", '["1", ~, "true"]'),     # values are characters, as whole values are
                              ('[]', '[ ]'),
                              ("['']", '[""]'),
                              # On purpose: a glued `:` before a quote is text, so `a:"b\ c"`
                              # is one plain item either way. ruamel and PyYAML read both sides as that one
                              # item; older libyaml rejects the form; no reader reads a pair. The base stopped it.
                              ('[a:"b\\ \n    c"]', '[a:"b\\ c"]')):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), [])

    def test_a_flow_that_is_not_surely_texts_stays_raw(self):
        # What a flow holds that is not surely a text keeps the whole value as
        # written, so the same thing written another way stops (safe: a person merges).
        for before, after in (('{a: b}', "{a: 'b'}"),                     # a mapping
                              ('{fileID: 0}', "{fileID: '0'}"),
                              ('[a, [b]]', "[a, ['b']]"),                 # a nested sequence
                              ('[a, {b: c}]', "[a, {b: 'c'}]"),
                              ('[a, b,]', '[a, b]'),                      # a trailing comma
                              ('[a, , b]', "[a, '', b]"),                 # an empty item
                              ('[&x a, b]', '[a, b]'),                    # an anchor
                              ('[*x, b]', "['*x', b]"),                   # an alias
                              ('[!!str a]', '[a]'),                       # a tag
                              ('[a, b] # c', "[a, 'b'] # c"),             # a comment after it
                              ('[a #c, b]', "[a, 'b']"),                  # a comment inside it
                              ('[a: b]', "[a: 'b']"),                     # a pair: a mapping in the sequence
                              ('["a": b]', '[a: b]'),
                              ('[a\n\n    b]', '["a\\nb"]'),              # a plain item over a blank line
                              ('[a\tb]', '["a\\tb"]'),                    # a tab in a plain item: readers differ
                              ('[:a]', '[":a"]')):                        # and on `:` first (libyaml, ruamel)
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), self.UNTOUCHED)

    def line_kept(self, before: str, after: str) -> list[str]:
        """lost_changes when one side renames object 10 and the merge writes a line of object 20, `before`, as `after`."""
        base = f'%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m:\n  {before}\n  m_X: 1\n'
        renamed = base.replace("m_Name: A", "m_Name: A2")
        return lost_changes(base, renamed, base, renamed.replace(f"\n  {before}\n", f"\n  {after}\n"))

    def test_a_list_item_whose_quoted_value_holds_a_colon(self):
        # `- 'a: b'` is a list item holding the text `a: b`; `_ENTRY` read `- 'a` as a key.
        for before, after in (("- 'a: b'", '- "a: b"'), ("- 'a: b'", '- "a\\x3a b"'), ('- "a: b"', "- 'a: b'"),
                              ("- 'a: #b'", '- "a: #b"'),
                              ("- 'a: b': 'c'", "- 'a: b': c")):          # a quoted key holding `: ` is a key
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), [])
        for before, after in (("- 'a: b'", "- a: b"),                     # a text, and a mapping
                              ("- 'a: b'", "- a: 'b'"), ("- 'a: b'", '- "a": b'), ("- 'a: b'", "- 'a: b '"),
                              ("- 'a: b'", "- - 'a: b'"),                 # a text, and a list holding it
                              ("- {a: b}", "- {a: 'b'}")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)

    def test_lines_the_list_item_change_must_not_break(self):
        # Each line `_ENTRY` read right before still reads the same.
        for before, after in (("- key: 'value'", "- key: value"),         # a key in a list item
                              ("-a: '1'", "-a: 1"),                       # a key that starts with `-`
                              ("- -a: '1'", "- -a: 1"),
                              ("- - a: 'b'", "- - a: b"),                 # a key in a nested list item
                              ("- '-1'", "- -1"),                         # a dash with no space is text
                              ('- "a": \'b\'', '- "a": b'),               # a quoted key in a list item
                              ("- 'a#': 'b'", "- 'a#': b")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), [])
        for before, after in (("- - a", "- - 'a'"),                       # a nested list item stays raw, as before
                              ("- a: b", "- a: c"), ("-a: 1", "-a: 2"), ('- "a": b', '- "b": b')):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)
        # Lines the base read right that the first `_ENTRY` change left raw.
        for before, after in (("-  k: 'v'", "-  k: v"), ("- -  a: 'b'", "- -  a: b"), ("-   - a: 'b'", "-   - a: b")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), [])
        # The empty key is in doubt now. Under `%YAML 1.1`, as every Unity file starts,
        # ruamel rejects it, and its tail was not read whole; no Unity file holds one. So this pair, which
        # passed before, stops: a false stop.
        self.assertEqual(self.line_kept("- : 'a'", "- : a"), self.UNTOUCHED)

    def test_the_quotes_of_a_key_holding_a_colon_or_after_a_wide_dash_are_seen(self):
        # `_KEY` did not take a key holding `:` (`a:b`),
        # nor `_LINE` a dash followed by two spaces, so the double quotes after them were not seen and an
        # escaped space ending the line was dropped: YAML's `x  y` read as `xy`.
        for before, after in (('a:b: "x\\ \n    y"', 'a:b: "x\\\n    y"'),
                              ('- a:b: "x\\ \n    y"', '- a:b: "x\\\n    y"'),
                              ('-  k: "x\\ \n    y"', '-  k: "x\\\n    y"'),
                              ('a:b: "x\\\n    y"', 'a:b: "x\\ y"'),   # and an escaped break, `xy` against `x y`
                              ('-  k: "x\\\n    y"', '-  k: "x\\ y"'),
                              # With these keys read, a `,#` comment in their flow must end at its line.
                              ('- a:b: [a,## c\n    "b\\\n    d"]', '- a:b: [a,## c\n    "b\\ \n    d"]'),
                              ('-  k: [a,#\n    "b\\\n    c"]', '-  k: [a,#\n    "b\\ \n    c"]')):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)

    def test_different_texts_never_read_as_one(self):
        # Each of these read as one value at some point.
        for before, after in (
            ('"\\n"', '"\\"\\\\n\\""'),            # a line break, and the text `"\n"`
            ('"\\uD83D"', '"\\"\\\\uD83D\\""'),    # an escape YAML has not, and the text that spells it
            ("'\"\\n\"'", '"\\n"'),                # the text `"\n"` single-quoted, and a line break
            ('"a\\x01"', "'a\\x01'"),             # a control character, and the text `a\x01`
            ('"abc\\ \n    def"', "abcdef"),       # an escaped space at a fold is a space
            ('"abc\\\t\n    def"', "abcdef"),      # and so is an escaped tab
            ('" x"', " x"),                        # plain text loses its leading space
            ('"a #b"', "a #b"),                    # plain ` #` starts a comment
            ('"{fileID: 0}"', "{fileID: 0}"),      # a text, and a mapping
            ('"- x"', "- x"),                      # a text, and a list
            ('"Boss "', "Boss"),
            # Space after a `\` at a line's end is a space only inside double quotes.
            ("'a\\ '", "a\\ "),                    # plain `a\ ` is the text `a\`
            ("'a\\\t'", "a\\\t"),                  # and with a tab
            ("'a\\ \n    b'", "'a\\  b'"),          # a folded single-quoted `a\ b`
            ("a\\ \n    b", "a\\  b"),              # a folded plain `a\ b`
            ('"a\n    b\\ \n    c"', '"a bc"'),     # an escaped space ending a middle line of double quotes
            ('"a\n    b\\\t\n    c"', '"a bc"'),    # and an escaped tab
            ('{k: "a\n    b\\ \n    c"}', '{k: "a bc"}'),
            # An escaped break then blank lines is one line feed a blank line, no more.
            ('"a\\\n\n    b"', '"a b"'),
            ('"a\\\n\n    b"', '"ab"'),
            ('"a\\\n\n    b"', '"a\\n b"'),
            ('"a\\\n\n    b"', '"a\\n\\nb"'),         # one blank line, and two line feeds
            ('"a\\\n\n\n    b"', '"a\\nb"'),          # two blank lines, and one line feed
            ('"a\\ \n\n    b"', '"a\\nb"'),           # the escaped space stays
            ("'a\\\n\n    b'", '"a\\nb"'),            # in single quotes a `\` is text
            ('"a\\\\\n\n    b"', '"a\\nb"'),          # an even number of `\` escapes no break
            # A flow sequence of texts.
            ('[a, "b c"]', '[a, bc]'),
            ('[a, b]', '[a]'),
            ('[a, b]', '[b, a]'),
            ('[a, "b,c"]', '[a, b, c]'),
            ('[a, "[b]"]', '[a, [b]]'),
            ("[a, '']", '[a, ]'),
            ("[a, '']", "[a, ' ']"),
            ('[a, " b"]', '[a, b]'),
            ('[a, "b "]', '[a, b]'),
            ("[a, 'x: y']", '[a, x: y]'),
            ('[]', "['']"),
            ("['a, b']", '[a, b]'),                    # one text holding a comma, and two
            ("'[a, b]'", '[a, b]'),                    # the text `[a, b]`, and a sequence
            ('"[\\"a\\"]"', '["a"]'),                  # the text `["a"]`, and a sequence
            ('[a,  b]', '[a, b]'),                # a no-break space is text, not space YAML skips
            ('[a , b]', '[a, b]'),
            ('[a, "b\\\n\n    c"]', '[a, "b c"]'),
            # The text `- a: b`, and a list item (more in
            # test_a_list_item_whose_quoted_value_holds_a_colon, whose lines are list items, not `m_Text:` values).
            ("'- a: b'", "- a: b"),
            # A plain flow item starting with `?` or `:` is in doubt (libyaml and
            # ruamel read `[?a]` as a pair), so it stays as written.
            ('[?a]', '["?a"]'),
            ('[a, ?x]', '[a, "?x"]'),
            ('[:a]', '[":a"]'),
            # In a flow a `:` with no space after it is text, so `a:"b\` is a plain item and its `\` is text.
            ('[a:"b\\\n\n    c]', '[a:"b\n\n    c]'),
            ('[a:"b\\\n    c]', '[a:"bc]'),                 # the base read these as one
            ('[a:"b\\ \n    c]', '[a:"b\\  c]'),
            ('[x, a:"b\\\n    c]', '[x, a:"bc]'),
            ('{a:"b\\\n    c}', '{a:"bc}'),
            ('[a:"b\\\n    c"]', '["a:\\"bc\\""]'),
            ('[a"b]', "['a\"b']"),                       # a plain item holding a quote stays as written
            # U+0085, U+2028 and U+2029 are line breaks to YAML 1.1 and characters to 1.2: kept as written.
            ('[a, b\x85]', '[a, "b\\u0085"]'),
            ('[a, "\\N"]', '[a, "\x85"]'),
            ("'a\x85b'", '"a\\u0085b"'),                   # the base read these as one
            ('a\u2028', '"a\\u2028"'),
            ('"a\u2029b"', '"a\\u2029b"'),
            # A comment inside a flow ends at its line's end: what follows is a value again.
            ('[a, # c\n    "b\\\n    d"]', '[a, # c\n    "b\\ d"]'),   # the base read these as one
            # A quote straight after a `:` at a flow item's start, or after space,
            # is read in different ways (ruamel: `{k :"b"}` a pair, `[a :"b"]` one plain item), so the object
            # is compared as written: `bc` against `b  c`, and `a :"b\ c"` against `a :"bc"`.
            ('{:"b\\\n    c"}', '{:"b\\ \n    c"}'),
            ('{ :"b\\\n    c"}', '{ :"b\\ \n    c"}'),
            ('{a: 1, :"b\\\n    c"}', '{a: 1, :"b\\ \n    c"}'),
            ('{"k",:"b\\\n    c"}', '{"k",:"b\\ \n    c"}'),
            ('{k\t:"b\\\n    c"}', '{k\t:"b\\ \n    c"}'),
            ('[a :"b\\\n    c"]', '[a :"b\\ \n    c"]'),
            ('[a :"b\\\n    c"]', '[a :"bc"]'),
            # A `:` straight after `?` is not glued to plain text (ruamel reads `{?:"b"}`
            # as a pair), and in a flow a quote straight after `?` is an explicit key: both in doubt.
            ('{?:"b\\\n    c"}', '{?:"b\\ \n    c"}'),
            ('{ ?:"b\\\n    c"}', '{ ?:"b\\ \n    c"}'),
            ('{a, ?:"b\\\n    c"}', '{a, ?:"b\\ \n    c"}'),
            ('{"k", ?:"b\\\n    c"}', '{"k", ?:"b\\ \n    c"}'),
            ('[{?:"b\\\n    c"}]', '[{?:"b\\ \n    c"}]'),
            ('[? "b\\\n    d"]', '[? "b\\ \n    d"]'),
            ('[a, ? "b\\\n    d"]', '[a, ? "b\\ \n    d"]'),
            ('{? "b\\\n    d"}', '{? "b\\ \n    d"}'),
            ('[a,? # c\n    "b\\\n    d"]', '[a,? # c\n    "b\\ \n    d"]'),
            # A `#` straight after `,` starts a comment to ruamel and libyaml.
            ('[a,# c\n    "b\\\n    d"]', '[a,# c\n    "b\\ \n    d"]'),   # the base read these as one
            ('[a,#\n    "b\\\n    d"]', '[a,#\n    "b\\ \n    d"]'),
            # A lone CR is a line break to YAML 1.1 and 1.2: an object holding one is kept as written.
            ('[a, b\rc]', '[a, "b\\rc"]'),
            ('[a, "b\rc"]', '[a, "b\\rc"]'),
            ('"a\rb"', '"a\\rb"'),                         # the base read these whole values as one
            ("'a\rb'", '"a\\rb"'),
            ('a\rb', '"a\\rb"'),
            # A no-break space at a line's end is text, not space YAML trims.
            ("a ", "a"),
            ('"a \n    b"', '"a b"'),
        ):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), self.UNTOUCHED)
        # As lines of an object (a list item, a key): the same two rules.
        for before, after in (('- {?:"b\\\n    c"}', '- {?:"b\\ \n    c"}'),
                              ('k: {?:"b\\\n    c"}', 'k: {?:"b\\ \n    c"}'),
                              ('-  k: [a,? # c\n    "b\\\n    d"]', '-  k: [a,? # c\n    "b\\ \n    d"]')):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)
        # A double quote the check did not see open, so an escaped space ending its line was dropped
        # (YAML's `ab` against `a  b`). Each form, as a value of `m_Text:` or as a line.
        twin = ('"a\\\n    b"', '"a\\ \n    b"')
        for before, after in (
            # an anchor or a tag before the quote, as a whole value and in a flow
            ("&x " + twin[0], "&x " + twin[1]),
            ("!!str " + twin[0], "!!str " + twin[1]),
            ("[a, &x " + twin[0] + "]", "[a, &x " + twin[1] + "]"),
            ('{"a": &x ' + twin[0] + "}", '{"a": &x ' + twin[1] + "}"),
            ("[a, !!str " + twin[0] + "]", "[a, !!str " + twin[1] + "]"),
            ("[! " + twin[0] + "]", "[! " + twin[1] + "]"),           # an empty tag
            ("{a: !t " + twin[0] + "}", "{a: !t " + twin[1] + "}"),
            ("&x {a: 1, " + twin[0] + "}", "&x {a: 1, " + twin[1] + "}"),  # `_KEY` read `&x {a` as a key before
            # A mark straight after a `:` that is not glued, and a quote read as text taken for a closing one
            ('{? :#\n    "b\\\n    c"}', '{? :#\n    "b\\ \n    c"}'),
            ('{a, ? :# c\n    "b\\\n    c"}', '{a, ? :# c\n    "b\\ \n    c"}'),
            ('{-k":"b\\ \n    c"}', '{-k":"b\\\n \\  c"}'),
            # A `#` straight after `?` in a flow is a comment to ruamel
            ('[a, ?# c\n    ' + twin[0] + "]", '[a, ?# c\n    ' + twin[1] + "]"),
        ):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), self.UNTOUCHED)
        for before, after in (
            # in a block
            ("k: &x " + twin[0], "k: &x " + twin[1]),
            ("k: !!str " + twin[0], "k: !!str " + twin[1]),
            ("- &x " + twin[0], "- &x " + twin[1]),
            ("- !!str " + twin[0], "- !!str " + twin[1]),
            ("k: &x !t " + twin[0], "k: &x !t " + twin[1]),
            # an explicit key
            ("? " + twin[0], "? " + twin[1]),
            ("? k\n  : " + twin[0], "? k\n  : " + twin[1]),
            # a key starting with `?` or `:` after a dash
            ("- ?a: " + twin[0], "- ?a: " + twin[1]),
            ("- :a: " + twin[0], "- :a: " + twin[1]),
            # a quoted key with a space before its `:`
            ('- "k" : ' + twin[0], '- "k" : ' + twin[1]),
            # an empty key after a wide dash
            ("-  : " + twin[0], "-  : " + twin[1]),
            # a flow key, a quoted key with a space before its `:`, a key starting with `:`, an empty key
            ("[a]: " + twin[0], "[a]: " + twin[1]),
            ("[a] : " + twin[0], "[a] : " + twin[1]),
            ('"k" : ' + twin[0], '"k" : ' + twin[1]),
            ("'k' : " + twin[0], "'k' : " + twin[1]),
            (":a: " + twin[0], ":a: " + twin[1]),
            (": " + twin[0], ": " + twin[1]),
            ("- : " + twin[0], "- : " + twin[1]),
            ("-: " + twin[0], "-: " + twin[1]),                     # a key `-`: found by the sweep
            # A bracket inside the quotes after a key the check does not read; two spaces before a property or a
            # comment; a key after the empty key, its flow continued deeper
            ('- "k" : "{a\\\n    b"', '- "k" : "{a\\ \n    b"'),
            ('- "k" : "a]\\\n    b"', '- "k" : "a]\\ \n    b"'),
            ("k:  &x " + twin[0], "k:  &x " + twin[1]),
            ("k:  #\n    a", "k:  # a"),
            ('- : k: [a,\n      "a\\\n      b"]', '- : k: [a,\n      "a\\ \n      b"]'),
            ("k: #\n    a", "k: # a"),                               # a comment, then the value below it
            # A `#` straight after `?`, as a line
            ("- a:b: [a, ?# c\n    " + twin[0] + "]", "- a:b: [a, ?# c\n    " + twin[1] + "]"),
            # Each read as one by the base: a `#` straight after a
            # closed key's `:` in a flow (a comment to ruamel), a property then a comment, a `|` header with a
            # comment, `?` with a comment, the keys `-` and `:`
            ('k: {"k":# c\n    ' + twin[0] + "}", 'k: {"k":# c\n    ' + twin[1] + "}"),
            ("k: &x #\n    " + twin[0], "k: &x #\n    " + twin[1]),
            ("k: &x #\n    a", "k: &x # a"),
            ("k: | #\n    " + twin[0], "k: | #\n    " + twin[1]),
            ("k: |2 #\n    " + twin[0], "k: |2 #\n    " + twin[1]),
            ("? # c\n    " + twin[0], "? # c\n    " + twin[1]),
            ("- -: " + twin[0], "- -: " + twin[1]),
            (":: " + twin[0], ":: " + twin[1]),
            ("- :: " + twin[0], "- :: " + twin[1]),
            # Found beside them by this build: a `|` or `>` text keeps the spaces and tabs ending its lines, and
            # `- |` holds the lines deeper than its dash
            ("k: |\n    a \n    b", "k: |\n    a\n    b"),
            ("k: >\n    a \n    b", "k: >\n    a\n    b"),
            ("k: |\n    a\t\n    b", "k: |\n    a\n    b"),
            ("k: |\n    a\n      \n    b", "k: |\n    a\n\n    b"),
            ("- |\n    a \n    b", "- |\n    a\n    b"),
            ("k:  |\n    a\n    b", "k:  |\n    a b"),
            # Each read as one by the base. A `|` or `>` header alone on its line
            ("k:\n    |\n    a \n    b", "k:\n    |\n    a\n    b"),
            ("k:\n    |\n    a\n      b", "k:\n    |\n    a b"),
            ("k:\n    |\n    - 'b'", "k:\n    |\n    - b"),
            ("k:\n    >\n    a \n    b", "k:\n    >\n    a\n    b"),
            ("-\n    |\n    k: 'b'", "-\n    |\n    k: b"),
            # What follows the empty key
            (": - - x\n      - a", ": - - x\n            - a"),
            ('- : - [\n      "b\\\n      c"]', '- : - [\n      "b\\ \n      c"]'),
            ("- : [a]: " + twin[0], "- : [a]: " + twin[1]),
            ("- : - " + twin[0], "- : - " + twin[1]),
            # A space YAML reads as text, which Python's `\s` and `strip()` read as space
            ("\xa0': " + twin[0], "\xa0': " + twin[1]),
            ("\xa0'k': x", "\xa0k: x"),
            ('- \xa0"b\n  - c"', '- \xa0"b - c"'),
            ('- \u3000"b\n  - c"', '- \u3000"b - c"'),
            ('- \u2003"b\n  - c"', '- \u2003"b - c"'),
            # A line starting with `|` or `>`, read as a key
            ("- | # c:\n    a \n    b", "- | # c:\n    a\n    b"),
            ("- > # c:\n    a \n    b", "- > # c:\n    a\n    b"),
            # A tail after a closed node that leaves a bracket open
            ('- x\n    "b" [c\n  - d]', '- x\n    "b" [c - d]'),
            ("- x\n    [part.03]#7/Part {column\n  - d}", "- x\n    [part.03]#7/Part {column - d}"),
            # A plain text's next line at the text's own column, which YAML reads as the same text
            ("- x\n    - 'b'", "- x\n    - b"),
            ('- x\n    "b\n  - c"', '- x\n    "b - c"'),
            ("- x\n    [b\n  n:\n    o: 1\n  p: 2]", "- x\n    [b\n  n:\n  o: 1\n  p: 2]"),
            ('- x\n    - "b"', "- x\n    - b"),                 # YAML: `x - "b"` against `x - b`; a dash's line is rewritten
            ("k:\n    x\n    - 'b'", "k:\n    x\n    - b"),
            # A comment after a closed value, at depth 0
            ("k: {fileID: 0} # [\n  j: 1", "k: {fileID: 0} # [ j: 1"),
            ("- 'a' # [\n  - b", "- 'a' # [ - b"),
        ):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)
        for before, after in (('{"k":# c\n    ' + twin[0] + "}", '{"k":# c\n    ' + twin[1] + "}"),
                              ("{'k':#\n    " + twin[0] + "}", "{'k':#\n    " + twin[1] + "}"),
                              ("{[a]:#\n    " + twin[0] + "}", "{[a]:#\n    " + twin[1] + "}"),
                              ("| #\n    " + twin[0], "| #\n    " + twin[1]),
                              ("|\n    a \n    b", "|\n    a\n    b"),
                              # The header on the line below `m_Text:`
                              ("\n    |\n    a \n    b", "\n    |\n    a\n    b"),
                              # A property after `?` in a flow (red with `"?"` taken out of `_scan`)
                              ("[? &x " + twin[0] + "]", "[? &x " + twin[1] + "]"),
                              ("{? !t " + twin[0] + "}", "{? !t " + twin[1] + "}"),
                              ("[a, ? &x " + twin[0] + "]", "[a, ? &x " + twin[1] + "]"),
                              ("[?&x " + twin[0] + "]", "[?&x " + twin[1] + "]")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.value_kept(before, after), self.UNTOUCHED)

    def test_a_plain_texts_next_lines_below_a_dash_alone(self):
        # A dash alone as a plain text's next line, with a deeper line below it, was kept and
        # the deeper line joined to it and rewritten; and a text below a dash alone went on one column later than
        # YAML reads it (past the dash). Each pair: YAML reads two texts, the base read one.
        for before, after in (("- x\n    -\n      'b'", "- x\n    -\n      b"),
                              ("- x\n    -\n      ''", '- x\n    -\n      ""'),
                              ("- x\n    -\n      'a  b'", '- x\n    -\n      "a  b"'),
                              ("- x\n    -\n      [a, 'b']", "- x\n    -\n      [a, b]"),
                              ("k:\n    x\n    -\n      'b'", "k:\n    x\n    -\n      b"),
                              ("- x\n    y\n      - 'b'", "- x\n    y\n      - b"),
                              ("-\n    x\n   - 'b'", "-\n    x\n   - b"),
                              ('-\n    x\n   "b\n  - c"', '-\n    x\n   "b - c"'),
                              ("-\n   x\n   - 'b'", "-\n   x\n   - b"),
                              ("- -\n      x\n     - 'b'", "- -\n      x\n     - b"),
                              ("- k:\n    -\n      x\n     - 'b'", "- k:\n    -\n      x\n     - b")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)

    def body_kept(self, before: str, after: str) -> list[str]:
        """lost_changes when one side renames object 10 and the merge writes object 20's body (all below its
        `--- !u!114 &20` line) `before` as `after`."""
        base = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\n" + before
        renamed = base.replace("m_Name: A", "m_Name: A2")
        return lost_changes(base, renamed, base, renamed[:len(renamed) - len(before)] + after)

    def test_a_document_marker_or_a_root_that_is_no_mapping_stays_raw(self):
        # A `---` or `...` line inside an object starts a YAML document there; an object
        # whose root is a list or a text, not a mapping at column 0, was joined to its `--- !u!` line; an
        # object's line holding more than Unity writes is kept as written too.
        twin = ('"b\\\n    c"', '"b\\ \n    c"')
        head = "MonoBehaviour:\n  m_X: 1\n"
        for before, after in ((head + "--- " + twin[0] + "\n", head + "--- " + twin[1] + "\n"),
                              (head + "--- &x " + twin[0] + "\n", head + "--- &x " + twin[1] + "\n"),
                              (head + "--- [\n    " + twin[0] + "]\n", head + "--- [\n    " + twin[1] + "]\n"),
                              (head + "...\n--- " + twin[0] + "\n", head + "...\n--- " + twin[1] + "\n"),
                              (' - "b\\ \n   c"\n', ' - "b\\\n   c"\n'),
                              (" - b\n - b\n", " - b\n  - b\n"),
                              ("x\n- 'b'\n", "x\n- b\n")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.body_kept(before, after), self.UNTOUCHED)
        base = "%YAML 1.1\n--- !u!114 &20 x\n- 'b'\n"
        self.assertEqual(lost_changes(base, base, base, base.replace("'b'", "b")), self.UNTOUCHED)

    def test_every_space_python_reads_and_yaml_does_not_keeps_the_object_raw(self):
        # The rule keeps an object raw for each character `str.isspace()` takes but space,
        # tab and LF. Each here, in the shape `- <c>"b` / `- c"`.
        spaces = [chr(i) for i in range(0x110000) if chr(i).isspace() and chr(i) not in " \t\n"]
        self.assertGreater(len(spaces), 20)
        for c in spaces:
            with self.subTest(space=hex(ord(c))):
                self.assertEqual(self.line_kept('- ' + c + '"b\n  - c"', '- ' + c + '"b - c"'), self.UNTOUCHED)

    def test_the_text_before_the_first_object_is_compared(self):
        # `_documents` reads only the objects, so a side's change before the first
        # `--- !u!` line (a document added, the `%TAG` line changed, a comment added) was lost with no stop.
        head = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        base = head + "--- !u!1 &10\nGameObject:\n  m_Name: A\n"
        mine = base.replace("m_Name: A", "m_Name: B")
        for name, theirs in (("a document added", base.replace("--- !u!1", '--- "lost text"\n--- !u!1')),
                             ("the %TAG line changed", base.replace("unity3d.com,2011", "example.com,2099")),
                             ("a comment line added", base.replace("--- !u!1", "# kept?\n--- !u!1"))):
            with self.subTest(name):
                self.assertEqual(lost_changes(base, mine, theirs, mine),
                                 ["the text before the first object: one side's change is not in the merged file"])
                # the merge that keeps it passes
                self.assertEqual(lost_changes(base, mine, theirs, theirs.replace("m_Name: A", "m_Name: B")), [])
        tag = base.replace("unity3d.com,2011", "example.com,2099")
        both = tag.replace("m_Name: A", "m_Name: B")
        self.assertEqual(lost_changes(base, both, tag, both), [])          # the same change on both sides, kept
        self.assertEqual(lost_changes(base, both, tag, mine),              # ... and lost
                         ["the text before the first object: both sides' change is not in the merged file"])
        self.assertEqual(lost_changes(base, mine, base, tag.replace("m_Name: A", "m_Name: B")),  # neither changed it
                         ["the text before the first object: changed in the merged file, though neither side changed it"])
        crlf = base.replace("\n", "\r\n")                                   # line ends alone are no change
        self.assertEqual(lost_changes(base, mine, crlf, mine), [])
        # Two different changes: the merge that took one side's head stops; the
        # merge that has both passes, counted line by line as an object's are (`_both_kept`).
        ours = base.replace("--- !u!1", "# ours\n--- !u!1").replace("m_Name: A", "m_Name: B")
        theirs = base.replace("unity3d.com,2011", "example.com,2099")
        msg = ["the text before the first object: changed on both sides, and a change is not in the merged file"]
        self.assertEqual(lost_changes(base, ours, theirs, ours), msg)
        self.assertEqual(lost_changes(base, ours, theirs, ours.replace("unity3d.com,2011", "example.com,2099")), [])

    def test_a_file_with_no_object_is_all_head(self):
        # A text with no `--- !u!` object is compared whole, as written, line by line: two different changes stop
        # unless the merged text has both (a false stop at worst when it reads them another way: safe), one change
        # or the same change kept passes.
        base, mine, theirs = "a: 1\nb: 1\n", "a: 2\nb: 1\n", "a: 1\nb: 2\n"
        self.assertEqual(lost_changes(base, mine, theirs, "a: 2\nb: 2\n"), [])
        self.assertEqual(lost_changes(base, mine, theirs, mine),
                         ["the file (it holds no object): changed on both sides, and a change is not in the merged file"])
        self.assertEqual(lost_changes(base, mine, base, mine), [])
        self.assertEqual(lost_changes(base, mine, mine, mine), [])
        self.assertEqual(lost_changes(base, mine, base, base),
                         ["the file (it holds no object): one side's change is not in the merged file"])

    def test_a_kept_end_of_a_block_text_at_the_objects_end(self):
        # A `|+` or `>+` text keeps its last blank lines (YAML: `a\n\n` against `a\n`). At an
        # object's end they were stripped, as an object's last blank lines are. A block text is in doubt now, and an
        # object kept as written keeps its last blank lines.
        for header in ("|+", ">+"):
            with self.subTest(header=header):
                base = ("%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m_X: 1\n"
                        f"  m: {header}\n    a\n\n")
                renamed = base.replace("m_Name: A", "m_Name: A2")
                self.assertEqual(lost_changes(base, renamed, base, renamed[:-1]), self.UNTOUCHED)

    def test_a_colon_before_a_quote_is_glued_only_after_a_named_mark(self):
        # In a flow a quote straight after a `:` is text only when the `:` is glued
        # to a character `str.isalnum()` accepts, or `_`; it opens after a closing quote, `]` or `}`; after any other
        # mark it is in doubt. Before, every mark not listed made the `:` glued (`?` too).
        # No pair of the sweep is read as two by ruamel after such a mark, so this is the test of that branch.
        from upe.merge import _Doubt, _scan

        def read(value):
            try:
                return _scan(value)[0]
            except _Doubt:
                return "doubt"
        for c in DifferentTextsSweep.ALPHABET + ".-+=~$^()é1Z_\x0c\xa0":
            with self.subTest(mark=c):
                if c in "'\"]}":
                    continue  # a closing mark; for `'` and `"` a quote of its own is open first
                # `a1:"b` is one plain item, the quote text; after any other mark, in doubt
                self.assertEqual(read('[a' + c + ':"b'), None if c.isalnum() or c == "_" else "doubt")
        self.assertEqual(read('{"a":"b'), '"')                  # after a closing quote the quote opens
        self.assertEqual(read('[[a]:"b'), '"')

    def test_a_mark_straight_after_a_colon_that_is_not_glued_is_in_doubt(self):
        # In a flow, after a `:` at an item's start, after space or after `?` (an "other" `:`), readers
        # differ on what follows. Its pair (`{? :#`) is now caught first by the `#` rule, so no pair of the sweep
        # reaches this branch (the mutation harness found it live): this is its test.
        from upe.merge import _Doubt, _scan
        for opening in ("{? :", "[:", "{a: 1, :", "{k :"):
            for c in "a-[*%`,?":
                with self.subTest(value=opening + c), self.assertRaises(_Doubt):
                    _scan(opening + c)
        for value in ('{"a":b', "[a:b", "[[a]:b"):  # a closed or a glued `:`: read
            with self.subTest(value=value):
                _scan(value)

    def test_the_odd_breaks_are_yaml_1_1_b_char_without_lf(self):
        from upe.merge import _ODD_BREAKS
        self.assertEqual(set(_ODD_BREAKS), set(DifferentTextsSweep.B_CHAR))

    def test_a_folded_list_item_is_still_read(self):
        # Some objects fold a list item's text at the item's own column, `- Root ... part.01` then `    [part.03]#7/Part column#0`. An earlier version kept those objects
        # as written (a closed `[...]` followed by text), so a merge that only re-quoted `m_Name` there stopped. A
        # tail with no `:` is no key, and the object is read as before.
        folded = "\n  m:\n  - Root#0/box.frame.part.01\n    [part.03]#7/Part column#0\n  - Root#0/box-root\n    wall#2"
        self.assertEqual(self.value_kept('"Item 6 variant 1"' + folded, "Item 6 variant 1" + folded), [])

    def test_a_line_the_check_does_not_read_stays_raw(self):
        # Forms the check does not name, whose escaped space it would drop if it read them. ruamel calls
        # them invalid (a key over two lines), so they are not "two texts YAML reads as different"; the base passed them.
        twin = ('"a\\\n    b"', '"a\\ \n    b"')
        for before, after in (("- [a,\n    b]: " + twin[0], "- [a,\n    b]: " + twin[1]),   # a flow key closed on a later line
                              ('- "k\n    x" : ' + twin[0], '- "k\n    x" : ' + twin[1]),  # a quoted key over two lines
                              # Forms ruamel rejects in this place (a key after the empty key;
                              # a tab after a dash, which no reader in use reads, though the spec does)
                              ("- : 'k' :\n      \"a\\ \n      b\"", "- : 'k' :\n      \"a\\\n      b\""),
                              ("- : 'k' : |\n      x  \n      y", "- : 'k' : |\n      x\n      y"),
                              (": -\n    " + twin[0], ": -\n    " + twin[1]),
                              (": -: #\n    a", ": -: # a"),
                              ("-\t" + twin[0], "-\t" + twin[1])):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.line_kept(before, after), self.UNTOUCHED)

    def test_a_list_item_whose_value_is_below_its_dash(self):
        # `-` alone on its line, the value folded below it.
        base = '%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m:\n  - "a b"\n  k: 0\n'
        mine = base.replace('  - "a b"\n', '  -\n    "a\\\n    b"\n')  # the text `ab`
        theirs = base.replace("k: 0", "k: 1")
        self.assertEqual(lost_changes(base, mine, theirs, theirs),
                         ["object 20: changed on both sides, and a change is not in the merged object"])
        spaced = base.replace('  - "a b"\n', '  -\n    "a\\ \n    b"\n')  # the text `a  b`
        self.assertEqual(lost_changes(base, spaced, theirs, theirs),
                         ["object 20: changed on both sides, and a change is not in the merged object"])

    def test_an_escaped_break_inside_a_flow_mapping_joins_with_nothing(self):
        self.assertEqual(self.value_kept('{b: "x\\\n    y"}', '{b: "xy"}'), [])
        self.assertEqual(self.value_kept('{b: "x\\\n    y"}', '{b: "x y"}'), self.UNTOUCHED)
        # After a quoted key a `:` with no space is still a value indicator (`{"a":"b"}`), so the quote opens.
        self.assertEqual(self.value_kept('{"b":"x\\\n    y"}', '{"b":"xy"}'), [])
        self.assertEqual(self.value_kept('{"b":"x\\ \n    y"}', '{"b":"xy"}'), self.UNTOUCHED)

    def test_a_line_of_a_block_text_is_not_read_as_a_value(self):
        base = '%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n  m_Text: |\n    key: "x"\n'
        renamed = base.replace("m_Name: A", "m_Name: A2")
        self.assertEqual(lost_changes(base, renamed, base, renamed.replace('key: "x"', "key: x")), self.UNTOUCHED)

    def test_a_backslash_at_a_folded_line_end_joins_with_nothing(self):
        base = ('%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\nMonoBehaviour:\n'
                '  m_Text: "abc\\\n    def ghi"\n')
        renamed = base.replace("m_Name: A", "m_Name: A2")
        for merged_text, lost in (("m_Text: abcdef ghi", []), ("m_Text: 'abc def ghi'", ["object 20: changed or removed in the merged file, though neither side changed it"])):
            with self.subTest(merged_text):
                merged = renamed.replace('m_Text: "abc\\\n    def ghi"', merged_text)
                self.assertEqual(lost_changes(base, renamed, base, merged), lost)

    def test_dropped_quotes_are_not_a_lost_change(self):
        renamed = self.TARGET.replace("m_Name: C", "m_Name: D").replace("'a [b]'", "'a [c]'")
        self.assertEqual(lost_changes(self.TARGET, renamed, self.TARGET, renamed.replace("'a [c]'", "a [c]")), [])


class ChangedOnBoth(unittest.TestCase):
    def test_names_the_objects_both_sides_changed(self):
        base = LostChanges.BASE
        mine = base.replace("m_Name: A", "m_Name: A1").replace("m_Name: B", "m_Name: B2")
        theirs = base.replace("m_Name: A", "m_Name: A2").replace("m_Name: B", "m_Name: B2")
        self.assertEqual(changed_on_both(base, mine, theirs), ["object 10 (A)"])  # B changed the same way on both


class RealUnityYamlMerge(unittest.TestCase):
    """The real tool: a fake merger hid that it refuses files with no extension."""

    def setUp(self):
        # Only where the user named it: UPE_YAML_MERGE, or this checkout's upe.toml.
        named = os.environ.get("UPE_YAML_MERGE")
        if not named:
            config = Path(__file__).resolve().parents[1] / "upe.toml"
            text = config.read_text(encoding="utf-8") if config.exists() else ""
            found = re.search(r'(?m)^yaml_merge = "(.+)"', text)
            editor = re.search(r'(?m)^unity_editor = "(.+)"', text)
            named = found.group(1) if found else str(yaml_merge_for(Path(editor.group(1)))) if editor else None
        if not named or not Path(named).is_file():
            self.skipTest("no UnityYAMLMerge named (UPE_YAML_MERGE, or yaml_merge or unity_editor in upe.toml)")
        self.exe = Path(named)

    def test_a_scene_conflict_merges_for_real(self):
        scene = "Assets/S.unity"
        base = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!1 &10\nGameObject:\n  m_ObjectHideFlags: 0\n  m_Name: A\n"
                "--- !u!1660057539 &9223372036854775807\nSceneRoots:\n  m_ObjectHideFlags: 0\n  m_Roots:\n  - {fileID: 10}\n")
        def adding(object_id, name):
            head, roots = base.split("--- !u!1660057539")
            return f"{head}--- !u!1 &{object_id}\nGameObject:\n  m_ObjectHideFlags: 0\n  m_Name: {name}\n--- !u!1660057539{roots}  - {{fileID: {object_id}}}\n"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.com")
            git(repo, "config", "user.name", "t")
            write(repo, scene, base)
            git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base")
            git(repo, "checkout", "-q", "-b", "upe/dash")
            write(repo, scene, adding(600, "FromBranch"))
            git(repo, "commit", "-qam", "branch")
            git(repo, "checkout", "-q", "-b", "target", "main")
            write(repo, scene, adding(500, "FromTarget"))
            git(repo, "commit", "-qam", "target")
            git(repo, "checkout", "-q", "main")
            report = Merger(repo, yaml_merger=unity_yaml_merge(self.exe)).merge("dash", "upe/dash", "target", Path(tmp) / "wt")
            self.assertTrue(report.ok, report.steps)
            merged = (Path(tmp) / "wt" / scene).read_text(encoding="utf-8")
            self.assertIn("FromTarget", merged)
            self.assertIn("FromBranch", merged)


class DifferentTextsSweep(unittest.TestCase):
    """Every short form YAML's indicators make, tried as a pair of different texts.

    A random fuzz can miss a form its alphabet does not hold. This sweep
    takes its alphabet from the spec and tries every prefix of it in fixed templates, so a form the code reads
    wrongly shows here, before a reviewer finds it by hand.

    A flag is a pair the loss check reads as one (`lost_changes` gives nothing) while YAML reads two valid, different
    values. The oracle is `ruamel.yaml` (written against 0.19.1, the `test` extra in `pyproject.toml`). It reads a
    flow YAML 1.1-like: `[?a]` and `{?:"b"}` are pairs to it, which is what flags `?` and `:` at a flow item's start.
    Do not swap it for a reader of YAML 1.2 only: those forms would stop showing. Without it the test is skipped,
    and a skipped sweep is not a run. The oracle reads each object with no `%YAML 1.1` line
    with it, ruamel rejects the empty key (`- : a`), so a form it reads here may be one it rejects
    in a Unity file. The loss check now keeps the empty key as written, so no reading rests on that alone.

    Only the public `lost_changes` is used, so the class runs unchanged against an older commit's `src`.
    """

    # YAML 1.2.2, production [22] c-indicator: - ? : , [ ] { } # & * ! | > ' " % @ `
    C_INDICATOR = "-?:,[]{}#&*!|>'\"%@`"
    # YAML 1.1 (2005-01-18), production [27] b-char, without [22] b-line-feed: [23] b-carriage-return (CR),
    # [24] b-next-line (NEL), [25] b-line-separator (LS), [26] b-paragraph-separator (PS).
    # Both numbers were read in the spec's text (yaml.org/spec/1.1/ and yaml.org/spec/1.2.2/).
    B_CHAR = "\r\x85\u2028\u2029"
    ALPHABET = C_INDICATOR + B_CHAR + " \ta"
    LINE_ALPHABET = C_INDICATOR + " \ta"  # the whole-line prefixes of 3 characters (the breaks have their 2)

    SKIP = "no ruamel.yaml: pip install -e .[test] to run the sweep"

    # The flags the sweep gives that are not closed yet, each with its reason. None now.
    KNOWN: dict[tuple[str, str], str] = {}

    # The twin bodies: `"b\` then `c"` (YAML: `bc`) against `"b\ ` then `c"` (`b  c`); with a blank line after
    # the `\` (`b\nc` against `b \nc`).
    TWIN = ('"b\\\n    c"', '"b\\ \n    c"')
    TWIN_BLANK = ('"b\\\n\n    c"', '"b\\ \n\n    c"')
    FLOW_OPENINGS = (("{", "}"), ("[", "]"), ("{a: 1, ", "}"), ("[a, ", "]"))
    LEADS = ("", "- ", "-  ", "- - ")
    KEYS = ("", "k: ", "k:  ", "a:b: ", "? ", "?a: ", ":a: ", ": ", "[a]: ", '"k" : ', "'k' : ", "k : ", "k:\t")
    PROPS = ("", "&x ", "!!str ", "!t ", "! ", "!", "&x !t ", "? ", "k: &x ", "k: !!str ")
    FLOWS = ("", "[", "[a, ", "{", '{"a": ', "{a: 1, ")
    COMMENTS = ("[a, # c", "[a,# c", "[a, ?# c", "[a,? # c", "[a,?# c", "{a, ?# c", "[a,#", "{a: 1, # c")
    BLOCK_LINES = ("k: ", "- ", "-  k: ", "a:b: ")

    def setUp(self):
        try:
            from ruamel.yaml import YAML
        except ImportError:
            self.skipTest(self.SKIP)
        self.yaml = YAML(typ="base")  # every scalar a string, as the loss check compares characters
        self.readings: dict[str, str | None] = {}

    @staticmethod
    def prefixes(alphabet: str, most: int):
        for n in range(most + 1):
            for chars in itertools.product(alphabet, repeat=n):
                yield "".join(chars)

    def pairs(self):
        """(kind, a, b): `value` is the value of `m_Text:`, `line` a line of a list `m:`, in object 20."""
        for twin in (self.TWIN, self.TWIN_BLANK):
            most = 2 if twin is self.TWIN else 1
            for p in self.prefixes(self.ALPHABET, most):
                for open_, close in self.FLOW_OPENINGS:  # a prefix before a quote in a flow
                    yield "value", open_ + p + twin[0] + close, open_ + p + twin[1] + close
                yield "value", p + twin[0], p + twin[1]
            for lead, key, prop, flow in itertools.product(self.LEADS, self.KEYS, self.PROPS, self.FLOWS):
                close = "]" if flow.startswith("[") else "}" if flow.startswith("{") else ""
                start = lead + key + prop + flow
                yield "line", start + twin[0] + close, start + twin[1] + close
            for comment in self.COMMENTS:  # a comment line in a flow, then the twin on the next line
                close = "]" if comment.startswith("[") else "}"
                for start in ("",) + self.BLOCK_LINES:
                    kind = "value" if not start else "line"
                    yield kind, start + comment + "\n    " + twin[0] + close, start + comment + "\n    " + twin[1] + close
        for p in self.prefixes(self.LINE_ALPHABET, 3):  # a whole line's first 3 characters before the twin
            yield "line", p + self.TWIN[0], p + self.TWIN[1]
        for p in self.prefixes(self.ALPHABET, 2):
            if p:
                yield "line", p + self.TWIN[0], p + self.TWIN[1]
        for p in self.prefixes(self.ALPHABET, 3):  # a plain flow item against the same item quoted
            if p:
                q = json.dumps(p)  # a double-quoted YAML text of the same characters
                yield "value", "[" + p + "]", "[" + q + "]"
                yield "value", "[a, " + p + "]", "[a, " + q + "]"
        # Shapes past the templates above: a mark of the
        # alphabet inside the twin's quotes after a key the check does not read; a mark straight after a `:` that is
        # not glued, the twin on the next line; a quote read as text, then `:` and the twin.
        for c in self.ALPHABET:
            for start in ('- "k" : ', '"k" : ', "[a]: "):
                yield "line", start + '"' + c + self.TWIN[0][1:], start + '"' + c + self.TWIN[1][1:]
            for open_, close in self.FLOW_OPENINGS:
                for colon in ("? :", ":", " :"):
                    yield ("value", open_ + colon + c + "\n    " + self.TWIN[0] + close,
                           open_ + colon + c + "\n    " + self.TWIN[1] + close)
                yield "value", open_ + "-" + c + '":' + self.TWIN[0] + close, open_ + "-" + c + '":' + self.TWIN[1] + close
        # A mark straight after a closed key's `:` in a flow, the
        # twin on the next line (`{"k":#` is a comment to ruamel); a `|` or `>` header with up to two marks after it,
        # then the twin, a line ending in a space or a tab, or a blank line holding spaces, as the text below it.
        for c in self.ALPHABET:
            for open_, close in (('{"k":', "}"), ("{'k':", "}"), ("{[a]:", "}"), ('{"k" :', "}"), ('["k":', "]")):
                for twin in (self.TWIN, (self.TWIN[0] + "\n    ", self.TWIN[1] + "\n    ")):
                    yield ("value", open_ + c + "\n    " + twin[0] + close, open_ + c + "\n    " + twin[1] + close)
        texts = ((self.TWIN[0].replace("    ", "        "), self.TWIN[1].replace("    ", "        ")),
                 ("a \n        b", "a\n        b"), ("a\t\n        b", "a\n        b"),
                 ("a\n          \n        b", "a\n\n        b"), ("a\n        b", "a b"))
        for p in self.prefixes(self.ALPHABET, 2):
            for indicator in "|>":
                for start in ("", "- ", "- - ") + self.BLOCK_LINES:
                    for a, b in texts:
                        kind = "value" if not start else "line"
                        yield kind, start + indicator + p + "\n        " + a, start + indicator + p + "\n        " + b
        yield from self.family_pairs()
        yield from self.dash_pairs()
        # More pairs (the others are in the templates above: `[?a]` against
        # `["?a"]`, and `{:"b\` and `{?:"b\` then `c"}` against their `\ ` twins).
        yield "value", "[a, b\rc]", '[a, "b\\rc"]'
        for start in self.BLOCK_LINES + ("",):  # a comment after a key in block context, then a text below
            yield "line", start + "k: #\n    a", start + "k: # a"
            yield "line", start + "k: #\n    a", start + "k: #a"
            yield "line", start + "k: # c\n    a", start + "k: # c a"

    # Spaces YAML reads as text and Python's `\s` as space.
    TEXT_SPACES = "\xa0\u3000\u2003"

    def family_pairs(self):
        """Templates for the families past the ones above. Each family has
        its shapes, with each mark of the alphabet (and the spaces Python reads as space) where a mark may go."""
        marks = self.ALPHABET + self.TEXT_SPACES
        lines = (("a \n    b", "a\n    b"), ("a\t\n    b", "a\n    b"), ("a\n      b", "a b"), ("- 'b'", "- b"),
                 ("k: 'b'", "k: b"), (self.TWIN[0], self.TWIN[1]))
        for c in marks:
            # a line break before the node: the node alone on the line below its key or its dash
            for start in ("k:\n    ", "-\n    ", "- k:\n      ", "\n    "):
                kind = "value" if start == "\n    " else "line"
                for indicator in ("|", ">", "|" + c, ">" + c, c):
                    for a, b in lines:
                        yield kind, start + indicator + "\n    " + a, start + indicator + "\n    " + b
            # a second node after the empty key, and a line starting with a mark, read as a key
            for start in ("- : ", ": "):
                for second in ("'k' : ", "[a]: ", "- ", "- - ", "-: ", "k: ", c + " ", c + ": ", "- " + c):
                    yield "line", start + second + self.TWIN[0], start + second + self.TWIN[1]
                    for a, b in lines[:4]:
                        yield "line", start + second + "|\n      " + a, start + second + "|\n      " + b
            for lead in ("- ", ""):
                yield "line", lead + c + " # c:\n    a \n    b", lead + c + " # c:\n    a\n    b"
                yield "line", lead + c + "\n    a \n    b", lead + c + "\n    a\n    b"
            # a space Python reads as space, where indentation, a key or a quote is read
            for start in ("", "- ", "k: ", "- k: "):
                for form in ("'k': x", "'", '"b\n  - c"', "[b\n  - c]"):
                    a = start + c + form
                    b = a.replace("'k'", "k") if "'k'" in a else a.replace("\n  - ", " - ")
                    if form == "'":
                        a, b = start + c + "': " + self.TWIN[0], start + c + "': " + self.TWIN[1]
                    yield "line", a, b
            # a closed node, then a tail that opens a bracket or a quote, and a comment after a closed value
            for node in ('"b"', "'b'", "[r]", "{r: 1}", "[part.03]#7/Part"):
                for opening in ("[", "{", '"', "'"):
                    for tail in (" " + opening + c, opening + c, " #" + c + opening):
                        for lead in ("- x\n    ", "k: ", "- ", ""):
                            for nxt in ("\n  - d", "\n  j: 1", "\n    d"):
                                a = lead + node + tail + nxt
                                b = lead + node + tail + " " + nxt.strip()
                                yield "line", a, b
            # a plain text's next line at the text's own column
            for lead in ("- x\n    ", "k:\n    x\n    ", "- - x\n      "):
                for a, b in (("- 'b'", "- b"), ('"b\n  - c"', '"b - c"'), ("'b'", "b"), ("k: 'b'", "k: b"),
                             ("[b\n  - c]", "[b - c]"), ("{b\n  - c}", "{b - c}")):
                    yield "line", lead + c + a, lead + c + b
                    yield "line", lead + a, lead + b
            # the object's end: the text's last lines
            for indicator in ("|", ">"):
                for chomp in ("+", "-", "", "+" + c, c + "+"):
                    yield "end", indicator + chomp + "\n    a\n", indicator + chomp + "\n    a"
                    yield "end", indicator + chomp + "\n    a\n\n", indicator + chomp + "\n    a\n"
            # a property after an explicit key in a flow
            for open_, close in self.FLOW_OPENINGS:
                for prop in ("&x ", "!t ", "! ", "&x", "!t"):
                    for q in ("? ", "?"):
                        yield ("value", open_ + q + prop + c + self.TWIN[0] + close,
                               open_ + q + prop + c + self.TWIN[1] + close)
                        yield "value", open_ + q + prop + self.TWIN[0] + close, open_ + q + prop + self.TWIN[1] + close
            # a tab after a dash
            yield "line", "-\t" + c + self.TWIN[0], "-\t" + c + self.TWIN[1]

    def dash_pairs(self):
        """A dash alone near a plain text, a `---` or `...` line inside an object,
        and an object whose root is no mapping, each with each mark of the alphabet where a mark may go."""
        marks = self.ALPHABET + self.TEXT_SPACES
        nexts = (("- 'b'", "- b"), ("'b'", "b"), ('"b\n  - c"', '"b - c"'), ("[a, 'b']", "[a, b]"), ("''", '""'))
        for c in marks:
            # a text below a dash alone, its next line past the dash; a dash alone as a text's next line
            for lead in ("-\n    x\n   ", "-\n   x\n   ", "- -\n      x\n     ", "- k:\n    -\n      x\n     ",
                         "- x\n    -\n      ", "k:\n    x\n    -\n      ", "- x\n    y\n      ", "-\n    x\n    -\n     "):
                for a, b in nexts:
                    yield "line", lead + c + a, lead + c + b
                    yield "line", lead + a, lead + b
            # a document marker inside the object, then a node
            head = "MonoBehaviour:\n  m_X: 1\n"
            for mark in ("--- ", "---\n", "...\n--- ", "--- &x ", "--- [\n    ", "...\n", "--- " + c, c + "\n--- "):
                yield "raw", head + mark + self.TWIN[0] + "\n", head + mark + self.TWIN[1] + "\n"
            # the root no mapping at column 0
            for root in (" - ", "  ", " ", "- ", c, c + " ", "x\n" + c, " - x\n "):
                yield "raw", root + self.TWIN[0] + "\n", root + self.TWIN[1] + "\n"
                yield "raw", root + "x\n- 'b'\n", root + "x\n- b\n"

    @staticmethod
    def body(kind: str, text: str) -> str:
        if kind == "value":
            return f"MonoBehaviour:\n  m_Text: {text}\n  m_X: 1\n"
        if kind == "end":  # the text is the object's last: nothing after it
            return f"MonoBehaviour:\n  m_X: 1\n  m_Text: {text}\n"
        if kind == "raw":  # the text is the object's whole body, below its `--- !u!` line
            return text
        return f"MonoBehaviour:\n  m:\n  {text}\n  m_X: 1\n"

    @classmethod
    def read_as_one(cls, kind: str, a: str, b: str) -> bool:
        """Whether the loss check passes a merge that wrote `b` where object 20 held `a` (one side renamed object 10)."""
        base = "%YAML 1.1\n--- !u!1 &10\nGameObject:\n  m_Name: A\n--- !u!114 &20\n" + cls.body(kind, a)
        renamed = base.replace("m_Name: A", "m_Name: A2")
        merged = renamed.replace(cls.body(kind, a), cls.body(kind, b))
        try:
            return lost_changes(base, renamed, base, merged) == []
        except Exception:  # an error stops the land; it reads nothing as one
            return False

    def read(self, kind: str, text: str) -> str | None:
        """ruamel's reading of object 20 holding `text`, or None when it is not valid YAML to it."""
        body = self.body(kind, text)
        if body not in self.readings:
            try:
                # every document of the body: a `---` inside it starts one
                self.readings[body] = repr(list(self.yaml.load_all(io.StringIO(body))))
            except Exception:
                self.readings[body] = None
        return self.readings[body]

    def sweep(self) -> tuple[int, list[tuple[str, str, str, str, str]]]:
        """How many pairs were read, and the flags: (kind, a, b, ruamel's reading of a, of b)."""
        count, flags, seen = 0, [], set()
        for kind, a, b in self.pairs():
            if (kind, a, b) in seen:
                continue
            seen.add((kind, a, b))
            count += 1
            if not self.read_as_one(kind, a, b):
                continue
            ra, rb = self.read(kind, a), self.read(kind, b)
            if ra is not None and rb is not None and ra != rb:
                flags.append((kind, a, b, ra, rb))
        return count, flags

    def test_no_two_texts_yaml_reads_as_two_are_read_as_one(self):
        started = time.time()
        count, flags = self.sweep()
        outside = [f for f in flags if f[1:3] not in self.KNOWN]
        print(f"\nDifferentTextsSweep: pairs read: {count}; flags: {len(flags)}; outside KNOWN: {len(outside)}; "
              f"seconds: {time.time() - started:.0f}")
        if outside:
            self.fail("\n".join(
                [f"{len(outside)} flags outside KNOWN:"] +
                [f"the loss check reads {ascii(a)} and {ascii(b)} as one ({kind}), YAML reads {ra} and {rb}: "
                 "keep the object raw (`_Doubt`) or read it as YAML does" for kind, a, b, ra, rb in outside]))



class SweepOracleDeclared(unittest.TestCase):
    def test_the_skip_names_the_extra_that_installs_the_oracle(self):
        # The step a skipped sweep prints (`pip install -e .[test]`) installs what the sweep imports. The install
        # itself is not run here: it would change the machine's Python.
        import tomllib
        project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        extra = re.search(r"pip install -e \.\[(\w+)\]", DifferentTextsSweep.SKIP).group(1)
        self.assertTrue(any(re.match(r"ruamel\.yaml(?![\w.])", d) for d in project["project"]["optional-dependencies"][extra]))

class MetaFiles(RepoCase):
    def test_same_asset_added_with_different_guids_is_refused(self):
        write(self.feat, "Assets/Icons/Dash.png.meta", f"fileFormatVersion: 2\nguid: {GUID_A}\n")
        self.commit(self.feat)
        write(self.repo, "Assets/Icons/Dash.png.meta", f"fileFormatVersion: 2\nguid: {GUID_B}\n")
        self.commit(self.repo)
        report = self.merge(merger=union_merger([]))
        self.assertFalse(report.ok)
        step = report.unresolved[0]
        self.assertEqual(step.layer, "meta-guid")
        self.assertIn(GUID_A, step.detail)
        self.assertIn(GUID_B, step.detail)


class PackageFiles(RepoCase):
    def set_manifest(self, root, deps):
        write(root, "Packages/manifest.json", json.dumps({"dependencies": deps}, indent=2) + "\n")

    def test_manifest_merges_by_key_and_lock_is_regenerated(self):
        self.set_manifest(self.feat, {"com.a": "1.0.0", "com.dash": "2.0.0"})
        write(self.feat, LOCK_FILE, '{"dependencies": {"com.dash": {}}}\n')
        self.commit(self.feat)
        self.set_manifest(self.repo, {"com.a": "1.0.0", "com.jump": "3.0.0"})
        write(self.repo, LOCK_FILE, '{"dependencies": {"com.jump": {}}}\n')
        self.commit(self.repo)

        report = self.merge()
        self.assertTrue(report.ok, report.steps)
        deps = json.loads((self.candidate_wt / "Packages/manifest.json").read_text())["dependencies"]
        self.assertEqual(deps, {"com.a": "1.0.0", "com.jump": "3.0.0", "com.dash": "2.0.0"})
        self.assertFalse((self.candidate_wt / LOCK_FILE).exists())
        self.assertEqual(report.regenerate, [LOCK_FILE])

    def test_same_package_two_versions_is_a_conflict(self):
        self.set_manifest(self.feat, {"com.a": "1.1.0"})
        self.commit(self.feat)
        self.set_manifest(self.repo, {"com.a": "1.2.0"})
        self.commit(self.repo)
        report = self.merge()
        self.assertFalse(report.ok)
        self.assertIn("dependencies.com.a", report.unresolved[0].detail)


def tag_manager(tags, layers):
    lines = ["%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:", "--- !u!78 &1", "TagManager:", "  serializedVersion: 3"]
    lines.append("  tags:" if tags else "  tags: []")
    lines += [f"  - {t}" for t in tags]
    lines.append("  layers:")
    lines += [f"  - {l}" if l else "  - " for l in layers + [""] * (8 - len(layers))]
    return "\n".join(lines) + "\n"


class ProjectSettingsFiles(RepoCase):
    TAGS = "ProjectSettings/TagManager.asset"

    def setUp(self):
        super().setUp()
        write(self.repo, self.TAGS, tag_manager(["Spawn"], ["Default"]))
        self.commit(self.repo, "settings")
        git(self.feat, "merge", "-q", "--ff-only", "main")

    def test_tags_and_layers_merge_as_a_union(self):
        write(self.feat, self.TAGS, tag_manager(["Spawn", "Dash"], ["Default", "", "Dash"]))
        self.commit(self.feat)
        write(self.repo, self.TAGS, tag_manager(["Spawn", "Jump"], ["Default", "Jump"]))
        self.commit(self.repo)
        report = self.merge()
        self.assertTrue(report.ok, report.steps)
        self.assertEqual([(s.path, s.layer, s.outcome) for s in report.steps], [(self.TAGS, "project-settings", "merged")])
        self.assertEqual((self.candidate_wt / self.TAGS).read_text(),
                         tag_manager(["Spawn", "Jump", "Dash"], ["Default", "Jump", "Dash"]))

    def test_same_layer_slot_stops_the_merge(self):
        write(self.feat, self.TAGS, tag_manager(["Spawn"], ["Default", "Dash"]))
        self.commit(self.feat)
        write(self.repo, self.TAGS, tag_manager(["Spawn"], ["Default", "Jump"]))
        self.commit(self.repo)
        report = self.merge()
        self.assertFalse(report.ok)
        self.assertEqual(report.unresolved[0].layer, "unresolved")
        self.assertIn("layer 1", report.unresolved[0].detail)


class ManifestUnit(unittest.TestCase):
    def test_removal_on_one_side(self):
        base = json.dumps({"dependencies": {"a": "1", "b": "1"}})
        ours = json.dumps({"dependencies": {"a": "1"}})
        theirs = json.dumps({"dependencies": {"a": "1", "b": "1", "c": "1"}})
        merged, conflicts = merge_manifest(base, ours, theirs)
        self.assertEqual(conflicts, [])
        self.assertEqual(json.loads(merged)["dependencies"], {"a": "1", "c": "1"})

    def test_other_top_level_keys(self):
        base = json.dumps({"dependencies": {}, "scopedRegistries": []})
        ours = json.dumps({"dependencies": {}, "scopedRegistries": [{"name": "x"}]})
        merged, _ = merge_manifest(base, ours, base)
        self.assertEqual(json.loads(merged)["scopedRegistries"], [{"name": "x"}])


class YamlMergeTool(unittest.TestCase):
    def test_argv(self):
        seen = []
        run = lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "UnityYAMLMerge.exe"
            exe.write_text("")
            merge = unity_yaml_merge(exe, ("--nomappinginoneline",), runner=run)
            self.assertEqual(merge(Path("b"), Path("t"), Path("m"), Path("o")), 0)
        self.assertEqual(seen[0], [str(exe), "merge", "-h", "-p", "--fallback", "none", "--nomappinginoneline", "b", "t", "m", "o"])

    def test_a_path_without_exe_is_taken_as_windows_does(self):
        seen = []
        run = lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "UnityYAMLMerge.exe").write_text("")
            self.assertEqual(unity_yaml_merge(Path(tmp) / "UnityYAMLMerge", runner=run)(Path("b"), Path("t"), Path("m"), Path("o")), 0)
        self.assertEqual(len(seen), 1)

    def test_a_path_with_no_file_name_is_not_there(self):
        for path in (Path("C:/"), Path(".")):
            self.assertFalse(program_there(path), path)

    def test_a_missing_tool_is_named_not_a_raw_error(self):
        merge = unity_yaml_merge(lambda: Path("C:/nowhere/UnityYAMLMerge.exe"), runner=lambda *a, **k: self.fail("ran"))
        with self.assertRaises(MergeError) as ctx:
            merge(Path("b"), Path("t"), Path("m"), Path("o"))
        self.assertIn("name it as yaml_merge in upe.toml", str(ctx.exception))

    def test_flags_follow_editor_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.assertEqual(yaml_merge_flags(project), ())
            write(project, "ProjectSettings/EditorSettings.asset", "  m_SerializeInlineMappingsOnOneLine: 0\n")
            self.assertEqual(yaml_merge_flags(project), ("--nomappinginoneline",))
            write(project, "ProjectSettings/EditorSettings.asset", "  m_SerializeInlineMappingsOnOneLine: 1\n")
            self.assertEqual(yaml_merge_flags(project), ())


if __name__ == "__main__":
    unittest.main()
