# -*- coding: utf-8 -*-
"""Smoke tests for scripts/board_cli.py on a JSON file shaped like read_board.py's result."""
import io
import json
import os
import sys

from issue_board_fixtures import SAMPLE, HERE
import issue_board as ib

sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import board_cli  # noqa: E402

# Non-ASCII and a backslash, escaped the way read_board.py escapes them in IronPython.
RAW = SAMPLE.replace("sample.dwg (id1001)", "C:\\shared\\caf" + ib.unichr_(0xe9) + ".dwg (id1001)")


def escape(s):
    out = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif ord(c) < 128:
            out.append(c)
        else:
            out.append("\\u%04x" % ord(c))
    return "".join(out)


def formats_for(text):
    out = []
    for _, line in ib.split_paragraphs(text):
        kind, _ = ib.classify(line)
        if kind in ("title", "blank") or not out:
            out.append({"list": "None", "indent": 0, "bold": "All"})
        else:
            # Detail rows were read at indent 1 here, not the default 2.
            out.append({"list": "Bullet", "indent": 1, "bold": "None"})
    return out


def write_json(tmp_path, name, data):
    p = os.path.join(str(tmp_path), name)
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=True))
    return p


def run(capsys, args):
    code = board_cli.main(args)
    out = capsys.readouterr().out
    return code, (json.loads(out) if code == 0 else None)


def note_file(tmp_path):
    return write_json(tmp_path, "note.json", {"note": {"text": escape(RAW), "formats": formats_for(RAW),
                                                       "sha1": ib.text_sha1(RAW)}})


def test_parse_decodes_escapes(tmp_path, capsys):
    code, out = run(capsys, ["parse", note_file(tmp_path)])
    assert code == 0
    assert out["summary"]["issues"] == 4
    assert out["issues"][0]["findings"][0].startswith("C:\\shared\\caf" + ib.unichr_(0xe9))


def test_plans_have_matching_sha_and_apply(tmp_path, capsys):
    note = note_file(tmp_path)
    issue = write_json(tmp_path, "issue.json", {"title": "New", "related": [3], "todos": ["x"]})
    for args in (["add", note, issue],
                 ["status", note, "2", "DONE", "2026-01-09", "@user1", "done."],
                 ["comment", note, "3", "2026-01-09", "user1", "seen."],
                 ["relate", note, "2", "4"]):
        code, plan = run(capsys, args)
        assert code == 0, args
        assert plan["expected_sha1"] == ib.text_sha1(RAW)
        from issue_board_fixtures import FakeFormattedText, make_range, list_type
        ft = FakeFormattedText(RAW)
        ib.apply_plan(ft, plan, make_range, list_type)
        assert ft.text == plan["final_text"]


def test_insert_anchored_on_read_detail_row_keeps_its_indent():
    # Issue #2 ends on a detail row here, so its new Related line is anchored on it.
    text = RAW.replace("\rTo do: delete or simplify.", "")

    def anchor_indent(formats):
        plan = ib.plan_relate(text, 2, 4, formats)
        edit = [e for e in plan["edits"] if e["old"].startswith("View B")]
        assert edit, "the Related line of #2 is anchored on its last detail row"
        op = [o for o in plan["formats"] if o["op"] == "para" and o["start"] == edit[0]["start"]][0]
        return op["indent"]

    assert anchor_indent(formats_for(text)) == 1  # the indent read from Revit is kept
    assert anchor_indent(None) == 2               # default for a detail row


def test_create_and_errors(tmp_path, capsys):
    spec = write_json(tmp_path, "spec.json", {"title_line": "AUDIT - 2026-01-05 (by @user1)",
                                              "issues": [{"number": 1, "title": "A"}]})
    code, plan = run(capsys, ["create", spec])
    assert code == 0 and plan["mode"] == "create"
    note = note_file(tmp_path)
    assert board_cli.main(["status", note, "42", "DONE", "2026-01-09", "@u", "x"]) == 2
    assert board_cli.main(["relate", note, "1", "42"]) == 2
    assert board_cli.main(["nope", note]) == 2
