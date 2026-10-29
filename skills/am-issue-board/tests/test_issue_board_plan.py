# -*- coding: utf-8 -*-
"""Edit-plan, numbering, related-link and guard tests for scripts/issue_board.py.

The fake FormattedText follows Revit's range rule: a TextRange that starts at
the end of the text (the empty piece after the final break) throws.
"""
import pytest

from issue_board_fixtures import SAMPLE, FakeFormattedText, make_range, list_type
import issue_board as ib


def apply(text, plan):
    ft = FakeFormattedText(text)
    ib.apply_plan(ft, plan, make_range, list_type)
    return ft


def numbers_and_titles(text):
    return [(i["number"], i["title"]) for i in ib.parse_board(text)["issues"]]


NEW = {"title": "Purge unused types", "status": "OPEN", "assignees": ["@user3"],
       "severity": "Medium", "findings": ["120 unused types (id3001 is one)."],
       "todos": ["run Purge Unused."], "related": [2]}


def test_add_issue_takes_next_number_and_bumps_header():
    plan = ib.plan_add_issue(SAMPLE, NEW)
    assert plan["number"] == 5
    after = ib.parse_board(plan["final_text"])
    assert after["header"]["next_number"] == 6
    assert numbers_and_titles(plan["final_text"])[:4] == numbers_and_titles(SAMPLE)
    assert after["issues"][-1]["number"] == 5
    assert after["warnings"] == []


def test_add_issue_makes_related_symmetric():
    plan = ib.plan_add_issue(SAMPLE, NEW)
    after = ib.parse_board(plan["final_text"])
    assert ib.find_issue(after, 5)["related"] == [2]
    assert ib.find_issue(after, 2)["related"] == [5]  # #2 had no Related line before
    assert ib.check_related_symmetry(after) == []


def test_back_link_on_last_issue_stays_inside_that_issue():
    # Found live in Revit: the last issue has no Related line, so its new
    # Related line and the new issue share one anchor paragraph.
    text = ib.plan_create("H", [{"number": 1, "title": "A", "todos": ["x"]}])["final_text"]
    plan = ib.plan_add_issue(text, {"title": "B", "todos": ["y"], "related": [1]})
    paras = [t for _, t in ib.split_paragraphs(plan["final_text"])]
    assert paras[-6:] == ["To do: x", "Related: #2", "", "#2 [OPEN] B - @TBD", "To do: y", "Related: #1"]
    after = ib.parse_board(plan["final_text"])
    assert ib.find_issue(after, 1)["related"] == [2] and after["warnings"] == []


def test_add_twice_never_reuses_or_renumbers():
    p1 = ib.plan_add_issue(SAMPLE, NEW)
    p2 = ib.plan_add_issue(p1["final_text"], dict(NEW, title="Second", related=[]))
    nums = [i["number"] for i in ib.parse_board(p2["final_text"])["issues"]]
    assert nums == [1, 2, 3, 4, 5, 6]
    assert ib.parse_board(p2["final_text"])["header"]["next_number"] == 7


def test_add_refuses_used_next_number():
    bad = SAMPLE.replace("next new issue is #5", "next new issue is #3")
    with pytest.raises(ib.BoardError):
        ib.plan_add_issue(bad, NEW)


def test_add_refuses_unknown_related():
    with pytest.raises(ib.BoardError):
        ib.plan_add_issue(SAMPLE, dict(NEW, related=[42]))


def test_close_issue_keeps_numbers_and_adds_comment_before_related():
    plan = ib.plan_set_status(SAMPLE, 1, "DONE", "2026-01-09", "@user1", "relinked; verified.")
    after = ib.parse_board(plan["final_text"])
    assert numbers_and_titles(plan["final_text"]) == numbers_and_titles(SAMPLE)
    i1 = ib.find_issue(after, 1)
    assert i1["status"] == "DONE"
    assert [c["date"] for c in i1["comments"]] == ["2026-01-05", "2026-01-09"]
    kinds = [ln["kind"] for ln in i1["lines"]]
    assert kinds[-1] == "related" and kinds[-2] == "comment"
    assert after["header"]["next_number"] == 5


def test_status_change_needs_comment_and_valid_status():
    with pytest.raises(ib.BoardError):
        ib.plan_set_status(SAMPLE, 1, "DONE", "2026-01-09", "@user1", " ")
    with pytest.raises(ib.BoardError):
        ib.plan_set_status(SAMPLE, 1, "CLOSED", "2026-01-09", "@user1", "x")
    with pytest.raises(ib.BoardError):
        ib.plan_set_status(SAMPLE, 9, "DONE", "2026-01-09", "@user1", "x")


def test_relate_is_symmetric_and_keeps_parent():
    plan = ib.plan_relate(SAMPLE, 1, 2)
    after = ib.parse_board(plan["final_text"])
    assert ib.find_issue(after, 1)["related"] == [2, 3]
    assert ib.find_issue(after, 1)["parent"] == 3
    assert ib.find_issue(after, 2)["related"] == [1]
    assert ib.check_related_symmetry(after) == []
    with pytest.raises(ib.BoardError):
        ib.plan_relate(SAMPLE, 2, 2)


def test_relate_existing_link_changes_nothing():
    plan = ib.plan_relate(SAMPLE, 1, 3)
    assert plan["edits"] == [] and plan["final_text"] == SAMPLE


def test_apply_plan_gives_planned_text_and_formats():
    plan = ib.plan_add_issue(SAMPLE, NEW)
    ft = apply(SAMPLE, plan)
    assert ft.GetPlainText() == plan["final_text"]
    paras = dict((t, s) for s, t in ib.split_paragraphs(ft.text))
    title = ft.para_attr(paras["#5 [OPEN] Purge unused types - @user3"])
    assert title["bold"] and title["list"] == "none" and title["indent"] == 0
    # The edited header line keeps header formatting (found live in Revit: it became a bullet).
    rules = [t for t in paras if "next new issue is #6" in t][0]
    hdr = ft.para_attr(paras[rules])
    assert hdr["list"] == "none" and hdr["indent"] == 0 and not hdr["bold"]
    sev = ft.para_attr(paras["Severity: Medium"])
    assert sev["list"] == "bullet" and sev["indent"] == 1 and not sev["bold"]
    # Blank paragraph before the new issue: list None, indent 0.
    s = paras["#5 [OPEN] Purge unused types - @user3"]
    assert ft.text[s - 1] == "\r" and ft.attr[s - 1]["list"] == "none" and ft.attr[s - 1]["indent"] == 0
    # The element id is underlined, the rest of the line is not.
    line = "Finding: 120 unused types (id3001 is one)."
    ls = paras[line]
    i = line.index("id3001")
    assert all(ft.attr[ls + k]["ul"] for k in range(i, i + 6))
    assert not ft.attr[ls]["ul"] and not ft.attr[ls + i + 6]["ul"]


def test_apply_plan_edits_only_planned_paragraphs():
    plan = ib.plan_set_status(SAMPLE, 3, "IN PROGRESS", "2026-01-09", "@user1", "started.")
    ft = apply(SAMPLE, plan)
    starts = sorted(set(c[1] for c in ft.calls if c[0] == "SetPlainText"))
    assert len(starts) == len(plan["edits"]) == 2  # title line + one anchor line
    # Text before the first edit is untouched.
    first = min(e["start"] for e in plan["edits"])
    assert ft.text[:first] == SAMPLE[:first]


def test_no_range_on_empty_piece_after_final_break():
    # The board ends with "\r"; the fake throws on a range that starts at the end.
    for plan in (ib.plan_add_issue(SAMPLE, NEW),
                 ib.plan_set_status(SAMPLE, 4, "OPEN", "2026-01-09", "@user1", "reopened."),
                 ib.plan_relate(SAMPLE, 4, 2)):
        n = len(plan["final_text"])
        assert all(op["start"] < n and op["start"] + op["length"] <= n for op in plan["formats"])
        apply(SAMPLE, plan)


def test_guard_stops_when_text_changed_since_read():
    plan = ib.plan_set_status(SAMPLE, 1, "DONE", "2026-01-09", "@user1", "done.")
    # A manual edit in Revit, in a paragraph the plan does not touch.
    edited = SAMPLE.replace("Survey point check", "Survey point checked")
    ft = FakeFormattedText(edited)
    with pytest.raises(ib.BoardChanged) as err:
        ib.apply_plan(ft, plan, make_range, list_type)
    assert "changed since it was read" in str(err.value)
    assert ft.text == edited and ft.calls == []
    # A manual edit inside a planned paragraph also stops.
    edited = SAMPLE.replace("Link placed far from origin", "Link placed far away")
    ft = FakeFormattedText(edited)
    with pytest.raises(ib.BoardChanged):
        ib.apply_plan(ft, plan, make_range, list_type)
    assert ft.calls == []


def test_anchor_check_stops_before_any_write():
    plan = ib.plan_set_status(SAMPLE, 1, "DONE", "2026-01-09", "@user1", "done.")
    plan["expected_sha1"] = None  # bypass the text guard to test the second line of defence
    plan["edits"][-1]["start"] += 1
    ft = FakeFormattedText(SAMPLE)
    with pytest.raises(ib.BoardChanged):
        ib.apply_plan(ft, plan, make_range, list_type)
    assert ft.calls == []


def test_guard_passes_on_unchanged_text():
    plan = ib.plan_comment(SAMPLE, 2, "2026-01-09", "user1", "reviewed.")
    ft = apply(SAMPLE, plan)
    assert ib.find_issue(ib.parse_board(ft.text), 2)["comments"][-1]["user"] == "@user1"


def test_create_plan_formats_apply_cleanly():
    plan = ib.plan_create("AUDIT - 2026-01-05 (by @user1)", [dict(NEW, number=1, related=[])])
    ft = FakeFormattedText(plan["final_text"] + "\r")  # Revit may add a final break
    ib.apply_formats(ft, plan["formats"], make_range, list_type)
    assert ft.attr[0]["bold"]
    b = ib.parse_board(ft.text)
    assert b["header"]["next_number"] == 2 and [i["number"] for i in b["issues"]] == [1]
