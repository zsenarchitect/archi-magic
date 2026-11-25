"""Offline tests for scripts/rhino_texture_check.py (no Rhino needed)."""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rhino_texture_check", os.path.join(HERE, "..", "scripts", "rhino_texture_check.py"))
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

NEW_A = "C:\\Projects\\Project A\\textures\\model-a"
NEW_B = "C:\\Projects\\Project A\\textures\\model-b"


class Paths(unittest.TestCase):
    def test_in_folder_ignores_case_and_separators(self):
        self.assertTrue(t.in_folder("c:/projects/project a/TEXTURES/model-a/wood.jpg", NEW_A + "\\"))

    def test_sibling_folder_with_same_prefix_is_outside(self):
        self.assertFalse(t.in_folder(NEW_A + "-old\\wood.jpg", NEW_A))

    def test_repeated_names_from_two_models_clash_in_one_folder(self):
        paths = [NEW_A + "\\Unnamed-0-3.png", NEW_B + "\\Unnamed-0-3.png", NEW_A + "\\wood.jpg"]
        self.assertEqual(t.name_clashes(paths), ["unnamed-0-3.png"])

    def test_same_file_twice_is_not_a_clash(self):
        self.assertEqual(t.name_clashes([NEW_A + "\\wood.jpg", NEW_A.upper() + "\\WOOD.jpg"]), [])


class Summary(unittest.TestCase):
    def test_clean_after_repath(self):
        entries = [{"material": "Wood", "path": NEW_A + "\\wood.jpg"},
                   {"material": "Floor", "path": NEW_A + "\\wood.jpg"},
                   {"material": "Glass", "path": NEW_A + "\\Unnamed-0-3.png"}]
        s = t.summarize_textures(entries, NEW_A, lambda p: True, 0)
        self.assertTrue(s["ok"])
        self.assertEqual((s["texture_count"], s["unique_files"]), (3, 2))

    def test_texture_left_in_another_models_folder_is_reported(self):
        # The leftover-materials pitfall: a material from the previous file still points elsewhere.
        entries = [{"material": "Wood", "path": NEW_A + "\\wood.jpg"},
                   {"material": "Old", "path": NEW_B + "\\Unnamed-0-3.png"}]
        s = t.summarize_textures(entries, NEW_A, lambda p: True, 0)
        self.assertFalse(s["ok"])
        self.assertEqual(s["outside_folder"]["paths"], [NEW_B + "\\Unnamed-0-3.png"])

    def test_missing_and_embedded_are_problems(self):
        entries = [{"material": "Wood", "path": NEW_A + "\\wood.jpg"}]
        s = t.summarize_textures(entries, NEW_A, lambda p: False, 12)
        self.assertEqual(s["missing"]["count"], 1)
        self.assertEqual(len(s["problems"]), 2)

    def test_embedded_unknown_is_not_a_problem(self):
        s = t.summarize_textures([], "", lambda p: True, None)
        self.assertTrue(s["ok"])
        self.assertIsNone(s["embedded_files"])

    def test_no_folder_given_skips_outside_check(self):
        s = t.summarize_textures([{"material": "W", "path": "D:\\any\\w.jpg"}], "", lambda p: True, 0)
        self.assertEqual(s["outside_folder"]["count"], 0)


class Fakes(unittest.TestCase):
    class Tex(object):
        def __init__(self, f):
            self.FileName = f

    def test_material_without_get_textures_uses_slots(self):
        class Old(object):
            def GetBitmapTexture(self):
                return Fakes.Tex("a.jpg")

            def GetBumpTexture(self):
                return None
        self.assertEqual(t.material_textures(Old()), ["a.jpg"])

    def test_embedded_count_from_count_or_len_or_unknown(self):
        class WithCount(object):
            class EmbeddedFiles(object):
                Count = 4

        class WithList(object):
            EmbeddedFiles = [1, 2]

        self.assertEqual(t.embedded_file_count(WithCount()), 4)
        self.assertEqual(t.embedded_file_count(WithList()), 2)
        self.assertIsNone(t.embedded_file_count(object()))


if __name__ == "__main__":
    unittest.main()
