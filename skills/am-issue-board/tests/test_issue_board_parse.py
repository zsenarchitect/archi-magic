# -*- coding: utf-8 -*-
"""Parser and renderer tests for scripts/issue_board.py.

    python -m pytest -q skills/am-issue-board/tests
"""
from issue_board_fixtures import SAMPLE
import issue_board as ib


def board():
    return ib.parse_board(SAMPLE)


def test_split_paragraphs_offsets_and_no_trailing_piece():
    paras = ib.split_paragraphs("a\rbc\r\rd\r")
    assert paras == [(0, "a"), (2, "bc"), (5, ""), (6, "d")]
    assert ib.split_paragraphs("a\r\nb\nc") == [(0, "a"), (3, "b"), (5, "c")]
    assert ib.split_paragraphs("") == []


def test_header():
    h = board()["header"]
    assert h["title"] == "AUDIT - 2026-01-05 (by @user1)"
    assert h["next_number"] == 5
    assert h["next_para"] == 1


def test_every_issue_is_read():
    b = board()
    assert [i["number"] for i in b["issues"]] == [1, 2, 3, 4]
    assert [i["status"] for i in b["issues"]] == ["IN PROGRESS", "OPEN", "OPEN", "DONE"]
    assert b["warnings"] == []


def test_issue_fields():
    i1, i2, i3, i4 = board()["issues"]
    assert i1["title"] == "Link placed far from origin"
    assert i1["assignees"] == ["@user2"]
    assert i1["comments"] == [{"date": "2026-01-05", "user": "@user1",
                               "text": "moved to the links workset; pinned."}]
    assert i1["related"] == [3] and i1["parent"] == 3
    assert i1["element_ids"] == [1001]
    assert i1["todos"] == ["relink by shared coordinates, or remove."]
    assert i2["title"] == "Heavy 2D in drafting views, sketch set"
    assert i2["assignees"] == ["@user3", "@user4"]
    assert i2["severity"] == ["High"]
    assert i2["findings"] == ["2 views have 500+ 2D elements:",
                              "View A (id2001): 812 - 700 lines",
                              "View B (id2002, on sheet X-1): 640 - 600 lines"]
    assert i2["element_ids"] == [2001, 2002]
    assert i3["assignees"] == ["@TBD"]
    assert i3["related"] == [1, 4] and i3["parent"] is None
    # A detail row that starts with "@" is a detail, not a comment.
    assert "@user2 - id1001 sample.dwg" in i3["findings"]
    assert i4["assignees"] == ["@ALL"] and i4["comments"][0]["text"] == "checked; closed."


def test_hand_written_dash_bullets_parse_the_same():
    dashed = SAMPLE.replace("\rFinding:", "\r   - Finding:").replace("\rTo do:", "\r   - To do:")
    a, b = board(), ib.parse_board(dashed)
    for x, y in zip(a["issues"], b["issues"]):
        assert x["findings"] == y["findings"] and x["todos"] == y["todos"]


def test_summary_counts():
    s = ib.summary(board())
    assert s["issues"] == 4 and s["next_number"] == 5
    assert s["by_status"] == {"OPEN": 2, "IN PROGRESS": 1, "DONE": 1}
    assert s["comments"] == 2 and s["severity_lines"] == 1 and s["related_lines"] == 3
    assert s["asymmetric_links"] == 0 and s["with_parent"] == 1


def test_warnings():
    b = ib.parse_board(SAMPLE.replace("Related: #1, #4", "Related: #1"))
    assert b["warnings"] == ["#4 lists #3 in Related, but #3 does not list #4"]
    b = ib.parse_board(SAMPLE.replace("next new issue is #5", "next new issue is #4"))
    assert any("not above" in w for w in b["warnings"])


def test_parse_related():
    assert ib.parse_related("#13. Path: see #13.") == ([13], 13)
    assert ib.parse_related("#1, #2, #8, #16") == ([1, 2, 8, 16], None)
    assert ib.parse_related("#3, #4") == ([3, 4], None)


def test_render_issue_format():
    paras = ib.render_issue({"number": 7, "status": "OPEN", "title": "Sample", "assignees": ["user5"],
                             "severity": "Medium", "findings": [["Two views:", "V (id11)", "W (id12)"]],
                             "todos": ["check id11."],
                             "comments": [{"date": "2026-02-01", "user": "user1", "text": "seen"}],
                             "related": [2], "parent": 3})
    texts = [p["text"] for p in paras]
    assert texts == ["#7 [OPEN] Sample - @user5", "Severity: Medium", "Finding: Two views:",
                     "V (id11)", "W (id12)", "To do: check id11.", "2026-02-01 @user1: seen",
                     "Related: #2, #3. Path: see #3."]
    assert paras[0]["bold"] and paras[0]["list"] == "none" and paras[0]["indent"] == 0
    assert all(p["list"] == "bullet" and not p["bold"] for p in paras[1:])
    assert [p["indent"] for p in paras[1:]] == [1, 1, 2, 2, 1, 1, 1]
    assert paras[3]["underline"] == [(3, 4)]
    # The rendered issue parses back to the same fields.
    b = ib.parse_board("H\rnext new issue is #8\r\r" + ib.paragraphs_text(paras))
    i = b["issues"][0]
    assert (i["number"], i["status"], i["assignees"], i["related"], i["parent"]) == \
        (7, "OPEN", ["@user5"], [2, 3], 3)


def test_render_board_has_blank_between_issues_and_header_rule():
    paras = ib.render_board("AUDIT - 2026-01-05 (by @user1)",
                            [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}])
    kinds = [p["kind"] for p in paras]
    assert kinds == ["header", "rules", "blank", "title", "blank", "title"]
    assert "next new issue is #3" in paras[1]["text"]
    blank = paras[2]
    assert blank["list"] == "none" and blank["indent"] == 0


def test_render_board_rejects_bad_numbers():
    import pytest
    with pytest.raises(ib.BoardError):
        ib.render_board("H", [{"number": 1, "title": "A"}, {"number": 1, "title": "B"}])
    with pytest.raises(ib.BoardError):
        ib.render_board("H", [{"number": 4, "title": "A"}], next_number=3)


def test_format_ops_never_start_at_text_end():
    plan = ib.plan_create("H", [{"number": 1, "title": "A", "findings": ["x id5"]}])
    n = len(plan["final_text"])
    for op in plan["formats"]:
        assert 0 <= op["start"] < n and op["start"] + op["length"] <= n
    # The text from the renderer has no trailing break, so the last paragraph
    # range is its text only.
    last = [op for op in plan["formats"] if op["op"] == "para"][-1]
    assert last["start"] + last["length"] == n


def test_unescape():
    assert ib.unescape("caf\\u00e9 \\\\ x") == u"caf" + ib.unichr_(0xe9) + u" \\ x"
