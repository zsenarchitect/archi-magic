# -*- coding: utf-8 -*-
"""Worksharing ownership read for am-safe-change (issue #97). Read-only (run_query / --mode=query).

Use it twice in the skill:
- before the dry run, on the target ids, to find elements another user owns;
- after a kept or discarded run, on the target ids plus the categories of the
  dry run, to find what the current user now borrows.

inputs: {"ids": [123, 456],                       (target element ids)
         "unique_ids": {"123": "<unique_id>"},    (optional: re-find after sync renumbers ids)
         "guids": {"456": "<guid>"},              (optional: re-find SharedParameterElement by GUID)
         "categories": ["Views", "Text Notes"],   (optional: scan these for borrowed elements)
         "max_elements": 5000}                    (optional: scan limit, see am-revit-api-docs)
    ownership.py inputs --preview <preview.json> [--dryrun <dryrun.json>] writes this file.

File mode (issue #278, the normal path): pass two absolute paths instead of the ids.
    inputs: {"inputs_path": "C:/work/own-inputs.json",  (read the keys above from this file)
             "out_path": "C:/work/own.json"}            (write the full result here)
Keys given in `inputs` itself win over the keys in the file. With out_path,
the script writes the full result as UTF-8 JSON (real text, "escaped": false)
and returns only a short summary:
    {"document", "workshared", "user", "escaped", "out_path", "written": true,
     "bytes", "counts": {"elements", "missing", "id_changed", "owned_by_others",
     "owned_by_me", "not_owned", "updated_in_central", "scan_borrowed"},
     "owners": [[owner, count], ...], "scan": {"checked", "partial", "missing_categories"}}
Both paths must be absolute (a drive letter or a UNC path). A relative path or
%TEMP% resolves inside the Revit process, not in the agent's folder (issue #85).
A bad path returns {"status": "bad_inputs", "error": ...} and writes nothing.
Without out_path the script returns the full result, as before.

result = {"document", "workshared": bool, "user", "escaped": bool,
          "elements": [{"id", "exists", "status", "owner", "updates", "category", "name"}],
          "scan": {"categories": [...], "missing_categories": [...], "checked", "partial"},
          "scan_borrowed": [{"id", "category", "name"}]}
status is the WorksharingUtils.GetCheckoutStatus name. owner is read with
GetWorksharingTooltipInfo only for OwnedByOtherUser. updates is the
GetModelUpdatesStatus name. The script never borrows, syncs or relinquishes.
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import io
import json
import os
import re
import sys

from pyrevit.api import DB

ESCAPE = sys.platform == "cli"  # IronPython result encoder is ASCII-only (see read_sheets.py)
TEXT_ESCAPE = ESCAPE  # main() turns it off when the full result goes to a file

try:
    inputs
except NameError:
    inputs = {}
inputs = inputs or {}
CALL_INPUTS = inputs  # the run_query inputs as given; main() may merge a file into `inputs`

_ABS_PATH = re.compile(r"^([A-Za-z]:[\\/]|[\\/]{2}[^\\/])")


def path_error(name, path):
    """None when path is an absolute Windows path (or POSIX path on a POSIX host), else one sentence."""
    if not isinstance(path, (type(u""), str)):
        return "%s must be a text path." % name
    # A POSIX absolute path is valid only on a POSIX host (the offline tests on
    # Linux CI). Revit runs on Windows, where "/x" is drive-relative and refused.
    posix_abs = os.sep == "/" and path.startswith("/") and not path.startswith("//")
    if "%" in path or path.startswith("~") or not (_ABS_PATH.match(path) or posix_abs):
        return ("%s must be an absolute path, for example C:/work/own.json. A relative "
                "path or %%TEMP%% resolves inside the Revit process (issue #85)." % name)
    return None


def load_inputs(raw):
    """(merged inputs, error). The keys in raw win over the keys in raw["inputs_path"]."""
    path = raw.get("inputs_path")
    if not path:
        return dict(raw), None
    err = path_error("inputs_path", path)
    if err:
        return None, err
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            data = json.loads(f.read())
    except Exception as e:
        return None, "Cannot read inputs_path %s: %s" % (path, e)
    if not isinstance(data, dict):
        return None, "inputs_path %s does not hold a JSON object." % path
    merged = dict(data)
    for k in raw:
        if k != "inputs_path":
            merged[k] = raw[k]
    return merged, None


def esc(s):
    """Text for the inline result: ASCII-escaped under IronPython (see txt)."""
    if s is None:
        return ""
    return ascii_escape(s) if ESCAPE else s


def txt(s):
    """Text for the full result. Not escaped when the result goes to a file."""
    if s is None:
        return ""
    return ascii_escape(s) if TEXT_ESCAPE else s


def ascii_escape(s):
    out = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif ord(c) < 128:
            out.append(c)
        else:
            out.append("\\u%04x" % ord(c))
    return "".join(out)


def eid_int(element_id):
    try:
        return int(element_id.Value)         # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)  # removed in Revit 2026


def make_id(i):
    try:
        import System
        return DB.ElementId(System.Int64(i))  # Revit 2024+ (2026 has only Int64)
    except Exception:
        return DB.ElementId(int(i))


# Compare with the enum members, not str(): the two engines print enums differently.
CHECKOUT = [(DB.CheckoutStatus.OwnedByOtherUser, "OwnedByOtherUser"),
            (DB.CheckoutStatus.OwnedByCurrentUser, "OwnedByCurrentUser"),
            (DB.CheckoutStatus.NotOwned, "NotOwned")]
UPDATES = [(DB.ModelUpdatesStatus.CurrentWithCentral, "CurrentWithCentral"),
           (DB.ModelUpdatesStatus.UpdatedInCentral, "UpdatedInCentral"),
           (DB.ModelUpdatesStatus.NotYetInCentral, "NotYetInCentral"),
           (DB.ModelUpdatesStatus.DeletedInCentral, "DeletedInCentral")]


def enum_name(value, table):
    for member, name in table:
        if value == member:
            return name
    return str(value)


def category_name(el):
    try:
        return txt(el.Category.Name) if el.Category is not None else ""
    except Exception:
        return ""


def element_name(el):
    try:
        return txt(el.Name)
    except Exception:
        return ""


def find_fallback(i):
    uid_map = inputs.get("unique_ids") or {}
    uid = uid_map.get(str(i)) or uid_map.get(i)
    if uid:
        try:
            el = doc.GetElement(str(uid))
            if el is not None:
                return el
        except Exception:
            pass
    guid_map = inputs.get("guids") or {}
    guid_str = guid_map.get(str(i)) or guid_map.get(i)
    if guid_str:
        try:
            import System
            el = DB.SharedParameterElement.Lookup(doc, System.Guid(str(guid_str)))
            if el is not None:
                return el
        except Exception:
            pass
    return None


def describe(i):
    eid = make_id(i)
    el = doc.GetElement(eid)
    id_changed = False
    if el is None:
        el = find_fallback(i)
        if el is None:
            return {"id": i, "exists": False}
        eid = el.Id
        id_changed = True
    actual_id = eid_int(eid)
    status = enum_name(DB.WorksharingUtils.GetCheckoutStatus(doc, eid), CHECKOUT)
    owner = ""
    if status == "OwnedByOtherUser":
        try:
            owner = txt(DB.WorksharingUtils.GetWorksharingTooltipInfo(doc, eid).Owner)
        except Exception:
            owner = ""
    try:
        updates = enum_name(DB.WorksharingUtils.GetModelUpdatesStatus(doc, eid), UPDATES)
    except Exception:
        updates = ""
    out = {"id": actual_id, "exists": True, "status": status, "owner": owner,
           "updates": updates, "category": category_name(el), "name": element_name(el)}
    if id_changed or actual_id != i:
        out["requested_id"] = i
        out["id_changed"] = True
    return out


def scan(names, limit):
    """Elements in the named categories that the current user borrows."""
    wanted = set(names)
    cats = [c for c in doc.Settings.Categories if c.Name in wanted]
    found = set(c.Name for c in cats)
    borrowed, checked, partial = [], 0, False
    for cat in cats:
        for el in DB.FilteredElementCollector(doc).OfCategoryId(cat.Id):
            if checked >= limit:
                partial = True
                break
            checked += 1
            if DB.WorksharingUtils.GetCheckoutStatus(doc, el.Id) == DB.CheckoutStatus.OwnedByCurrentUser:
                borrowed.append({"id": eid_int(el.Id), "category": category_name(el),
                                 "name": element_name(el)})
        if partial:
            break
    return ({"categories": [txt(n) for n in sorted(found)],
             "missing_categories": [txt(n) for n in sorted(wanted - found)],
             "checked": checked, "partial": partial}, borrowed)


def read(merged):
    """The full ownership result for the active document."""
    base = {"document": txt(doc.Title), "escaped": TEXT_ESCAPE,
            "user": txt(doc.Application.Username)}
    if doc.IsFamilyDocument or not doc.IsWorkshared:
        base.update({"workshared": False, "elements": [], "scan_borrowed": [],
                     "scan": {"categories": [], "missing_categories": [],
                              "checked": 0, "partial": False}})
        return base
    ids = [int(i) for i in merged.get("ids") or []]
    limit = int(merged.get("max_elements") or 5000)
    info, borrowed = scan(merged.get("categories") or [], limit)
    base.update({"workshared": True, "elements": [describe(i) for i in ids],
                 "scan": info, "scan_borrowed": borrowed})
    return base


def summary(full, out_path, size):
    """Short inline result for file mode: counts and owners, no element rows."""
    counts = {"elements": 0, "missing": 0, "id_changed": 0, "owned_by_others": 0,
              "owned_by_me": 0, "not_owned": 0, "updated_in_central": 0,
              "scan_borrowed": len(full.get("scan_borrowed") or [])}
    owners = {}
    for e in full.get("elements") or []:
        counts["elements"] += 1
        if not e.get("exists", True):
            counts["missing"] += 1
            continue
        if e.get("id_changed"):
            counts["id_changed"] += 1
        status = e.get("status")
        if status == "OwnedByOtherUser":
            counts["owned_by_others"] += 1
            name = e.get("owner") or "another user"
            owners[name] = owners.get(name, 0) + 1
        elif status == "OwnedByCurrentUser":
            counts["owned_by_me"] += 1
        else:
            counts["not_owned"] += 1
        if e.get("updates") == "UpdatedInCentral":
            counts["updated_in_central"] += 1
    scan_info = full.get("scan") or {}
    return {"document": esc(full.get("document")), "workshared": full.get("workshared"),
            "user": esc(full.get("user")), "escaped": ESCAPE,
            "out_path": esc(out_path), "written": True, "bytes": size,
            "counts": counts,
            "owners": [[esc(n), owners[n]] for n in sorted(owners, key=lambda n: (-owners[n], n))],
            "scan": {"checked": scan_info.get("checked", 0),
                     "partial": bool(scan_info.get("partial")),
                     "missing_categories": [esc(n) for n in scan_info.get("missing_categories") or []]}}


def write_result(path, full):
    """Write the full result as UTF-8 JSON. Returns the number of bytes."""
    text = json.dumps(full, indent=1, sort_keys=True)  # ASCII: non-ASCII text becomes \uXXXX
    if not isinstance(text, type(u"")):
        text = text.decode("ascii")
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(text)


def main():
    global inputs, TEXT_ESCAPE
    if doc is None:
        return {"status": "no_active_document"}
    merged, err = load_inputs(CALL_INPUTS)
    if err:
        return {"status": "bad_inputs", "error": esc(err)}
    out_path = merged.get("out_path")
    if out_path:
        err = path_error("out_path", out_path)
        if err:
            return {"status": "bad_inputs", "error": esc(err)}
    inputs = merged  # find_fallback reads unique_ids and guids from here
    if not out_path:
        return read(merged)
    TEXT_ESCAPE = False  # json.dumps escapes the text itself, so the file holds real text
    full = read(merged)
    try:
        size = write_result(out_path, full)
    except Exception as e:
        return {"status": "write_failed", "error": esc("Cannot write out_path %s: %s" % (out_path, e))}
    return summary(full, out_path, size)


result = main()
