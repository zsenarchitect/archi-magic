# -*- coding: utf-8 -*-
"""Invented sample data and a fake Revit FormattedText for the am-issue-board tests."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

# Revit format: "\r" breaks, no "- " on child lines, a trailing break.
SAMPLE = "\r".join([
    "AUDIT - 2026-01-05 (by @user1)",
    "Issues use fixed numbers; next new issue is #5. Severity scale: High 500+. Never renumber.",
    "",
    "#1 [IN PROGRESS] Link placed far from origin - @user2",
    "Finding: sample.dwg (id1001) is 3,000 ft away from the other links.",
    "To do: relink by shared coordinates, or remove.",
    "2026-01-05 @user1: moved to the links workset; pinned.",
    "Related: #3. Path: see #3.",
    "",
    "#2 [OPEN] Heavy 2D in drafting views, sketch set - @user3 @user4",
    "Severity: High",
    "Finding: 2 views have 500+ 2D elements:",
    "View A (id2001): 812 - 700 lines",
    "View B (id2002, on sheet X-1): 640 - 600 lines",
    "To do: delete or simplify.",
    "",
    "#3 [OPEN] Links on personal folders - @TBD",
    "Finding: 2 links point to personal folders.",
    "Paths:",
    "@user2 - id1001 sample.dwg",
    "To do: move the files to the shared folder.",
    "Related: #1, #4",
    "",
    "#4 [DONE] Survey point check - @ALL",
    "To do: none.",
    "2026-01-06 @user1: checked; closed.",
    "Related: #3",
]) + "\r"


class FakeRange(object):
    def __init__(self, start, length):
        self.Start = start
        self.Length = length


class FakeFormattedText(object):
    """Per-character list/indent/bold/underline, with Revit's range rules:
    a range must start inside the text and must not run past its end."""

    def __init__(self, text):
        self.text = text
        n = len(text)
        self.attr = [{"list": "none", "indent": 0, "bold": False, "ul": False} for _ in range(n)]
        self.calls = []

    def GetPlainText(self):
        return self.text

    def _check(self, rng):
        if rng.Start < 0 or rng.Start >= len(self.text) or rng.Start + rng.Length > len(self.text):
            raise ValueError("start index %d is not within the text range" % rng.Start)

    def Find(self, s, start, match_case, whole_word):
        i = self.text.find(s, start)
        return None if i < 0 else FakeRange(i, len(s))

    def SetPlainText(self, rng, s):
        self._check(rng)
        self.calls.append(("SetPlainText", rng.Start, rng.Length))
        tmpl = dict(self.attr[rng.Start])
        self.text = self.text[:rng.Start] + s + self.text[rng.Start + rng.Length:]
        self.attr = (self.attr[:rng.Start] + [dict(tmpl) for _ in s]
                     + self.attr[rng.Start + rng.Length:])

    def _set(self, rng, key, value):
        self._check(rng)
        self.calls.append((key, rng.Start, rng.Length))
        for i in range(rng.Start, rng.Start + rng.Length):
            self.attr[i][key] = value

    def SetListType(self, rng, value):
        self._set(rng, "list", value)

    def SetIndentLevel(self, rng, value):
        self._set(rng, "indent", value)

    def SetBoldStatus(self, rng, value):
        self._set(rng, "bold", value)

    def SetUnderlineStatus(self, rng, value):
        self._set(rng, "ul", value)

    def para_attr(self, line_start):
        return self.attr[line_start]


def make_range(start, length):
    return FakeRange(start, length)


def list_type(name):
    return name
