"""Offline tests for scripts/rhino_massing_check.py (no Rhino needed)."""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rhino_massing_check", os.path.join(HERE, "..", "scripts", "rhino_massing_check.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Layers(unittest.TestCase):
    def test_sublayers_are_selected(self):
        self.assertTrue(m.layer_selected("Massing::opt1.1::Tower", ["Massing::opt1.1"]))

    def test_similar_name_is_not_a_sublayer(self):
        # opt1.10 must not match opt1.1
        self.assertFalse(m.layer_selected("Massing::opt1.10", ["Massing::opt1.1"]))

    def test_empty_filter_selects_nothing(self):
        self.assertFalse(m.layer_selected("Massing", []))


class OptionNames(unittest.TestCase):
    def test_first_subnumber(self):
        self.assertEqual(m.next_option_name("opt1", ["opt1", "opt2"]), "opt1.1")

    def test_subnumber_goes_up(self):
        self.assertEqual(m.next_option_name("opt2.1", ["opt1", "opt2", "opt2.1"]), "opt2.2")

    def test_never_reuses_a_taken_name(self):
        # opt2.2 and opt2.3 exist already (made from opt2.1 earlier): the next one is opt2.4.
        self.assertEqual(m.next_option_name("opt2.1", ["opt2.1", "opt2.2", "OPT2.3"]), "opt2.4")

    def test_other_branches_do_not_count(self):
        self.assertEqual(m.next_option_name("opt1", ["opt1", "opt10.4", "opt2.7"]), "opt1.1")

    def test_name_without_number(self):
        self.assertEqual(m.next_option_name("Tower", ["Tower", "Tower.1"]), "Tower.2")


class Downward(unittest.TestCase):
    FACES = [
        {"nz": -1.0, "z": 0.0, "area": 900.0},     # ground face of the mass
        {"nz": 1.0, "z": 120.0, "area": 900.0},    # roof
        {"nz": 0.0, "z": 60.0, "area": 400.0},     # wall
        {"nz": -1.0, "z": 120.0, "area": 12.0},    # sliver hanging past the facade at roof level
        {"nz": -0.5, "z": 80.0, "area": 50.0},     # sloped soffit, not steep enough by default
    ]

    def test_only_raised_downward_faces_count(self):
        d = m.downward_faces(self.FACES, 0.0, 0.01)
        self.assertEqual([f["area"] for f in d], [12.0])

    def test_limit_can_include_sloped_soffits(self):
        d = m.downward_faces(self.FACES, 0.0, 0.01, limit=-0.4)
        self.assertEqual(sorted(f["area"] for f in d), [12.0, 50.0])

    def test_face_within_tolerance_of_bottom_is_ground(self):
        self.assertEqual(m.downward_faces([{"nz": -1.0, "z": 0.005, "area": 1.0}], 0.0, 0.01), [])


class Volume(unittest.TestCase):
    def test_change_against_base(self):
        v = m.volume_change(1100.0, 1000.0)
        self.assertEqual((v["delta"], v["percent"]), (100.0, 10.0))

    def test_no_base(self):
        v = m.volume_change(1100.0, None)
        self.assertIsNone(v["delta"])
        self.assertIsNone(v["percent"])

    def test_zero_base_has_delta_no_percent(self):
        v = m.volume_change(5.0, 0.0)
        self.assertEqual(v["delta"], 5.0)
        self.assertIsNone(v["percent"])


class Findings(unittest.TestCase):
    OBJECTS = [
        {"id": "a", "layer": "opt2.2", "solid": True, "downward": []},
        {"id": "b", "layer": "opt2.2", "solid": False, "downward": []},
        {"id": "c", "layer": "opt2.2", "solid": True, "downward": [{"nz": -1.0, "z": 120.0, "area": 12.0}]},
        {"id": "d", "layer": "opt2.2", "solid": True, "downward": [{"nz": -1.0, "z": 40.0, "area": 300.0}]},
        {"id": "e", "layer": "opt2.2", "solid": None, "downward": []},
    ]

    def test_ranked_and_complete(self):
        f = m.build_findings(self.OBJECTS, 30.0, 50)
        self.assertEqual([x["code"] for x in f], ["NOT_SOLID", "SLIVER_FACES", "DOWNWARD_FACES", "NOT_CHECKED"])
        by = dict((x["code"], x) for x in f)
        self.assertEqual(by["NOT_SOLID"]["ids"], ["b"])
        self.assertEqual(by["SLIVER_FACES"]["ids"], ["c"])
        self.assertEqual(by["DOWNWARD_FACES"]["ids"], ["c", "d"])
        self.assertEqual(by["NOT_CHECKED"]["ids"], ["e"])

    def test_sliver_check_off_by_default(self):
        f = m.build_findings(self.OBJECTS, 0, 50)
        self.assertNotIn("SLIVER_FACES", [x["code"] for x in f])

    def test_clean_option_has_no_findings(self):
        self.assertEqual(m.build_findings(self.OBJECTS[:1], 30.0, 50), [])


if __name__ == "__main__":
    unittest.main()
