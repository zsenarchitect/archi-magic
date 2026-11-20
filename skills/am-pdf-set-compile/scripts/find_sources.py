"""Find the source page for every listed sheet across many folders, including pages inside
combined (multi-page) PDFs. Writes a page plan that compile_set.py --plan builds from.

Usage:
  python find_sources.py --check
  python find_sources.py <sheets.json> --folder <dir> [--folder <dir> ...] --plan <plan.json>
         [--prefer <dir> --prefer-prefix A,G,T] [--skip-dir NAME ...] [--skip-file TEXT ...]
         [--no-combined] [--no-text] [--workers N]

Rules (fixed, so every package is found the same way):
- Folders are scanned with all subfolders. Folder names that contain a --skip-dir word are left out
  (default: archive, superseded, superceded, combined, specification, __macosx).
- A file matches a sheet when its name holds the sheet number anywhere, for example
  "A-101 PLAN.pdf", "Firm_Project - Sheet - M-101 - PLAN.pdf", "FS1-0 PLAN.pdf" (for FS1.0),
  "P-900 PLAN.pdf" (for P-900.00), "TA-101M PLAN.pdf". Spaces, "-" and "." in numbers are ignored.
- A single-sheet PDF with no listed number in its name is matched only by its own bookmark.
- A multi-page PDF is a combined PDF. Each page gets a sheet from the PDF's own bookmarks. A
  bookmark sheet that points to more than one page is a group label and is not used. Pages with
  no bookmark sheet are read from the title block: the biggest listed sheet number in the
  bottom-right quarter of the page (needs pdfplumber). A page between two pages that are
  consecutive in the list gets the missing sheet by position.
- A page from a combined PDF loses to any other source of that sheet in the combined PDF's own
  folder or its subfolders: a combined PDF there was made from those files later, so its date is
  the date it was combined, not the date the sheet changed.
- Then the newest file (modified time) wins. A sheet whose prefix is in --prefer-prefix takes the newest
  single-sheet PDF in the --prefer folder first, when that folder has one.
- Bookmark: "<SHEET> <TITLE>", upper case, single spaces. The title is the text after the sheet
  number in the file name (single PDFs) or in the source bookmark (combined pages). With neither,
  the title from the drawing list is used and title_from says "drawing list".
Exit codes: 0 ok, 2 bad arguments, 3 pypdf missing.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

SKIP_DIRS = ("archive", "superseded", "superceded", "combined", "specification", "__macosx")
TOK = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3})\s?-?\s?(\d{1,4})(?:[.\-](\d{1,2}))?([A-Z])?(?![0-9A-Za-z])", re.I)
TITLE_TOK = re.compile(r"^([A-Z]{1,3})-?(\d{1,4})(?:[.\-](\d{1,2}))?([A-Z])?$", re.I)


def long_path(p):
    """Windows paths over 260 characters need the \\\\?\\ prefix."""
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p
    return p


def norm(s):
    return re.sub(r"[\s\-\.]", "", s or "").upper()


def sheet_key(s):
    """The sheet number as it goes in the bookmark: list text without spaces, upper case."""
    return re.sub(r"\s+", "", s).upper()


def clean_title(t):
    t = re.sub(r"^[\s_\-]+", "", t or "")
    return re.sub(r"\s+", " ", t).strip().upper()


def find_sheet(text, want):
    """Return (listed sheet, title after the number) for the first listed sheet number in text.
    Pure logic, used by the tests."""
    for m in TOK.finditer(text):
        letters, digits, sub, suffix = m.group(1).upper(), m.group(2), m.group(3), (m.group(4) or "").upper()
        base = letters + digits
        options = []
        if sub:
            options.append(base + sub + suffix)
        options += [base + suffix, base] if suffix else [base]
        options.append(base + "00")
        for k in options:
            if k in want:
                return want[k], clean_title(text[m.end():])
    return None, None


def page_count(path):
    from pypdf import PdfReader
    try:
        return len(PdfReader(long_path(path)).pages)
    except Exception:
        return -1


def bookmark_pages(path, want):
    """Map page index -> (sheet, title) from a PDF's own bookmarks (group labels removed)."""
    from pypdf import PdfReader
    r = PdfReader(long_path(path))
    n = len(r.pages)
    got = {}

    def walk(ol):
        for o in ol:
            if isinstance(o, list):
                walk(o)
                continue
            try:
                pg = r.get_destination_page_number(o)
            except Exception:
                continue
            sh, title = find_sheet(getattr(o, "title", "") or "", want)
            if sh and pg is not None and 0 <= pg < n:
                got.setdefault(pg, (sh, title))
    try:
        walk(r.outline)
    except Exception:
        pass
    count = collections.Counter(v[0] for v in got.values())
    return n, {k: v for k, v in got.items() if count[v[0]] == 1}


def title_block_sheet(args):
    """Biggest listed sheet number in the bottom-right quarter of one page. Runs in a worker."""
    path, index, want = args
    try:
        import logging
        logging.disable(logging.CRITICAL)
        import pdfplumber
        with pdfplumber.open(long_path(path), pages=[index + 1]) as pdf:
            pg = pdf.pages[0]
            x0, top, x1, bottom = pg.bbox
            box = (x0 + (x1 - x0) * 0.6, top + (bottom - top) * 0.5, x1, bottom)
            words = pg.within_bbox(box).extract_words(extra_attrs=["size"])
    except Exception:
        return path, index, None
    best = None
    for w in words:
        t = w["text"].strip().strip(",;:()")
        if not TITLE_TOK.match(t):
            continue
        sh, _ = find_sheet(t, want)
        if sh and (best is None or w["size"] > best[1]):
            best = (sh, w["size"])
    return path, index, best[0] if best else None


def fill_by_position(pages, order):
    """Pure logic. pages: {index: sheet or None}. A page with no sheet between two pages whose
    sheets are two apart in the list order gets the sheet in between. Returns {index: sheet}."""
    pos = {s: i for i, s in enumerate(order)}
    added = {}
    for i in sorted(pages):
        if pages[i]:
            continue
        a, b = pages.get(i - 1), pages.get(i + 1)
        if a in pos and b in pos and pos[b] - pos[a] == 2:
            added[i] = order[pos[a] + 1]
    return added


def scan(folders, want, skip_dirs, skip_files):
    """Return single-sheet candidates {sheet: [..]}, combined PDF paths, and names not read."""
    single, combined, unread, bookmark_only = collections.defaultdict(list), [], [], []
    for root in folders:
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not any(s in d.lower() for s in skip_dirs)]
            for f in fn:
                if not f.lower().endswith(".pdf") or any(s.lower() in f.lower() for s in skip_files):
                    continue
                full = os.path.join(dp, f)
                n = page_count(full)
                if n > 1:
                    combined.append(full)
                    continue
                if n != 1:
                    unread.append(full)
                    continue
                sh, title = find_sheet(f[:-4], want)
                if not sh:
                    bookmark_only.append(full)  # no number in the name: trust only its own bookmark
                    continue
                if sh:
                    single[sh].append({"kind": "single", "file": full, "page": 0, "title": title,
                                       "mtime": os.path.getmtime(long_path(full)), "how": "file name"})
    return single, combined, unread, bookmark_only


def inside(child_dir, parent_dir):
    c = os.path.normcase(os.path.abspath(child_dir))
    p = os.path.normcase(os.path.abspath(parent_dir))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def drop_compiled(cands):
    """Pure logic: remove combined-PDF pages that have another source in their own folder tree."""
    keep = []
    for c in cands:
        if c["kind"] == "page":
            home = os.path.dirname(c["file"])
            if any(o["file"] != c["file"] and inside(os.path.dirname(o["file"]), home) for o in cands):
                continue
        keep.append(c)
    return keep


def choose(sheets, single, pages, prefer, prefer_prefix):
    """Pure logic: pick one source per listed sheet. Returns (plan, missing)."""
    plan, missing = [], []
    pref = os.path.normcase(os.path.abspath(prefer)) if prefer else None
    for s in sheets:
        sh = s["sheet"]
        everything = single.get(sh, []) + pages.get(sh, [])
        kept = drop_compiled(everything)
        # dropped compiled pages stay at the end, only for the "not used" list
        cands = sorted(kept, key=lambda c: -c["mtime"]) + sorted([c for c in everything if c not in kept], key=lambda c: -c["mtime"])
        prefix = re.match(r"[A-Za-z]+", sh.strip())
        if pref and prefix and prefix.group().upper() in prefer_prefix:
            first = [c for c in cands if c["kind"] == "single"
                     and os.path.normcase(os.path.abspath(os.path.dirname(c["file"]))) == pref]
            cands = first[:1] + [c for c in cands if c not in first[:1]]
        if not cands:
            missing.append({"sheet": sheet_key(sh), "list_title": (s.get("title") or "").strip(), "group": s.get("group", "")})
            continue
        pick = cands[0]
        title, frm = pick.get("title"), "file name" if pick["kind"] == "single" else "source bookmark"
        if not title:
            title, frm = clean_title(s.get("title", "")), "drawing list"
        plan.append({"sheet": sheet_key(sh), "list_title": (s.get("title") or "").strip(), "group": s.get("group", ""),
                     "bookmark": (sheet_key(sh) + " " + title).strip(), "title_from": frm,
                     "kind": pick["kind"], "how": pick["how"], "file": pick["file"], "page": pick["page"],
                     "mtime": datetime.datetime.fromtimestamp(pick["mtime"]).strftime("%Y-%m-%d %H:%M"),
                     "not_used": [{"file": c["file"], "page": c["page"],
                                   "mtime": datetime.datetime.fromtimestamp(c["mtime"]).strftime("%Y-%m-%d %H:%M")}
                                  for c in cands[1:]]})
    return plan, missing


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("sheets", nargs="?")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--folder", action="append", default=[])
    ap.add_argument("--plan")
    ap.add_argument("--prefer")
    ap.add_argument("--prefer-prefix", default="")
    ap.add_argument("--skip-dir", action="append", default=[])
    ap.add_argument("--skip-file", action="append", default=[])
    ap.add_argument("--no-combined", action="store_true")
    ap.add_argument("--no-text", action="store_true")
    ap.add_argument("--workers", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("pypdf is missing. Install it with: python -m pip install --user pypdf")
        return 3
    if a.check:
        import pypdf
        print("pypdf", pypdf.__version__)
        try:
            import pdfplumber
            print("pdfplumber", pdfplumber.__version__)
        except ImportError:
            print("pdfplumber missing (optional: reads sheet numbers from title blocks)")
        return 0
    if not (a.sheets and a.folder and a.plan):
        ap.print_usage()
        return 2
    import logging
    logging.disable(logging.CRITICAL)
    with open(a.sheets, encoding="utf-8") as fh:
        sheets = json.load(fh)
    want = {norm(s["sheet"]): s["sheet"] for s in sheets}
    order = [s["sheet"] for s in sheets]
    skip_dirs = tuple(SKIP_DIRS) + tuple(x.lower() for x in a.skip_dir)
    single, combined, unread, bookmark_only = scan(a.folder, want, skip_dirs, a.skip_file)

    pages = collections.defaultdict(list)
    if not a.no_combined:
        todo = []
        found = {}
        for path in combined + bookmark_only:
            n, bm = bookmark_pages(path, want)
            found[path] = {i: (bm[i] if i in bm else None) for i in range(n)}
            if path in combined:
                todo += [(path, i, want) for i in range(n) if i not in bm]
        if todo and not a.no_text:
            try:
                import pdfplumber  # noqa: F401
                from concurrent.futures import ProcessPoolExecutor
                workers = a.workers or min(12, os.cpu_count() or 1)
                with ProcessPoolExecutor(max_workers=workers) as ex:
                    for path, i, sh in ex.map(title_block_sheet, todo, chunksize=2):
                        if sh:
                            found[path][i] = (sh, None, "title block")
            except ImportError:
                print("NOTE      pdfplumber missing: pages with no bookmark are not read")
        for path, pm in found.items():
            mt = os.path.getmtime(long_path(path))
            flat = {i: (v[0] if v else None) for i, v in pm.items()}
            for i, sh in (fill_by_position(flat, order).items() if path in combined else []):
                pm[i] = (sh, None, "position")
            for i, v in pm.items():
                if not v:
                    continue
                how = v[2] if len(v) > 2 else "bookmark"
                pages[v[0]].append({"kind": "page", "file": path, "page": i, "title": v[1],
                                    "mtime": mt, "how": how})

    plan, missing = choose(sheets, single, pages, a.prefer, {p.strip().upper() for p in a.prefer_prefix.split(",") if p.strip()})
    used = collections.Counter((p["file"], p["page"]) for p in plan)
    twice = [k for k, v in used.items() if v > 1]
    with open(a.plan, "w", encoding="utf-8") as fh:
        json.dump({"plan": plan, "missing": missing, "order": [sheet_key(s["sheet"]) for s in sheets],
                   "folders": a.folder, "unread": unread}, fh, indent=1, ensure_ascii=False)

    print("LISTED    %d" % len(sheets))
    print("FOUND     %d  (single PDFs %d, pages from combined PDFs %d)" % (
        len(plan), sum(p["kind"] == "single" for p in plan), sum(p["kind"] == "page" for p in plan)))
    by = collections.Counter((p["kind"], os.path.dirname(p["file"]) if p["kind"] == "single" else p["file"]) for p in plan)
    for (kind, where), n in sorted(by.items(), key=lambda x: -x[1]):
        print("SOURCE    %4d  %-6s %s" % (n, kind, where))
    for p in plan:
        if p["how"] in ("title block", "position"):
            print("READ BY %-11s %s  %s p%d" % (p["how"].upper(), p["sheet"], os.path.basename(p["file"]), p["page"] + 1))
    for m in missing:
        print("MISSING   %s %s" % (m["sheet"], m["list_title"]))
    for k in twice:
        print("PAGE USED TWICE  %s p%d" % (k[0], k[1] + 1))
    for u in unread:
        print("NOT READ  " + u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
