import json
import tempfile
import threading
import unittest
from pathlib import Path

from upe.claims import Claim, ClaimError, ClaimRefused, ClaimRegistry, file_lock, normalize, unclaimed


def overlaps(a, b):
    return bool(Claim.of("a", [a]).overlaps(Claim.of("b", [b])))


class NormalizeTests(unittest.TestCase):
    def test_slashes_case_and_meta(self):
        self.assertEqual(normalize(".\\Assets\\Scenes\\Arena.unity.meta"), "assets/scenes/arena.unity")

    def test_rejects_paths_outside_project(self):
        for bad in ("C:/game/Assets/x", "/abs", "Assets/../../x", "", "  ", "/"):
            with self.assertRaises(ClaimError):
                normalize(bad)


class OverlapTests(unittest.TestCase):
    def test_same_file(self):
        self.assertTrue(overlaps("Assets/Scenes/Arena.unity", "assets/scenes/ARENA.unity"))

    def test_asset_and_its_meta(self):
        self.assertTrue(overlaps("Assets/a.prefab", "Assets/a.prefab.meta"))

    def test_different_files(self):
        self.assertFalse(overlaps("Assets/Scenes/Arena.unity", "Assets/Scenes/Hub.unity"))

    def test_file_in_folder(self):
        self.assertTrue(overlaps("Assets/Features/Dash/", "Assets/Features/Dash/Dash.prefab"))
        self.assertFalse(overlaps("Assets/Features/Dash/", "Assets/Features/Dashboard.prefab"))

    def test_nested_folders(self):
        self.assertTrue(overlaps("Assets/Features/", "Assets/Features/Dash/"))
        self.assertFalse(overlaps("Assets/Features/Dash/", "Assets/Features/Jump/"))

    def test_glob_and_file(self):
        self.assertTrue(overlaps("Assets/Prefabs/Enemy*.prefab", "Assets/Prefabs/EnemyBat.prefab"))
        self.assertFalse(overlaps("Assets/Prefabs/Enemy*.prefab", "Assets/Prefabs/Hero.prefab"))
        self.assertFalse(overlaps("Assets/Prefabs/*.prefab", "Assets/Prefabs/Sub/X.prefab"))

    def test_double_star(self):
        self.assertTrue(overlaps("Assets/**/*.unity", "Assets/Levels/Deep/One.unity"))
        self.assertTrue(overlaps("Assets/**/*.unity", "Assets/Top.unity"))

    def test_glob_against_glob_is_conservative(self):
        self.assertTrue(overlaps("Assets/Prefabs/Enemy*", "Assets/Prefabs/*Bat"))
        self.assertFalse(overlaps("Assets/Prefabs/Enemy*", "Assets/Audio/*"))


class UnclaimedTests(unittest.TestCase):
    def test_reports_writes_outside_the_claim(self):
        claim = Claim.of("t", ["Assets/Features/Dash/", "Assets/Scenes/Arena.unity"])
        changed = [
            "Assets/Features/Dash/Dash.cs",
            "Assets/Features/Dash/Dash.cs.meta",
            "Assets/Scenes/Arena.unity",
            "Assets/Scenes/Hub.unity",
            "ProjectSettings/TagManager.asset",
            "Packages/packages-lock.json",
        ]
        self.assertEqual(
            unclaimed(changed, claim, shared=["Packages/packages-lock.json"]),
            ["Assets/Scenes/Hub.unity", "ProjectSettings/TagManager.asset"],
        )

    def test_folder_metas_above_a_claim_need_no_claim(self):
        claim = Claim.of("t", ["Assets/Features/Dash/", "Assets/Art/Dash*.png"])
        changed = ["Assets/Features.meta", "Assets/Features/Dash.meta", "Assets/Art.meta",
                   "Assets/Features", "Assets/Other.meta", "Assets/Features/Jump.meta"]
        self.assertEqual(unclaimed(changed, claim), ["Assets/Features", "Assets/Features/Jump.meta", "Assets/Other.meta"])


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reg = ClaimRegistry(Path(self._tmp.name) / "claims.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_claim_and_release(self):
        self.reg.claim("dash", ["Assets/Features/Dash/"])
        self.reg.claim("jump", ["Assets/Features/Jump/"])
        self.assertEqual(set(self.reg.all()), {"dash", "jump"})
        self.assertTrue(self.reg.release("dash"))
        self.assertFalse(self.reg.release("dash"))
        self.assertEqual(set(self.reg.all()), {"jump"})

    def test_a_second_claim_by_one_task_points_to_status(self):
        self.reg.claim("dash", ["Assets/Features/Dash/"])
        with self.assertRaises(ClaimError) as ctx:
            self.reg.claim("dash", ["Assets/Features/Dash/"])
        self.assertIn("being started, or is running; `upe status dash` says which", str(ctx.exception))

    def test_overlap_refused_and_named(self):
        self.reg.claim("dash", ["Assets/Scenes/Arena.unity", "Assets/Features/Dash/"])
        with self.assertRaises(ClaimRefused) as ctx:
            self.reg.claim("arena-lights", ["Assets/Scenes/Arena.unity.meta", "Assets/Lights/"])
        self.assertEqual(ctx.exception.conflicts, {"dash": [("assets/scenes/arena.unity", "assets/scenes/arena.unity")]})
        self.assertIn("dash", str(ctx.exception))
        self.assertIn("Assets/Scenes/Arena.unity.meta  overlaps dash's", str(ctx.exception))  # as the agent wrote it
        self.assertIn("freed when its task lands or is finished", str(ctx.exception))
        self.assertEqual(set(self.reg.all()), {"dash"})

    def test_released_claim_frees_the_path(self):
        self.reg.claim("dash", ["Assets/Scenes/Arena.unity"])
        self.reg.release("dash")
        self.reg.claim("arena-lights", ["Assets/Scenes/Arena.unity"])

    def test_same_task_twice(self):
        self.reg.claim("dash", ["Assets/A/"])
        with self.assertRaises(ClaimError):
            self.reg.claim("dash", ["Assets/B/"])

    def test_empty_claim_refused(self):
        with self.assertRaises(ClaimError):
            self.reg.claim("dash", [])

    def test_file_survives_round_trip(self):
        self.reg.claim("dash", ["Assets/A/"])
        data = json.loads(self.reg.path.read_text())
        self.assertEqual(data["dash"]["entries"], ["assets/a/"])

    def test_concurrent_claims_on_one_path_admit_exactly_one(self):
        results = []

        def go(i):
            try:
                self.reg.claim(f"t{i}", ["Assets/Scenes/Arena.unity"])
                results.append(True)
            except ClaimRefused:
                results.append(False)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(results), 8)  # no thread died on the lock

    def test_lock_timeout(self):
        self.reg.lock_timeout = 0.2
        with file_lock(self.reg.lock_path, ClaimError):  # another holder
            with self.assertRaises(ClaimError):
                self.reg.claim("dash", ["Assets/A/"])
        self.reg.claim("dash", ["Assets/A/"])

    def test_a_broken_registry_is_a_claim_error(self):
        # `status` reads the claims, and a broken file crashed it.
        for text in ("{not json", "[]", '{"dash": 5}', '{"dash": {"entries": "Assets/"}}', '{"dash": {"entries": [], "since": null}}'):
            with self.subTest(text=text):
                self.reg.path.write_text(text)
                with self.assertRaises(ClaimError) as ctx:
                    self.reg.all()
                self.assertIn("does not read", str(ctx.exception))

    def test_a_lock_file_left_on_disk_does_not_block(self):
        # A holder that died leaves its file; the OS lock went with the process.
        self.reg.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.reg.lock_path.write_text("dead")
        self.reg.lock_timeout = 0.2
        self.reg.claim("dash", ["Assets/A/"])

    # A claim made by `start` under the task's lock carries a mark -------------------------------

    def test_a_claim_written_by_start_carries_the_mark(self):
        self.reg.claim("dash", ["Assets/A/"], locked=True)
        self.reg.claim("jump", ["Assets/B/"])
        raw = json.loads(self.reg.path.read_text())
        self.assertIs(raw["dash"]["locked"], True)
        self.assertNotIn("locked", raw["jump"], "the key is written only when true: no field renamed, none removed")
        self.assertEqual({t: c.locked for t, c in self.reg.all().items()}, {"dash": True, "jump": False})
        self.reg.release("jump")  # a rewrite of the file keeps the mark of the claims it did not touch
        self.assertIs(json.loads(self.reg.path.read_text())["dash"]["locked"], True)

    def test_an_old_claims_file_reads_unmarked_and_keeps_its_shape(self):
        old = {"dash": {"entries": ["assets/a/"], "since": "2026-11-01T12:00:00+00:00"}}
        self.reg.path.write_text(json.dumps(old))
        claim = self.reg.all()["dash"]
        self.assertEqual((claim.entries, claim.since, claim.locked), (("assets/a/",), "2026-11-01T12:00:00+00:00", False))
        self.reg.claim("jump", ["Assets/B/"])
        self.assertEqual(json.loads(self.reg.path.read_text())["dash"], old["dash"], "an old claim is written back as it was")
        for odd in ('"yes"', "1", "null"):
            with self.subTest(locked=odd):
                self.reg.path.write_text('{"dash": {"entries": ["assets/a/"], "since": "", "locked": %s}}' % odd)
                with self.assertRaises(ClaimError) as ctx:
                    self.reg.all()
                self.assertIn("does not read", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
