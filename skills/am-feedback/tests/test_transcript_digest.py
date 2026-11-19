# -*- coding: utf-8 -*-
"""Tests for transcript_digest.py (issue #274). Fixture transcripts are built here."""

import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import transcript_digest as td  # noqa: E402

PY = os.path.join(SCRIPTS, "transcript_digest.py")
PLUGIN = "archi-magic:"  # joined at run time: skill files must not spell the namespaced name
SINCE = "2026-11-18T10:00:00.0000000Z"  # the 7-digit form that the nudge hook writes
BEFORE = "2026-11-18T09:00:00.000Z"
AFTER = "2026-11-18T10:30:00.000Z"
LATER = "2026-11-18T10:31:00.000Z"
TOOL_LIST = "Available tools: mcp__pyrevit__get_context, mcp__pyrevit__run_query, mcp__pyrevit__run_modify"


def rec(kind, content, ts=AFTER, **extra):
    r = {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}
    r.update(extra)
    return r


def tool_use(name, inp, tid="t1"):
    return rec("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}])


def tool_result(text, tid="t1", is_error=False, ts=LATER):
    return rec("user", [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}], ts=ts)


def write_jsonl(path, records, meta=None):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            # Compact JSON, as Claude Code writes it: "skill":"..." with no space.
            fh.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
    if meta is not None:
        with io.open(str(path)[: -len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    return str(path)


def make_projects(root):
    proj = root / "projects" / "proj-a"
    # 1. Tool list names pyRevit, but no pyRevit call: not selected.
    write_jsonl(proj / "toollist.jsonl", [
        {"type": "attachment", "timestamp": AFTER, "content": TOOL_LIST},
        rec("user", "please list the open issues"),
        tool_use("Bash", {"command": "gh issue list"}),
    ])
    # 2. Compact skill launch with a plugin prefix: selected.
    write_jsonl(proj / "skill.jsonl", [
        rec("user", "renumber the sheets", ts=BEFORE),
        tool_use("Skill", {"skill": PLUGIN + "am-safe-change"}),
        rec("user", "no, that is the wrong sheet 中文", ts=LATER),
    ])
    # 3. Only am-feedback itself: not archi-magic use.
    write_jsonl(proj / "feedback.jsonl", [
        rec("user", "<command-name>/" + PLUGIN + "am-feedback</command-name>"),
        tool_use("Skill", {"skill": PLUGIN + "am-feedback"}),
    ])
    # 4. Parent without archi-magic use; its subagent makes a failing pyRevit call: selected.
    write_jsonl(proj / "parent.jsonl", [rec("user", "fix the doors in a subagent")])
    write_jsonl(proj / "parent" / "subagents" / "agent-a1.jsonl", [
        rec("user", "Check the door marks.", isSidechain=True),
        tool_use("mcp__pyrevit__run_query", {"script": "print(1)"}, tid="q1"),
        tool_result("Exit code 1\n{\"error\":\"revit_busy\",\"message\":\"a dialog is open\"}", tid="q1", is_error=True),
    ], meta={"agentType": "general-purpose"})
    # 5. An earlier scout run: skipped, even though it is full of signals.
    write_jsonl(proj / "parent" / "subagents" / "agent-s1.jsonl", [
        rec("user", "since=2026-11-18T08:00:00Z", isSidechain=True),
        tool_use("mcp__pyrevit__get_context", {}, tid="s1"),
        tool_use("Skill", {"skill": PLUGIN + "am-model-health"}, tid="s2"),
    ], meta={"agentType": PLUGIN + "am-feedback-scout"})
    # 6. Old session: changed before `since`, never read.
    old = write_jsonl(proj / "old.jsonl", [tool_use("mcp__pyrevit__run_modify", {}, tid="o1")])
    t_old = td.parse_utc("2026-11-18T09:00:00Z").timestamp()
    os.utime(old, (t_old, t_old))
    return str(root / "projects")


def test_parse_utc_accepts_the_hook_format_and_offsets():
    assert td.parse_utc(SINCE).isoformat() == "2026-11-18T10:00:00+00:00"
    assert td.parse_utc("2026-11-18T12:00:00+02:00").isoformat() == "2026-11-18T10:00:00+00:00"
    assert td.parse_utc("2026-11-18").isoformat() == "2026-11-18T00:00:00+00:00"


def test_skill_name_accepts_any_prefix_and_slash():
    assert td.skill_name(PLUGIN + "am-safe-change") == "am-safe-change"
    assert td.skill_name("/am-corner-guard") == "am-corner-guard"
    assert td.skill_name("superpowers:brainstorming") is None


def test_selects_only_sessions_that_really_used_archi_magic(tmp_path):
    projects = make_projects(tmp_path)
    out = td.build_digest(projects, td.parse_utc(SINCE))
    first = out.splitlines()[0]
    assert first.startswith("DIGEST since 2026-11-18T10:00:00Z: 2 of 4 changed session(s) used archi-magic")
    assert "1 scout transcript(s) skipped" in first
    assert "2 without archi-magic use" in first
    assert "toollist.jsonl" not in out
    assert "feedback.jsonl" not in out
    assert "old.jsonl" not in out
    assert "agent-s1" not in out and "am-model-health" not in out


def test_compact_skill_launch_is_found_and_events_before_since_are_not_shown(tmp_path):
    out = td.build_digest(make_projects(tmp_path), td.parse_utc(SINCE))
    assert "use: pyRevit calls 0; skills am-safe-change" in out
    assert "tool Skill " + PLUGIN + "am-safe-change" in out
    assert "renumber the sheets" not in out  # before since
    assert "user [correction?] no, that is the wrong sheet" in out


def test_pyrevit_call_in_a_subagent_is_found_with_its_error(tmp_path):
    out = td.build_digest(make_projects(tmp_path), td.parse_utc(SINCE))
    assert "1 subagent(s)  use: pyRevit calls 1; skills none" in out
    assert "sub1: " in out and "agent-a1.jsonl" in out
    assert "sub1 tool mcp__pyrevit__run_query print(1)" in out
    assert 'sub1 ERROR mcp__pyrevit__run_query Exit code 1 | {"error":"revit_busy"' in out


def test_no_archi_magic_use_says_nothing_to_read(tmp_path):
    proj = tmp_path / "projects" / "p"
    write_jsonl(proj / "a.jsonl", [{"type": "attachment", "timestamp": AFTER, "content": TOOL_LIST},
                                   tool_use("Skill", {"skill": PLUGIN + "am-feedback"})])
    out = td.build_digest(str(tmp_path / "projects"), td.parse_utc(SINCE))
    assert out.startswith("DIGEST since 2026-11-18T10:00:00Z: no session used archi-magic")
    assert "Nothing to read." in out
    out_all = td.build_digest(str(tmp_path / "projects"), td.parse_utc(SINCE), include_all=True)
    assert "1 of 1 changed session(s) listed (--all)" in out_all


def test_long_lines_are_cut_and_limits_apply(tmp_path):
    proj = tmp_path / "projects" / "p"
    write_jsonl(proj / "a.jsonl", [tool_use("mcp__pyrevit__run_query", {"script": "x" * 5000}, tid="t%d" % i)
                                   for i in range(5)])
    out = td.build_digest(str(tmp_path / "projects"), td.parse_utc(SINCE), max_events=3, text_chars=40)
    lines = out.splitlines()
    assert max(len(line) for line in lines if "tool mcp__pyrevit" in line) < 120
    assert "... 2 more event(s) not shown" in out


def test_scout_without_meta_file_is_found_by_its_prompt(tmp_path):
    p = write_jsonl(tmp_path / "s" / "subagents" / "agent-x.jsonl",
                    [rec("user", "You are the am-feedback-scout. since=...", isSidechain=True)])
    assert td.is_scout_transcript(p)
    q = write_jsonl(tmp_path / "s" / "subagents" / "agent-y.jsonl",
                    [rec("user", "Check the door marks.", isSidechain=True)])
    assert not td.is_scout_transcript(q)


def test_cjk_text_under_isolated_mode_has_no_encoding_error(tmp_path):
    projects = make_projects(tmp_path)
    out_file = tmp_path / "digest.txt"
    # -I ignores PYTHONIOENCODING; without -I it would force ASCII. The script must work in both.
    for flags in (["-I"], []):
        env = dict(os.environ, PYTHONIOENCODING="ascii")
        res = subprocess.run([sys.executable] + flags + [PY, "--since", SINCE, "--projects", projects,
                                                        "--out", str(out_file)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
        assert "中文" in res.stdout.decode("utf-8")
        with io.open(str(out_file), "r", encoding="utf-8") as fh:
            assert "中文" in fh.read()


def test_bad_since_or_missing_folder_is_an_error(tmp_path, capsys):
    assert td.main(["--since", "yesterday", "--projects", str(tmp_path)]) == 2
    assert "DIGEST ERROR" in capsys.readouterr().out
    assert td.main(["--since", SINCE, "--projects", str(tmp_path / "missing")]) == 2


def test_default_since_is_two_hours(tmp_path, capsys):
    proj = tmp_path / "projects" / "p"
    write_jsonl(proj / "a.jsonl", [tool_use("mcp__pyrevit__get_context", {},)])
    assert td.main(["--projects", str(tmp_path / "projects")]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    since = td.parse_utc(first.split()[2].rstrip(":"))
    assert abs((time.time() - 7200) - since.timestamp()) < 120
