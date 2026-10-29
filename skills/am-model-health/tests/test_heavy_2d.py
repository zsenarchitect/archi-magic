# -*- coding: utf-8 -*-
"""Tests for the heavy 2D views pure functions in scripts/model_health.py (issue #61).

All data here is invented. Run:
    python -m pytest -q skills/am-model-health/tests
"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "model_health.py")


def load():
    spec = importlib.util.spec_from_file_location("model_health_2d", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mh = load()


def counts(lines=0, detail=0, filled=0, text=0, dims=0):
    return {"lines": lines, "detail_items": detail, "filled_regions": filled,
            "text_notes": text, "dimensions": dims}


def test_severity_boundaries():
    assert mh.severity_2d(0) is None
    assert mh.severity_2d(249) is None
    assert mh.severity_2d(250) == "Medium"
    assert mh.severity_2d(499) == "Medium"
    assert mh.severity_2d(500) == "High"
    assert mh.severity_2d(999) == "High"
    assert mh.severity_2d(1000) == "Critical"
    assert mh.severity_2d(250000) == "Critical"


def test_sketch_and_uncategorized_are_never_bloat():
    assert not mh.counts_as_2d("Lines", "<Sketch>")
    assert not mh.counts_as_2d("Lines", " <sketch> ")
    assert not mh.counts_as_2d("<Sketch>")
    assert not mh.counts_as_2d("Automatic Sketch Dimensions")
    assert not mh.counts_as_2d(None)
    assert not mh.counts_as_2d("")


def test_normal_2d_counts():
    assert mh.counts_as_2d("Lines", "<Medium Lines>")
    assert mh.counts_as_2d("Lines", "Office Thin")
    assert mh.counts_as_2d("Detail Items")
    assert mh.counts_as_2d("Text Notes")
    assert mh.counts_as_2d("Dimensions")


def test_view_row_totals_and_line_share():
    r = mh.view_2d_row(7, "Plan A", counts(lines=900, detail=60, filled=40, text=30, dims=10),
                       {"Thin": 500, "Medium": 300, "Wide": 90, "Dash": 10}, "A-101")
    assert r["total"] == 1000  # filled regions are inside detail items, not added again
    assert r["severity"] == "Critical"
    assert r["line_share"] == 0.9
    assert r["line_heavy"] is True
    assert r["sheet"] == "A-101"
    assert [s["name"] for s in r["top_line_styles"]] == ["Thin", "Medium", "Wide"]


def test_line_share_threshold_and_zero_total():
    assert mh.view_2d_row(1, "v", counts(lines=85, text=15))["line_heavy"] is True
    assert mh.view_2d_row(1, "v", counts(lines=84, text=16))["line_heavy"] is False
    empty = mh.view_2d_row(1, "v", counts())
    assert empty["line_share"] == 0.0 and empty["line_heavy"] is False and empty["severity"] is None


def test_archive_name():
    assert mh.is_archive_name("ARCHIVE_Level 1 diagram")
    assert mh.is_archive_name("archive_old")
    assert not mh.is_archive_name("Level 1 ARCHIVE_")
    assert not mh.is_archive_name(None)


def test_cad_layer_like():
    assert mh.cad_layer_like("A-WALL")
    assert mh.cad_layer_like("A-WALL-PATT")
    assert mh.cad_layer_like("xref$0$A-DOOR")
    assert mh.cad_layer_like("survey|TOPO")
    assert not mh.cad_layer_like("<Medium Lines>")
    assert not mh.cad_layer_like("Office Thin 0.18")
    assert not mh.cad_layer_like("")


def test_near_duplicates_need_identical_counts():
    rows = [mh.view_2d_row(1, "Diagram 1", counts(lines=300, text=5)),
            mh.view_2d_row(2, "ARCHIVE_Diagram 1", counts(lines=300, text=5)),
            mh.view_2d_row(3, "Diagram 2", counts(lines=301, text=5)),
            mh.view_2d_row(4, "Empty", counts()),
            mh.view_2d_row(5, "Empty 2", counts())]
    mh.mark_near_duplicates(rows)
    assert [r["same_counts_as"] for r in rows] == [1, 1, 0, 0, 0]


def test_rank_orders_by_severity_then_total():
    rows = [mh.view_2d_row(1, "m", counts(lines=260)), mh.view_2d_row(2, "c", counts(lines=1200)),
            mh.view_2d_row(3, "h", counts(lines=600)), mh.view_2d_row(4, "x", counts(lines=100)),
            mh.view_2d_row(5, "c2", counts(lines=5000))]
    assert [r["id"] for r in mh.rank_heavy_views(rows)] == [5, 2, 3, 1]


def test_label_never_claims_exploded_cad():
    r = mh.view_2d_row(1, "ARCHIVE_Plan", counts(lines=950, detail=50, filled=20),
                       {"A-WALL": 900, "A-DOOR": 50}, "A-201", "someone")
    r["same_counts_as"] = 2
    label = mh.heavy_view_label(r)
    assert "exploded" not in label.lower()
    assert label.startswith("Critical: ARCHIVE_Plan - 1000 2D elements (")
    for part in ("line-heavy", "20 filled regions", "on sheet A-201", "ARCHIVE view",
                 "same counts as 2 other view(s)", "line styles look like CAD layers", "by someone"):
        assert part in label, part


def test_label_not_on_sheet():
    r = mh.view_2d_row(1, "Plan", counts(lines=100, text=200))
    assert "not on a sheet" in mh.heavy_view_label(r)
    assert "line-heavy" not in mh.heavy_view_label(r)


def test_summary_counts():
    rows = [mh.view_2d_row(1, "a", counts(lines=1500), sheet="S1"),
            mh.view_2d_row(2, "ARCHIVE_b", counts(lines=700)),
            mh.view_2d_row(3, "c", counts(lines=300)),
            mh.view_2d_row(4, "d", counts(lines=300)),
            mh.view_2d_row(5, "e", counts(lines=10))]
    mh.mark_near_duplicates(rows)
    ranked = mh.rank_heavy_views(rows)
    s = mh.summarize_heavy(rows, ranked)
    assert (s["critical"], s["high"], s["medium"]) == (1, 1, 2)
    assert s["views_with_2d"] == 5
    assert s["on_sheet"] == 1 and s["archive"] == 1 and s["near_duplicate"] == 2
    assert s["by_creator"] == {"(unknown)": 4}
