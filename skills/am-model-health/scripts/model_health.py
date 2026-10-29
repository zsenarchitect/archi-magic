# -*- coding: utf-8 -*-
"""am-model-health: read-only model QA for the pyRevit Agent Runtime.

Submit with the pyrevit MCP tool run_query (or `pyrevit agent run <this file> --mode=query`).
IronPython 2.7 compatible: no f-strings, no type hints, no print kwargs.
The script changes nothing and writes no files. It sets `result` to a JSON-safe dict:

  {model, revit_year, timestamp, path, metrics: [
      {id, label, value, unit, threshold: {warn, bad}, status: ok|warn|bad|info,
       hint, details: [{label, count, ids: [int, ...]}]}]}

Optional inputs (dict): {"max_ids": 300, "top_warnings": 15}
"""
import datetime
import math
import re
import sys
import time

try:
    import System
    from pyrevit.api import DB
except ImportError:
    # Plain CPython (the unit tests): only the pure functions below are used.
    System = None
    DB = None

try:
    doc  # noqa: F821  (injected by the agent runtime)
except NameError:
    doc = None

try:
    INPUTS = inputs  # noqa: F821  (injected by the agent runtime)
except NameError:
    INPUTS = None
if not isinstance(INPUTS, dict):
    INPUTS = {}

MAX_IDS = int(INPUTS.get("max_ids", 300))
TOP_WARNINGS = int(INPUTS.get("top_warnings", 15))
FAR_FT = 328.084  # 100 m. A link origin farther than this is a coordinate hint.
CAD_OUTLIER_FT = float(INPUTS.get("cad_outlier_ft", 1000.0))  # about 300 m from the median of the other CAD links
CAD_MANY_VIEWS = int(INPUTS.get("cad_many_views", 25))  # a model-level CAD link shown in this many views or more
CAD_VIEW_BUDGET_S = float(INPUTS.get("cad_view_budget_s", 30.0))  # stop counting views after this; result says partial
MAX_ROWS = 200  # per-link and per-view detail rows, so the run_query response stays small


def eid(element_id):
    """ElementId -> int. Revit 2024+ has .Value; 2023 and older only .IntegerValue."""
    if hasattr(element_id, "Value"):
        return int(element_id.Value)
    return int(element_id.IntegerValue)


IRONPYTHON = sys.platform == "cli"


def jsonable(value):
    """Make non-ASCII text survive the runtime's json.dumps on IronPython 2.7.

    IronPython's json encoder decodes every non-ASCII str as UTF-8 bytes and fails
    on names like 'Cafe' with an accent (ResultSerializationError). Handing it the
    UTF-8 bytes as latin-1 code points makes that decode return the real text.
    Tested live on Revit 2026 / IronPython 2.7.12. CPython needs no change.
    """
    if isinstance(value, dict):
        return dict((jsonable(k), jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if IRONPYTHON and isinstance(value, basestring):  # noqa: F821
        if any(ord(c) > 127 for c in value):
            return value.encode("utf-8").decode("latin-1")
    return value


def status_for(value, warn, bad):
    """Higher value is worse. None thresholds mean information only."""
    if warn is None and bad is None:
        return "info"
    if bad is not None and value > bad:
        return "bad"
    if warn is not None and value > warn:
        return "warn"
    return "ok"


def metric(mid, label, value, warn, bad, hint, details=None, unit=""):
    details = [d for d in (details or []) if d.get("count")]
    return {
        "id": mid,
        "label": label,
        "value": value,
        "unit": unit,
        "threshold": {"warn": warn, "bad": bad},
        "status": status_for(value, warn, bad),
        "hint": hint,
        "details": details or [],
    }


def collect(cls=None, bic=None, types=False):
    col = DB.FilteredElementCollector(doc)  # noqa: F821
    if cls is not None:
        col = col.OfClass(cls)
    if bic is not None:
        col = col.OfCategory(bic)
    if not types and cls is None:
        col = col.WhereElementIsNotElementType()
    return list(col)


def safe_name(element):
    try:
        return element.Name
    except Exception:
        pass
    try:
        # IronPython cannot read .Name on an ElementType (CAD and Revit link types); the
        # base-class property getter can.
        return DB.Element.Name.__get__(element)
    except Exception:
        return "(no name)"


def check_warnings():
    groups = {}
    order = []
    for w in doc.GetWarnings():  # noqa: F821
        text = w.GetDescriptionText()
        if text not in groups:
            groups[text] = []
            order.append(text)
        groups[text].extend([eid(i) for i in w.GetFailingElements()])
        groups[text].append(None)  # one marker per warning instance
    rows = []
    total = 0
    for text in order:
        ids = [i for i in groups[text] if i is not None]
        count = len(groups[text]) - len(ids)
        total += count
        rows.append({"label": text, "count": count, "ids": ids[:MAX_IDS]})
    rows.sort(key=lambda r: -r["count"])
    return total, rows


def check_links():
    types = collect(cls=DB.RevitLinkType)
    insts = collect(cls=DB.RevitLinkInstance)
    unloaded = []
    for lt in types:
        try:
            loaded = DB.RevitLinkType.IsLoaded(doc, lt.Id)  # noqa: F821
        except Exception:
            loaded = False
        if not loaded:
            unloaded.append(lt)
    unpinned = [li for li in insts if not li.Pinned]
    far = []
    for li in insts:
        try:
            o = li.GetTotalTransform().Origin
            if abs(o.X) > FAR_FT or abs(o.Y) > FAR_FT or abs(o.Z) > FAR_FT:
                far.append(li)
        except Exception:
            pass
    placed_type_ids = set([eid(li.GetTypeId()) for li in insts])
    not_placed = [lt for lt in types if eid(lt.Id) not in placed_type_ids]
    return types, insts, unloaded, unpinned, far, not_placed


def check_in_place():
    inplace = [f for f in collect(cls=DB.Family) if f.IsInPlace]
    inplace_ids = set([eid(f.Id) for f in inplace])
    rows = {}
    for fi in collect(cls=DB.FamilyInstance):
        try:
            fam = fi.Symbol.Family
        except Exception:
            continue
        if fam is not None and eid(fam.Id) in inplace_ids:
            key = fam.Name
            rows.setdefault(key, []).append(eid(fi.Id))
    details = [{"label": k, "count": len(v), "ids": v[:MAX_IDS]} for k, v in rows.items()]
    details.sort(key=lambda r: -r["count"])
    return len(inplace), details


def check_rooms(warning_rows):
    unplaced, not_enclosed = [], []
    total = 0
    for r in collect(bic=DB.BuiltInCategory.OST_Rooms):
        total += 1
        try:
            area = r.Area
        except Exception:
            continue
        if area > 0:
            continue
        if r.Location is None:
            unplaced.append(eid(r.Id))
        else:
            not_enclosed.append(eid(r.Id))
    redundant = []
    for row in warning_rows:
        if "Multiple Rooms" in row["label"] or "redundant" in row["label"].lower():
            redundant.extend(row["ids"])
    redundant = sorted(set(redundant))
    # A redundant room can also report zero area; count it once, as redundant.
    red = set(redundant)
    not_enclosed = [i for i in not_enclosed if i not in red]
    return total, unplaced, not_enclosed, redundant


def check_duplicate_marks():
    marks = {}
    for el in DB.FilteredElementCollector(doc).WhereElementIsNotElementType():  # noqa: F821
        cat = el.Category
        if cat is None or cat.CategoryType != DB.CategoryType.Model:
            continue
        p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if p is None:
            continue
        v = p.AsString()
        if not v or not v.strip():
            continue
        marks.setdefault((cat.Name, v.strip()), []).append(eid(el.Id))
    rows = []
    for (cat_name, mark), ids in marks.items():
        if len(ids) > 1:
            rows.append({"label": "%s / Mark %s" % (cat_name, mark), "count": len(ids), "ids": ids[:MAX_IDS]})
    rows.sort(key=lambda r: -r["count"])
    return sum([r["count"] for r in rows]), rows


# ---------------------------------------------------------------------------
# CAD links (issue #59). Pure functions first (unit tested in tests/), then the Revit reader.
# ---------------------------------------------------------------------------
_USER_PATH = re.compile(r"^[A-Za-z]:\\+Users\\+([^\\]+)\\+([^\\]*)", re.IGNORECASE)
_PROFILE_FOLDERS = ("desktop", "documents", "downloads", "appdata", "pictures", "videos", "music", "")


def personal_path_kind(path):
    """Classify a CAD path that lives in one person's Windows profile.

    Returns (kind, user_folder) or (None, None). kind is "onedrive" for a OneDrive or
    SharePoint sync folder (C:/Users/<name>/OneDrive - <company>/...), "sync" for a
    company sync folder (C:/Users/<name>/<company>/...), else "profile" (Desktop,
    Downloads, ...). Nobody else can reload a file from any of these.
    """
    if not path:
        return None, None
    m = _USER_PATH.match(path.replace("/", "\\"))
    if not m:
        return None, None
    user, first = m.group(1), m.group(2)
    low = first.lower()
    if user.lower() in ("public", "default", "all users"):
        return None, None
    if low.startswith("onedrive") or low.startswith("sharepoint"):
        return "onedrive", user
    if low in _PROFILE_FOLDERS:
        return "profile", user
    return "sync", user


def cad_status(is_linked, status_text):
    """Status label for one CAD instance: Imported, unreadable, or the LinkedFileStatus name."""
    if not is_linked:
        return "Imported"
    if not status_text:
        return "unreadable"
    return str(status_text)


def median(values):
    """Median of a list of numbers (no statistics module on IronPython 2.7). None if empty."""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def origin_outliers(points, threshold_ft):
    """points: [(key, x, y)] in feet. Returns {key: distance_ft to the nearest other point}
    for each point that is farther than threshold_ft from the median of the OTHER points
    AND from every other point. Needs 3 points or more.

    The second test matters when placement is mixed (some links by shared coordinates,
    some near the internal origin): the median then sits between the groups and would
    flag every link. A link that shares its spot with another link is not an outlier.
    """
    out = {}
    if len(points) < 3:
        return out
    for i, (key, x, y) in enumerate(points):
        others = [p for j, p in enumerate(points) if j != i]
        mx = median([p[1] for p in others])
        my = median([p[2] for p in others])
        if math.hypot(x - mx, y - my) <= threshold_ft:
            continue
        nearest = min([math.hypot(x - p[1], y - p[2]) for p in others])
        if nearest > threshold_ft:
            out[key] = nearest
    return out


def cad_file_key(name):
    """Normalized file name: base name, lower case, no extension."""
    base = (name or "").replace("/", "\\").split("\\")[-1].strip().lower()
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base


def duplicate_cad_keys(rows):
    """rows: dicts with key, name, owner_view_id (0 for model level). Returns the set of keys
    whose file is placed more than once in the same place (model level, or one view)."""
    groups = {}
    for r in rows:
        k = (cad_file_key(r.get("name")), r.get("owner_view_id") or 0)
        if not k[0]:
            continue
        groups.setdefault(k, []).append(r["key"])
    dup = set()
    for keys in groups.values():
        if len(keys) > 1:
            dup.update(keys)
    return dup


def cad_link_flags(row, workshared, outlier_ft=None, duplicate=False, many_views=CAD_MANY_VIEWS):
    """Flags for one CAD instance row. Read only: SKILL.md maps each flag to a suggested fix."""
    flags = []
    kind = row.get("path_kind")
    if kind == "onedrive":
        flags.append("personal OneDrive/SharePoint path")
    elif kind in ("sync", "profile"):
        flags.append("personal folder path")
    status = row.get("status")
    if status == "NotFound":
        flags.append("file not found")
    elif status == "unreadable":
        flags.append("path unreadable")
    elif status not in ("Loaded", "Imported", None):
        flags.append("not loaded (%s)" % status)
    if not row.get("pinned"):
        flags.append("not pinned")
    if workshared and not row.get("view_specific"):
        if "cad" not in (row.get("workset") or "").lower():
            flags.append("not on a CAD workset")
    if outlier_ft is not None:
        flags.append("far from the other CAD links (nearest %d ft)" % int(round(outlier_ft)))
    if duplicate:
        flags.append("same file placed more than once")
    views = row.get("views") or 0
    if not row.get("view_specific") and views >= many_views:
        flags.append("shows in %d views" % views)
    return flags


def cad_link_details(rows):
    """Detail rows for the dashboard: one per flagged CAD instance, most flags first."""
    flagged = [r for r in rows if r.get("flags")]
    flagged.sort(key=lambda r: (-len(r["flags"]), r["name"]))
    return [{"label": "%s (%s, %s): %s" % (r["name"], r["kind"], r["status"], "; ".join(r["flags"])),
             "count": len(r["flags"]), "ids": [r["id"]]} for r in flagged[:MAX_ROWS]]


_GRAPHICAL_VIEW_TYPES = ("FloorPlan", "CeilingPlan", "Elevation", "ThreeD", "Section",
                         "Detail", "AreaPlan", "EngineeringPlan", "Walkthrough")


def _model_view(v):
    """A non-template view that can show model-level elements."""
    if v.IsTemplate or isinstance(v, (DB.ViewSheet, DB.ViewSchedule)):
        return False
    return str(v.ViewType) in _GRAPHICAL_VIEW_TYPES


def _creator(element_id):
    try:
        return DB.WorksharingUtils.GetWorksharingTooltipInfo(doc, element_id).Creator or ""  # noqa: F821
    except Exception:
        return ""


def _cad_path_and_status(ctype):
    """(user-visible path, LinkedFileStatus name) of a CAD link type. Empty/None when unreadable."""
    path, status_text = "", None
    try:
        ref = ctype.GetExternalFileReference()
        if ref is not None:
            status_text = str(ref.GetLinkedFileStatus())
            mp = ref.GetAbsolutePath() or ref.GetPath()
            if mp is not None:
                path = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(mp) or ""
    except Exception:
        return "", None
    if not path:
        return "", None  # no readable path: report "unreadable", not a guess
    return path, status_text


def read_cad_links():
    """One row per ImportInstance (linked and imported CAD). Per-class collectors only."""
    workshared = bool(doc.IsWorkshared)  # noqa: F821
    wst = doc.GetWorksetTable() if workshared else None  # noqa: F821
    rows = []
    type_cache = {}
    insts = collect(cls=DB.ImportInstance)
    for ci in insts:
        ctype = doc.GetElement(ci.GetTypeId())  # noqa: F821
        name = safe_name(ctype) if ctype is not None else "(unknown)"
        is_linked = bool(ci.IsLinked)
        path, status_text = "", None
        if is_linked and ctype is not None:
            tid = eid(ctype.Id)
            if tid not in type_cache:
                type_cache[tid] = _cad_path_and_status(ctype)
            path, status_text = type_cache[tid]
        view_specific = bool(ci.ViewSpecific)
        owner_view_id = eid(ci.OwnerViewId) if view_specific else 0
        owner_view = ""
        if view_specific:
            ov = doc.GetElement(ci.OwnerViewId)  # noqa: F821
            owner_view = safe_name(ov) if ov is not None else ""
        workset = ""
        if wst is not None:
            try:
                workset = wst.GetWorkset(ci.WorksetId).Name
            except Exception:
                workset = ""
        origin, rotation = [0.0, 0.0, 0.0], 0.0
        try:
            t = ci.GetTotalTransform()
            origin = [round(t.Origin.X, 2), round(t.Origin.Y, 2), round(t.Origin.Z, 2)]
            rotation = round(math.degrees(math.atan2(t.BasisX.Y, t.BasisX.X)), 2)
        except Exception:
            pass
        extent = None
        try:
            bb = ci.get_BoundingBox(None)
            if bb is not None:
                extent = [round(bb.Max.X - bb.Min.X, 1), round(bb.Max.Y - bb.Min.Y, 1)]
        except Exception:
            pass
        rows.append({
            "key": eid(ci.Id), "id": eid(ci.Id), "name": name,
            "kind": "linked" if is_linked else "imported",
            "path": path, "path_kind": personal_path_kind(path)[0],
            "status": cad_status(is_linked, status_text),
            "workset": workset, "pinned": bool(ci.Pinned),
            "view_specific": view_specific, "owner_view_id": owner_view_id, "owner_view": owner_view,
            "origin_ft": origin, "rotation_deg": rotation, "extent_ft": extent,
            "views": 1 if view_specific else 0,
            "creator": _creator(ci.Id) if workshared else "",
        })
    model_level = [ci for ci in insts if not ci.ViewSpecific]
    counts, view_stats = count_cad_views(model_level)
    for r in rows:
        if not r["view_specific"]:
            r["views"] = counts.get(r["id"], 0)
    # Outliers: model-level instances only (view-specific origins are in drafting-view space).
    outliers = origin_outliers([(r["key"], r["origin_ft"][0], r["origin_ft"][1])
                                for r in rows if not r["view_specific"]], CAD_OUTLIER_FT)
    dups = duplicate_cad_keys(rows)
    for r in rows:
        r["flags"] = cad_link_flags(r, workshared, outliers.get(r["key"]), r["key"] in dups)
        del r["key"]
    return rows, view_stats


def _hidden_in(ci, v):
    try:
        if ci.IsHidden(v):
            return True
        cat = ci.Category
        return cat is not None and v.GetCategoryHidden(cat.Id)
    except Exception:
        return False  # unknown: let the view collector decide


def count_cad_views(model_level):
    """{instance id: number of model views that show it}, plus run stats.

    A view collector is the truth, but it is slow on a cold model. A view is skipped
    when every model-level CAD instance is hidden in it (element or category hidden).
    The loop stops after CAD_VIEW_BUDGET_S seconds; stats["partial"] then says the
    counts are lower bounds.
    """
    counts = {}
    stats = {"views_checked": 0, "views_skipped_hidden": 0, "views_not_checked": 0, "partial": False}
    if not model_level:
        return counts, stats
    ids = set([eid(ci.Id) for ci in model_level])
    t0 = time.time()
    for v in collect(cls=DB.View):
        if not _model_view(v):
            continue
        if stats["partial"] or time.time() - t0 > CAD_VIEW_BUDGET_S:
            stats["partial"] = True
            stats["views_not_checked"] += 1
            continue
        if all([_hidden_in(ci, v) for ci in model_level]):
            stats["views_skipped_hidden"] += 1
            continue
        stats["views_checked"] += 1
        try:
            for ci in DB.FilteredElementCollector(doc, v.Id).OfClass(DB.ImportInstance):  # noqa: F821
                i = eid(ci.Id)
                if i in ids:
                    counts[i] = counts.get(i, 0) + 1
        except Exception:
            continue  # some view kinds refuse a view collector
    stats["seconds"] = round(time.time() - t0, 1)
    return counts, stats


# ---------------------------------------------------------------------------
# Heavy 2D views (issue #61). Pure functions first (unit tested in tests/), then the Revit reader.
# Never count <Sketch> lines, Automatic Sketch Dimensions or elements with no category:
# they are normal, passive elements, not bloat.
# ---------------------------------------------------------------------------
SEVERITY_2D = (("Critical", 1000), ("High", 500), ("Medium", 250))
LINE_HEAVY_SHARE = 0.85
_NOT_BLOAT_CATEGORIES = ("<sketch>", "sketch lines", "automatic sketch dimensions")
_NOT_BLOAT_STYLES = ("<sketch>",)
_CAD_LAYER = re.compile(r"(\$0\$|\||^[A-Z]{1,2}-[A-Z0-9]{2,6}(-[A-Z0-9]{2,6})*$)")


def severity_2d(total):
    """Critical 1,000+, High 500-999, Medium 250-499, else None."""
    for label, floor in SEVERITY_2D:
        if total >= floor:
            return label
    return None


def counts_as_2d(category_name, style_name=""):
    """False for elements that must never be reported as 2D bloat."""
    if not category_name:
        return False  # no category
    if category_name.strip().lower() in _NOT_BLOAT_CATEGORIES:
        return False
    if (style_name or "").strip().lower() in _NOT_BLOAT_STYLES:
        return False
    return True


def is_archive_name(name):
    return (name or "").strip().upper().startswith("ARCHIVE_")


def cad_layer_like(style_name):
    """True when a line style name looks like a CAD layer (A-WALL, X-REF$0$LAYER, xref|layer).
    Only then may a report say the lines may come from exploded CAD."""
    return bool(_CAD_LAYER.search((style_name or "").strip()))


def top_items(counter, n=3):
    """[(name, count)] of the n biggest counts, ties by name."""
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def view_2d_row(view_id, name, counts, styles=None, sheet=None, creator=""):
    """One view row. counts: lines, detail_items (filled regions included), filled_regions,
    text_notes, dimensions."""
    lines = counts.get("lines", 0)
    total = lines + counts.get("detail_items", 0) + counts.get("text_notes", 0) + counts.get("dimensions", 0)
    share = (float(lines) / total) if total else 0.0
    top = top_items(styles or {}, 3)
    return {
        "id": view_id, "name": name, "total": total,
        "lines": lines, "detail_items": counts.get("detail_items", 0),
        "filled_regions": counts.get("filled_regions", 0),
        "text_notes": counts.get("text_notes", 0), "dimensions": counts.get("dimensions", 0),
        "line_share": round(share, 3), "line_heavy": total > 0 and share >= LINE_HEAVY_SHARE,
        "severity": severity_2d(total), "sheet": sheet or "", "archive": is_archive_name(name),
        "creator": creator or "",
        "top_line_styles": [{"name": k, "count": v} for k, v in top],
        "cad_layer_styles": len([k for k, _ in top if cad_layer_like(k)]),
        "same_counts_as": 0,
    }


def _count_key(r):
    return (r["lines"], r["detail_items"], r["filled_regions"], r["text_notes"], r["dimensions"])


def mark_near_duplicates(rows):
    """Set same_counts_as = number of OTHER views with exactly the same counts."""
    groups = {}
    for r in rows:
        groups.setdefault(_count_key(r), []).append(r)
    for g in groups.values():
        for r in g:
            r["same_counts_as"] = len(g) - 1 if r["total"] else 0
    return rows


def rank_heavy_views(rows):
    """Views with a severity, worst first."""
    order = dict((label, i) for i, (label, _) in enumerate(SEVERITY_2D))
    ranked = [r for r in rows if r["severity"]]
    ranked.sort(key=lambda r: (order[r["severity"]], -r["total"], r["name"]))
    return ranked


def heavy_view_label(r):
    """Plain-words label for one ranked view. Never says "exploded CAD"."""
    bits = ["lines %d%%" % int(round(r["line_share"] * 100))]
    if r["line_heavy"]:
        bits.append("line-heavy")
    if r["filled_regions"]:
        bits.append("%d filled regions" % r["filled_regions"])
    bits.append("on sheet %s" % r["sheet"] if r["sheet"] else "not on a sheet")
    if r["archive"]:
        bits.append("ARCHIVE view")
    if r["same_counts_as"]:
        bits.append("same counts as %d other view(s)" % r["same_counts_as"])
    if r["cad_layer_styles"]:
        bits.append("line styles look like CAD layers")
    if r["creator"]:
        bits.append("by " + r["creator"])
    styles = ", ".join([s["name"] for s in r["top_line_styles"]])
    if styles:
        bits.append("top styles: " + styles)
    return "%s: %s - %d 2D elements (%s)" % (r["severity"], r["name"], r["total"], "; ".join(bits))


def summarize_heavy(rows, ranked):
    by_sev = dict((label, 0) for label, _ in SEVERITY_2D)
    by_creator = {}
    for r in ranked:
        by_sev[r["severity"]] += 1
        k = r["creator"] or "(unknown)"
        by_creator[k] = by_creator.get(k, 0) + 1
    return {
        "views_with_2d": len([r for r in rows if r["total"]]),
        "critical": by_sev["Critical"], "high": by_sev["High"], "medium": by_sev["Medium"],
        "on_sheet": len([r for r in ranked if r["sheet"]]),
        "archive": len([r for r in ranked if r["archive"]]),
        "near_duplicate": len([r for r in ranked if r["same_counts_as"]]),
        "line_heavy": len([r for r in ranked if r["line_heavy"]]),
        "by_creator": by_creator,
    }


def _style_name(e, cache):
    try:
        p = e.get_Parameter(DB.BuiltInParameter.BUILDING_CURVE_GSTYLE)
        sid = p.AsElementId() if p is not None else None
        if sid is None:
            return ""
        k = eid(sid)
        if k not in cache:
            gs = doc.GetElement(sid)  # noqa: F821
            cache[k] = safe_name(gs) if gs is not None else ""
        return cache[k]
    except Exception:
        return ""


def read_heavy_2d():
    """Per-view 2D counts from four per-category collectors, grouped by OwnerViewId."""
    t0 = time.time()
    invalid = DB.ElementId.InvalidElementId
    per = {}
    view_ids = {}
    styles = {}
    style_cache = {}
    sketch_cat = int(DB.BuiltInCategory.OST_SketchLines)

    def bump(vid, key):
        c = per.setdefault(vid, {})
        c[key] = c.get(key, 0) + 1

    cats = (("lines", DB.BuiltInCategory.OST_Lines), ("detail_items", DB.BuiltInCategory.OST_DetailComponents),
            ("text_notes", DB.BuiltInCategory.OST_TextNotes), ("dimensions", DB.BuiltInCategory.OST_Dimensions))
    for key, bic in cats:
        for e in DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType():  # noqa: F821
            ov = e.OwnerViewId
            if ov is None or ov == invalid:
                continue  # model element, not 2D
            cat = e.Category
            if cat is None or eid(cat.Id) == sketch_cat:
                continue
            style = _style_name(e, style_cache) if key == "lines" else ""
            if not counts_as_2d(cat.Name, style):
                continue
            vid = eid(ov)
            view_ids[vid] = ov
            bump(vid, key)
            if key == "lines":
                s = styles.setdefault(vid, {})
                s[style or "(none)"] = s.get(style or "(none)", 0) + 1
            elif key == "detail_items" and isinstance(e, DB.FilledRegion):
                bump(vid, "filled_regions")
    sheet_of = {}
    for vp in collect(cls=DB.Viewport):
        try:
            sh = doc.GetElement(vp.SheetId)  # noqa: F821
            sheet_of[eid(vp.ViewId)] = sh.SheetNumber if sh is not None else ""
        except Exception:
            pass
    rows = []
    for vid, counts in per.items():
        v = doc.GetElement(view_ids[vid])  # noqa: F821
        name = safe_name(v) if v is not None else "(view %d)" % vid
        sheet = sheet_of.get(vid)
        if isinstance(v, DB.ViewSheet):
            sheet = v.SheetNumber  # 2D drawn on the sheet itself is part of the printed drawing too
        rows.append(view_2d_row(vid, name, counts, styles.get(vid), sheet))
    mark_near_duplicates(rows)
    ranked = rank_heavy_views(rows)
    if doc.IsWorkshared:  # noqa: F821
        for r in ranked:
            r["creator"] = _creator(view_ids[r["id"]])
    summary = summarize_heavy(rows, ranked)
    summary["seconds"] = round(time.time() - t0, 1)
    return ranked, summary


def check_views():
    views = [v for v in collect(cls=DB.View) if not v.IsTemplate]
    sheets = [v for v in views if isinstance(v, DB.ViewSheet)]
    placed = set()
    for s in sheets:
        for vid in s.GetAllPlacedViews():
            placed.add(eid(vid))
    # GetAllPlacedViews does not return schedules; those sit on sheets as ScheduleSheetInstance.
    for ssi in collect(cls=DB.ScheduleSheetInstance):
        placed.add(eid(ssi.ScheduleId))

    def internal(v):
        if isinstance(v, DB.ViewSchedule):
            return v.IsTitleblockRevisionSchedule or v.IsInternalKeynoteSchedule
        return False
    loose = [v for v in views
             if not isinstance(v, DB.ViewSheet) and v.CanBePrinted and not internal(v)
             and eid(v.Id) not in placed]
    return views, sheets, loose


def file_size_mb():
    path = doc.PathName  # noqa: F821
    if not path:
        return None, "Model is not saved yet."
    try:
        if System.IO.File.Exists(path):
            return round(System.IO.FileInfo(path).Length / 1048576.0, 1), path
    except Exception:
        pass
    return None, "File not on a local path (cloud or server model): " + path


if doc is None or doc.IsFamilyDocument:  # noqa: F821
    result = {"error": "no_project_document",
              "message": "Open a Revit project (.rvt), not a family, then run again."}
else:
    metrics = []
    w_total, w_rows = check_warnings()
    metrics.append(metric("warnings", "Revit warnings", w_total, 50, 500,
                          "Fix the biggest warning types first. Many warnings slow the model.",
                          w_rows[:TOP_WARNINGS]))

    l_types, l_insts, l_unloaded, l_unpinned, l_far, l_unplaced = check_links()
    metrics.append(metric("links_unloaded", "Revit links not loaded", len(l_unloaded), 0, 2,
                          "Reload or remove these links in Manage Links.",
                          [{"label": safe_name(t), "count": 1, "ids": [eid(t.Id)]} for t in l_unloaded]))
    metrics.append(metric("links_unpinned", "Revit links not pinned", len(l_unpinned), 0, 3,
                          "Pin links so nobody moves them by accident.",
                          [{"label": safe_name(i), "count": 1, "ids": [eid(i.Id)]} for i in l_unpinned]))
    metrics.append(metric("links_far", "Links far from the origin (shared coordinates hint)", len(l_far), 0, None,
                          "Heuristic only: origin more than 100 m away. Check shared coordinates with the link owner.",
                          [{"label": safe_name(i), "count": 1, "ids": [eid(i.Id)]} for i in l_far]))
    # Revit names a link instance "<file> : <id> : location <Not Shared>" when it is not placed by
    # shared coordinates. English Revit only; other languages show 0 here. Heuristic, like links_far.
    l_not_shared = [i for i in l_insts if "<Not Shared>" in safe_name(i)]
    metrics.append(metric("links_not_shared", "Links not on shared coordinates (hint)", len(l_not_shared), 0, None,
                          "Heuristic from the link name. Agree on shared coordinates with the link owners.",
                          [{"label": safe_name(i), "count": 1, "ids": [eid(i.Id)]} for i in l_not_shared]))
    metrics.append(metric("links_total", "Revit link instances", len(l_insts), None, None,
                          "%d link types, %d not placed." % (len(l_types), len(l_unplaced)),
                          [{"label": safe_name(t) + " (type not placed)", "count": 1, "ids": [eid(t.Id)]}
                           for t in l_unplaced]))

    ip_count, ip_rows = check_in_place()
    metrics.append(metric("in_place_families", "In-place families", ip_count, 10, 30,
                          "Turn repeated in-place families into loadable families.", ip_rows))

    r_total, r_unplaced, r_open, r_redundant = check_rooms(w_rows)
    metrics.append(metric("rooms_unplaced", "Rooms not placed", len(r_unplaced), 0, 20,
                          "Place these rooms or delete them (Room schedule).",
                          [{"label": "Not placed", "count": len(r_unplaced), "ids": r_unplaced[:MAX_IDS]}]))
    metrics.append(metric("rooms_not_enclosed", "Rooms not enclosed", len(r_open), 0, 10,
                          "Close the room boundaries or add room separation lines.",
                          [{"label": "Not enclosed", "count": len(r_open), "ids": r_open[:MAX_IDS]}]))
    metrics.append(metric("rooms_redundant", "Redundant rooms (two rooms in one space)", len(r_redundant), 0, 10,
                          "Delete the extra room in each space.",
                          [{"label": "Redundant", "count": len(r_redundant), "ids": r_redundant[:MAX_IDS]}]))

    dm_count, dm_rows = check_duplicate_marks()
    metrics.append(metric("duplicate_marks", "Elements with duplicate Mark", dm_count, 0, 50,
                          "Give each element in a category its own Mark.", dm_rows[:50]))

    cad_rows, cad_view_stats = read_cad_links()
    c_imp = {}
    for r in cad_rows:
        if r["kind"] == "imported":
            c_imp.setdefault(r["name"], []).append(r["id"])
    metrics.append(metric("cad_imported", "Imported CAD (not linked)", sum([len(v) for v in c_imp.values()]), 0, 5,
                          "Delete imported CAD, or replace it with a linked CAD file.",
                          [{"label": k, "count": len(v), "ids": v[:MAX_IDS]} for k, v in c_imp.items()]))
    c_flagged = len([r for r in cad_rows if r["flags"]])
    metrics.append(metric("cad_links", "CAD links and imports with problems", c_flagged, 0, 3,
                          "%d CAD instances in total. Pin them and move them to the CAD workset (am-safe-change). "
                          "Repath by hand: move the file to the shared document store, then link it again.%s"
                          % (len(cad_rows), " View counts are partial (time limit)." if cad_view_stats["partial"] else ""),
                          cad_link_details(cad_rows)))

    h_ranked, h_summary = read_heavy_2d()
    metrics.append(metric("heavy_2d_views", "Views with heavy 2D content", len(h_ranked), 0, 10,
                          "%d Critical (1,000+), %d High (500-999), %d Medium (250-499) of %d views with 2D. "
                          "%d of them are on sheets: a cleanup there changes a printed drawing. Ask before you delete."
                          % (h_summary["critical"], h_summary["high"], h_summary["medium"],
                             h_summary["views_with_2d"], h_summary["on_sheet"]),
                          [{"label": heavy_view_label(r), "count": r["total"], "ids": [r["id"]]}
                           for r in h_ranked[:MAX_ROWS]]))

    v_all, v_sheets, v_loose = check_views()
    metrics.append(metric("views_not_on_sheets", "Views not on sheets", len(v_loose), 150, 400,
                          "%d views and %d sheets in total. Delete views you do not need." % (len(v_all), len(v_sheets)),
                          [{"label": safe_name(v), "count": 1, "ids": [eid(v.Id)]} for v in v_loose[:MAX_IDS]]))

    size, size_note = file_size_mb()
    if size is not None:
        metrics.append(metric("file_size", "File size", size, 300, 600,
                              "Big files open slowly. Purge unused items and check imported CAD.", [], "MB"))
    else:
        metrics.append(metric("file_size", "File size", 0, None, None, size_note, [], "MB"))

    result = {
        "model": doc.Title,  # noqa: F821
        "revit_year": str(app.VersionNumber),  # noqa: F821
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "path": doc.PathName or "",  # noqa: F821
        "workshared": bool(doc.IsWorkshared),  # noqa: F821
        "metrics": metrics,
        "cad_links": cad_rows[:MAX_ROWS],
        "cad_view_count": cad_view_stats,
        "heavy_2d_views": h_ranked[:MAX_ROWS],
        "heavy_2d_summary": h_summary,
    }
    result = jsonable(result)
    bad = len([m for m in metrics if m["status"] == "bad"])
    warn = len([m for m in metrics if m["status"] == "warn"])
    print("am-model-health: %d metrics, %d bad, %d warn" % (len(metrics), bad, warn))
