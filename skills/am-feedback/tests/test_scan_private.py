# -*- coding: utf-8 -*-
"""Tests for scan_private.py and its PowerShell twin scan-private.ps1 (issue #273)."""

import io
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import scan_private as sp  # noqa: E402

PS1 = os.path.join(SCRIPTS, "scan-private.ps1")
PY = os.path.join(SCRIPTS, "scan_private.py")
BS = "\\"  # one backslash, built in Python so no shell can change it


@pytest.fixture(autouse=True)
def no_local_list(tmp_path, monkeypatch):
    # Never read the deny list of the computer that runs the tests.
    monkeypatch.setenv("ARCHI_MAGIC_PRIVATE_TERMS", str(tmp_path / "no-such-list.txt"))


def write(path, text, encoding="utf-8"):
    with io.open(str(path), "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    return str(path)


def scan(*argv):
    out = io.StringIO()
    code = sp.run(list(argv), out=out)
    return code, out.getvalue()


def test_self_test_passes_on_the_shipped_patterns():
    code, out = scan("--self-test")
    assert code == 0
    assert out.startswith("SELF-TEST OK")


def test_windows_user_profile_path_is_a_hit(tmp_path):
    draft = write(tmp_path / "d.md", "Context\nThe log is in C:" + BS + "Users" + BS + "jdoe" + BS + "x.log\n")
    code, out = scan(draft)
    assert code == 1
    assert draft + ":2: user-profile-path" in out
    assert "SCAN HITS" in out and "SCAN CLEAN" not in out


def test_git_bash_and_mac_home_paths_are_hits(tmp_path):
    draft = write(tmp_path / "d.md", "a /c/Users/jdoe/x\nb /home/jdoe/y\n")
    code, out = scan(draft)
    assert code == 1
    assert draft + ":1: home-path" in out
    assert draft + ":2: home-path" in out


def test_plugin_id_is_the_only_at_sign_and_scans_clean(tmp_path):
    draft = write(tmp_path / "d.md", "Fix: run `claude plugin update archi-magic@archi-magic`.\n"
                                     "See https://github.com/zsenarchitect/archi-magic/issues/273\n")
    code, out = scan(draft)
    assert code == 0, out
    assert out.strip().splitlines()[-1].startswith("SCAN CLEAN: 1 file(s)")


def test_other_at_signs_links_numbers_and_guids_are_hits(tmp_path):
    draft = write(tmp_path / "d.md", "\n".join([
        "mail jdoe@example.com",
        "link https://example.com/page",
        "fake https://github.com/zsenarchitect-evil.example/x",
        "element 12345678",
        "run 0123abcd-4567-89ab-cdef-0123456789ab",
        "plugin id with another domain archi-magic@archi-magic.example",
    ]) + "\n")
    code, out = scan(draft)
    assert code == 1
    for line, name in [(1, "at-sign"), (2, "link"), (3, "link"), (4, "long-number"), (5, "guid"), (6, "at-sign")]:
        assert "%s:%d: %s" % (draft, line, name) in out


def test_fixed_term_ending_with_a_backslash_is_plain_text(tmp_path):
    terms = write(tmp_path / "terms.txt", "# names from the session\nProject Zeta\nD:" + BS + "Jobs" + BS + "\n")
    draft = write(tmp_path / "d.md", "The model of project zeta\nsaved in d:" + BS + "jobs" + BS + "a.rvt\nclean line\n")
    code, out = scan("--terms", terms, draft)
    assert code == 1, out
    assert draft + ":1: term file line 2 (terms.txt)" in out
    assert draft + ":2: term file line 3 (terms.txt)" in out
    assert ":3:" not in out


def test_term_given_on_the_command_line(tmp_path):
    draft = write(tmp_path / "d.md", "Tower B is late\n")
    code, out = scan("--term", "tower b", draft)
    assert code == 1
    assert draft + ":1: term 1" in out


def test_broken_regex_terms_file_is_an_error_not_clean(tmp_path):
    # The failure in issue #273: a pattern that ends with one backslash.
    bad = write(tmp_path / "bad.txt", "C:" + BS + "Users" + BS + "\n")
    draft = write(tmp_path / "d.md", "nothing private here\n")
    code, out = scan("--terms-regex", bad, draft)
    assert code == 2
    assert out.startswith("SCAN ERROR: regex file bad.txt line 1")
    assert "CLEAN" not in out


def test_local_deny_list_is_read_and_a_broken_one_is_an_error(tmp_path, monkeypatch):
    draft = write(tmp_path / "d.md", "Firm Q drawing\n")
    good = write(tmp_path / "local.txt", "# local list\nfirm\\s+q\n")
    monkeypatch.setenv("ARCHI_MAGIC_PRIVATE_TERMS", good)
    code, out = scan(draft)
    assert code == 1
    assert draft + ":1: local deny list line 2" in out
    assert "Firm Q" not in out.replace(draft, "")  # the term itself is never printed
    code, out = scan("--no-local-terms", draft)
    assert code == 0
    broken = write(tmp_path / "broken.txt", "(unclosed\n")
    monkeypatch.setenv("ARCHI_MAGIC_PRIVATE_TERMS", broken)
    code, out = scan(draft)
    assert code == 2 and out.startswith("SCAN ERROR: local deny list line 1")


@pytest.mark.parametrize("case", ["missing", "empty", "not-utf8", "none"])
def test_unreadable_input_is_an_error(tmp_path, case):
    good = write(tmp_path / "good.md", "plain text\n")
    if case == "missing":
        argv = [good, str(tmp_path / "missing.md")]
    elif case == "empty":
        argv = [good, write(tmp_path / "empty.md", "  \n")]
    elif case == "not-utf8":
        p = tmp_path / "latin.md"
        p.write_bytes(b"caf\xe9\n")
        argv = [good, str(p)]
    else:
        argv = []
    code, out = scan(*argv)
    assert code == 2
    assert out.startswith("SCAN ERROR")
    assert "CLEAN" not in out


def test_missing_terms_file_is_an_error(tmp_path):
    draft = write(tmp_path / "d.md", "plain\n")
    code, out = scan("--terms", str(tmp_path / "nope.txt"), draft)
    assert code == 2 and "SCAN ERROR" in out


def test_bom_and_crlf_drafts_keep_line_numbers(tmp_path):
    p = tmp_path / "d.md"
    p.write_bytes(b"\xef\xbb\xbfline one\r\nline two jdoe@example.com\r\n")
    code, out = scan(str(p))
    assert code == 1
    assert str(p) + ":2: at-sign" in out


def test_broken_pattern_file_or_a_dead_pattern_is_an_error(tmp_path, monkeypatch):
    draft = write(tmp_path / "d.md", "plain\n")
    bad = write(tmp_path / "p.txt", "deny\tbroken\t(oops\tx\n")
    monkeypatch.setattr(sp, "PATTERN_FILE", bad)
    code, out = scan(draft)
    assert code == 2 and "does not compile" in out
    # A pattern that cannot find its own test line fails the self-test.
    dead = write(tmp_path / "dead.txt", "deny\tat-sign\t@@@@\tjdoe@example.com\n")
    monkeypatch.setattr(sp, "PATTERN_FILE", dead)
    code, out = scan(draft)
    assert code == 2 and "self-test: pattern 'at-sign' missed its test line" in out


def test_bad_arguments_are_an_error(capsys):
    code = sp.main(["--no-such-flag"])
    out = capsys.readouterr().out
    assert code == 2 and "SCAN ERROR" in out


def test_cjk_draft_under_isolated_mode_has_no_encoding_error(tmp_path):
    draft = write(tmp_path / "d.md", "中文 テスト jdoe@example.com\n")
    env = dict(os.environ, PYTHONIOENCODING="ascii", ARCHI_MAGIC_PRIVATE_TERMS=str(tmp_path / "none.txt"))
    res = subprocess.run([sys.executable, "-I", PY, "--term", "中文", draft],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert res.returncode == 1, res.stderr
    out = res.stdout.decode("utf-8")
    assert ":1: at-sign" in out and ":1: term 1" in out


# ---- PowerShell twin: same results as the Python scan -------------------------------------

def _powershell():
    for exe in ("powershell", "pwsh"):
        path = shutil.which(exe)
        if path:
            return path
    return None


PWSH = _powershell()


def run_ps(*args):
    cmd = [PWSH, "-NoProfile", "-NonInteractive"]
    if os.name == "nt":
        cmd += ["-ExecutionPolicy", "Bypass"]
    res = subprocess.run(cmd + ["-File", PS1] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return res.returncode, res.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


@pytest.mark.skipif(PWSH is None, reason="no PowerShell on this computer")
def test_powershell_twin_gives_the_same_results(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHI_MAGIC_PRIVATE_TERMS", str(tmp_path / "none.txt"))
    terms = write(tmp_path / "terms.txt", "Project Zeta\nD:" + BS + "Jobs" + BS + "\n")
    hits = write(tmp_path / "hits.md", "\n".join([
        "Context for project zeta",
        "path C:" + BS + "Users" + BS + "jdoe" + BS + "x and /c/Users/jdoe and d:" + BS + "jobs" + BS + "a",
        "mail jdoe@example.com, id 12345678, run 0123abcd-4567-89ab",
        "links https://example.com and https://github.com/zsenarchitect/archi-magic/issues/1",
        "update archi-magic@archi-magic",
    ]) + "\n")
    clean = write(tmp_path / "clean.md", "run `claude plugin update archi-magic@archi-magic`\n")
    bad = write(tmp_path / "bad.txt", "C:" + BS + "Users" + BS + "\n")

    def hit_lines(text):
        return sorted(line for line in text.splitlines() if not line.startswith("SCAN"))

    code_py, out_py = scan("--terms", terms, hits)
    code_ps, out_ps = run_ps("-TermsFile", terms, hits)
    assert code_py == code_ps == 1, out_ps
    assert hit_lines(out_py) == hit_lines(out_ps)

    code_ps, out_ps = run_ps(clean)
    assert code_ps == 0 and "SCAN CLEAN: 1 file(s)" in out_ps, out_ps

    code_ps, out_ps = run_ps("-TermsRegexFile", bad, clean)
    assert code_ps == 2 and out_ps.startswith("SCAN ERROR") and "CLEAN" not in out_ps, out_ps

    code_ps, out_ps = run_ps(str(tmp_path / "missing.md"))
    assert code_ps == 2 and out_ps.startswith("SCAN ERROR"), out_ps

    code_ps, out_ps = run_ps("-SelfTest")
    assert code_ps == 0 and out_ps.startswith("SELF-TEST OK"), out_ps
