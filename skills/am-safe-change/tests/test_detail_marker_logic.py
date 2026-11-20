# -*- coding: utf-8 -*-
"""Tests for scripts/lib/detail_marker_logic.py and scripts/detail_marker_plan.py
(issue #257, comments of 2026-11-18). No Revit needed.

Run either way:
    python -m pytest skills/am-safe-change/tests -q
    python skills/am-safe-change/tests/test_detail_marker_logic.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, os.path.join(SCRIPTS, "lib"))
sys.path.insert(0, SCRIPTS)

import detail_marker_logic as dm  # noqa: E402
import detail_marker_plan as cli  # noqa: E402
import detail_preview  # noqa: E402

SHEET = 100
PLAN_L1 = 10   # finish floor plan, level 1
PLAN_L2 = 20   # finish floor plan, level 2
OLD = 300      # existing detail, cut on level 1, on the detail sheet
NEW = 400      # new detail that the user removed by hand (viewport only)
OTHER = 500    # placed detail of another condition
MARKER_L2 = 7001   # plan marker on level 2 that points to the new detail
MARKER_L1 = 7002   # plan marker on level 1 that points to the old detail


def read_fixture(new_on_sheet=False):
    """The comment's state: the new detail's viewport was deleted by hand."""
    new_sheet = ({"sheet_id": SHEET, "sheet_number": "A-401", "viewport_id": 4100,
                  "detail_number": "24"} if new_on_sheet else None)
    return {
        "document": "Test",
        "views": [
            {"id": PLAN_L1, "name": "Level 1 finish plan", "view_type": "FloorPlan",
             "title_on_sheet": None, "sheet": None, "hidden_viewport_ids": []},
            {"id": PLAN_L2, "name": "Level 2 finish plan", "view_type": "FloorPlan",
             "title_on_sheet": None, "sheet": None, "hidden_viewport_ids": []},
            {"id": OLD, "name": "Floor transition 12", "view_type": "Detail",
             "title_on_sheet": "Carpet to porcelain tile",
             "sheet": {"sheet_id": SHEET, "sheet_number": "A-401", "viewport_id": 3100,
                       "detail_number": "12"},
             "hidden_viewport_ids": [3101],
             "floor_types": ["Finish - Carpet A", "Finish - Porcelain Tile B", "Slab 8in"]},
            {"id": NEW, "name": "Floor transition 24", "view_type": "Detail",
             "title_on_sheet": "Carpet to porcelain tile", "sheet": new_sheet,
             "hidden_viewport_ids": [4101],
             "floor_types": ["Finish - Carpet A", "Finish - Porcelain Tile B", "Slab 8in"]},
            {"id": OTHER, "name": "Floor transition 13", "view_type": "Detail",
             "title_on_sheet": "Carpet to resilient",
             "sheet": {"sheet_id": SHEET, "sheet_number": "A-401", "viewport_id": 5100,
                       "detail_number": "13"},
             "hidden_viewport_ids": [5101],
             "floor_types": ["Finish - Carpet A", "Finish - Resilient C", "Slab 8in"]},
        ],
        "references": [
            {"id": MARKER_L2, "kind": "section", "name": "Floor transition 24",
             "host_view_id": PLAN_L2, "target_view_id": NEW},
            {"id": MARKER_L1, "kind": "section", "name": "Floor transition 12",
             "host_view_id": PLAN_L1, "target_view_id": OLD},
        ],
        "dependents": {str(NEW): [
            {"id": NEW, "category": "Views", "class": "ViewSection", "name": "Floor transition 24",
             "owner_view_id": None},
            {"id": 4101, "category": "Viewports", "class": "Viewport", "name": "No Title",
             "owner_view_id": None},
            {"id": 4102, "category": "Views", "class": "Element", "name": "Floor transition 24",
             "owner_view_id": None},
            {"id": 4103, "category": None, "class": "SketchPlane", "name": "", "owner_view_id": None},
            {"id": MARKER_L2, "category": "Views", "class": "Element", "name": "Floor transition 24",
             "owner_view_id": PLAN_L2},
        ]},
        "cuts": [{"name": "M1", "floor_types": ["Finish - Carpet A", "Finish - Porcelain Tile B"]}],
    }


# ------------------------------------------------------ duplicate by condition

def test_same_condition_by_title_finds_detail_cut_on_another_level():
    found = dm.find_same_condition(read_fixture(), ["porcelain tile", "Carpet"], sheet_id=SHEET)
    assert [r["view_id"] for r in found["same"]] == [OLD]
    assert [r["view_id"] for r in found["partial"]] == [OTHER]
    assert found["partial"][0]["missing"] == ["porcelain tile"]


def test_same_condition_word_order_and_punctuation_do_not_matter():
    found = dm.find_same_condition(read_fixture(), ["TILE, PORCELAIN"], sheet_id=SHEET)
    # one phrase in another word order is not a whole-word match
    assert found["same"] == []
    found = dm.find_same_condition(read_fixture(), ["porcelain-tile", "carpet"], sheet_id=SHEET)
    assert [r["view_id"] for r in found["same"]] == [OLD]


def test_same_condition_by_floor_types_of_the_cut():
    read = read_fixture()
    floors = dm.cut_floor_types(read, "M1")
    found = dm.find_same_condition(read, floor_types=floors, sheet_id=SHEET)
    assert [r["view_id"] for r in found["same"]] == [OLD]
    assert found["same"][0]["floor_types_matched"] == floors


def test_same_condition_needs_two_parts_for_same():
    found = dm.find_same_condition(read_fixture(), ["carpet"], sheet_id=SHEET)
    assert found["same"] == []
    assert sorted(r["view_id"] for r in found["partial"]) == [OLD, OTHER]


def test_same_condition_skips_views_on_no_sheet_and_excluded():
    found = dm.find_same_condition(read_fixture(), ["carpet", "porcelain tile"], exclude_view_ids=[OLD])
    assert found["same"] == []


def test_same_condition_rejects_empty_request():
    try:
        dm.find_same_condition(read_fixture(), [" "], [])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_cut_floor_types_unknown_cut():
    try:
        dm.cut_floor_types(read_fixture(), "nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# --------------------------------------------------------------------- audit

def test_audit_finds_marker_of_view_on_no_sheet():
    rows = dm.audit_references(read_fixture())
    assert len(rows) == 1
    assert rows[0]["marker_id"] == MARKER_L2
    assert rows[0]["host_view"] == "Level 2 finish plan"
    assert rows[0]["target_view_id"] == NEW
    assert rows[0]["state"] == "target on no sheet"
    assert "Level 2 finish plan" in dm.audit_text(rows)


def test_audit_clean_when_every_target_is_placed():
    assert dm.audit_references(read_fixture(new_on_sheet=True)) == []


def test_audit_missing_target():
    read = read_fixture()
    read["references"].append({"id": 7003, "kind": "callout", "host_view_id": PLAN_L1,
                               "target_view_id": 999})
    states = dict((r["marker_id"], r["state"]) for r in dm.audit_references(read))
    assert states[7003] == "target view missing"


def test_removal_state():
    assert dm.removal_state(read_fixture(), NEW)["state"] == "viewport removed"
    assert dm.removal_state(read_fixture(new_on_sheet=True), NEW)["state"] == "placed"
    assert dm.removal_state(read_fixture(), 999)["state"] == "view missing"
    assert [r["id"] for r in dm.removal_state(read_fixture(), NEW)["references"]] == [MARKER_L2]


# --------------------------------------------------------------------- plans

def test_retarget_only():
    p = dm.plan_changes(read_fixture(), retargets=[(MARKER_L2, OLD)])
    assert not p["blocked"]
    assert p["steps"] == [{"action": "retarget", "marker_id": MARKER_L2, "from_view_id": NEW,
                           "to_view_id": OLD}]
    assert p["before"][0][3].startswith("Floor transition 24")
    assert p["after"][0][4] == "Detail 12 on A-401"
    assert p["deletions"] == []
    assert p["ownership_ids"] == [MARKER_L2]


def test_retarget_guards():
    read = read_fixture()
    assert dm.plan_changes(read, retargets=[(MARKER_L2, PLAN_L2)])["blocked"]  # its own host view
    assert dm.plan_changes(read, retargets=[(9999, OLD)])["blocked"]          # not a marker
    assert dm.plan_changes(read, retargets=[(MARKER_L2, 9999)])["blocked"]    # unknown target
    assert dm.plan_changes(read, retargets=[(MARKER_L2, PLAN_L1)])["blocked"]  # plan is not a section target
    same = dm.plan_changes(read, retargets=[(MARKER_L1, OLD)])                  # no change
    assert same["blocked"] and same["steps"] == []


def test_remove_blocks_until_every_marker_has_a_decision():
    p = dm.plan_changes(read_fixture(), remove_view_id=NEW)
    assert p["blocked"]
    assert [q["marker_id"] for q in p["questions"]] == [MARKER_L2]
    assert "Level 2 finish plan" in p["questions"][0]["text"]
    assert "ASK:" in dm.plan_text(p)


def test_remove_after_manual_viewport_delete_retargets_first():
    p = dm.plan_changes(read_fixture(), retargets=[(MARKER_L2, OLD)], remove_view_id=NEW)
    assert not p["blocked"], p["warnings"]
    assert [s["action"] for s in p["steps"]] == ["retarget", "delete_view"]
    deleted = [int(d["id"]) for d in p["deletions"]]
    assert MARKER_L2 not in deleted
    assert sorted(deleted) == sorted([NEW, 4101, 4102, 4103])
    assert p["steps"][1]["expected_deleted_ids"] == sorted(deleted)
    assert any("already removed" in n for n in p["notes"])
    assert all(d["category"] and d["name"] for d in p["deletions"])
    assert "(no category)" in p["expected_dependent_categories"]
    assert set(p["ownership_ids"]) == set([MARKER_L2, NEW, 4101, 4102, 4103])


def test_remove_with_marker_deleted_only_when_chosen():
    p = dm.plan_changes(read_fixture(), remove_view_id=NEW, delete_markers=[MARKER_L2])
    assert not p["blocked"], p["warnings"]
    assert MARKER_L2 in [int(d["id"]) for d in p["deletions"]]


def test_remove_guards():
    read = read_fixture()
    both = dm.plan_changes(read, retargets=[(MARKER_L2, OLD)], remove_view_id=NEW, delete_markers=[MARKER_L2])
    assert both["blocked"]
    into_deleted = dm.plan_changes(read, retargets=[(MARKER_L1, NEW)], remove_view_id=NEW,
                                   delete_markers=[MARKER_L2])
    assert into_deleted["blocked"]
    wrong_marker = dm.plan_changes(read, remove_view_id=NEW, delete_markers=[MARKER_L1, MARKER_L2])
    assert wrong_marker["blocked"]
    del read["dependents"]
    no_deps = dm.plan_changes(read, retargets=[(MARKER_L2, OLD)], remove_view_id=NEW)
    assert no_deps["blocked"]
    assert dm.plan_changes(read_fixture(), remove_view_id=999)["blocked"]


def test_remove_placed_view_lists_its_view_first_when_dependents_omit_it():
    read = read_fixture(new_on_sheet=True)
    read["dependents"][str(NEW)] = [d for d in read["dependents"][str(NEW)] if d["id"] != NEW]
    p = dm.plan_changes(read, retargets=[(MARKER_L2, OLD)], remove_view_id=NEW)
    assert not p["blocked"]
    assert p["deletions"][0] == {"id": str(NEW), "category": "Views", "name": "Floor transition 24"}
    assert not any("already removed" in n for n in p["notes"])


def test_add_reference():
    spec = {"host_view_id": PLAN_L2, "target_view_id": OLD, "head": [1, 2, 0], "tail": [1, 5, 0]}
    p = dm.plan_changes(read_fixture(), add_references=[spec])
    assert not p["blocked"], p["warnings"]
    assert p["steps"][0]["action"] == "add_reference"
    assert p["after"][0][3].startswith("Floor transition 12")
    bad = dict(spec, tail=[1, 2, 5])
    assert dm.plan_changes(read_fixture(), add_references=[bad])["blocked"]
    into_plan = dict(spec, target_view_id=PLAN_L1)
    assert dm.plan_changes(read_fixture(), add_references=[into_plan])["blocked"]


def test_empty_plan_is_blocked():
    assert dm.plan_changes(read_fixture())["blocked"]


# -------------------------------------------------------------------- verify

def test_compare_apply_result_and_fresh_read():
    p = dm.plan_changes(read_fixture(), retargets=[(MARKER_L2, OLD)], remove_view_id=NEW)
    ok = {"retargeted": [[MARKER_L2, OLD]], "added": [], "deleted": [NEW, 4101, 4102, 4103]}
    assert dm.compare(p, ok) == []
    extra = dict(ok, deleted=[NEW, 4101, 4102, 4103, 4104])
    assert "Extra: 4104" in dm.compare(p, extra)[0]
    wrong = dict(ok, retargeted=[[MARKER_L2, NEW]])
    assert dm.compare(p, wrong)

    after = read_fixture()
    after["views"] = [v for v in after["views"] if v["id"] != NEW]
    after["references"][0]["target_view_id"] = OLD
    assert dm.compare(p, after) == []
    assert dm.audit_references(after) == []
    stale = read_fixture()
    diffs = dm.compare(p, stale)
    assert any("still in the model" in d for d in diffs)
    assert any("points to" in d for d in diffs)


def test_load_read_unwraps_and_unescapes():
    data = {"response": {"result": {"views": [{"id": 1, "name": "Caf\\u00e9", "view_type": "Detail"}],
                                    "references": [], "escaped": True}}}
    read = dm.load_read(data)
    assert read["views"][0]["name"] == u"Café"
    assert read["views"][0]["id"] == 1


# ------------------------------------------------------------------- helpers

def dry_run(by_category):
    return {"response": {"mode": "dry_run", "changes": {"by_category": by_category}}}


def test_helpers_of_one_new_view_are_named():
    run = dry_run({"Views": 3, "Viewports": 2, "Sun Path": 1, "Work Plane Grid": 1,
                   "(no category)": 2, "Sheets": 1})
    s = dm.classify_new_view_changes(run, 1, 1)
    assert s["available"] and s["unexpected"] == []
    kinds = dict((r[0], r[2]) for r in s["rows"])
    assert kinds["Viewports"] == "helper" and kinds["Sun Path"] == "helper"
    text = dm.helpers_text(s)
    assert "No Title" in text and "SheetId -1" in text


def test_helpers_flag_too_many_and_unknown():
    s = dm.classify_new_view_changes(dry_run({"Viewports": 4, "(no category)": 3, "Floors": 1}), 1, 1)
    assert sorted(s["unexpected"]) == ["(no category)", "Floors", "Viewports"]
    nested = {"added": {"Viewports": 2}, "modified": {"Sheets": 1}}
    s = dm.classify_new_view_changes(dry_run(nested), 1, 1)
    assert s["unexpected"] == []
    rows = {"response": {"changes": {"added": [{"id": 1, "category": "Viewports"},
                                               {"id": 2, "category": "Viewports"},
                                               {"id": 3, "category": None}]}}}
    assert dm.classify_new_view_changes(rows, 1, 1)["unexpected"] == []
    assert not dm.classify_new_view_changes({"result": {}}, 1, 1)["available"]


# ----------------------------------------------------------------------- CLI

def write(tmp_path, name, data):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w") as f:
        json.dump(data, f)
    return path


def test_cli_same_condition_audit_plan_compare(tmp_path, capsys):
    read = write(tmp_path, "refs.json", {"result": read_fixture()})
    assert cli.main(["same-condition", "--read", read, "--sheet", str(SHEET), "--cut", "M1"]) == 1
    assert "point the marker to the existing detail" in capsys.readouterr().out
    assert cli.main(["same-condition", "--read", read, "--term", "wood", "--term", "stone"]) == 0
    assert cli.main(["audit", "--read", read]) == 1
    assert cli.main(["state", "--read", read, "--view", str(NEW)]) == 0
    assert "viewport removed" in capsys.readouterr().out
    out = os.path.join(str(tmp_path), "preview.json")
    assert cli.main(["plan", "--read", read, "--remove-view", str(NEW), "--out", out]) == 1
    assert cli.main(["plan", "--read", read, "--retarget", "%d:%d" % (MARKER_L2, OLD),
                     "--remove-view", str(NEW), "--out", out]) == 0
    run = write(tmp_path, "run.json", {"response": {"result": {
        "retargeted": [[MARKER_L2, OLD]], "added": [], "deleted": [NEW, 4101, 4102, 4103]}}})
    assert cli.main(["compare", "--preview", out, "--actual", run]) == 0
    assert cli.main(["plan", "--read", read, "--retarget", "bad", "--out", out]) == 2
    ref = "%d:%d:1,2,0:1,5,0" % (PLAN_L2, OLD)
    assert cli.main(["plan", "--read", read, "--add-reference", ref, "--out", out]) == 0
    dry = write(tmp_path, "dry.json", dry_run({"Viewports": 2, "Views": 2}))
    assert cli.main(["helpers", "--dryrun", dry, "--new-views", "1", "--viewports", "1"]) == 0


def test_detail_preview_expects_view_helpers():
    cats = detail_preview.new_view_categories({"expected_dependent_categories": ["Views"]})
    for name in ("Views", "Viewports", "Sun Path", "Work Plane Grid", "(no category)", "Sheets"):
        assert name in cats


def test_revit_scripts_parse_without_f_strings():
    """The Revit-side scripts must run under IronPython 2.7 (no f-strings)."""
    import ast
    for name in ("detail_read_references.py", "detail_apply_markers.py", "detail_marker_plan.py",
                 os.path.join("lib", "detail_marker_logic.py")):
        with open(os.path.join(SCRIPTS, name), "rb") as f:
            tree = ast.parse(f.read())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)], name


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
