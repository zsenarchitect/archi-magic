"""Write an English source chart (.xlsx) for a set built from a find_sources.py plan, for the team.

Usage:
  python source_chart.py <plan.json> <chart.xlsx> --pdf-name "<set.pdf>" [--title "<chart title>"]
         [--base <folder shown relative to>] [--exclude SHEET ...] [--overwrite]

One row per listed sheet, in list order: group, sheet number, drawing list name, status
(Included / MISSING / REMOVED BY USER), page in the set, bookmark, the name in the source, name
match, where the bookmark name came from, source type, folder, file, page in the source file and the
source's last modified time. MISSING rows are red. A source name that differs from the list name
is yellow ("NO - check") so the team can pick the right name. A second tab lists the versions not
used. Never overwrites without --overwrite. Exit codes: 0 ok, 2 bad arguments, 3 openpyxl
missing, 4 output exists.
"""
import argparse
import datetime
import json
import os
import re
import sys


def name_key(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def rows_from_plan(d, drop=()):
    """Pure logic: merge plan + missing back into list order. Returns row dicts."""
    drop = {x.strip().upper() for x in drop}
    items = [dict(p, status="Included") for p in d["plan"]] + [dict(m, status="MISSING") for m in d.get("missing", [])]
    order = {}
    for i, p in enumerate(d.get("order", [])):
        order[p] = i
    items.sort(key=lambda x: order.get(x["sheet"], x.get("row", 0)))
    page = 0
    out = []
    for it in items:
        if it["sheet"].upper() in drop:
            it = dict(it, status="REMOVED BY USER")
        if it["status"] == "Included":
            page += 1
            src = it["bookmark"][len(it["sheet"]):].strip()
            if it["title_from"] == "drawing list":
                src, match = "(no name in source)", "n/a"
            else:
                match = "Yes" if name_key(src) == name_key(it.get("list_title")) else "NO - check"
            it = dict(it, set_page=page, source_name=src, match=match)
        out.append(it)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("--pdf-name", default="")
    ap.add_argument("--title", default="Drawing Set - Sheet Sources")
    ap.add_argument("--base", help="show source folders relative to this folder")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("openpyxl is missing. Install it with: python -m pip install --user openpyxl")
        return 3
    if os.path.exists(a.out) and not a.overwrite:
        print("STOP: the chart exists. Ask the user, then run again with --overwrite or a new name.")
        return 4
    with open(a.plan, encoding="utf-8") as fh:
        d = json.load(fh)
    rows = rows_from_plan(d, a.exclude)
    rel = (lambda p: os.path.relpath(p, a.base)) if a.base else (lambda p: p)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet Sources"
    inc = sum(r["status"] == "Included" for r in rows)
    miss = sum(r["status"] == "MISSING" for r in rows)
    ws["A1"] = a.title
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Set: %s   |   Built: %s   |   Listed: %d   Included: %d   Missing: %d" % (
        a.pdf_name, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), len(rows), inc, miss)
    ws["A3"] = ("Newest source file (last modified) is used for each sheet. Bookmark name = PDF file name for single-sheet "
                "PDFs, or the source's own bookmark for pages taken from a combined PDF; the drawing list name when the "
                "source has none. Yellow = the source name differs from the drawing list name.")
    ws["A3"].alignment = Alignment(wrap_text=True, vertical="top")
    hdr = ["#", "Group", "Sheet No.", "Sheet Name (Drawing List)", "Status", "Page in Set", "Bookmark in Set",
           "Sheet Name in Source", "Name Match", "Bookmark Name From", "Source Type", "Source Folder", "Source File",
           "Page in Source File", "Source Last Modified", "Other Versions Not Used"]
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=len(hdr))
    ws.row_dimensions[3].height = 45
    H = 5
    for j, h in enumerate(hdr, 1):
        c = ws.cell(H, j, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="404040")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    red, yellow, grey = (PatternFill("solid", fgColor=x) for x in ("F8CBAD", "FFF2CC", "D9D9D9"))
    thin = Side(style="thin", color="BFBFBF")
    frm = {"file name": "PDF file name", "source bookmark": "Source bookmark", "drawing list": "Drawing list (source has no name)"}
    for k, r in enumerate(rows, 1):
        if r["status"] == "Included":
            vals = [k, r.get("group", ""), r["sheet"], r.get("list_title", ""), r["status"], r["set_page"], r["bookmark"],
                    r["source_name"], r["match"], frm.get(r["title_from"], r["title_from"]),
                    "Single-sheet PDF" if r["kind"] == "single" else "Page from combined PDF",
                    rel(os.path.dirname(r["file"])), os.path.basename(r["file"]), r["page"] + 1, r["mtime"],
                    len(r.get("not_used", [])) or ""]
        else:
            vals = [k, r.get("group", ""), r["sheet"], r.get("list_title", ""), r["status"]] + [""] * 11
        for j, v in enumerate(vals, 1):
            c = ws.cell(H + k, j, v)
            c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
            c.alignment = Alignment(vertical="top", wrap_text=j in (4, 7, 8, 12, 13))
        if r["status"] == "MISSING":
            for j in range(1, len(hdr) + 1):
                ws.cell(H + k, j).fill = red
            ws.cell(H + k, 5).font = Font(bold=True, color="C00000")
        elif r["status"] != "Included":
            for j in range(1, len(hdr) + 1):
                ws.cell(H + k, j).fill = grey
        elif r["match"] == "NO - check":
            for j in (4, 8, 9):
                ws.cell(H + k, j).fill = yellow
    for j, wd in enumerate([5, 18, 12, 40, 12, 8, 45, 45, 11, 22, 18, 50, 50, 8, 16, 9], 1):
        ws.column_dimensions[get_column_letter(j)].width = wd
    ws.freeze_panes = "E6"
    ws.auto_filter.ref = "A%d:%s%d" % (H, get_column_letter(len(hdr)), H + len(rows))

    w2 = wb.create_sheet("Versions Not Used")
    for j, h in enumerate(["Sheet No.", "Used", "Not used", "Not used - last modified"], 1):
        c = w2.cell(1, j, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="404040")
    n = 2
    for r in rows:
        for o in r.get("not_used", []) if r["status"] == "Included" else []:
            w2.append([r["sheet"], "%s p%d" % (rel(r["file"]), r["page"] + 1), "%s p%d" % (rel(o["file"]), o["page"] + 1), o["mtime"]])
            n += 1
    for j, wd in enumerate([12, 90, 90, 18], 1):
        w2.column_dimensions[get_column_letter(j)].width = wd
    w2.freeze_panes = "A2"
    wb.save(a.out)
    mism = sum(r.get("match") == "NO - check" for r in rows)
    print("CHART     %s" % a.out)
    print("ROWS      %d  included %d  missing %d  name differs %d  versions not used %d" % (len(rows), inc, miss, mism, n - 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
