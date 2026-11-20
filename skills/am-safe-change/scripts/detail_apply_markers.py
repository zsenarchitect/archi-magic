# -*- coding: utf-8 -*-
"""EXECUTE step for detail markers (run_modify / --mode=dry_run|modify).

inputs: {"preview_path": "C:/.../markers.preview.json"}   (forward slashes are fine)
    or  {"preview": {...preview dict...}}

The preview comes from detail_marker_plan.py (retarget, reference or
remove). The script replays its ordered "steps" in ONE transaction, so
the user's Keep/Discard in Revit is all-or-nothing:
- "retarget": ReferenceableViewUtils.ChangeReferencedView points a
  reference marker to another view. Guard: the marker still points to
  the view in the preview.
- "add_reference": ViewSection.CreateReferenceSection adds a
  view-specific reference section in the host view (head, tail in feet).
  The method returns nothing, so the new id is the one new entry in
  the host view's GetReferenceSections().
- "delete_view": after the steps above, the view's dependents
  (GetDependentElements) must equal the preview's delete list. If not,
  the script stops and Revit rolls everything back. Then doc.Delete.
  Deleting a section view also deletes every marker that still points
  to it, so the retargets come first.

result = {"retargeted": [[marker_id, view_id_after], ...],
          "added": [[name, marker_id, host_view_id, view_id], ...],
          "deleted": [id, ...], "steps_applied": n, "escaped": bool}
A dry run returns the same result and Revit rolls everything back.
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import io
import json
import sys

from pyrevit.api import DB

ESCAPE = sys.platform == "cli"  # IronPython result encoder is ASCII-only (see read_sheets.py)


def txt(s):
    if s is None or not ESCAPE:
        return s
    out = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif ord(c) < 128:
            out.append(c)
        else:
            out.append("\\u%04x" % ord(c))
    return "".join(out)


def id_int(element_id):
    try:
        return int(element_id.Value)        # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)  # removed in Revit 2026


# DB.ElementId(int) is ambiguous in IronPython (issue #50).
try:
    import System

    def eid(n):
        return DB.ElementId(System.Int64(int(n)))
except ImportError:
    def eid(n):
        return DB.ElementId(int(n))


def load_preview():
    if inputs.get("preview"):  # noqa: F821
        return inputs["preview"]  # noqa: F821
    path = inputs.get("preview_path")  # noqa: F821
    if not path:
        raise ValueError("inputs needs preview_path or preview")
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.loads(f.read())


def referenced(marker_id):
    return id_int(DB.ReferenceableViewUtils.GetReferencedViewId(doc, eid(marker_id)))  # noqa: F821


def view_of(view_id, what):
    view = doc.GetElement(eid(view_id))  # noqa: F821
    if view is None or not isinstance(view, DB.View):
        raise ValueError("%s %s no longer exists. Nothing changed." % (what, view_id))
    return view


preview = load_preview()
if preview.get("kind") != "change-preview":
    raise ValueError("Not a change-preview file.")
if preview.get("blocked"):
    raise ValueError("The preview is BLOCKED. Resolve its blocking warnings first.")
steps = preview.get("steps") or []
if not steps:
    raise ValueError("The preview has no steps. Nothing to apply.")

# Stale-state guard, before any change.
for st in steps:
    action = st.get("action")
    if action == "retarget":
        if doc.GetElement(eid(st["marker_id"])) is None:  # noqa: F821
            raise ValueError("Marker %s no longer exists. Nothing changed." % st["marker_id"])
        now = referenced(st["marker_id"])
        if st.get("from_view_id") is not None and now != int(st["from_view_id"]):
            raise ValueError("Model changed since the preview. Marker %s now points to %s, not %s."
                             % (st["marker_id"], now, st["from_view_id"]))
        view_of(st["to_view_id"], "Target view")
    elif action == "add_reference":
        view_of(st["host_view_id"], "Host view")
        view_of(st["target_view_id"], "Target view")
    elif action == "delete_view":
        view_of(st["view_id"], "View")
    else:
        raise ValueError("Unknown step action %r." % action)

t = DB.Transaction(doc, preview.get("title") or "Change detail markers")  # noqa: F821
t.Start()
retargeted, added, deleted = [], [], []
done = 0
try:
    for st in steps:
        action = st["action"]
        if action == "retarget":
            DB.ReferenceableViewUtils.ChangeReferencedView(doc, eid(st["marker_id"]),  # noqa: F821
                                                          eid(st["to_view_id"]))
            retargeted.append([int(st["marker_id"]), referenced(st["marker_id"])])
        elif action == "add_reference":
            host = view_of(st["host_view_id"], "Host view")
            old = set(id_int(i) for i in host.GetReferenceSections())
            head, tail = st["head"], st["tail"]
            DB.ViewSection.CreateReferenceSection(doc, host.Id, eid(st["target_view_id"]),  # noqa: F821
                                                  DB.XYZ(head[0], head[1], head[2]),
                                                  DB.XYZ(tail[0], tail[1], tail[2]))
            new = [i for i in (id_int(x) for x in host.GetReferenceSections()) if i not in old]
            if len(new) != 1:
                raise ValueError("Could not find the new reference section in view %s." % st["host_view_id"])
            added.append([txt(st.get("name")), new[0], int(st["host_view_id"]), referenced(new[0])])
        elif action == "delete_view":
            view = view_of(st["view_id"], "View")
            ids = set(id_int(i) for i in view.GetDependentElements(None))
            ids.add(int(st["view_id"]))
            want = set(int(i) for i in st["expected_deleted_ids"])
            if ids != want:
                raise ValueError("The view's dependents differ from the preview. Extra: %s. Missing: %s. "
                                 "Nothing changed. Read again." % (
                                     sorted(ids - want)[:20], sorted(want - ids)[:20]))
            for i in doc.Delete(view.Id):  # noqa: F821
                deleted.append(id_int(i))
        done += 1
    # Re-read every retargeted marker after the delete: it must still exist.
    for row in retargeted:
        if doc.GetElement(eid(row[0])) is None:  # noqa: F821
            raise ValueError("Marker %s was deleted. Nothing changed." % row[0])
        row[1] = referenced(row[0])
    t.Commit()
except Exception:
    if t.HasStarted() and not t.HasEnded():
        t.RollBack()
    raise

result = {"retargeted": retargeted, "added": added, "deleted": sorted(deleted),  # noqa: F821
          "steps_applied": done, "escaped": ESCAPE}
