# -*- coding: utf-8 -*-
"""COMPUTE and VERIFY steps for the detail-views recipe. Runs outside Revit (CPython 3).

Compute a preview from a read_detail_views.py result:
    python detail_preview.py compute --read read.json --sheet 123 --reference 456
        --detail "North transition:12.5:8.0:789" [--detail ...]
        [--title "..."] --out preview.json

Each --detail value is "name:x:y:source_view_id". x and y are the
requested box center on the sheet, in feet. source_view_id is the
section view whose crop box and view template the new detail copies.

Compare a later result (apply dry run, or a fresh read) with the preview:
    python detail_preview.py compare --preview preview.json --actual run.json

Show the side-effect counts of a dry run (issue #28):
    python detail_preview.py side-effects --preview preview.json --dryrun dryrun.json

Input files may be the bare `result` object or the whole run JSON that
`pyrevit agent run` prints. Exit codes: 0 ok, 1 differences or blocked, 2 bad input.
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import detail_views as dv  # noqa: E402
import side_effects as se  # noqa: E402


def load_json(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def parse_detail(spec):
    """One --detail value into a detail dict. Raises ValueError on bad input."""
    parts = spec.split(":")
    if len(parts) != 4:
        raise ValueError("Bad --detail value %r: use \"name:x:y:source_view_id\"." % spec)
    name, xs, ys, src = parts
    try:
        x = float(xs)
        y = float(ys)
        src_id = int(src)
    except ValueError:
        raise ValueError("Bad --detail value %r: x, y and source_view_id must be numbers." % spec)
    return {"name": name, "x": x, "y": y, "source_view_id": src_id,
            "source_view_name": ""}


def actual_rows(data):
    """Rows from an apply result or a read result, in apply-row shape."""
    if "result" in data and "rows" not in data and "sheets" not in data:
        data = data["result"]
    if data is None:
        raise ValueError("The run has no result (check status and error).")
    if "rows" in data:
        rows = []
        for r in data["rows"]:
            name = dv.unescape(r[0]) if data.get("escaped") else r[0]
            rows.append([name, r[1], r[2], r[3], r[4]])
        return rows, data.get("partial", False)
    read = dv.load_read_result(data)
    rows = []
    for sheet in read["sheets"]:
        for vp in sheet.get("viewports", []):
            if vp.get("label_offset") is None:
                continue
            rows.append([vp.get("view_name", ""), vp.get("detail_number"),
                         vp["label_offset"], vp.get("label_line_length"),
                         vp.get("box_center", [0, 0, 0])[:2]])
    return rows, False


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("compute")
    c.add_argument("--read", required=True)
    c.add_argument("--sheet", required=True, type=int)
    c.add_argument("--reference", required=True, type=int)
    c.add_argument("--detail", action="append", default=[])
    c.add_argument("--title")
    c.add_argument("--out", required=True)
    v = sub.add_parser("compare")
    v.add_argument("--preview", required=True)
    v.add_argument("--actual", required=True)
    s = sub.add_parser("side-effects")
    s.add_argument("--preview", required=True)
    s.add_argument("--dryrun", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "compute":
        if not a.detail:
            ap.error("compute needs at least one --detail value.")
        read = dv.load_read_result(load_json(a.read))
        sections = dict((s["id"], s) for s in read.get("sections", []))
        details = []
        for spec in a.detail:
            d = parse_detail(spec)
            d["source_view_name"] = sections.get(d["source_view_id"], {}).get("name", "")
            details.append(d)
        p = dv.compute_detail_plan(read, a.sheet, a.reference, details, a.title)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(p, indent=1, ensure_ascii=False))
        print(dv.text_table(p))
        return 1 if p["blocked"] else 0
    if a.cmd == "compare":
        preview = load_json(a.preview)
        rows, partial = actual_rows(load_json(a.actual))
        if partial:
            names = set(r[0] for r in rows)
            preview = dict(preview)
            preview["after"] = [r for r in preview["after"] if r[0] in names]
        diffs = dv.compare(preview, rows)
        if diffs:
            print("DIFFERENT from preview (%d):" % len(diffs))
            for d in diffs:
                print("  " + d)
            return 1
        print("MATCHES PREVIEW: %d new viewport(s) checked." % len(preview["after"]))
        return 0
    if a.cmd == "side-effects":
        preview = load_json(a.preview)
        summary = se.summarize(preview, load_json(a.dryrun), ["Viewports"])
        print(se.text_table(summary))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
