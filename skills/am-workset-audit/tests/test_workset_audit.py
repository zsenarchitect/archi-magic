# -*- coding: utf-8 -*-
"""Tests for the pure decision logic in scripts/workset_audit.py (no Revit needed).

Run either way:
    python -m pytest skills/am-workset-audit/tests -q
    python skills/am-workset-audit/tests/test_workset_audit.py
Outside Revit `pyrevit.api` is missing, so the script sets DB = None and does not run the
audit on import. Only the pure functions are tested here. All data is invented.
"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "workset_audit.py")

_spec = importlib.util.spec_from_file_location("workset_audit", SCRIPT)
wa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wa)

# Invented workset table: (workset id, name).
WORKSETS = [
    (101, "Workset1"),
    (102, "Shared Levels and Grids"),
    (103, "2_RVT Links"),
    (104, "3_CAD Links"),
    (105, "Exterior Facade"),
    (106, "Interior"),
]


def cad_target(pairs, wanted=None):
    """The exact call the cad_links rule makes."""
    return wa.match_workset(pairs, wanted, ["cad"], ["link"], word=True)


def links_target(pairs, wanted=None):
    """The exact call the links rule makes."""
    return wa.match_workset(pairs, wanted, ["link"], ["rvt", "revit"])


# ---------------------------------------------------------------- target matching
def test_import_does_not_run_the_audit():
    assert wa.DB is None
    assert not hasattr(wa, "result")


def test_cad_target_is_the_cad_workset_not_facade():
    assert cad_target(WORKSETS) == (104, ["3_CAD Links"])


def test_links_target_still_prefers_rvt_over_cad():
    assert links_target(WORKSETS) == (103, ["2_RVT Links", "3_CAD Links"])


def test_name_has_whole_word():
    assert wa.name_has("3_CAD Links", "cad", word=True)
    assert wa.name_has("Links-CAD", "cad", word=True)
    assert wa.name_has("cad", "cad", word=True)
    assert not wa.name_has("Exterior Facade", "cad", word=True)
    assert not wa.name_has("Arcade", "cad", word=True)
    assert not wa.name_has("Cascade", "cad", word=True)
    # Substring mode is unchanged for the other rules.
    assert wa.name_has("Exterior Facade", "cad")


def test_cad_target_none_when_no_cad_workset():
    pairs = [(1, "Workset1"), (2, "Exterior Facade"), (3, "2_RVT Links")]
    assert cad_target(pairs) == (None, [])


def test_cad_target_none_when_two_cad_worksets():
    pairs = [(1, "CAD Links"), (2, "CAD Links Site"), (3, "Interior")]
    assert cad_target(pairs) == (None, ["CAD Links", "CAD Links Site"])


def test_cad_target_prefers_the_links_one_of_two():
    pairs = [(1, "CAD Imports"), (2, "CAD Links")]
    assert cad_target(pairs) == (2, ["CAD Imports", "CAD Links"])


def test_explicit_id_and_exact_name_win():
    assert cad_target(WORKSETS, 106) == (106, ["Interior"])
    assert cad_target(WORKSETS, "interior") == (106, ["Interior"])
    assert cad_target(WORKSETS, 999) == (None, [])
    assert cad_target(WORKSETS, "No Such Workset") == (None, [])


# ---------------------------------------------------------------- off-target CAD
def test_split_cad_off_target_and_view_specific():
    items = [
        (1, 104, False),  # already on target
        (2, 105, False),  # off target
        (3, 106, False),  # off target
        (4, 900, True),   # view-specific: skipped, never off target
        (5, 104, True),   # view-specific, even on target: still skipped
    ]
    off, skipped = wa.split_cad(items, 104)
    assert off == [2, 3]
    assert skipped == [{"element_id": 4, "reason": "view workset"},
                       {"element_id": 5, "reason": "view workset"}]


def test_split_cad_all_on_target():
    items = [(1, 104, False), (2, 104, False), (3, 900, True)]
    off, skipped = wa.split_cad(items, 104)
    assert off == []
    assert skipped == [{"element_id": 3, "reason": "view workset"}]


def test_split_cad_no_target_lists_every_model_cad():
    items = [(1, 104, False), (2, 105, False), (3, 900, True)]
    off, skipped = wa.split_cad(items, None)
    assert off == [1, 2]
    assert skipped == [{"element_id": 3, "reason": "view workset"}]


def test_split_cad_empty():
    assert wa.split_cad([], 104) == ([], [])


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print("ok: %d tests" % n)
