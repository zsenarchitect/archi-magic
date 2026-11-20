# -*- coding: utf-8 -*-
"""COMPUTE and VERIFY steps for detail markers (issue #257). Runs outside Revit (CPython 3).

refs.json is the result of detail_read_references.py (run_query).

Duplicate by condition: details that already show the requested condition.
    python detail_marker_plan.py same-condition --read refs.json [--sheet 123]
        [--term "carpet" --term "porcelain tile"] [--floor-type "<type>" ...]
        [--cut "M1"] [--exclude-view 456]
  Exit 1 when a placed detail shows every part of the condition: ask the
  user before you cut a new view.

Audit: markers whose target view is on no sheet (a half-done removal).
    python detail_marker_plan.py audit --read refs.json
  Exit 1 when it finds one.

State of a view to remove (placed, viewport removed, view missing):
    python detail_marker_plan.py state --read refs.json --view 456

Plan a change (preview for detail_apply_markers.py):
    python detail_marker_plan.py plan --read refs.json
        [--retarget "<marker id>:<view id>" ...]
        [--add-reference "<host view id>:<target view id>:hx,hy,hz:tx,ty,tz" ...]
        [--remove-view 456 [--delete-marker <marker id> ...]]
        [--title "..."] --out preview.json
  Exit 1 when the preview is BLOCKED (for example a marker of the removed
  view has no decision: the output lists the question to ask).

Compare an apply result (dry run or real) or a fresh read with the preview:
    python detail_marker_plan.py compare --preview preview.json --actual run.json

Name the helper elements of new section views in a dry run:
    python detail_marker_plan.py helpers --dryrun dryrun.json --new-views 1 --viewports 1

Input files may be the bare result or the whole run record. Exit codes:
0 ok, 1 a finding, a difference or blocked, 2 bad input.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import detail_marker_logic as dm  # noqa: E402


def load_json(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def pair(text, what):
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("Bad %s value %r: use \"<id>:<id>\"." % (what, text))
    return int(parts[0]), int(parts[1])


def point(text):
    values = [float(c) for c in text.split(",")]
    if len(values) != 3:
        raise ValueError("A point needs x,y,z (feet): %r" % text)
    return values


def reference_spec(text):
    parts = text.split(":")
    if len(parts) != 4:
        raise ValueError("Bad --add-reference value %r: use \"host:target:hx,hy,hz:tx,ty,tz\"." % text)
    return {"host_view_id": int(parts[0]), "target_view_id": int(parts[1]),
            "head": point(parts[2]), "tail": point(parts[3])}


def unwrap(data):
    if isinstance(data, dict) and isinstance(data.get("response"), dict):
        data = data["response"]
    if isinstance(data, dict) and "views" not in data and "retargeted" not in data \
            and isinstance(data.get("result"), dict):
        data = data["result"]
    return data


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("same-condition")
    s.add_argument("--read", required=True)
    s.add_argument("--sheet", type=int)
    s.add_argument("--term", action="append", default=[])
    s.add_argument("--floor-type", action="append", default=[])
    s.add_argument("--cut")
    s.add_argument("--exclude-view", action="append", type=int, default=[])
    a_ = sub.add_parser("audit")
    a_.add_argument("--read", required=True)
    st = sub.add_parser("state")
    st.add_argument("--read", required=True)
    st.add_argument("--view", required=True, type=int)
    p = sub.add_parser("plan")
    p.add_argument("--read", required=True)
    p.add_argument("--retarget", action="append", default=[])
    p.add_argument("--add-reference", action="append", default=[])
    p.add_argument("--remove-view", type=int)
    p.add_argument("--delete-marker", action="append", type=int, default=[])
    p.add_argument("--title")
    p.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--preview", required=True)
    c.add_argument("--actual", required=True)
    h = sub.add_parser("helpers")
    h.add_argument("--dryrun", required=True)
    h.add_argument("--new-views", required=True, type=int)
    h.add_argument("--viewports", required=True, type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "same-condition":
            read = dm.load_read(load_json(a.read))
            floors = list(a.floor_type)
            if a.cut:
                floors.extend(dm.cut_floor_types(read, a.cut))
            found = dm.find_same_condition(read, a.term, floors, a.sheet, a.exclude_view)
            print(dm.same_condition_text(found))
            return 1 if found["same"] else 0
        if a.cmd == "audit":
            rows = dm.audit_references(dm.load_read(load_json(a.read)))
            print(dm.audit_text(rows))
            return 1 if rows else 0
        if a.cmd == "state":
            read = dm.load_read(load_json(a.read))
            state = dm.removal_state(read, a.view)
            print("View %s: %s (%s)." % (a.view, state["state"], dm.placement_text(state["view"])))
            views = dm.views_by_id(read)
            for ref in state["references"]:
                host = views.get(int(ref["host_view_id"]))
                print("- %s %s in %s points to it." % (dm.KIND_NAMES.get(ref.get("kind"), "Marker"), ref["id"],
                                                      dm.view_label(host, ref["host_view_id"])))
            if state["view"] is not None and state["view"].get("hidden_viewport_ids"):
                print("Hidden viewport(s) on no sheet: %s (Revit helpers, deleted with the view)."
                      % ", ".join(str(i) for i in state["view"]["hidden_viewport_ids"]))
            return 0
        if a.cmd == "plan":
            read = dm.load_read(load_json(a.read))
            retargets = [pair(t, "--retarget") for t in a.retarget]
            refs = [reference_spec(t) for t in a.add_reference]
            preview = dm.plan_changes(read, retargets, refs, a.remove_view, a.delete_marker, a.title)
            with io.open(a.out, "w", encoding="utf-8") as f:
                f.write(json.dumps(preview, indent=1, ensure_ascii=False))
            print(dm.plan_text(preview))
            return 1 if preview["blocked"] else 0
        if a.cmd == "compare":
            preview = load_json(a.preview)
            actual = unwrap(load_json(a.actual))
            if isinstance(actual, dict) and "views" in actual:
                actual = dm.load_read(actual)
            diffs = dm.compare(preview, actual)
            if diffs:
                print("DIFFERENT from preview (%d):" % len(diffs))
                for d in diffs:
                    print("  " + d)
                return 1
            print("MATCHES PREVIEW: %d step(s) checked." % len(preview["steps"]))
            return 0
        if a.cmd == "helpers":
            summary = dm.classify_new_view_changes(load_json(a.dryrun), a.new_views, a.viewports)
            print(dm.helpers_text(summary))
            return 1 if summary["unexpected"] or not summary["available"] else 0
    except (KeyError, TypeError, ValueError) as error:
        print("Bad input: %s" % error, file=sys.stderr)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
