# -*- coding: utf-8 -*-
"""am-rhino-model-health: READ-ONLY check of material texture paths (issue #292).

Run with the rhino-mcp tool run_python (CPython 3 inside Rhino). The tool injects __rhino_doc__.
The result is ONE output line that starts with RESULT_PREFIX. It changes nothing.

Use it after a texture repath and save. With "path", it reads the SAVED file from disk
(File3dm.Read), not the open document, so it shows what Rhino finds on the next open:
  - texture count and unique image files
  - texture paths outside the new folder
  - texture files that do not exist
  - image names that come from two different folders (they clash when copied into one folder)
  - embedded file count (0 after "-_SaveAs SaveTextures=No"; None when this Rhino cannot tell)

Inputs: put a line `INPUTS = {...}` before this script.
  path        full path of the saved .3dm to check (default: the open document's materials)
  folder      the texture folder that every path must start with (optional)
  max_items   cap on listed paths per list (default 50)

Limit: it reads the texture slots of each material (Material.GetTextures). It does not walk
render-content-only textures. The pure functions have no Rhino imports, so
tests/test_rhino_texture_check.py loads this file with plain Python.
"""
import json
import ntpath
import os

INPUTS = globals().get("INPUTS") or {}
RESULT_PREFIX = "AM_RESULT "


def norm(path):
    """Windows path compare: same case, same separators, no trailing separator."""
    return ntpath.normcase(ntpath.normpath(path or "")).rstrip("\\")


def in_folder(path, folder):
    f = norm(folder)
    return bool(f) and norm(path).startswith(f + "\\")


def name_clashes(paths):
    """Image names (case-insensitive) that come from more than one folder. Sorted."""
    dirs = {}
    for p in paths:
        if not p:
            continue
        dirs.setdefault(ntpath.basename(norm(p)), set()).add(ntpath.dirname(norm(p)))
    return sorted(n for n, d in dirs.items() if len(d) > 1)


def summarize_textures(entries, folder, exists, embedded_count, max_items=50):
    """entries: list of {"material": name, "path": texture file path}.
    exists: function(path) -> bool. embedded_count: int or None."""
    paths = [e["path"] for e in entries if e.get("path")]
    unique = sorted(set(paths), key=norm)
    outside = [p for p in unique if folder and not in_folder(p, folder)]
    missing = [p for p in unique if not exists(p)]
    clashes = name_clashes(unique)
    problems = []
    if outside:
        problems.append("%d texture file(s) outside the folder" % len(outside))
    if missing:
        problems.append("%d texture file(s) missing" % len(missing))
    if embedded_count:
        problems.append("%d embedded file(s): Rhino unpacks them on open and repoints the textures" % embedded_count)
    return {
        "texture_count": len(paths),
        "unique_files": len(unique),
        "folder": folder or None,
        "outside_folder": {"count": len(outside), "paths": outside[:max_items]},
        "missing": {"count": len(missing), "paths": missing[:max_items]},
        "name_clashes": {"count": len(clashes), "names": clashes[:max_items]},
        "embedded_files": embedded_count,
        "ok": not problems,
        "problems": problems,
    }


def material_textures(material):
    """File names of a material's texture slots. Works with Material from a doc or a File3dm."""
    texs = []
    get_all = getattr(material, "GetTextures", None)
    if get_all is not None:
        texs = list(get_all() or [])
    else:
        for name in ("GetBitmapTexture", "GetBumpTexture", "GetEnvironmentTexture", "GetTransparencyTexture"):
            fn = getattr(material, name, None)
            t = fn() if fn is not None else None
            if t is not None:
                texs.append(t)
    return [t.FileName for t in texs if t is not None and t.FileName]


def embedded_file_count(f3dm):
    table = getattr(f3dm, "EmbeddedFiles", None)
    if table is None:
        return None
    count = getattr(table, "Count", None)
    if count is not None:
        return int(count)
    try:
        return len(list(table))
    except TypeError:
        return None


def main(doc):
    path = INPUTS.get("path") or ""
    folder = INPUTS.get("folder") or ""
    max_items = int(INPUTS.get("max_items") or 50)
    entries, embedded, source = [], None, "open document"
    if path:
        import Rhino
        if not os.path.exists(path):
            return {"status": "error", "error": "File not found: %s" % path}
        f3dm = Rhino.FileIO.File3dm.Read(path)
        if f3dm is None:
            return {"status": "error", "error": "File3dm.Read returned None. Make the file local (not cloud-only) and try once more."}
        try:
            for m in f3dm.Materials:
                for p in material_textures(m):
                    entries.append({"material": m.Name, "path": p})
            embedded = embedded_file_count(f3dm)
        finally:
            f3dm.Dispose()
        source = path
    else:
        for m in doc.Materials:
            if m.IsDeleted:
                continue
            for p in material_textures(m):
                entries.append({"material": m.Name, "path": p})
    out = summarize_textures(entries, folder, os.path.exists, embedded, max_items)
    out["status"] = "ok"
    out["source"] = source
    return out


if "__rhino_doc__" in globals():
    try:
        print(RESULT_PREFIX + json.dumps(main(__rhino_doc__)))
    except Exception as ex:  # report, never crash silently
        import traceback
        print(RESULT_PREFIX + json.dumps({"status": "error", "error": "%s: %s" % (type(ex).__name__, ex),
                          "traceback": traceback.format_exc()}))
