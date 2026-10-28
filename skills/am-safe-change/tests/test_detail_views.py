# -*- coding: utf-8 -*-
"""Tests for scripts/lib/detail_views.py.

Run either way:
    python -m pytest skills/am-safe-change/tests -q
    python skills/am-safe-change/tests/test_detail_views.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "lib"))

import detail_views as dv  # noqa: E402


def read_fixture():
    """One sheet with the field report's viewports, plus two sections."""
    return {
        "document": "Test",
        "sheets": [{
            "id": 100, "number": "A401", "name": "Details",
            "viewports": [
                {"id": 1, "view_id": 11, "view_name": "Detail 10",
                 "detail_number": 10, "viewport_type_id": 900,
                 "label_offset": [0.01, 0.01], "label_line_length": 0.81,
                 "box_center": [1.0, 2.0, 0.0]},
                {"id": 2, "view_id": 12, "view_name": "Detail 11",
                 "detail_number": 11, "viewport_type_id": 900,
                 "label_offset": [0.01, 0.01], "label_line_length": 0.81,
                 "box_center": [3.0, 2.0, 0.0]},
                {"id": 3, "view_id": 13, "view_name": "Detail 12",
                 "detail_number": 12, "viewport_type_id": 900,
                 "label_offset": [0.01, 0.01], "label_line_length": 0.81,
                 "box_center": [5.0, 2.0, 0.0]},
                {"id": 4, "view_id": 14, "view_name": "Detail 20",
                 "detail_number": 20, "viewport_type_id": 900,
                 "label_offset": [0.01, 0.01], "label_line_length": 0.81,
                 "box_center": [1.0, 4.0, 0.0]},
                {"id": 5, "view_id": 15, "view_name": "Detail 21",
                 "detail_number": 21, "viewport_type_id": 900,
                 "label_offset": [0.01, 0.01], "label_line_length": 0.81,
                 "box_center": [3.0, 4.0, 0.0]},
            ],
        }],
        "sections": [
            {"id": 50, "name": "Floor transition A"},
            {"id": 51, "name": "Floor transition B"},
        ],
    }


def test_parse_detail_number():
    assert dv.parse_detail_number(10) == 10
    assert dv.parse_detail_number("21") == 21
    assert dv.parse_detail_number(" 12 ") == 12
    assert dv.parse_detail_number("A301") is None
    assert dv.parse_detail_number("") is None
    assert dv.parse_detail_number(None) is None


def test_propose_follows_the_field_report_pattern():
    # Existing 10-12, 20-21, two new details -> 22 and 30 (issue #50).
    assert dv.propose_detail_numbers([10, 11, 12, 20, 21], 2) == [22, 30]


def test_propose_empty_sheet():
    assert dv.propose_detail_numbers([], 3) == [1, 10, 20]


def test_propose_single_run_continues_then_jumps_decade():
    assert dv.propose_detail_numbers([5, 6, 7], 3) == [8, 10, 20]


def test_propose_ignores_duplicates_and_sorts():
    assert dv.propose_detail_numbers([21, 10, 21, 12], 1) == [22]


def test_compute_copies_labels_and_numbers():
    read = read_fixture()
    details = [{"name": "North transition", "x": 7.5, "y": 2.0, "source_view_id": 50,
                "source_view_name": "Floor transition A"},
               {"name": "South transition", "x": 7.5, "y": 4.0, "source_view_id": 51,
                "source_view_name": "Floor transition B"}]
    p = dv.compute_detail_plan(read, 100, 1, details)
    assert not p["blocked"]
    assert [r[1] for r in p["after"]] == [22, 30]
    # Label settings come from the reference viewport, not the Create default.
    assert p["after"][0][2] == "0.01, 0.01"
    assert p["after"][0][3] == "0.81"
    assert p["after"][0][4] == "7.50, 2.00"
    assert p["steps"][0]["viewport_type_id"] == 900
    assert p["steps"][0]["box_center"] == [7.5, 2.0, 0.0]
    assert p["steps"][0]["source_view_id"] == 50


def test_compare_matches_and_differs():
    read = read_fixture()
    details = [{"name": "North transition", "x": 7.5, "y": 2.0, "source_view_id": 50,
                "source_view_name": "Floor transition A"}]
    p = dv.compute_detail_plan(read, 100, 1, details)
    good = [["North transition", 22, [0.01, 0.01], 0.81, [7.5, 2.0]]]
    assert dv.compare(p, good) == []
    bad = [["North transition", 22, [-1.16, 0.01], 0.26, [8.9, 2.0]]]
    diffs = dv.compare(p, bad)
    assert len(diffs) == 1
    missing = []
    assert dv.compare(p, missing) == ["North transition: missing from the actual result"]


def test_compute_blocks_on_missing_reference():
    read = read_fixture()
    details = [{"name": "X", "x": 1.0, "y": 1.0, "source_view_id": 50,
                "source_view_name": "Floor transition A"}]
    p = dv.compute_detail_plan(read, 100, 999, details)
    assert p["blocked"]
    assert p["steps"] == []


def test_compute_blocks_on_missing_source_section():
    read = read_fixture()
    details = [{"name": "X", "x": 1.0, "y": 1.0, "source_view_id": 999,
                "source_view_name": "?"}]
    p = dv.compute_detail_plan(read, 100, 1, details)
    assert p["blocked"]


def test_compute_blocks_on_missing_label_settings():
    read = read_fixture()
    read["sheets"][0]["viewports"][0]["label_offset"] = None
    details = [{"name": "X", "x": 1.0, "y": 1.0, "source_view_id": 50,
                "source_view_name": "Floor transition A"}]
    p = dv.compute_detail_plan(read, 100, 1, details)
    assert p["blocked"]


def test_compute_warns_on_non_integer_numbers():
    read = read_fixture()
    read["sheets"][0]["viewports"].append(
        {"id": 6, "view_id": 16, "view_name": "Odd",
         "detail_number": "A301", "viewport_type_id": 900,
         "label_offset": [0.01, 0.01], "label_line_length": 0.81,
         "box_center": [9.0, 2.0, 0.0]})
    details = [{"name": "X", "x": 1.0, "y": 1.0, "source_view_id": 50,
                "source_view_name": "Floor transition A"}]
    p = dv.compute_detail_plan(read, 100, 1, details)
    assert not p["blocked"]
    assert any("not an integer" in w["text"] for w in p["warnings"])
    assert p["after"][0][1] == 22


def test_unescape():
    assert dv.unescape("Caf\\u00e9") == "Café"
    assert dv.unescape("a\\\\b") == "a\\b"
    assert dv.unescape("plain") == "plain"
    assert dv.unescape(None) is None
