# -*- coding: utf-8 -*-
"""Private-data scan for am-feedback drafts (issue #273).

Scans draft files before they go to the public archi-magic repo. The built-in
patterns live in private-patterns.txt next to this script, so no agent copies a
backslash through a shell. scan-private.ps1 is the same scan for computers
without Python; both read that file.

Usage:
    py -3 -I scan_private.py [--term TEXT ...] [--terms FILE] [--terms-regex FILE]
                             [--no-local-terms] DRAFT [DRAFT ...]
    py -3 -I scan_private.py --self-test

Inputs:
    --term TEXT        a project or client name from the session (fixed text).
    --terms FILE       one fixed term per line (case-insensitive, not a regex).
    --terms-regex FILE one regular expression per line (the local deny list format).
    The local deny list (ARCHI_MAGIC_PRIVATE_TERMS, else
    %LOCALAPPDATA%\\archi-magic\\private-terms.txt) is read as regexes when it
    exists, unless --no-local-terms is given. In all term files, blank lines and
    lines that start with "#" are skipped.

Output and exit code (not the grep convention):
    <file>:<line>: <pattern name>   one line for each hit
    SCAN CLEAN: ...   exit 0  read every file, self-test passed, no hit
    SCAN HITS: ...    exit 1  at least one hit; fix the draft and scan again
    SCAN ERROR: ...   exit 2  a file, a term file or a pattern could not be used
A run that printed SCAN ERROR, or no SCAN line at all, is never clean.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERN_FILE = os.path.join(HERE, "private-patterns.txt")
LINE_SPLIT = re.compile(r"\r\n|\n|\r")

EXIT_CLEAN = 0
EXIT_HITS = 1
EXIT_ERROR = 2


class ScanError(Exception):
    """A problem that makes the scan result unusable."""


def _utf8_stdout() -> None:
    # Isolated mode (-I) ignores PYTHONIOENCODING; reconfigure the streams here.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_text(path: str) -> str:
    """Read a UTF-8 file (a byte-order mark is allowed). Raise ScanError on any problem."""
    if not os.path.isfile(path):
        raise ScanError("not a file: %s" % path)
    try:
        with io.open(path, "rb") as fh:
            raw = fh.read()
    except (IOError, OSError) as exc:
        raise ScanError("cannot read %s (%s)" % (path, exc.__class__.__name__))
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ScanError("not UTF-8 text: %s" % path)


def split_lines(text: str) -> List[str]:
    return LINE_SPLIT.split(text)


def _term_lines(text: str) -> List[Tuple[int, str]]:
    out = []
    for n, line in enumerate(split_lines(text), 1):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append((n, s))
    return out


def load_patterns(path: str = PATTERN_FILE) -> Dict[str, list]:
    """Read the built-in patterns. Return {"deny": [...], "allow": [...]}, each item (name, regex, canary)."""
    text = read_text(path)
    result = {"deny": [], "allow": []}
    for n, line in enumerate(split_lines(text), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] not in result:
            raise ScanError("pattern file line %d is not 'kind<TAB>name<TAB>regex<TAB>canary'" % n)
        kind, name, rx, canary = parts
        try:
            compiled = re.compile(rx, re.IGNORECASE)
        except re.error as exc:
            raise ScanError("pattern file line %d does not compile (%s)" % (n, exc))
        result[kind].append((name, compiled, canary))
    if not result["deny"]:
        raise ScanError("pattern file has no deny pattern")
    return result


def load_fixed_terms(path: str) -> List[Tuple[str, str]]:
    """Fixed terms from a file. Return [(label, term)]. A trailing backslash is plain text."""
    return [("term file line %d" % n, s) for n, s in _term_lines(read_text(path))]


def load_regex_terms(path: str, label: str) -> List[Tuple[str, "re.Pattern"]]:
    """Regex terms (local deny list format). A bad line is a ScanError that names the line, not the term."""
    out = []
    for n, s in _term_lines(read_text(path)):
        try:
            out.append(("%s line %d" % (label, n), re.compile(s, re.IGNORECASE)))
        except re.error:
            raise ScanError("%s line %d is not a valid regular expression" % (label, n))
    return out


def scan_line(line: str, patterns: Dict[str, list], fixed: List[Tuple[str, str]], regexes: list) -> List[str]:
    """Return the names of everything that matches one line."""
    hits = []
    masked = line
    for _name, rx, _canary in patterns["allow"]:
        masked = rx.sub(lambda m: " " * len(m.group(0)), masked)
    for name, rx, _canary in patterns["deny"]:
        if rx.search(masked):
            hits.append(name)
    low = line.casefold()
    for label, term in fixed:
        if term.casefold() in low:
            hits.append(label)
    for label, rx in regexes:
        if rx.search(line):
            hits.append(label)
    return hits


def self_test(patterns: Dict[str, list], fixed=None, regexes=None) -> None:
    """Prove that each pattern works on a known line. Raise ScanError when one does not."""
    fixed = fixed or []
    regexes = regexes or []
    for name, _rx, canary in patterns["deny"]:
        if name not in scan_line(canary, patterns, [], []):
            raise ScanError("self-test: pattern '%s' missed its test line" % name)
    for name, _rx, canary in patterns["allow"]:
        found = scan_line(canary, patterns, [], [])
        if found:
            raise ScanError("self-test: allowed text '%s' still gives a hit (%s)" % (name, ", ".join(found)))
    if scan_line("plain text with no private data", patterns, [], []):
        raise ScanError("self-test: a plain line gives a hit")
    for label, term in fixed:
        if label not in scan_line("x " + term + " x", patterns, fixed, []):
            raise ScanError("self-test: %s does not match itself" % label)


def default_local_terms() -> Optional[str]:
    env = os.environ.get("ARCHI_MAGIC_PRIVATE_TERMS")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return os.path.join(base, "archi-magic", "private-terms.txt")
    return None


def run(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="Scan am-feedback drafts for private data.")
    ap.add_argument("drafts", nargs="*", help="draft files to scan")
    ap.add_argument("--term", action="append", default=[], help="a fixed term, for example a project name")
    ap.add_argument("--terms", action="append", default=[], help="file with one fixed term per line")
    ap.add_argument("--terms-regex", action="append", default=[], help="file with one regex per line")
    ap.add_argument("--no-local-terms", action="store_true", help="do not read the local deny list")
    ap.add_argument("--self-test", action="store_true", help="only prove that the patterns work")
    args = ap.parse_args(argv)

    def say(text):
        out.write(text + "\n")

    try:
        patterns = load_patterns(PATTERN_FILE)
        fixed = [("term %d" % (i + 1), t.strip()) for i, t in enumerate(args.term) if t.strip()]
        for p in args.terms:
            fixed.extend(("%s (%s)" % (label, os.path.basename(p)), t) for label, t in load_fixed_terms(p))
        regexes = []
        for p in args.terms_regex:
            regexes.extend(load_regex_terms(p, "regex file %s" % os.path.basename(p)))
        local_note = "local deny list: not used"
        if not args.no_local_terms:
            local = default_local_terms()
            if local and os.path.isfile(local):
                found = load_regex_terms(local, "local deny list")
                regexes.extend(found)
                local_note = "local deny list: %d pattern(s)" % len(found)
            else:
                local_note = "local deny list: none"
        self_test(patterns, fixed, regexes)
        if args.self_test:
            say("SELF-TEST OK: %d built-in pattern(s), %d allow rule(s)" % (len(patterns["deny"]), len(patterns["allow"])))
            return EXIT_CLEAN
        if not args.drafts:
            raise ScanError("no draft file given")
        texts = []
        for path in args.drafts:
            text = read_text(path)
            if not text.strip():
                raise ScanError("empty file: %s" % path)
            texts.append((path, text))
    except ScanError as exc:
        say("SCAN ERROR: %s. This is not a clean scan." % exc)
        return EXIT_ERROR

    hits = 0
    for path, text in texts:
        for n, line in enumerate(split_lines(text), 1):
            for name in scan_line(line, patterns, fixed, regexes):
                say("%s:%d: %s" % (path, n, name))
                hits += 1
    summary = "%d file(s), %d built-in pattern(s), %d fixed term(s), %d regex term(s), %s, self-test passed" % (
        len(texts), len(patterns["deny"]), len(fixed), len(regexes), local_note)
    if hits:
        say("SCAN HITS: %d hit(s) in %s. Fix the drafts and scan again." % (hits, summary))
        return EXIT_HITS
    say("SCAN CLEAN: %s" % summary)
    return EXIT_CLEAN


def main(argv=None) -> int:
    _utf8_stdout()
    try:
        return run(argv)
    except SystemExit as exc:  # argparse usage error
        if exc.code not in (0, None):
            sys.stdout.write("SCAN ERROR: bad arguments. This is not a clean scan.\n")
            return EXIT_ERROR
        return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
