import unittest

from upe.project_settings import SettingsConflict, merge_set, merge_settings

HEAD = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!78 &1\nTagManager:\n  serializedVersion: 3\n"


def tag_manager(tags, layers, sorting="Default"):
    lines = [HEAD.rstrip("\n")]
    lines.append("  tags:" if tags else "  tags: []")
    lines += [f"  - {t}" for t in tags]
    lines.append("  layers:")
    lines += [f"  - {l}" if l else "  - " for l in layers + [""] * (32 - len(layers))]
    lines += ["  m_SortingLayers:", f"  - name: {sorting}", "    uniqueID: 0", "    locked: 0"]
    return "\n".join(lines) + "\n"


BASE_LAYERS = ["Default", "TransparentFX", "Ignore Raycast", "", "Water", "UI"]


def build_settings(*scenes):
    lines = ["%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:", "--- !u!1045 &1", "EditorBuildSettings:",
             "  m_ObjectHideFlags: 0", "  serializedVersion: 2", "  m_Scenes:"]
    for path, guid in scenes:
        lines += ["  - enabled: 1", f"    path: {path}", f"    guid: {guid}"]
    lines += ["  m_configObjects: {}"]
    return "\n".join(lines) + "\n"


A, B, C, D = ("Assets/A.unity", "a" * 32), ("Assets/B.unity", "b" * 32), ("Assets/C.unity", "c" * 32), ("Assets/D.unity", "d" * 32)


class TagTests(unittest.TestCase):
    def test_both_add_tags(self):
        out = merge_settings("TagManager.asset", tag_manager(["Spawn"], BASE_LAYERS),
                             tag_manager(["Spawn", "Jump"], BASE_LAYERS), tag_manager(["Spawn", "Dash"], BASE_LAYERS))
        self.assertEqual(out, tag_manager(["Spawn", "Jump", "Dash"], BASE_LAYERS))

    def test_removal_on_one_side(self):
        self.assertEqual(merge_set(["a", "b"], ["a", "b", "c"], ["a"]), ["a", "c"])
        self.assertEqual(merge_set(["a", "b"], ["b"], ["a", "b", "d"]), ["b", "d"])

    def test_empty_base_tags(self):
        out = merge_settings("TagManager.asset", tag_manager([], BASE_LAYERS), tag_manager([], BASE_LAYERS),
                             tag_manager(["Dash"], BASE_LAYERS))
        self.assertEqual(out, tag_manager(["Dash"], BASE_LAYERS))


class LayerTests(unittest.TestCase):
    def test_different_slots_merge(self):
        ours = BASE_LAYERS + ["Mob"]
        theirs = BASE_LAYERS[:3] + ["Player"] + BASE_LAYERS[4:]
        out = merge_settings("TagManager.asset", tag_manager([], BASE_LAYERS), tag_manager([], ours), tag_manager([], theirs))
        self.assertEqual(out, tag_manager([], BASE_LAYERS[:3] + ["Player"] + BASE_LAYERS[4:] + ["Mob"]))

    def test_same_free_slot_is_a_conflict(self):
        with self.assertRaises(SettingsConflict) as ctx:
            merge_settings("TagManager.asset", tag_manager([], BASE_LAYERS),
                           tag_manager([], BASE_LAYERS + ["Mob"]), tag_manager([], BASE_LAYERS + ["Ship"]))
        self.assertIn("layer 6", str(ctx.exception))

    def test_change_elsewhere_on_both_sides_is_a_conflict(self):
        with self.assertRaises(SettingsConflict):
            merge_settings("TagManager.asset", tag_manager([], BASE_LAYERS),
                           tag_manager(["x"], BASE_LAYERS, sorting="Front"), tag_manager(["y"], BASE_LAYERS, sorting="Back"))

    def test_change_elsewhere_on_one_side_is_kept(self):
        out = merge_settings("TagManager.asset", tag_manager([], BASE_LAYERS),
                             tag_manager(["x"], BASE_LAYERS), tag_manager(["y"], BASE_LAYERS, sorting="Back"))
        self.assertEqual(out, tag_manager(["x", "y"], BASE_LAYERS, sorting="Back"))


class BuildSceneTests(unittest.TestCase):
    def test_both_add_scenes_target_order_first(self):
        out = merge_settings("EditorBuildSettings.asset", build_settings(A, B), build_settings(A, B, C), build_settings(A, B, D))
        self.assertEqual(out, build_settings(A, B, C, D))

    def test_removed_on_one_side(self):
        out = merge_settings("EditorBuildSettings.asset", build_settings(A, B), build_settings(A, B, C), build_settings(A))
        self.assertEqual(out, build_settings(A, C))

    def test_toggle_on_one_side(self):
        base = build_settings(A, B)
        theirs = base.replace("  - enabled: 1\n    path: Assets/B.unity", "  - enabled: 0\n    path: Assets/B.unity")
        out = merge_settings("EditorBuildSettings.asset", base, build_settings(A, B, C), theirs)
        self.assertIn("  - enabled: 0\n    path: Assets/B.unity", out)
        self.assertIn("Assets/C.unity", out)

    def test_removed_and_changed_is_a_conflict(self):
        base = build_settings(A, B)
        theirs = base.replace("  - enabled: 1\n    path: Assets/B.unity", "  - enabled: 0\n    path: Assets/B.unity")
        with self.assertRaises(SettingsConflict):
            merge_settings("EditorBuildSettings.asset", base, build_settings(A), theirs)

    def test_unknown_file(self):
        with self.assertRaises(SettingsConflict):
            merge_settings("InputManager.asset", "", "", "")


if __name__ == "__main__":
    unittest.main()
