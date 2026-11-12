import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_formula_dependencies import dependency_plan, formula_dependencies
from family_library_discovery import manifest_digest


def test_cli_resolves_transitive_exact_names_and_orders_only_selected_formulas(tmp_path):
    request = {"selected": ["Overall Width", "Intermediate Width"], "definitions": [
        {"name": "Overall Width", "formula": "(Intermediate Width + Clearance) / 2"},
        {"name": "Intermediate Width", "formula": "Panel Width * 2"},
        {"name": "Panel Width", "formula": None},
        {"name": "Clearance", "formula": "Écart / 2"},
        {"name": "Écart", "formula": None},
    ]}
    path = tmp_path.resolve() / "request.json"
    path.write_text(json.dumps(request))
    result = subprocess.run([sys.executable, str(SCRIPTS / "family_formula_dependencies.py"), str(path)],
                            capture_output=True, text=True, check=True)
    graph = json.loads(result.stdout)
    assert graph["formula_order"] == ["Intermediate Width", "Overall Width"]
    assert graph["dependencies"] == {
        "Overall Width": ["Clearance", "Intermediate Width"], "Intermediate Width": ["Panel Width"],
        "Panel Width": [], "Clearance": ["Écart"], "Écart": [],
    }
    assert graph.pop("dependency_sha256") == manifest_digest(graph)


@pytest.mark.parametrize(("expression", "names", "reason"), [
    ("width * 2", ["Width"], "Unknown"),
    ("WidthExtra * 2", ["Width"], "Unknown"),
    ("Width-Height", ["Width", "Height", "Width-Height"], "unsupported punctuation"),
    ("Width + 2", ["Width", "width"], "ambiguous"),
    ("if(Width, 2, 3)", ["Width"], "Unknown"),
    ("Width (2 + 3)", ["Width"], "trailing input"),
    ("(Width + 2", ["Width"], "unclosed parenthesis"),
    ("Width +", ["Width"], "operand"),
    ("(" * 40 + "Width" + ")" * 40, ["Width"], "nesting"),
])
def test_rejects_unsupported_or_ambiguous_formula_text(expression, names, reason):
    with pytest.raises(ValueError, match=reason):
        formula_dependencies(expression, names)


def test_detects_cycle_through_reused_unselected_formulas():
    definitions = [{"name": "A", "formula": "B + 1"}, {"name": "B", "formula": "C"},
                   {"name": "C", "formula": "A * 2"}]
    with pytest.raises(ValueError, match="contain a cycle"):
        dependency_plan(definitions, ["A"])


def test_parses_arithmetic_without_claiming_native_evaluation():
    assert formula_dependencies("-Panel Width ^ 2 ^ 3 + 2 * (Width - .5)", ["Width", "Panel Width"]) == [
        "Panel Width", "Width",
    ]
