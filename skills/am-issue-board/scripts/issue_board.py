# -*- coding: utf-8 -*-
"""Pure logic for the am-issue-board skill: an issue tracker kept in a Revit text note.

Runs under IronPython 2.7 (inside Revit, through run_query / run_modify with
`workspace` set to this folder) and under CPython 3 (tests, board_cli.py).
No f-strings, no type annotations, no nonlocal. The only functions that touch
a Revit object are apply_formats() and apply_plan(); they receive the
FormattedText and two small adapters, so tests can pass a fake.

Board format (one paragraph per line, paragraphs separated by "\\r" in Revit):

    AUDIT - 2026-01-05 (by @user1)                      bold, list None, indent 0
    Issues use fixed numbers; next new issue is #3. ...  list None, indent 0
    <blank>                                              list None, indent 0
    #1 [OPEN] Title text - @user2                        bold, list None, indent 0
    Severity: High                                       bullet, indent 1
    Finding: ... (id1001) ...                            bullet, indent 1, ids underlined
    detail row                                           bullet, indent 2
    To do: ...                                           bullet, indent 1
    2026-01-05 @user1: dated comment                     bullet, indent 1
    Related: #2. Path: see #2.                           bullet, indent 1

In Revit the bullet comes from the list type, so child lines have no "- " in
the plain text. The parser also accepts a leading "- " (hand-written boards).
"""
import hashlib
import re

STATUSES = ("OPEN", "IN PROGRESS", "DONE")
PARA_BREAK = "\r"

RULES_TEMPLATE = ("Issues use fixed numbers; next new issue is #%d. Never renumber. "
                  "Status: [OPEN], [IN PROGRESS], [DONE]. Close an issue by setting [DONE] "
                  "and adding a dated comment.")

TITLE_RE = re.compile(r"^#(\d+)\s+\[(OPEN|IN PROGRESS|DONE)\]\s+(.*?)"
                      r"(?:\s+-\s+(@\S+(?:\s+@\S+)*))?\s*$", re.UNICODE)
NEXT_RE = re.compile(r"next new issue is #(\d+)", re.IGNORECASE)
COMMENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(@\S+(?:\s+@\S+)*)\s*:\s*(.*)$", re.UNICODE)
ID_RE = re.compile(r"\bid(\d+)\b")
REF_RE = re.compile(r"#(\d+)")
PARENT_RE = re.compile(r"see\s+#(\d+)", re.IGNORECASE)
FIELD_PREFIXES = (("severity", "Severity:"), ("finding", "Finding:"),
                  ("todo", "To do:"), ("related", "Related:"))


class BoardError(Exception):
    """A plan cannot be made (bad input, unknown issue)."""


class BoardChanged(Exception):
    """The note text changed since it was read. Nothing was written."""


# ---------------------------------------------------------------- paragraphs

def split_paragraphs(text):
    """Return [(start, text)] for each paragraph, with offsets in the raw text.

    Accepts "\\r", "\\r\\n" and "\\n" breaks. The empty piece after a final
    break is not a paragraph (Revit throws on a TextRange there).
    """
    out = []
    i = 0
    start = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\r" or c == "\n":
            out.append((start, text[start:i]))
            if c == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            start = i + 1
        i += 1
    if start < n:
        out.append((start, text[start:]))
    return out


def text_sha1(text):
    if not isinstance(text, bytes):
        text = text.encode("utf-8")
    return hashlib.sha1(text).hexdigest()


def id_spans(text):
    """[(offset, length)] of every element id written as id<number>."""
    return [(m.start(), m.end() - m.start()) for m in ID_RE.finditer(text)]


def unescape(text):
    """Undo the read script's ASCII escaping (\\uXXXX and doubled backslashes)."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == "u" and i + 6 <= n:
                try:
                    out.append(unichr_(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(c)
        i += 1
    return "".join(out)


def unichr_(code):
    try:
        return unichr(code)  # noqa: F821  (IronPython 2.7)
    except NameError:
        return chr(code)


# ---------------------------------------------------------------- parsing

def _strip_bullet(line):
    s = line.strip()
    if s.startswith("- "):
        s = s[2:].strip()
    return s


def classify(line):
    """Return (kind, value) for one paragraph of the board."""
    raw = line.strip()
    if not raw:
        return "blank", ""
    m = TITLE_RE.match(raw)
    if m:
        return "title", m
    s = _strip_bullet(raw)
    m = COMMENT_RE.match(s)
    if m:
        return "comment", m
    low = s.lower()
    for kind, prefix in FIELD_PREFIXES:
        if low.startswith(prefix.lower()):
            return kind, s[len(prefix):].strip()
    return "detail", s


def parse_related(value):
    """'#2, #5. Path: see #5.' -> ([2, 5], 5). Numbers after the first '. ' are not links."""
    head = value
    rest = ""
    m = re.search(r"\.(\s|$)", value)
    if m:
        head = value[:m.start()]
        rest = value[m.end():]
    related = [int(x) for x in REF_RE.findall(head)]
    pm = PARENT_RE.search(rest) or PARENT_RE.search(head)
    parent = int(pm.group(1)) if pm else None
    if parent is not None and PARENT_RE.search(head):
        related = [r for r in related if r != parent] or related
    return related, parent


def _new_issue(m, index):
    assignees = m.group(4).split() if m.group(4) else []
    return {"number": int(m.group(1)), "status": m.group(2), "title": m.group(3).strip(),
            "assignees": assignees, "severity": [], "findings": [], "todos": [],
            "comments": [], "related": [], "parent": None, "element_ids": [],
            "first_para": index, "last_para": index, "related_para": None,
            "lines": []}


def parse_board(text, formats=None):
    """Parse board text into {"header", "issues", "paragraphs", "warnings"}.

    `formats` is optional: one dict per paragraph (list, indent, bold) as the
    read script returns it. It is only copied onto each paragraph record.
    """
    paras = split_paragraphs(text)
    header = {"title": "", "lines": [], "next_number": None, "next_para": None}
    issues = []
    records = []
    warnings = []
    current = None
    last_field = None
    for idx, (start, line) in enumerate(paras):
        kind, val = classify(line)
        rec = {"index": idx, "start": start, "text": line, "kind": kind}
        if formats and idx < len(formats):
            rec["format"] = formats[idx]
        records.append(rec)
        if kind == "title":
            current = _new_issue(val, idx)
            issues.append(current)
            last_field = None
            continue
        if current is None:
            if kind != "blank":
                # Header lines keep header formatting (list None, indent 0) on edits.
                rec["kind"] = "rules" if header["title"] else "header"
                if not header["title"]:
                    header["title"] = line.strip()
                header["lines"].append(line.strip())
                m = NEXT_RE.search(line)
                if m and header["next_number"] is None:
                    header["next_number"] = int(m.group(1))
                    header["next_para"] = idx
            continue
        if kind == "blank":
            continue
        current["last_para"] = idx
        current["lines"].append({"kind": kind, "text": _strip_bullet(line), "index": idx})
        current["element_ids"].extend(int(x) for x in ID_RE.findall(line))
        if kind == "comment":
            current["comments"].append({"date": val.group(1), "user": val.group(2),
                                        "text": val.group(3)})
        elif kind == "severity":
            current["severity"].append(val)
        elif kind == "finding":
            current["findings"].append(val)
        elif kind == "todo":
            current["todos"].append(val)
        elif kind == "related":
            rel, parent = parse_related(val)
            for r in rel:
                if r not in current["related"]:
                    current["related"].append(r)
            if parent is not None:
                current["parent"] = parent
            current["related_para"] = idx
        elif kind == "detail":
            target = {"severity": "severity", "todo": "todos"}.get(last_field, "findings")
            current[target].append(val)
        if kind in ("severity", "finding", "todo"):
            last_field = kind
    seen = {}
    for iss in issues:
        if iss["number"] in seen:
            warnings.append("issue #%d appears twice" % iss["number"])
        seen[iss["number"]] = True
    if header["next_number"] is None:
        warnings.append("header has no 'next new issue is #N' line")
    elif issues and header["next_number"] <= max(i["number"] for i in issues):
        warnings.append("next new issue #%d is not above the highest issue #%d"
                        % (header["next_number"], max(i["number"] for i in issues)))
    for a, b in check_related_symmetry({"issues": issues}):
        warnings.append("#%d lists #%d in Related, but #%d does not list #%d" % (a, b, b, a))
    return {"header": header, "issues": issues, "paragraphs": records, "warnings": warnings}


def find_issue(board, number):
    for iss in board["issues"]:
        if iss["number"] == number:
            return iss
    raise BoardError("issue #%d is not on the board" % number)


def check_related_symmetry(board):
    """[(a, b)] where #a lists #b in Related but #b does not list #a (b must exist)."""
    rel = dict((i["number"], i["related"]) for i in board["issues"])
    out = []
    for a in sorted(rel):
        for b in rel[a]:
            if b in rel and a not in rel[b]:
                out.append((a, b))
    return out


def summary(board):
    """Counts only (no text): safe to print or return from Revit."""
    issues = board["issues"]
    by_status = dict((s, 0) for s in STATUSES)
    for i in issues:
        by_status[i["status"]] += 1
    return {"issues": len(issues), "next_number": board["header"]["next_number"],
            "by_status": by_status,
            "comments": sum(len(i["comments"]) for i in issues),
            "severity_lines": sum(1 for i in issues for ln in i["lines"] if ln["kind"] == "severity"),
            "related_lines": sum(1 for i in issues if i["related_para"] is not None),
            "with_parent": sum(1 for i in issues if i["parent"] is not None),
            "element_ids": sum(len(i["element_ids"]) for i in issues),
            "asymmetric_links": len(check_related_symmetry(board)),
            "numbers": [i["number"] for i in issues],
            "paragraphs": len(board["paragraphs"]),
            "warnings": len(board["warnings"])}


# ---------------------------------------------------------------- rendering

def para(text, kind, indent, bold=False):
    """One paragraph of the model. list is "bullet" when indent > 0."""
    return {"text": text, "kind": kind, "indent": indent,
            "list": "bullet" if indent > 0 else "none", "bold": bold,
            "underline": id_spans(text)}


def blank_para():
    return para("", "blank", 0)


def title_text(number, status, title, assignees):
    if status not in STATUSES:
        raise BoardError("status must be one of %s" % ", ".join(STATUSES))
    who = " ".join(_at(a) for a in (assignees or ["TBD"]))
    return "#%d [%s] %s - %s" % (number, status, title.strip(), who)


def _at(name):
    name = name.strip()
    return name if name.startswith("@") else "@" + name


def related_text(related, parent=None):
    rel = sorted(set(related))
    s = "Related: " + ", ".join("#%d" % r for r in rel)
    if parent is not None:
        s += ". Path: see #%d." % parent
    return s


def comment_text(date, user, text):
    return "%s %s: %s" % (date, _at(user), text.strip())


def render_header(title_line, next_number):
    return [para(title_line, "header", 0, bold=True),
            para(RULES_TEMPLATE % next_number, "rules", 0)]


def render_issue(issue):
    """Paragraphs for one issue dict (number, status, title, assignees, severity,
    findings, todos, comments, related, parent). A finding or to-do may be a
    list [line, detail1, detail2]; details go to indent 2."""
    out = [para(title_text(issue["number"], issue.get("status", "OPEN"), issue["title"],
                           issue.get("assignees")), "title", 0, bold=True)]
    sev = issue.get("severity")
    for s in ([sev] if isinstance(sev, str) else (sev or [])):
        out.append(para("Severity: " + s, "severity", 1))
    for label, kind, key in (("Finding: ", "finding", "findings"), ("To do: ", "todo", "todos")):
        for item in issue.get(key) or []:
            rows = item if isinstance(item, (list, tuple)) else [item]
            out.append(para(label + rows[0], kind, 1))
            for d in rows[1:]:
                out.append(para(d, "detail", 2))
    for c in issue.get("comments") or []:
        out.append(para(comment_text(c["date"], c["user"], c["text"]), "comment", 1))
    if issue.get("related") or issue.get("parent") is not None:
        rel = list(issue.get("related") or [])
        if issue.get("parent") is not None and issue["parent"] not in rel:
            rel.append(issue["parent"])
        out.append(para(related_text(rel, issue.get("parent")), "related", 1))
    return out


def render_board(title_line, issues, next_number=None):
    nums = [i["number"] for i in issues]
    if len(nums) != len(set(nums)):
        raise BoardError("issue numbers must be unique")
    if next_number is None:
        next_number = (max(nums) + 1) if nums else 1
    if nums and next_number <= max(nums):
        raise BoardError("next new issue number must be above every issue number")
    out = render_header(title_line, next_number)
    for iss in issues:
        out.append(blank_para())
        out.extend(render_issue(iss))
    return out


def paragraphs_text(paras):
    return PARA_BREAK.join(p["text"] for p in paras)


def format_ops(paras, base, text_len):
    """Format operations for paragraphs that start at offset `base` of a text of
    length `text_len`. Each range covers the paragraph text plus its break when
    one follows. A range never starts at or past the end of the text."""
    ops = []
    pos = base
    for p in paras:
        n = len(p["text"])
        length = n + 1 if pos + n < text_len else n
        if length > 0 and pos < text_len:
            length = min(length, text_len - pos)
            ops.append({"op": "para", "start": pos, "length": length, "list": p["list"],
                        "indent": p["indent"], "bold": bool(p["bold"])})
            if n > 0:
                ops.append({"op": "underline", "start": pos, "length": n, "value": False})
            for off, ln in p["underline"]:
                ops.append({"op": "underline", "start": pos + off, "length": ln, "value": True})
        pos += n + 1
    return ops


def plan_create(title_line, issues, next_number=None):
    paras = render_board(title_line, issues, next_number)
    text = paragraphs_text(paras)
    return {"kind": "issue-board-plan", "mode": "create", "final_text": text,
            "formats": format_ops(paras, 0, len(text)), "edits": [],
            "expected_sha1": None}


# ---------------------------------------------------------------- edit plans

def _default_model(rec):
    """Paragraph model for an existing paragraph, keeping its read format if known."""
    kind = rec["kind"]
    text = rec["text"]
    fmt = rec.get("format") or {}
    if kind in ("title", "header"):
        p = para(text, kind, 0, bold=True)
    elif kind in ("blank", "rules"):
        p = para(text, kind, 0)
    elif kind == "detail":
        p = para(text, kind, 2)
    else:
        p = para(text, kind, 1)
    if "indent" in fmt:
        p["indent"] = int(fmt["indent"])
    if "list" in fmt:
        p["list"] = "none" if str(fmt["list"]).lower() == "none" else "bullet"
    return p


class _Planner(object):
    """Collects paragraph replacements against one read of the board text."""

    def __init__(self, text, formats=None):
        self.text = text
        self.board = parse_board(text, formats)
        self.repl = {}  # paragraph index -> list of paragraph models

    def models_at(self, idx):
        if idx not in self.repl:
            self.repl[idx] = [_default_model(self.board["paragraphs"][idx])]
        return self.repl[idx]

    def replace(self, idx, new_text, kind=None):
        models = self.models_at(idx)
        old = models[0]
        p = para(new_text, kind or old["kind"], old["indent"], old["bold"])
        p["list"] = old["list"]
        models[0] = p

    def insert_after(self, idx, new_paras):
        rec = self.board["paragraphs"][idx]
        if not rec["text"]:
            raise BoardError("cannot anchor an insert on a blank paragraph")
        self.models_at(idx).extend(new_paras)

    def build(self, action):
        edits = []
        for idx in sorted(self.repl):
            rec = self.board["paragraphs"][idx]
            if not rec["text"]:
                raise BoardError("cannot replace a blank paragraph")
            edits.append({"start": rec["start"], "old": rec["text"],
                          "new": paragraphs_text(self.repl[idx]), "paras": self.repl[idx]})
        final = self.text
        for e in sorted(edits, key=lambda x: -x["start"]):
            final = final[:e["start"]] + e["new"] + final[e["start"] + len(e["old"]):]
        # Format ops in final-text coordinates.
        formats = []
        shift = 0
        for e in sorted(edits, key=lambda x: x["start"]):
            formats.extend(format_ops(e["paras"], e["start"] + shift, len(final)))
            shift += len(e["new"]) - len(e["old"])
        for e in edits:
            del e["paras"]
        return {"kind": "issue-board-plan", "mode": "edit", "action": action,
                "expected_sha1": text_sha1(self.text), "expected_length": len(self.text),
                "edits": edits, "final_text": final, "formats": formats}

    def set_related_line(self, iss, related, parent):
        new = related_text(related, parent)
        if iss["related_para"] is not None:
            self.replace(iss["related_para"], new, "related")
        else:
            self.insert_after(iss["last_para"], [para(new, "related", 1)])


def _bump_header(pl, new_next):
    h = pl.board["header"]
    if h["next_para"] is None:
        raise BoardError("the header has no 'next new issue is #N' line; fix the header first")
    rec = pl.board["paragraphs"][h["next_para"]]
    new = NEXT_RE.sub("next new issue is #%d" % new_next, rec["text"], count=1)
    pl.replace(h["next_para"], new, "rules")


def plan_add_issue(text, issue, formats=None):
    """Add one issue at the end with number = the header's next number.
    Existing issues keep their numbers. Related links are made symmetric."""
    pl = _Planner(text, formats)
    board = pl.board
    number = board["header"]["next_number"]
    if number is None:
        raise BoardError("the header has no 'next new issue is #N' line; fix the header first")
    existing = [i["number"] for i in board["issues"]]
    if number in existing or (existing and number <= max(existing)):
        raise BoardError("next new issue #%d is already used; fix the header by hand" % number)
    new = dict(issue)
    new["number"] = number
    for r in list(new.get("related") or []) + ([new["parent"]] if new.get("parent") else []):
        find_issue(board, r)
    paras = [blank_para()] + render_issue(new)
    anchors = [r for r in board["paragraphs"] if r["text"]]
    if not anchors:
        raise BoardError("the board is empty")
    last = anchors[-1]["index"]
    _bump_header(pl, number + 1)
    # Back-links first: when the last issue gets a new Related line, it shares
    # the anchor paragraph with the new issue and must come before it.
    for r in sorted(set(list(new.get("related") or []) + ([new["parent"]] if new.get("parent") else []))):
        other = find_issue(board, r)
        if number not in other["related"]:
            pl.set_related_line(other, other["related"] + [number], other["parent"])
    pl.insert_after(last, paras)
    plan = pl.build("add #%d" % number)
    plan["number"] = number
    return plan


def plan_set_status(text, number, status, date, user, comment, formats=None):
    """Change the status of #number and add a dated comment (before Related)."""
    if status not in STATUSES:
        raise BoardError("status must be one of %s" % ", ".join(STATUSES))
    if not comment or not comment.strip():
        raise BoardError("a status change needs a dated comment")
    pl = _Planner(text, formats)
    iss = find_issue(pl.board, number)
    pl.replace(iss["first_para"], title_text(number, status, iss["title"], iss["assignees"]),
               "title")
    _add_comment(pl, iss, date, user, comment)
    return pl.build("#%d -> %s" % (number, status))


def plan_comment(text, number, date, user, comment, formats=None):
    pl = _Planner(text, formats)
    iss = find_issue(pl.board, number)
    _add_comment(pl, iss, date, user, comment)
    return pl.build("comment on #%d" % number)


def _add_comment(pl, iss, date, user, comment):
    p = para(comment_text(date, user, comment), "comment", 1)
    # Put it after the last line that is not the Related line.
    lines = [ln for ln in iss["lines"] if ln["kind"] != "related"]
    anchor = lines[-1]["index"] if lines else iss["first_para"]
    pl.insert_after(anchor, [p])


def plan_relate(text, a, b, formats=None):
    """Link #a and #b both ways."""
    if a == b:
        raise BoardError("an issue cannot relate to itself")
    pl = _Planner(text, formats)
    ia = find_issue(pl.board, a)
    ib = find_issue(pl.board, b)
    for x, y in ((ia, b), (ib, a)):
        if y not in x["related"]:
            pl.set_related_line(x, x["related"] + [y], x["parent"])
    return pl.build("relate #%d and #%d" % (a, b))


# ---------------------------------------------------------------- apply (Revit side)

def check_guard(current_text, plan):
    """Stop when the note text is not the text the plan was made from."""
    sha = plan.get("expected_sha1")
    if sha is None:
        return
    if text_sha1(current_text) != sha:
        raise BoardChanged("The note text changed since it was read (for example a manual edit "
                           "in Revit). Nothing was written. Read the board again.")


def apply_formats(ft, ops, make_range, list_type):
    """Apply format ops to a FormattedText. make_range(start, length) builds a
    TextRange; list_type("bullet"|"none") returns the ListType value."""
    n = len(ft.GetPlainText())
    done = 0
    for op in ops:
        start = op["start"]
        length = min(op["length"], n - start)
        if start >= n or length <= 0:
            continue
        rng = make_range(start, length)
        if op["op"] == "para":
            ft.SetListType(rng, list_type(op["list"]))
            ft.SetIndentLevel(rng, op["indent"])
            ft.SetBoldStatus(rng, op["bold"])
        else:
            ft.SetUnderlineStatus(rng, op["value"])
        done += 1
    return done


def apply_plan(ft, plan, make_range, list_type):
    """Apply an edit plan to a FormattedText in place. Raises BoardChanged (and
    writes nothing) when the guard or any anchor check fails."""
    check_guard(ft.GetPlainText(), plan)
    edits = sorted(plan["edits"], key=lambda e: -e["start"])
    prev = None
    for e in edits:
        if prev is not None and e["start"] + len(e["old"]) > prev:
            raise BoardError("plan edits overlap")
        prev = e["start"]
    # Check every anchor before the first write.
    for e in edits:
        rng = ft.Find(e["old"], e["start"], True, False)
        if rng is None or _range_start(rng) != e["start"]:
            raise BoardChanged("Text '%s' is not at offset %d any more. Nothing was written."
                               % (e["old"][:40], e["start"]))
    for e in edits:
        rng = ft.Find(e["old"], e["start"], True, False)
        ft.SetPlainText(rng, e["new"])
    if ft.GetPlainText() != plan["final_text"]:
        raise BoardChanged("The text after the edits is not the planned text.")
    return apply_formats(ft, plan["formats"], make_range, list_type)


def _range_start(rng):
    return rng.Start if hasattr(rng, "Start") else rng[0]
