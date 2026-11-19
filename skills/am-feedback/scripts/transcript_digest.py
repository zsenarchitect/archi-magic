# -*- coding: utf-8 -*-
"""Transcript digest for am-feedback-scout and am-feedback (issue #274).

One JSONL line of a Claude Code transcript can hold a whole tool result, so a
Grep match prints a very long line. This script reads the transcripts that
changed after `since` and prints one short line for each event after `since`:
time, transcript, kind, tool name, an ERROR mark for a failed tool call, a
[correction?] mark for user text that looks like a correction, and a short text.

It selects only the sessions that really used archi-magic after `since`:
- a real pyRevit tool call (a tool_use block named mcp__pyrevit__*), not the
  tool list that Claude Code writes into every transcript;
- an am-* skill launch (Skill tool call or slash command), with or without a
  plugin prefix, but not am-feedback itself.
Subagent transcripts (<session>/subagents/*.jsonl) are read with their parent
session. Transcripts of earlier feedback scout runs are skipped.

Usage (call a real Python: "py -3" or the full path, not the Store alias):
    py -3 -I transcript_digest.py [--since <UTC time>] [--projects <dir>]
          [--max-sessions 10] [--max-events 400] [--text-chars 160] [--all] [--out <file>]

The first line starts with "DIGEST since". When no session used archi-magic,
it says "no session used archi-magic": there is nothing to read.
Output is UTF-8, also under -I (isolated mode ignores PYTHONIOENCODING).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import io
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

UTC = dt.timezone.utc
SCOUT_AGENT = "am-feedback-scout"
NOT_ARCHI_MAGIC_USE = {"am-feedback"}

TIME_RX = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?\s*$",
    re.IGNORECASE,
)
SKILL_RX = re.compile(r"^/?(?:[\w-]+:)?(am-[\w-]+)$")
ANY_COMMAND_RX = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>")
REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
TAG_RX = re.compile(r"</?[\w-]+>")
SPACE_RX = re.compile(r"\s+")
CORRECTION_RX = re.compile(
    r"^\s*(no|nope)\b|\b(wrong|stale|not right|not what|not correct|fix the|this is also|instead|undo|revert|you missed|didn't|did not)\b",
    re.IGNORECASE,
)
SIGNAL_RX = re.compile(
    r"revit_busy|pipe_error|ResultSerializationError|Traceback \(most recent call last\)|\"ok\"\s*:\s*false",
    re.IGNORECASE,
)
INPUT_KEYS = ("skill", "subagent_type", "description", "command", "file_path", "pattern", "query", "url", "path")


def parse_utc(text: str) -> dt.datetime:
    """Parse an ISO 8601 time. A time without a zone is UTC. 7 fraction digits are fine."""
    m = TIME_RX.match(str(text or ""))
    if not m:
        raise ValueError("not an ISO 8601 time: %r" % text)
    y, mo, d, h, mi, s, frac, zone = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    tz = UTC
    if zone and zone.upper() != "Z":
        z = zone.replace(":", "")
        offset = dt.timedelta(hours=int(z[1:3]), minutes=int(z[3:5]))
        tz = dt.timezone(-offset if z[0] == "-" else offset)
    when = dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), micro, tzinfo=tz)
    return when.astimezone(UTC)


def _clean(text: Any, limit: int) -> str:
    s = REMINDER_RX.sub(" ", str(text or ""))
    s = SPACE_RX.sub(" ", s).strip()
    if len(s) > limit:
        s = s[: max(limit - 3, 1)] + "..."
    return s


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _first_line(text: str) -> str:
    """First non-empty line. A bare 'Exit code N' line also gets the next line (the message)."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) > 1 and re.match(r"^\s*Exit code -?\d+\s*$", lines[0]):
        return lines[0].strip() + " | " + lines[1]
    return lines[0]


def _input_summary(inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in INPUT_KEYS:
        if inp.get(key):
            return str(inp[key])
    for value in inp.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


def skill_name(raw: Any) -> Optional[str]:
    """'<plugin>:am-safe-change', 'am-safe-change' or '/am-safe-change' -> 'am-safe-change'."""
    m = SKILL_RX.match(str(raw or "").strip())
    return m.group(1) if m else None


def is_scout_transcript(path: str) -> bool:
    """True for the transcript of a feedback scout run (its .meta.json names the scout agent)."""
    meta = path[: -len(".jsonl")] + ".meta.json"
    if os.path.isfile(meta):
        try:
            with io.open(meta, "r", encoding="utf-8-sig") as fh:
                agent = str(json.load(fh).get("agentType") or "")
            return agent == SCOUT_AGENT or agent.endswith(":" + SCOUT_AGENT)
        except (ValueError, OSError, AttributeError):
            pass
    # No readable meta file: look at the first prompt of a subagent transcript.
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "user" and rec.get("isSidechain"):
                    text = _block_text((rec.get("message") or {}).get("content"))
                    return SCOUT_AGENT in text[:2000]
    except OSError:
        pass
    return False


def read_events(path: str, since: dt.datetime, text_chars: int = 160) -> Dict[str, Any]:
    """Read one transcript. Return its events after `since` and its archi-magic use."""
    events: List[Dict[str, Any]] = []
    tool_names: Dict[str, str] = {}
    pyrevit_calls = 0
    skills = set()
    bad_lines = 0
    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                bad_lines += 1
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind not in ("user", "assistant"):
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            # Remember every tool id, also before `since`, to name a later result.
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_names[str(b.get("id"))] = str(b.get("name") or "?")
            try:
                when = parse_utc(rec.get("timestamp"))
            except ValueError:
                continue
            if when <= since:
                continue

            def add(ev_kind, text="", tool="", error=False, correction=False):
                events.append({"time": when, "kind": ev_kind, "tool": tool, "error": error,
                               "correction": correction, "text": _clean(text, text_chars)})

            if kind == "user":
                if rec.get("isMeta"):
                    continue
                if isinstance(content, str):
                    cmd = ANY_COMMAND_RX.search(content)
                    if cmd:
                        name = skill_name(cmd.group(1))
                        if name and name not in NOT_ARCHI_MAGIC_USE:
                            skills.add(name)
                        add("command", cmd.group(1))
                        continue
                    text = TAG_RX.sub(" ", REMINDER_RX.sub(" ", content))
                    if text.strip():
                        add("user", text, correction=bool(CORRECTION_RX.search(text)))
                    continue
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        name = tool_names.get(str(b.get("tool_use_id")), "?")
                        body = _block_text(b.get("content"))
                        if b.get("is_error"):
                            add("result", _first_line(body), tool=name, error=True)
                        else:
                            sig = SIGNAL_RX.search(body)
                            if sig:
                                add("signal", sig.group(0) + ": " + _first_line(body), tool=name)
                    elif b.get("type") == "text":
                        text = REMINDER_RX.sub(" ", str(b.get("text") or ""))
                        if text.strip():
                            add("user", text, correction=bool(CORRECTION_RX.search(text)))
            else:
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        name = str(b.get("name") or "?")
                        inp = b.get("input")
                        if name.startswith("mcp__pyrevit__"):
                            pyrevit_calls += 1
                        if name == "Skill" and isinstance(inp, dict):
                            sk = skill_name(inp.get("skill"))
                            if sk and sk not in NOT_ARCHI_MAGIC_USE:
                                skills.add(sk)
                        add("tool", _input_summary(inp), tool=name)
                    elif b.get("type") == "text" and str(b.get("text") or "").strip():
                        add("said", b.get("text"))
    return {"events": events, "pyrevit_calls": pyrevit_calls, "skills": skills, "bad_lines": bad_lines}


def find_sessions(projects: str, since: dt.datetime) -> Dict[str, Any]:
    """Group the transcripts changed after `since` by session (main file plus subagents)."""
    since_ts = since.timestamp()
    groups: Dict[str, Dict[str, Any]] = {}
    scouts = 0
    mains = glob.glob(os.path.join(projects, "*", "*.jsonl"))
    subs = glob.glob(os.path.join(projects, "*", "*", "subagents", "*.jsonl"))
    for path in mains + subs:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime <= since_ts:
            continue
        is_sub = os.path.basename(os.path.dirname(path)) == "subagents"
        if is_sub:
            session_dir = os.path.dirname(os.path.dirname(path))
            key = session_dir
            if is_scout_transcript(path):
                scouts += 1
                continue
        else:
            key = path[: -len(".jsonl")]
        g = groups.setdefault(key, {"id": os.path.basename(key), "main": None, "subagents": [], "mtime": 0.0})
        if is_sub:
            g["subagents"].append(path)
        else:
            g["main"] = path
        g["mtime"] = max(g["mtime"], mtime)
    # A subagent whose main file did not change still belongs to its session.
    for key, g in groups.items():
        if g["main"] is None and os.path.isfile(key + ".jsonl"):
            g["main"] = key + ".jsonl"
        g["subagents"].sort()
    return {"sessions": sorted(groups.values(), key=lambda s: s["mtime"], reverse=True), "scouts_skipped": scouts}


def build_digest(projects: str, since: dt.datetime, max_sessions: int = 10, max_events: int = 400,
                 text_chars: int = 160, include_all: bool = False) -> str:
    found = find_sessions(projects, since)
    since_txt = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    selected = []
    without = 0
    for s in found["sessions"]:
        files = ([("main", s["main"])] if s["main"] else []) + [
            ("sub%d" % (i + 1), p) for i, p in enumerate(s["subagents"])]
        reads = []
        for tag, path in files:
            try:
                reads.append((tag, path, read_events(path, since, text_chars)))
            except OSError:
                reads.append((tag, path, {"events": [], "pyrevit_calls": 0, "skills": set(), "bad_lines": -1}))
        calls = sum(r["pyrevit_calls"] for _t, _p, r in reads)
        skills = sorted(set().union(*[r["skills"] for _t, _p, r in reads])) if reads else []
        if calls or skills or include_all:
            selected.append((s, reads, calls, skills))
        else:
            without += 1
    total = len(found["sessions"])
    lines = []
    if not selected:
        lines.append("DIGEST since %s: no session used archi-magic (%d changed session(s), %d scout transcript(s) skipped). "
                     "Nothing to read." % (since_txt, total, found["scouts_skipped"]))
        return "\n".join(lines) + "\n"
    shown = selected[:max_sessions]
    lines.append("DIGEST since %s: %d of %d changed session(s) %s; showing %d; %d scout transcript(s) skipped; "
                 "%d without archi-magic use" % (since_txt, len(selected), total,
                                                 "listed (--all)" if include_all else "used archi-magic",
                                                 len(shown), found["scouts_skipped"], without))
    lines.append("Columns: time (UTC), transcript, kind, tool, text. ERROR = failed tool call; "
                 "[correction?] = user text that may correct the agent.")
    for n, (s, reads, calls, skills) in enumerate(shown, 1):
        use = "pyRevit calls %d; skills %s" % (calls, ", ".join(skills) if skills else "none")
        lines.append("")
        lines.append("== session %d/%d  id %s  %d subagent(s)  use: %s" % (n, len(shown), s["id"][:8], len(s["subagents"]), use))
        merged = []
        for tag, path, r in reads:
            note = ""
            if r["bad_lines"] > 0:
                note = "  (%d unreadable line(s))" % r["bad_lines"]
            elif r["bad_lines"] < 0:
                note = "  (could not read)"
            lines.append("   %s: %s%s" % (tag, path, note))
            merged.extend((ev["time"], tag, ev) for ev in r["events"])
        merged.sort(key=lambda item: item[0])
        for when, tag, ev in merged[:max_events]:
            parts = [when.strftime("%m-%d %H:%M:%S"), tag, "ERROR" if ev["error"] else ev["kind"]]
            if ev["tool"]:
                parts.append(ev["tool"])
            if ev["correction"]:
                parts.append("[correction?]")
            if ev["text"]:
                parts.append(ev["text"])
            lines.append(" ".join(parts))
        if len(merged) > max_events:
            lines.append("... %d more event(s) not shown (raise --max-events)" % (len(merged) - max_events))
    if len(selected) > len(shown):
        lines.append("")
        lines.append("... %d older session(s) not shown (raise --max-sessions)" % (len(selected) - len(shown)))
    return "\n".join(lines) + "\n"


def default_projects_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="One line per transcript event after a UTC time.")
    ap.add_argument("--since", help="UTC time (ISO 8601). Default: 2 hours ago.")
    ap.add_argument("--projects", default=default_projects_dir(), help="Claude Code projects folder")
    ap.add_argument("--max-sessions", type=int, default=10)
    ap.add_argument("--max-events", type=int, default=400, help="per session")
    ap.add_argument("--text-chars", type=int, default=160)
    ap.add_argument("--all", action="store_true", help="list every changed session, not only archi-magic ones")
    ap.add_argument("--out", help="also write the digest to this UTF-8 file")
    args = ap.parse_args(argv)
    try:
        since = parse_utc(args.since) if args.since else dt.datetime.now(UTC) - dt.timedelta(hours=2)
    except ValueError as exc:
        sys.stdout.write("DIGEST ERROR: %s\n" % exc)
        return 2
    if not os.path.isdir(args.projects):
        sys.stdout.write("DIGEST ERROR: no projects folder at %s\n" % args.projects)
        return 2
    text = build_digest(args.projects, since, args.max_sessions, args.max_events, args.text_chars, args.all)
    sys.stdout.write(text)
    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
