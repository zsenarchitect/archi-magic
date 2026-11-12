"""Exercise the structural query, standalone CLI, and board transaction entry."""
from hashlib import sha256
from itertools import product
import json
import math
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import issue_board as ib


class Id:
    def __init__(self, value):
        self.Value = value


class Transform:
    def __init__(self, apply):
        self.OfPoint = apply

    def Multiply(self, child):
        return Transform(lambda point: self.OfPoint(child.OfPoint(point)))


Transform.Identity = Transform(lambda p: p)


class Instance:
    def __init__(self, transform, geometry):
        self.Transform = transform
        self.GetSymbolGeometry = lambda: geometry


class Solid:
    Volume = 1

    def __init__(self, points):
        face = NS(Triangulate=lambda: NS(Vertices=type("Vertices", (list,), {"Count": len(points)})(points)))
        self.Faces = type("Faces", (list,), {"Size": 1})([face])


class Link:
    UniqueId = "selected-link"

    def __init__(self, document, transform):
        self.GetLinkDocument = lambda: document
        self.GetTransform = lambda: transform


def point(x, y, z):
    return NS(X=x, Y=y, Z=z)


def element(category, unique_id, geometry):
    return NS(Category=NS(Id=Id(category)), UniqueId=unique_id, get_Geometry=lambda _: geometry)


def install_runtime(monkeypatch, db):
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))


def read_query(monkeypatch):
    db = NS(ElementId=Id, Transform=Transform, GeometryInstance=Instance, Solid=Solid,
            Mesh=type("Mesh", (), {}), RevitLinkInstance=Link, ElementType=type("ElementType", (), {}),
            Options=lambda: NS(), ViewDetailLevel=NS(Fine="fine"),
            BuiltInCategory=NS(OST_Columns=1, OST_StructuralColumns=2, OST_Walls=3, OST_Floors=4))
    install_runtime(monkeypatch, db)
    box = [point(x, y, z) for x, y, z in product((0, 2), (0, 1), (0, 3))]
    translation = Transform(lambda p: point(p.X + 2, p.Y - 1, p.Z))
    theta = math.radians(30)

    def reflected_rotation(p):
        return point(10 - p.X * math.cos(theta) - p.Y * math.sin(theta),
                     5 - p.X * math.sin(theta) + p.Y * math.cos(theta), p.Z + 2)

    transform = Transform(reflected_rotation)
    transformed = [reflected_rotation(translation.OfPoint(p)) for p in box]
    shifted = [point(p.X + 50 / 304.8, p.Y, p.Z) for p in transformed]
    linked_items = {10: element(2, "linked-column", [Instance(translation, [Solid(box)])]),
                    11: element(2, "linked-matching", [Instance(translation, [Solid(box)])])}
    link_doc = NS(PathName="fixture-link", ProjectInformation=NS(UniqueId="structural"),
                  GetElement=lambda identifier: linked_items.get(identifier.Value))
    host_items = {1: element(1, "host-column", [Solid(shifted)]),
                  2: element(1, "host-matching", [Solid(transformed)]),
                  3: element(3, "host-wall", [Solid(box)]), 5: Link(link_doc, transform)}
    host = NS(IsFamilyDocument=False, PathName="fixture-host", ProjectInformation=NS(UniqueId="architecture"),
              GetElement=lambda identifier: host_items.get(identifier.Value))
    result = runpy.run_path(str(SCRIPTS / "read_structural_geometry.py"), init_globals={
        "doc": host,
        "inputs": {"link_instance_id": "5", "host_ids": ["1", "2", "3"], "link_ids": ["10", "11"],
                   "scope": "Synthetic phase and level"},
    })["result"]
    assert result["status"] == "ok", result
    return result["snapshot_json"], host


def save_board(path, body, identity, identifier="40", note_uid="board-note"):
    path.write_text(json.dumps({"host_identity": identity,
                                "note": {"text": body, "id": identifier, "unique_id": note_uid,
                                         "sha1": ib.text_sha1(body)}}))


@pytest.fixture
def case(tmp_path, monkeypatch):
    encoded, host = read_query(monkeypatch)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(encoded)
    review = tmp_path / "review.json"
    criteria = {"snapshot_sha256": sha256(snapshot.read_bytes()).hexdigest(), "center_tolerance_mm": 1,
                "extent_tolerance_mm": 1, "dispositions": [
                    {"kind": "pair", "host": "1", "link": "10"},
                    {"kind": "pair", "host": "2", "link": "11"},
                    {"kind": "missing", "host": "3"},
                ]}
    review.write_text(json.dumps(criteria))
    active = tmp_path / "board.json"
    original = ib.plan_create("Fixture", [{"number": 1, "title": "Existing"}])["final_text"]
    save_board(active, original, ib.saved_document_identity(host))
    return snapshot, review, active, host, original


def invoke(snapshot, review, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / "structural_compare_cli.py"),
                           str(snapshot), str(review), *map(str, args)], capture_output=True, text=True)


def test_geometry_pipeline_and_board_retry(case, tmp_path):
    snapshot, review, board, host, original = case
    original_file = board.read_bytes()
    response = invoke(snapshot, review, "--board", board)
    assert response.returncode == 0, response.stderr
    assert board.read_bytes() == original_file
    report = json.loads(response.stdout)
    offset, aligned, missing = report["elements"]
    assert offset["center_shift_mm"] == pytest.approx(50)
    assert offset["findings"] == ["center_shift"]
    assert aligned["center_shift_mm"] == pytest.approx(0)
    assert aligned["findings"] == []
    assert missing["findings"] == ["missing_link"]
    plan = report["plan"]
    assert plan["structural_target"]["active"]["unique_id"] == "board-note"
    issues = ib.parse_board(plan["final_text"])["issues"]
    assert issues[0] == ib.parse_board(original)["issues"][0]
    assert issues[1]["findings"][1] == "Sampled envelope center shift 50.000 mm."
    assert all(not item["element_ids"] for item in issues)
    save_board(board, plan["final_text"], ib.saved_document_identity(host))
    repeated = json.loads(invoke(snapshot, review, "--board", board).stdout)
    assert repeated["plan"] is None and len(repeated["skipped_keys"]) == 2
    archive = tmp_path / "archive.json"
    records = [row for issue in issues[1:] for row in ib.render_issue(issue)]
    archived = "Issue Board Archive\r" + ib.ARCHIVE_RULES + "\r\r"
    archived += "\r".join(row["text"] for row in records)
    save_board(archive, archived, ib.saved_document_identity(host), "41", "archive-note")
    save_board(board, original, ib.saved_document_identity(host))
    recovered = json.loads(invoke(snapshot, review, "--board", board, "--archive", archive).stdout)
    assert recovered["plan"] is None and len(recovered["skipped_keys"]) == 2


@pytest.mark.parametrize("change", ["digest", "duplicate", "incomplete", "wrong_board"])
def test_invalid_review_or_board_stops(case, change):
    snapshot, review, board, _, _ = case
    criteria = json.loads(review.read_text())
    if change == "digest":
        criteria["snapshot_sha256"] = "0" * 64
    elif change == "duplicate":
        criteria["dispositions"].append(criteria["dispositions"][0])
    elif change == "incomplete":
        criteria["dispositions"].pop()
    else:
        data = json.loads(board.read_text())
        data["host_identity"]["path_sha256"] = "f" * 64
        board.write_text(json.dumps(data))
    review.write_text(json.dumps(criteria))
    result = invoke(snapshot, review, "--board", board)
    assert result.returncode == 2 and result.stdout == ""


def test_extent_change_and_deferral_are_distinct(case):
    snapshot, review, _, _, _ = case
    data = json.loads(snapshot.read_text())
    data["link"][1]["max"][2] += 1
    data["link"][1]["min"][2] -= 1
    snapshot.write_text(json.dumps(data))
    criteria = json.loads(review.read_text())
    criteria["snapshot_sha256"] = sha256(snapshot.read_bytes()).hexdigest()
    criteria["dispositions"][2]["kind"] = "defer"
    review.write_text(json.dumps(criteria))
    report = json.loads(invoke(snapshot, review).stdout)
    assert report["elements"][1]["extent_delta_mm"] == pytest.approx([0, 0, 609.6])
    assert report["elements"][1]["findings"] == ["envelope_extent"]
    assert report["elements"][2]["disposition"] == "defer"
    assert report["elements"][2]["findings"] == []


@pytest.mark.parametrize("guard_key", ["structural_target", "mep_penetration_target"])
@pytest.mark.parametrize("change", ["valid", "wrong_note", "save_as", "archive_changed"])
def test_board_guard_runs_before_transaction(case, monkeypatch, change, guard_key):
    snapshot, review, board, host, original = case
    plan = json.loads(invoke(snapshot, review, "--board", board).stdout)["plan"]

    plan[guard_key] = plan.pop("structural_target")

    class TextNote:
        UniqueId = "board-note"

        def GetFormattedText(self):
            return NS(GetPlainText=lambda: original)

    if change == "archive_changed":
        plan[guard_key]["archive"] = {"id": "41", "unique_id": "archive-note", "sha1": "f" * 40}
    actual_note = TextNote()
    if change == "wrong_note":
        actual_note.UniqueId = "same-text-different-note"
    if change == "save_as":
        host.PathName = "saved-copy"
    host.GetElement = lambda identifier: actual_note

    def transaction(*_):
        raise RuntimeError("transaction entry reached")

    install_runtime(monkeypatch, NS(ElementId=Id, TextNote=TextNote, Transaction=transaction))
    globals_ = {"inputs": {"plan": plan, "note_id": "40"},
                "__revit__": NS(ActiveUIDocument=NS(Document=host))}
    expected = RuntimeError if change == "valid" else ValueError
    with pytest.raises(expected):
        runpy.run_path(str(SCRIPTS / "apply_board.py"), init_globals=globals_)
