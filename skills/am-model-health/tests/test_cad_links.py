# -*- coding: utf-8 -*-
"""Tests for the CAD link audit pure functions in scripts/model_health.py (issue #59).

The script runs inside Revit, but its imports are guarded, so plain CPython can load it
and use the pure functions. All data here is invented.

Run:
    python -m pytest -q skills/am-model-health/tests
"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "model_health.py")


def load():
    spec = importlib.util.spec_from_file_location("model_health", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mh = load()


def row(**kw):
    base = {"key": 1, "id": 1, "name": "site.dwg", "kind": "linked", "path": "",
            "path_kind": None, "status": "Loaded", "workset": "CAD Links", "pinned": True,
            "view_specific": False, "owner_view_id": 0, "views": 3}
    base.update(kw)
    return base


def test_loads_outside_revit_without_running_the_check():
    assert mh.DB is None
    assert mh.result["error"] == "no_project_document"


def test_personal_path_kind():
    assert mh.personal_path_kind(r"C:\Users\someone\OneDrive - Example Co\Lib\a.dwg") == ("onedrive", "someone")
    assert mh.personal_path_kind("c:/users/someone/OneDrive/a.dwg") == ("onedrive", "someone")
    assert mh.personal_path_kind(r"C:\Users\someone\Example Co\Site - Docs\a.dwg") == ("sync", "someone")
    assert mh.personal_path_kind(r"C:\Users\someone\Desktop\a.dwg") == ("profile", "someone")
    assert mh.personal_path_kind(r"C:\Users\someone\Downloads\a.dwg") == ("profile", "someone")


def test_shared_paths_are_not_personal():
    for p in (r"\\server\projects\a.dwg", r"P:\Project\CAD\a.dwg", r"C:\Users\Public\a.dwg",
              "Autodesk Docs://Project/a.dwg", "", None):
        assert mh.personal_path_kind(p) == (None, None), p


def test_cad_status():
    assert mh.cad_status(False, None) == "Imported"
    assert mh.cad_status(False, "Loaded") == "Imported"
    assert mh.cad_status(True, None) == "unreadable"
    assert mh.cad_status(True, "") == "unreadable"
    assert mh.cad_status(True, "NotFound") == "NotFound"
    assert mh.cad_status(True, "Unloaded") == "Unloaded"


def test_median():
    assert mh.median([]) is None
    assert mh.median([3]) == 3.0
    assert mh.median([5, 1, 3]) == 3.0
    assert mh.median([4, 1, 3, 2]) == 2.5


def test_outlier_far_from_shared_group():
    pts = [("a", 1000.0, 2000.0), ("b", 1000.0, 2000.0), ("c", 1001.0, 2000.0), ("d", 1000.0, 5000.0)]
    out = mh.origin_outliers(pts, 1000.0)
    assert set(out) == {"d"}
    assert round(out["d"]) == 3000


def test_outlier_needs_three_points():
    assert mh.origin_outliers([("a", 0.0, 0.0), ("b", 9000.0, 0.0)], 1000.0) == {}


def test_mixed_placement_does_not_flag_every_group():
    # Four links by shared coordinates far away, three near the internal origin.
    pts = [("s1", -90000.0, -60000.0), ("s2", -90000.0, -60000.0), ("s3", -90000.0, -60000.0),
           ("s4", -90000.0, -60000.0), ("o1", 5.0, -12.0), ("o2", 5.0, -12.0), ("o3", 14.0, 10.0)]
    assert mh.origin_outliers(pts, 1000.0) == {}
    pts.append(("hand", 1050.0, 450.0))
    assert set(mh.origin_outliers(pts, 1000.0)) == {"hand"}


def test_duplicate_cad_keys():
    rows = [row(key=1, name="Survey.dwg"), row(key=2, name="survey.DWG"),
            row(key=3, name="Plan.dwg"),
            row(key=4, name="Detail.dwg", owner_view_id=50), row(key=5, name="Detail.dwg", owner_view_id=51),
            row(key=6, name="Note.dwg", owner_view_id=60), row(key=7, name="Note.dwg", owner_view_id=60)]
    assert mh.duplicate_cad_keys(rows) == {1, 2, 6, 7}


def test_clean_link_has_no_flags():
    assert mh.cad_link_flags(row(), True) == []


def test_flags_personal_onedrive_not_found():
    flags = mh.cad_link_flags(row(path_kind="onedrive", status="NotFound"), True)
    assert flags == ["personal OneDrive/SharePoint path", "file not found"]


def test_flags_each_problem():
    assert mh.cad_link_flags(row(path_kind="sync"), True) == ["personal folder path"]
    assert mh.cad_link_flags(row(status="unreadable"), True) == ["path unreadable"]
    assert mh.cad_link_flags(row(status="Unloaded"), True) == ["not loaded (Unloaded)"]
    assert mh.cad_link_flags(row(pinned=False), True) == ["not pinned"]
    assert mh.cad_link_flags(row(workset="Shared Levels and Grids"), True) == ["not on a CAD workset"]
    assert mh.cad_link_flags(row(), True, outlier_ft=2970.4) == ["far from the other CAD links (nearest 2970 ft)"]
    assert mh.cad_link_flags(row(), True, duplicate=True) == ["same file placed more than once"]
    assert mh.cad_link_flags(row(views=55), True, many_views=25) == ["shows in 55 views"]


def test_workset_flag_skipped_when_not_workshared_or_view_specific():
    assert mh.cad_link_flags(row(workset=""), False) == []
    assert mh.cad_link_flags(row(workset="View \"Plan\"", view_specific=True, views=1), True) == []


def test_imported_cad_is_not_reported_as_unreadable():
    assert mh.cad_link_flags(row(kind="imported", status="Imported"), True) == []


def test_cad_link_details_lists_flagged_rows_most_flags_first():
    rows = [row(id=1, name="b.dwg", flags=["not pinned"]),
            row(id=2, name="a.dwg", flags=[]),
            row(id=3, name="c.dwg", status="NotFound", flags=["file not found", "not pinned"])]
    d = mh.cad_link_details(rows)
    assert [x["ids"] for x in d] == [[3], [1]]
    assert d[0]["count"] == 2
    assert d[0]["label"] == "c.dwg (linked, NotFound): file not found; not pinned"
