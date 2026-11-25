#! python 3
# -*- coding: utf-8 -*-
"""am-rhino-setup: template for one job on the RhinoCode fallback path (issue #305).

RhinoCode does not send print() output back to the shell. So this template writes two files:
  <OUT_BASE>.log   "started", then "done", or "failed" and the traceback
  <OUT_BASE>.json  the value that job(doc) returns, as JSON (written before "done")

Use:
  1. Copy this file to <OUT_BASE>.py. Use a folder path with no spaces.
  2. Set OUT_BASE to that full path without the extension.
  3. Put the work in job(). Keep it read-only unless the user approved a change.
  4. Run: RhinoCode.exe --rhino <id> command "_-RunPythonScript <OUT_BASE>.py"
  5. Poll <OUT_BASE>.log for a "done" or "failed" line (loop in am-rhino-setup SKILL.md).

The "#! python 3" line selects CPython 3. The code also runs in IronPython 2 (no f-strings).
The pure helpers have no Rhino import, so tests/test_rhinocode_job.py loads this file with plain
Python. The job runs only when Rhino can be imported.
"""
import io
import json
import os
import time
import traceback

OUT_BASE = r"C:\temp\am-rhino-job"


def job(doc):
    """Put the work here. Return a value that JSON can store. This example only reads."""
    return {"document": doc.Name or "(unsaved)", "objects": doc.Objects.Count}


def _text(value):
    # IronPython 2: json.dumps returns a byte string; io.open in text mode needs unicode.
    if isinstance(value, bytes) and not isinstance(value, type(u"")):
        return value.decode("utf-8")
    return value


def append_line(path, line):
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(_text(line) + u"\n")


def write_json(path, value):
    """Write to a temp file, then rename, so a reader never sees half a file."""
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(_text(json.dumps(value, indent=2, sort_keys=True, default=str)))
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def run_job(fn, out_base, doc):
    """Run fn(doc). Write the log and the result files. Return True when fn finished."""
    log_path = out_base + ".log"
    result_path = out_base + ".json"
    for p in (log_path, result_path):
        if os.path.exists(p):
            os.remove(p)
    append_line(log_path, u"started " + _text(time.strftime("%Y-%m-%d %H:%M:%S")))
    try:
        result = fn(doc)
        write_json(result_path, result)
        append_line(log_path, u"done")
        return True
    except Exception:
        append_line(log_path, u"failed")
        append_line(log_path, _text(traceback.format_exc()))
        return False


try:
    import Rhino  # noqa: F401  (present only inside Rhino)
except ImportError:
    Rhino = None

if Rhino is not None:
    run_job(job, OUT_BASE, Rhino.RhinoDoc.ActiveDoc)
