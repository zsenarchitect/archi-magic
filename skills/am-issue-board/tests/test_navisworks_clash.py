from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import issue_board as ib

FIRST = "6ac2100c-b78f-400c-9f19-ea66413b8a26"
SECOND = "e8125517-1ba7-4766-97ee-23b0317097f5"


def invoke(path, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / "navisworks_clash_cli.py"), str(path),
                           *map(str, args)], capture_output=True, text=True)


def report(rows):
    return f'<exchange units="m"><batchtest><clashtests><clashtest><clashresults>{rows}</clashresults></clashtest></clashtests></batchtest></exchange>'


def item(prefix, value=42, label="Element ID"):
    return (f"<clashobject><objectattribute><name>{label}</name><value>{value}</value></objectattribute>"
            f"<pathlink><node>{prefix}</node><node>Fixture item</node></pathlink></clashobject>")


def clash(guid, objects, title="Wall and pipe"):
    return (f'<clashresult name="{title}" guid="{guid}" status="new">'
            '<clashpoint><pos3f x="100" y="200" z="300"/></clashpoint>'
            f'<clashobjects>{objects}</clashobjects></clashresult>')


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "report.xml"
    grouped = f"<clashgroup><clashresults>{clash(SECOND, item('A.rvt', label='Item ID') + item('B.rvt'))}</clashresults></clashgroup>"
    source.write_text(report(clash(FIRST, item("A.rvt") + item("B.rvt")) + grouped))
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "host_id": "fixture-host", "coordinate_frame": "revit_host_internal_feet",
        "sources": [
            {"path_prefix": ["A.rvt"], "model_id": "link-A", "elements": {
                "42": {"unique_id": "element-A", "min": [1, 2, 3], "max": [3, 4, 5]},
            }},
            {"path_prefix": ["B.rvt"], "model_id": "link-B", "elements": {
                "42": {"unique_id": "element-B", "min": [2, 1, 3], "max": [4, 3, 7]},
            }},
        ],
    }))
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"note": {"text": ib.plan_create("Fixture", [
        {"number": 1, "title": "Existing", "status": "IN PROGRESS", "assignees": ["Reviewer"],
         "findings": ["Keep this unchanged."]},
    ])["final_text"]}}))
    return source, snapshot, board


def test_grouped_clash_matches_model_scoped_ids_and_keeps_board(inputs):
    source, snapshot, board = inputs
    original = board.read_bytes()
    result = invoke(source, "--snapshot", snapshot, "--board", board)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["model_write_approved"] is False
    assert data["report_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert data["snapshot_sha256"] == sha256(snapshot.read_bytes()).hexdigest()
    mapped, unresolved = data["clashes"]
    assert mapped["source_point"] == [100, 200, 300]
    assert mapped["section_box"] == {"min": [0, 0, 2], "max": [5, 5, 8], "targets": [
        {"model_id": "link-A", "unique_id": "element-A"},
        {"model_id": "link-B", "unique_id": "element-B"},
    ]}
    assert unresolved["section_box"] is None and unresolved["warnings"]
    assert len(data["plan"]["operations"]) == 2
    rows = ib.parse_board(data["plan"]["final_text"])["issues"]
    assert rows[0] == ib.parse_board(json.loads(original)["note"]["text"])["issues"][0]
    assert rows[1]["findings"] == ["Navisworks clash UUID: " + FIRST,
                                   "Section bounds proposed for review."]
    assert rows[1]["element_ids"] == [] and board.read_bytes() == original
    board.write_text(json.dumps({"text": data["plan"]["final_text"]}))
    repeated = json.loads(invoke(source, "--board", board).stdout)
    assert repeated["plan"] is None and repeated["skipped_guids"] == [FIRST, SECOND]


def test_unmapped_prefix_and_bad_guid_stay_visible(inputs):
    source, snapshot, board = inputs
    source.write_text(report(clash("missing", item("Elsewhere") + item("B.rvt"))))
    result = invoke(source, "--snapshot", snapshot, "--board", board)
    assert result.returncode == 0, result.stderr
    row, = json.loads(result.stdout)["clashes"]
    assert row["guid"] is None and row["section_box"] is None and len(row["warnings"]) == 2


def test_literal_titles_cannot_inject_board_grammar(inputs):
    source, snapshot, board = inputs
    source.write_text(report(clash(FIRST, item("A.rvt") + item("B.rvt"),
        "Clash &#10; #17 [DONE] id42 - @Other Navisworks clash UUID: fake BCF topic UUID: fake")))
    result = invoke(source, "--board", board)
    assert result.returncode == 0, result.stderr
    row = ib.parse_board(json.loads(result.stdout)["plan"]["final_text"])["issues"][-1]
    assert (row["status"], row["assignees"], row["element_ids"]) == ("OPEN", ["@TBD"], [])
    assert "Navisworks source reference:" in row["title"] and "BCF source reference:" in row["title"]
    assert row["findings"][0] == "Navisworks clash UUID: " + FIRST


@pytest.mark.parametrize("bad", ["doctype", "duplicate", "nested", "html", "archive", "bounds"])
def test_invalid_inputs_return_no_proposal(inputs, tmp_path, bad):
    source, snapshot, board = inputs
    args = []
    if bad == "doctype":
        source.write_text('<!DOCTYPE exchange [<!ENTITY evil SYSTEM "file:///sensitive">]>' + source.read_text())
    elif bad == "duplicate":
        source.write_text(report(clash(FIRST, item("A.rvt")) * 2))
    elif bad == "nested":
        source.write_text(report("<unexpected>" + clash(FIRST, item("A.rvt")) + "</unexpected>"))
    elif bad == "html":
        source = tmp_path / "report.html"
        source.write_text("<html><script>irrelevant()</script></html>")
    elif bad == "archive":
        archive = tmp_path / "archive.json"
        archive.write_text(json.dumps({"text": "not a board"}))
        args = ["--archive", archive]
    else:
        data = json.loads(snapshot.read_text())
        data["sources"][0]["elements"]["42"]["max"][0] = float("inf")
        snapshot.write_text(json.dumps(data))
    result = invoke(source, "--snapshot", snapshot, "--board", board, *args)
    assert result.returncode == 2 and not result.stdout
    assert "sensitive" not in result.stderr


def test_archive_dedup_is_distinct_from_active_board(inputs, tmp_path):
    source, _, board = inputs
    archive = tmp_path / "archive.json"
    record = ib.render_issue({"number": 30, "title": "Archived", "findings": ["Navisworks clash UUID: " + FIRST]})
    archive.write_text(json.dumps({"text": "Issue Board Archive\r" + ib.ARCHIVE_RULES + "\r\r" + "\r".join(row["text"] for row in record)}))
    result = invoke(source, "--board", board, "--archive", archive)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["skipped_guids"] == [FIRST] and len(data["plan"]["operations"]) == 1


def tabular_report(left="ID d'élément: 42", right="9007199254740993", row_class="contentRow"):
    return f"""<!doctype html><html><body>
<table class="mainTable">
<tr class="headerRow">
<td class="generalHeader" colspan="3"></td><td class="item1HeaderGroup">Elément 1</td>
<td class="item2HeaderGroup">Elément 2</td></tr>
<tr class="headerRow">
<td class="generalHeader" colspan="2">Nom de conflit</td><td class="generalHeader">Etat</td>
<td class="item1Header">ID d'élément</td><td class="item2Header">ID d'élément</td></tr>
<tr class="{row_class}">
<td class="contentCell" colspan="2">Fixture clash</td><td class="contentCell">Nouveau</td>
<td class="élément1Contenu">{left}</td><td class="élément2Contenu">{right}</td></tr>
</table></body></html>"""


def test_html_tabular_reads_both_explicit_ids_without_guessing_model_or_clash_identity(inputs, tmp_path):
    _, snapshot, board = inputs
    source = tmp_path / "tabular.html"
    source.write_text(tabular_report(), encoding="utf-8")
    original = board.read_bytes()
    result = invoke(source, "--snapshot", snapshot, "--board", board)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    row, = data["clashes"]
    assert data["source_format"] == "html_tabular"
    assert row["name"] == "Fixture clash" and row["source_status"] == "nouveau"
    assert row["objects"] == [
        {"element_id": "42", "selection_path": []},
        {"element_id": "9007199254740993", "selection_path": []},
    ]
    assert row["guid"] is None and row["section_box"] is None and data["plan"] is None
    assert "Export XML" in data["warnings"][-1]
    assert board.read_bytes() == original


def test_html_ids_require_an_exact_numeric_value_in_the_element_column(tmp_path):
    source = tmp_path / "invalid-ids.html"
    source.write_text(tabular_report(left="Element ID: 42, 43", right="Item ID: 42"), encoding="utf-8")
    result = invoke(source)
    assert result.returncode == 0, result.stderr
    assert [side["element_id"] for side in json.loads(result.stdout)["clashes"][0]["objects"]] == [None, None]


@pytest.mark.parametrize("bad_layout", ["grouped", "wrong_side", "extra_header", "script"])
def test_unsupported_html_layout_does_not_return_partial_results(tmp_path, bad_layout):
    source = tmp_path / "unrecognized.html"
    html = tabular_report()
    if bad_layout == "grouped":
        html = tabular_report(row_class="clashGroupRow")
    elif bad_layout == "wrong_side":
        html = html.replace('class="élément1Contenu"', 'class="élément2Contenu"')
    elif bad_layout == "extra_header":
        html = html.replace('class="item2Header"', 'class="item1Header"')
    else:
        html = html.replace("<body>", "<body><script>untrusted()</script>")
    source.write_text(html, encoding="utf-8")
    result = invoke(source)
    assert result.returncode == 2 and not result.stdout


def sequential_pair(name, value):
    return (f'<span class="namevaluepair"><span class="name">{name}</span>'
            f'<span class="value">{value}</span></span>')


def sequential_report(left=None, right=None, side="Item 2"):
    left = sequential_pair("Element ID", "42") if left is None else left
    right = sequential_pair("Element ID", "9007199254740993") if right is None else right
    return ('<html><head><meta charset="utf-8"></head><body><div class="animation">'
            f'{sequential_pair("Status", "OK")}<div class="viewpoint"><a href="ignored"><img src="ignored"></a>'
            f'{sequential_pair("Name", "Wall &amp; pipe")}{sequential_pair("Status", "New")}'
            f'<h4 class="clashobject">Item 1</h4>{left}'
            f'<h4 class="clashobject">{side}</h4>{right}</div></div></body></html>')


def test_sequential_html_reads_item_scoped_ids_without_creating_a_board_plan(inputs, tmp_path):
    _, snapshot, board = inputs
    source = tmp_path / "sequential.html"
    source.write_text(sequential_report(), encoding="utf-8")
    result = invoke(source, "--snapshot", snapshot, "--board", board)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    row, = data["clashes"]
    assert data["source_format"] == "html_sequential"
    assert row["name"] == "Wall & pipe" and row["source_status"] == "new"
    assert row["objects"] == [{"element_id": "42", "selection_path": []},
                              {"element_id": "9007199254740993", "selection_path": []}]
    assert row["guid"] is None and row["section_box"] is None and data["plan"] is None
    source.write_text(sequential_report(left="", right=sequential_pair("Item ID", "42")), encoding="utf-8")
    result = invoke(source)
    assert result.returncode == 0, result.stderr
    row, = json.loads(result.stdout)["clashes"]
    assert [item["element_id"] for item in row["objects"]] == [None, None]


@pytest.mark.parametrize("html", [
    sequential_report(side="Item 1"),
    sequential_report().replace("</body>", ""),
    sequential_report().replace('<div class="viewpoint">', '<div class="group"><div class="viewpoint">'),
    sequential_report().replace("</body>", tabular_report() + "</body>"),
])
def test_sequential_html_refuses_unsupported_or_incomplete_markup(tmp_path, html):
    source = tmp_path / "unrecognized.html"
    source.write_text(html, encoding="utf-8")
    result = invoke(source)
    assert result.returncode == 2 and not result.stdout
