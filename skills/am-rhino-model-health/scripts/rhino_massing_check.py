# -*- coding: utf-8 -*-
"""am-rhino-model-health: READ-ONLY check of massing solids after an edit (issue #290).

Run with the rhino-mcp tool run_python (CPython 3 inside Rhino). The tool injects __rhino_doc__.
The result is ONE output line that starts with RESULT_PREFIX. It changes nothing.

For the objects on the given layers (and their sublayers, hidden objects included) it reports:
  - each polysurface or extrusion that is not a closed solid (NOT_SOLID)
  - faces that point down and sit above the bottom of their object (DOWNWARD_FACES): overhangs,
    or slivers left by blocks that hang past a facade
  - the total volume, and the change against the base option layers when base_layers is given

Inputs: put a line `INPUTS = {...}` before this script.
  layers          list of layer full paths of the new option (required)
  base_layers     list of layer full paths of the base option (optional, for the volume change)
  sliver_area     a downward face smaller than this (document units squared) is a SLIVER_FACES
                  error (default 0 = off)
  downward_limit  a face points down when its unit normal Z is below this (default -0.7)
  max_items       cap on listed ids per finding (default 50)
  base_option     leaf layer name of the base option, for example "opt2.1" (optional). The result
                  then gives next_option_name: the next free subnumber ("opt2.2"), checked against
                  every layer name in the document, hidden layers included.

The pure functions at the top have no Rhino imports, so tests/test_rhino_massing_check.py loads
this file with plain Python. The Rhino part runs only when __rhino_doc__ exists.
"""
import json
import re

INPUTS = globals().get("INPUTS") or {}
RESULT_PREFIX = "AM_RESULT "

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def layer_selected(path, wanted):
    """True when path is a wanted layer or one of its sublayers. An empty filter selects nothing."""
    return any(path == w or path.startswith(w + "::") for w in (wanted or []))


def next_option_name(base, existing):
    """Next free version subnumber for a design option name (issue #290).
    opt1 -> opt1.1, opt2.1 -> opt2.2. Never returns a name in existing (case-insensitive).
    base and existing are leaf layer names, for example "opt2.1"."""
    m = re.match(r"^(.*?\d+)(?:\.(\d+))?$", base.strip())
    if not m:
        stem, start = base.strip() + ".", 1
    else:
        stem, start = m.group(1) + ".", int(m.group(2) or 0) + 1
    taken = set(e.strip().lower() for e in existing)
    used = [int(x.group(1)) for x in (re.match("^" + re.escape(stem.lower()) + r"(\d+)$", e) for e in taken) if x]
    n = max([start] + [u + 1 for u in used])
    while (stem + str(n)).lower() in taken:
        n += 1
    return stem + str(n)


def downward_faces(faces, bottom_z, tol, limit=-0.7):
    """faces: list of {"nz": unit normal Z (outward), "z": centroid Z, "area": float}.
    A face counts when it points down (nz < limit) and is above the object's bottom (z > bottom + tol).
    The bottom face of a mass points down too, but it sits at bottom_z, so it does not count."""
    return [f for f in faces if f["nz"] < limit and f["z"] > bottom_z + tol]


def volume_change(new_volume, base_volume):
    """Volume of the new option against the base option. percent is None when there is no base."""
    out = {"new": new_volume, "base": base_volume, "delta": None, "percent": None}
    if base_volume is None:
        return out
    out["delta"] = new_volume - base_volume
    if base_volume:
        out["percent"] = round(100.0 * (new_volume - base_volume) / base_volume, 2)
    return out


def build_findings(objects, sliver_area, max_items):
    """objects: list of {"id", "layer", "solid": bool or None, "downward": [faces]}.
    solid None means the object type is not checked (for example a mesh)."""
    not_solid = [o["id"] for o in objects if o["solid"] is False]
    down = [o for o in objects if o["downward"]]
    slivers = [o for o in down if sliver_area and any(f["area"] < sliver_area for f in o["downward"])]
    skipped = [o["id"] for o in objects if o["solid"] is None]
    findings = []
    if not_solid:
        findings.append({"code": "NOT_SOLID", "severity": "error", "count": len(not_solid), "ids": not_solid[:max_items],
                         "message": "Not closed solids. Booleans and volumes fail on these. Check naked edges (ShowEdges)."})
    if slivers:
        findings.append({"code": "SLIVER_FACES", "severity": "error", "count": len(slivers),
                         "ids": [o["id"] for o in slivers][:max_items],
                         "message": "Small downward faces above the ground. Usually a block hangs past the facade. Clip the footprint to the host solid."})
    if down:
        findings.append({"code": "DOWNWARD_FACES", "severity": "warn", "count": len(down),
                         "ids": [o["id"] for o in down][:max_items],
                         "areas": [round(sum(f["area"] for f in o["downward"]), 3) for o in down][:max_items],
                         "message": "Faces that point down above the bottom of the mass: overhangs or slivers. Confirm each one is intended."})
    if skipped:
        findings.append({"code": "NOT_CHECKED", "severity": "info", "count": len(skipped), "ids": skipped[:max_items],
                         "message": "Objects that are not polysurfaces or extrusions (for example meshes). Not checked."})
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))


def main(doc):
    import Rhino
    D = Rhino.DocObjects
    G = Rhino.Geometry
    wanted = list(INPUTS.get("layers") or [])
    base = list(INPUTS.get("base_layers") or [])
    base_option = INPUTS.get("base_option") or ""
    leaf_names = [l.Name for l in doc.Layers if not l.IsDeleted]
    next_name = next_option_name(base_option, leaf_names) if base_option else None
    if not wanted:
        if next_name:
            return {"status": "ok", "document": doc.Name or "(unsaved)", "next_option_name": next_name}
        return {"status": "error", "error": "Give INPUTS = {\"layers\": [<layer full paths of the new option>]}."}
    sliver_area = float(INPUTS.get("sliver_area") or 0)
    limit = float(INPUTS.get("downward_limit") or -0.7)
    max_items = int(INPUTS.get("max_items") or 50)
    tol = doc.ModelAbsoluteTolerance

    # doc.Objects alone skips objects on hidden layers. Old options usually sit on hidden layers.
    settings = D.ObjectEnumeratorSettings()
    settings.HiddenObjects = True
    settings.LockedObjects = True

    def as_brep(g):
        if isinstance(g, G.Brep):
            return g
        if isinstance(g, G.Extrusion):
            return g.ToBrep(True)
        return None

    def face_rows(brep):
        rows = []
        flip = brep.SolidOrientation == G.BrepSolidOrientation.Inward
        for face in brep.Faces:
            single = face.DuplicateFace(False)
            amp = G.AreaMassProperties.Compute(single) if single is not None else None
            if amp is None:
                continue
            c = amp.Centroid
            ok, u, v = face.ClosestPoint(c)
            if not ok:
                continue
            n = face.NormalAt(u, v)
            if n.Length == 0:
                continue
            nz = n.Z / n.Length
            if face.OrientationIsReversed:
                nz = -nz
            if flip:
                nz = -nz
            rows.append({"nz": nz, "z": c.Z, "area": amp.Area})
        return rows

    objects, new_volume, base_volume = [], 0.0, 0.0
    by_layer = {}
    for o in doc.Objects.GetObjectList(settings):
        layer = doc.Layers[o.Attributes.LayerIndex].FullPath
        in_new = layer_selected(layer, wanted)
        in_base = layer_selected(layer, base)
        if not (in_new or in_base):
            continue
        brep = as_brep(o.Geometry)
        solid = None if brep is None else bool(brep.IsSolid)
        vol = 0.0
        if solid:
            vmp = G.VolumeMassProperties.Compute(brep)
            vol = abs(vmp.Volume) if vmp is not None else 0.0
        if in_base:
            base_volume += vol
        if not in_new:
            continue
        new_volume += vol
        by_layer[layer] = by_layer.get(layer, 0.0) + vol
        down = []
        if brep is not None:
            bottom = brep.GetBoundingBox(True).Min.Z
            down = downward_faces(face_rows(brep), bottom, tol, limit)
        objects.append({"id": str(o.Id), "layer": layer, "solid": solid, "downward": down})

    return {
        "status": "ok",
        "document": doc.Name or "(unsaved)",
        "units": str(doc.ModelUnitSystem), "absolute_tolerance": tol,
        "object_count": len(objects),
        "next_option_name": next_name,
        "volume": volume_change(new_volume, base_volume if base else None),
        "volume_by_layer": dict((k, round(v, 3)) for k, v in by_layer.items()),
        "findings": build_findings(objects, sliver_area, max_items),
    }


if "__rhino_doc__" in globals():
    try:
        print(RESULT_PREFIX + json.dumps(main(__rhino_doc__)))
    except Exception as ex:  # report, never crash silently
        import traceback
        print(RESULT_PREFIX + json.dumps({"status": "error", "error": "%s: %s" % (type(ex).__name__, ex),
                          "traceback": traceback.format_exc()}))
