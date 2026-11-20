# -*- coding: utf-8 -*-
"""Worksharing ownership steps for every am-safe-change recipe (issue #97, CPython 3).

Write the inputs for read_ownership.py (target ids from the preview steps):
    python ownership.py inputs --preview preview.json [--ids 123,456] --out own-inputs.json
After the run, add the dry run's categories, so the read also finds borrowed dependents:
    python ownership.py inputs --preview preview.json --dryrun dryrun.json --out own-after-inputs.json
It also prints the `inputs` object for run_query, with absolute paths:
    {"inputs_path": "C:/work/own-inputs.json", "out_path": "C:/work/own.json"}
read_ownership.py then writes the full read to out_path itself (issue #278).
The default out_path drops "-inputs" from the --out name. --result sets another one.

Check the read before the dry run. Adds the ownership line and blocking warnings to the preview:
    python ownership.py check --read own.json --preview preview.json [--out preview.json]

Report what the current user borrows after a kept or discarded run:
    python ownership.py borrowed --read own.json

The --read and --dryrun files may be any of these (issue #278):
- the bare `result` object, or the file that read_ownership.py wrote to out_path;
- the whole run JSON that `pyrevit agent run` prints, or the MCP envelope
  {"status", "run_id", "result"}, also when "result" is a JSON string;
- a Claude Code tool-results file (the saved tool output when it was too large
  for the chat): that envelope, or a list of {"type": "text", "text": ...} blocks;
- the short file-mode summary of read_ownership.py: the command reads the file
  at its "out_path" and checks that the element count matches.
Exit codes: 0 ok, 1 blocked (owned by others or updated in central), 2 bad input.
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import ownership as ow  # noqa: E402
import side_effects as se  # noqa: E402


def load_json(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def decode(data):
    """Plain JSON from a tool-results file or a run envelope.

    Decodes text blocks and JSON strings, and a "result" that is a JSON
    string. Leaves the envelope dict itself in place (unwrap() removes it).
    """
    for _ in range(8):
        if isinstance(data, str):
            data = json.loads(data)
        elif isinstance(data, list) and data and all(
                isinstance(b, dict) and "text" in b for b in data):
            data = json.loads("".join(b.get("text") or "" for b in data
                                      if b.get("type", "text") == "text"))
        elif (isinstance(data, dict) and isinstance(data.get("content"), list)
              and "result" not in data and "elements" not in data):
            data = data["content"]
        elif isinstance(data, dict) and isinstance(data.get("result"), str):
            data = dict(data)
            data["result"] = json.loads(data["result"])
        else:
            return data
    raise ValueError("The file is nested too deeply to decode.")


def unwrap_read(data):
    """The read_ownership.py result dict, or None for a file-mode summary."""
    data = decode(data)
    if isinstance(data, dict) and "result" in data and "elements" not in data             and "workshared" not in data:
        envelope = data
        data = decode(data["result"])
        if data is None:
            raise ValueError("The ownership read has no result. status: %s, error: %s."
                             % (envelope.get("status"), envelope.get("error")))
    if not isinstance(data, dict):
        raise ValueError("The file does not hold a read_ownership.py result.")
    return data


def load_read(path, follow=True):
    """The full read_ownership.py result from any accepted --read file."""
    data = unwrap_read(load_json(path))
    if "elements" in data:
        return data
    out_path = data.get("out_path")
    if follow and out_path and data.get("written"):
        full = load_read(out_path, follow=False)
        want = (data.get("counts") or {}).get("elements")
        got = len(full.get("elements") or [])
        if want is not None and int(want) != got:
            raise ValueError("%s has %d elements, but the run summary says %s. "
                             "Run read_ownership.py again." % (out_path, got, want))
        return full
    if data.get("status") or data.get("error"):
        raise ValueError("The ownership read failed. status: %s, error: %s."
                         % (data.get("status"), data.get("error")))
    raise ValueError("The file has no \"elements\" list. It is not a full "
                     "read_ownership.py result.")


def result_path(out, result=None):
    """Absolute path, with forward slashes, for read_ownership.py to write to."""
    if not result:
        folder, name = os.path.split(os.path.abspath(out))
        stem = name[:-5] if name.lower().endswith(".json") else name
        if stem.endswith("-inputs"):
            stem = stem[:-len("-inputs")]
        else:
            stem += "-read"
        result = os.path.join(folder, stem + ".json")
    return os.path.abspath(result).replace("\\", "/")


def write_json(path, data):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=1, ensure_ascii=False))


def id_list(text):
    return [int(x) for x in (text or "").split(",") if x.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    i = sub.add_parser("inputs")
    i.add_argument("--preview", required=True)
    i.add_argument("--ids", default="")
    i.add_argument("--dryrun")
    i.add_argument("--max-elements", type=int, default=5000)
    i.add_argument("--out", required=True)
    i.add_argument("--result", help="where read_ownership.py writes the full read")
    c = sub.add_parser("check")
    c.add_argument("--read", required=True)
    c.add_argument("--preview")
    c.add_argument("--out")
    b = sub.add_parser("borrowed")
    b.add_argument("--read", required=True)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "inputs":
            ids = ow.target_ids(load_json(a.preview), id_list(a.ids))
            if not ids:
                print("No target ids in the preview. Pass them with --ids.")
                return 2
            data = {"ids": ids, "max_elements": a.max_elements}
            if a.dryrun:
                dry = se.unwrap_dryrun(decode(load_json(a.dryrun)))
                data["categories"] = sorted((dry.get("modified_by_category") or {}).keys())
            write_json(a.out, data)
            print("Wrote %s: %d target id(s), %d categor(ies) to scan."
                  % (a.out, len(ids), len(data.get("categories") or [])))
            call = {"inputs_path": os.path.abspath(a.out).replace("\\", "/"),
                    "out_path": result_path(a.out, a.result)}
            print("run_query inputs for read_ownership.py:")
            print(json.dumps(call))
            return 0
        if a.cmd == "check":
            summary = ow.check(load_read(a.read))
            print(ow.text_table(summary))
            if a.preview:
                out = a.out or a.preview
                write_json(out, ow.annotate(load_json(a.preview), summary))
                print("Wrote %s." % out)
            return 1 if summary["blocked"] else 0
        if a.cmd == "borrowed":
            print(ow.borrowed(load_read(a.read))["line"])
            return 0
    except (ValueError, KeyError, TypeError, OSError) as e:
        print("Bad input: %s" % e)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
