"""Offline tests for scripts/rhinocode_job.py (no Rhino needed)."""
import datetime
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rhinocode_job", os.path.join(HERE, "..", "scripts", "rhinocode_job.py"))
j = importlib.util.module_from_spec(spec)
spec.loader.exec_module(j)


class FakeObjects(object):
    Count = 3


class FakeDoc(object):
    Name = "Example.3dm"
    Objects = FakeObjects()


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class RunJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "job")

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_does_not_run_without_rhino(self):
        self.assertIsNone(j.Rhino)

    def test_success_writes_result_then_done(self):
        self.assertTrue(j.run_job(j.job, self.base, FakeDoc()))
        lines = read(self.base + ".log").splitlines()
        self.assertTrue(lines[0].startswith("started "))
        self.assertEqual(lines[-1], "done")
        self.assertEqual(json.loads(read(self.base + ".json")), {"document": "Example.3dm", "objects": 3})
        self.assertFalse(os.path.exists(self.base + ".json.tmp"))

    def test_failure_writes_traceback_and_no_result(self):
        def boom(doc):
            raise ValueError("bad input")
        self.assertFalse(j.run_job(boom, self.base, FakeDoc()))
        log = read(self.base + ".log")
        self.assertIn("\nfailed\n", log)
        self.assertIn("ValueError: bad input", log)
        self.assertFalse(os.path.exists(self.base + ".json"))

    def test_old_files_from_a_previous_run_are_removed_first(self):
        j.run_job(j.job, self.base, FakeDoc())

        def boom(doc):
            raise RuntimeError("second run")
        j.run_job(boom, self.base, FakeDoc())
        self.assertFalse(os.path.exists(self.base + ".json"))
        self.assertNotIn("done", read(self.base + ".log").splitlines())

    def test_non_json_values_are_stored_as_text(self):
        j.run_job(lambda doc: {"when": datetime.date(2026, 1, 2)}, self.base, FakeDoc())
        self.assertEqual(json.loads(read(self.base + ".json")), {"when": "2026-01-02"})


if __name__ == "__main__":
    unittest.main()
