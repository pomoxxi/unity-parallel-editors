"""The kept-result format, its store, and the guard (`kept.decide`) as a pure function, with data."""

import json
import tempfile
import unittest
from pathlib import Path


def kept_module():
    """Imported in each test, so this file loads on a commit without `upe.kept` and each test is read on its own."""
    from upe import kept

    return kept


class Decide(unittest.TestCase):
    def setUp(self):
        k = kept_module()
        self.k = k
        self.ancestor = ("a" * 40, k.Kept(failed=frozenset({"Old.Broken"}), flaky=frozenset({"Slow.Once"}),
                                          unconfirmed=frozenset({"Param.Case(1)"})), 3)

    def alone(self, failed, ran=None):
        return self.k.ClassRun("t" * 40, ("Old",), None if failed is None else frozenset(failed),
                               frozenset(ran if ran is not None else failed or ()))

    def test_row_a_the_targets_own_result_decides_every_test(self):
        own = self.k.Kept(unconfirmed=frozenset({"Old.Broken"}), format=1)  # an old one-list file of the target's own
        decision = self.k.decide({"Old.Broken", "Dash.Breaks"}, own, self.ancestor, None)
        self.assertEqual((decision.known, decision.new, decision.need_full),
                         ({"Old.Broken": self.k.BY_OWN}, {"Dash.Breaks"}, set()))

    def test_row_b_a_class_only_run_with_no_report_compares_nothing(self):
        # The land stops, "not compared", and no test is taken as known. Red with row b removed (the test then
        # falls to row c, which reads `alone.failed` of a run with no report).
        decision = self.k.decide({"Old.Broken"}, None, self.ancestor, self.alone(None))
        self.assertEqual((decision.known, decision.new, decision.not_compared), ({}, {"Old.Broken"}, True))

    def test_row_c_a_test_that_passes_alone_or_is_not_there_is_new_at_once(self):
        decision = self.k.decide({"Old.Broken", "Dash.New"}, None, self.ancestor,
                                 self.alone(set(), ran={"Old.Broken"}))
        self.assertEqual((decision.known, decision.new, decision.need_full), ({}, {"Old.Broken", "Dash.New"}, set()))

    def test_row_d_failed_alone_and_failed_on_every_attempt_at_the_ancestor_is_known(self):
        decision = self.k.decide({"Old.Broken"}, None, self.ancestor, self.alone({"Old.Broken"}))
        self.assertEqual(decision.known, {"Old.Broken": self.k.BY_CLASSES})

    def test_row_e_only_flaky_or_unconfirmed_or_absent_at_the_ancestor_needs_the_full_run(self):
        # A test only flaky in an ancestor's result, failing alone on the target, is not known by case 2.
        # Red with `ancestor[1].failed` read as `ancestor[1].seen`.
        for name in ("Slow.Once", "Param.Case(1)", "Renamed.Broken"):
            with self.subTest(name=name):
                decision = self.k.decide({name}, None, self.ancestor, self.alone({name}))
                self.assertEqual((decision.known, decision.new, decision.need_full), ({}, set(), {name}))

    def test_no_result_of_its_own_and_no_ancestors_makes_every_test_new(self):
        decision = self.k.decide({"Old.Broken"}, None, None, None)
        self.assertEqual((decision.known, decision.new, decision.not_compared), ({}, {"Old.Broken"}, False),
                         "new because the target's tests did not finish, not because a class-only run left no report")


class Store(unittest.TestCase):
    def setUp(self):
        self.k = kept_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "test-baselines"
        self.store = self.k.KeptStore(self.folder)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_new_format_keeps_failed_flaky_and_unconfirmed_apart(self):
        kept = self.k.Kept(frozenset({"A.Twice"}), frozenset({"B.Once"}), frozenset({"C.Param(1)"}), cases=7)
        self.assertTrue(self.store.put("1" * 40, kept))
        data = json.loads(self.store.path("1" * 40).read_text(encoding="utf-8"))
        self.assertEqual(data, {"format": 2, "scope": "full", "cases": 7, "failed": ["A.Twice"], "flaky": ["B.Once"],
                                "unconfirmed": ["C.Param(1)"]})
        self.assertEqual(self.store.get("1" * 40), kept)
        self.assertEqual(kept.seen, {"A.Twice", "B.Once", "C.Param(1)"})

    def test_put_refuses_a_second_write_a_part_and_an_old_list(self):
        # A part is never the whole. A `ClassRun` cannot be put; nor can an old one-list result.
        self.assertTrue(self.store.put("1" * 40, self.k.Kept(cases=1)))
        self.assertFalse(self.store.put("1" * 40, self.k.Kept(frozenset({"X.Y"}), cases=1)))
        self.assertEqual(self.store.get("1" * 40).failed, frozenset())
        with self.assertRaises(TypeError):
            self.store.put("2" * 40, self.k.ClassRun("2" * 40, ("X",), frozenset({"X.Y"})))
        with self.assertRaises(TypeError):
            self.store.put("2" * 40, self.k.Kept(unconfirmed=frozenset({"X.Y"}), format=1))
        self.assertFalse(self.store.path("2" * 40).exists())

    def test_an_unreadable_file_is_absent_and_an_old_list_is_format_1(self):
        self.folder.mkdir(parents=True)
        self.store.path("1" * 40).write_text("{not json", encoding="utf-8")
        self.store.path("2" * 40).write_text('["Old.Broken"]\n', encoding="utf-8")
        self.store.path("3" * 40).write_text('{"format": 2, "scope": "classes", "cases": 1, "failed": ["X.Y"], '
                                             '"flaky": [], "unconfirmed": []}', encoding="utf-8")
        self.assertIsNone(self.store.get("1" * 40))
        self.assertEqual(self.store.get("2" * 40), self.k.Kept(unconfirmed=frozenset({"Old.Broken"}), format=1))
        self.assertIsNone(self.store.get("3" * 40), "only a full result reads")

    def test_nearest_takes_the_closest_new_format_ancestor_and_skips_old_lists_and_locks(self):
        self.folder.mkdir(parents=True)
        far, near, old, other = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        for commit in (far, near, other):
            self.store.put(commit, self.k.Kept(cases=1))
        self.store.path(old).write_text("[]\n", encoding="utf-8")
        (self.folder / f"{near}.lock").write_text("", encoding="utf-8")
        distance = {far: 5, near: 2, old: 1, other: None}
        found = self.store.nearest("t" * 40, distance.get)
        self.assertEqual((found[0], found[2]), (near, 2))
        self.assertIsNone(self.store.nearest("t" * 40, lambda c: None))


if __name__ == "__main__":
    unittest.main()
