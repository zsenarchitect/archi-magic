# -*- coding: utf-8 -*-
"""Plan board changes outside Revit (CPython 3). Prints JSON to stdout.

The READ file is the "note" object from read_board.py, saved as JSON
({"text": ..., "formats": [...], ...}). Its \\uXXXX escapes are decoded here.

    python board_cli.py parse READ.json
    python board_cli.py create  SPEC.json     SPEC = {"title_line": ..., "issues": [...], "next_number": n}
    python board_cli.py add     READ.json ISSUE.json
    python board_cli.py status  READ.json NUMBER STATUS DATE USER "comment"
    python board_cli.py comment READ.json NUMBER DATE USER "comment"
    python board_cli.py relate  READ.json A B

ISSUE.json = {"title": ..., "status": "OPEN", "assignees": ["@user"], "severity": "High",
              "findings": ["... id1001 ...", ["header line", "detail row"]],
              "todos": [...], "comments": [{"date": ..., "user": ..., "text": ...}],
              "related": [2], "parent": 2}

Each plan has "final_text": show the user the changed paragraphs before the dry run.
Exit code 2 = the plan cannot be made (the message says why).
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_board as ib  # noqa: E402


def load(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.loads(f.read())


def read_note(path):
    note = load(path)
    if "note" in note and isinstance(note["note"], dict):
        note = note["note"]
    return ib.unescape(note["text"]), note.get("formats")


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    cmd = argv[0]
    try:
        if cmd == "create":
            spec = load(argv[1])
            out = ib.plan_create(spec["title_line"], spec.get("issues", []), spec.get("next_number"))
        else:
            text, formats = read_note(argv[1])
            if cmd == "parse":
                board = ib.parse_board(text, formats)
                for i in board["issues"]:
                    del i["lines"]
                out = {"summary": ib.summary(board), "header": board["header"],
                       "issues": board["issues"], "warnings": board["warnings"]}
            elif cmd == "add":
                out = ib.plan_add_issue(text, load(argv[2]), formats)
            elif cmd == "status":
                out = ib.plan_set_status(text, int(argv[2]), argv[3], argv[4], argv[5], argv[6], formats)
            elif cmd == "comment":
                out = ib.plan_comment(text, int(argv[2]), argv[3], argv[4], argv[5], formats)
            elif cmd == "relate":
                out = ib.plan_relate(text, int(argv[2]), int(argv[3]), formats)
            else:
                sys.stderr.write("unknown command: %s\n" % cmd)
                return 2
    except (ib.BoardError, IndexError, KeyError) as e:
        sys.stderr.write("cannot plan: %s\n" % e)
        return 2
    sys.stdout.write(json.dumps(out, indent=1, ensure_ascii=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
