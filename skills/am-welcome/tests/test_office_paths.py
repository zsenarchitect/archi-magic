"""Run the office-paths.ps1 tests (issue #277) from pytest, so CI runs them with every skill test.

CI runs every skills/*/tests folder that has a test_*.py file. The helper is PowerShell, because
many office computers have no Python. This wrapper starts test-office-paths.ps1 with pwsh (CI on
Linux) or Windows PowerShell, and skips when neither is installed.
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_SCRIPT = os.path.join(HERE, "test-office-paths.ps1")


def _find_powershell():
    for name in ("pwsh", "powershell.exe", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    return None


def test_office_paths_powershell_suite():
    shell = _find_powershell()
    if not shell:
        pytest.skip("no PowerShell on this computer")
    proc = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", TEST_SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "0 failure(s)" in output, output
