"""Exercise the production pyRevit entry point with controlled view geometry."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import random
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "read_shared_text.py"
VIEW_ID = 9007199254740993
FIRST_NOTE = 9007199254741100


@dataclass(frozen=True)
class XYZ:
    x: float
    y: float
    z: float = 0

    def DotProduct(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def Normalize(self):
        length = math.sqrt(self.DotProduct(self))
        return XYZ(self.x / length, self.y / length, self.z / length) if length else self

    def IsAlmostEqualTo(self, other):
        diff = self.Subtract(other)
        return diff.DotProduct(diff) < 1e-18

    def Subtract(self, other):
        return XYZ(self.x - other.x, self.y - other.y, self.z - other.z)


class View:
    pass


class DraftingView(View):
    Id = NS(Value=VIEW_ID)
    UniqueId = "drafting-view-uid"
    Name = "BEP – 配管"
    IsTemplate = False
    Scale = 20
    Origin = XYZ(50, 20)
    RightDirection = XYZ(0, 1)
    UpDirection = XYZ(-1, 0)
    ViewType = "DraftingView"


class TextNote:
    HorizontalAlignment = "Left"
    VerticalAlignment = "Top"
    BaseDirection = DraftingView.RightDirection
    UpDirection = DraftingView.UpDirection

    def __init__(self, identifier, x_mm, y_mm, text, owner=VIEW_ID, cropped=False):
        self.Id = NS(Value=identifier)
        self.OwnerViewId = NS(Value=owner)
        self.UniqueId = "uid-" + str(identifier)
        feet_per_mm = DraftingView.Scale / 304.8
        origin = DraftingView.Origin
        self.Coord = XYZ(origin.x - y_mm * feet_per_mm, origin.y + x_mm * feet_per_mm)
        self.Text = text
        self.cropped = cropped

    def GetFormattedText(self):
        return NS(GetPlainText=lambda: self.Text)


class Collector:
    def __init__(self, doc, view_id=None):
        self.doc = doc
        self.visible_only = view_id is not None
        self.view_id = view_id.Value if view_id else None

    def OwnedByView(self, view_id):
        assert not self.visible_only
        self.view_id = view_id.Value
        return self

    def OfClass(self, element_class):
        assert element_class is TextNote
        return (note for note in self.doc.notes if note.OwnerViewId.Value == self.view_id
                and (not self.visible_only or not note.cropped))


@pytest.fixture
def project(monkeypatch):
    view = DraftingView()
    doc = NS(IsFamilyDocument=False, Title="Project", PathName=r"C:\BEP.rvt",
             ProjectInformation=NS(UniqueId="project-uid"), notes=[], view=view)

    def element(element_id):
        if element_id.Value == VIEW_ID:
            return view
        return next((note for note in doc.notes if note.Id.Value == element_id.Value), None)

    doc.GetElement = element
    db = NS(View=View, ViewDrafting=DraftingView, TextNote=TextNote,
            ElementId=lambda value: NS(Value=value), FilteredElementCollector=Collector,
            HorizontalTextAlignment=NS(Left="Left"), VerticalTextAlignment=NS(Top="Top"))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    return doc


def run(doc, options):
    return runpy.run_path(str(SCRIPT), init_globals={
        "__revit__": NS(ActiveUIDocument=NS(Document=doc)), "inputs": options,
    })["result"]


def export(doc, **overrides):
    options = {
        "export_view": True, "expected_title": "Project", "expected_path": r"C:\BEP.rvt",
        "expected_project_uid": "project-uid", "expected_view_uid": "drafting-view-uid",
        "view_id": str(VIEW_ID),
    }
    options.update(overrides)
    raw = run(doc, options)
    assert raw.isascii()
    return json.loads(raw)


def test_70_notes_are_lossless_and_grouped_in_verified_column_order(project):
    text = "Étage 1\r\n配管 😀 " + r"\u4e2d"
    expected = []
    heading_ids = []
    for column in (1, 2):
        active_heading = None
        for row in range(1, 36):
            number = FIRST_NOTE + (column - 1) * 35 + row
            note_id = str(number)
            heading = row in (2, 7, 12, 17, 22, 27) or (column == 1 and row == 32)
            if heading:
                heading_ids.append(note_id)
                active_heading = note_id
            project.notes.append(TextNote(number, 4 + 45 * (column - 1), 120 - 2 * row,
                                          text, cropped=(row == 16)))
            expected.append((note_id, "uid-" + note_id, text, column, row, heading, active_heading))
    project.notes.append(TextNote(99, 1, 500, "different view", owner=VIEW_ID + 1))
    random.Random(229).shuffle(project.notes)
    assert len(heading_ids) == 13

    first = export(project, heading_ids=heading_ids, column_boundaries_mm=[25])
    project.notes.reverse()
    second = export(project, heading_ids=list(reversed(heading_ids)), column_boundaries_mm=[25])
    assert first == second
    assert first["inventory_complete"] is True
    assert first["visual_order"] == "verified"
    fields = ("id", "uid", "text", "column", "row", "heading", "section_heading_id")
    assert [tuple(note[field] for field in fields) for note in first["notes"]] == expected
    assert first["notes"][0]["x_mm"] == pytest.approx(4)
    assert first["notes"][0]["y_mm"] == pytest.approx(118)


def test_tolerance_uses_fixed_row_y_and_exact_ids_break_position_ties(project):
    project.notes = [
        TextNote(FIRST_NOTE + 1, 5, 10.8, "highest"),
        TextNote(FIRST_NOTE + 3, 2, 10.4, "second at the same spot"),
        TextNote(FIRST_NOTE + 2, 2, 10.4, "first at the same spot"),
        TextNote(FIRST_NOTE + 4, 0, 10, "next row"),
    ]
    result = export(project, row_tolerance_mm=0.5)
    assert [(row["id"], row["row"]) for row in result["notes"]] == [
        (str(FIRST_NOTE + 2), 1), (str(FIRST_NOTE + 3), 1),
        (str(FIRST_NOTE + 1), 1), (str(FIRST_NOTE + 4), 2),
    ]
    assert export(project, heading_ids=[str(FIRST_NOTE + 1), str(FIRST_NOTE + 2)])[
        "error"] == "multiple_headings_in_row"


@pytest.mark.parametrize("override", [{"HorizontalAlignment": "Center"},
                                       {"VerticalAlignment": "Bottom"},
                                       {"BaseDirection": XYZ(1, 0)}])
def test_unsupported_geometry_preserves_unordered_full_inventory(project, override):
    note = TextNote(FIRST_NOTE, 1, 20, "Complete text\n二")
    for name, value in override.items():
        setattr(note, name, value)
    project.notes = [TextNote(FIRST_NOTE + 1, 1, 10, "second"), note]
    result = export(project)
    assert result["status"] == "incomplete"
    assert result["inventory_complete"] is True
    assert result["visual_order"] == "unverified"
    assert result["error"] == "unsupported_note_geometry"
    assert [row["text"] for row in result["notes"]] == ["Complete text\n二", "second"]
    assert "column" not in result["notes"][0]


@pytest.mark.parametrize("options,error", [
    ({"expected_project_uid": "other"}, "document_identity_mismatch"),
    ({"expected_view_uid": "other"}, "drafting_view_identity_mismatch"),
    ({"view_id": 123}, "invalid_element_id"),
    ({"heading_ids": ["7", "7"]}, "duplicate_heading_id"),
    ({"heading_ids": ["7"]}, "heading_not_in_view"),
    ({"column_boundaries_mm": [10, 5]}, "invalid_column_boundaries"),
    ({"row_tolerance_mm": math.inf}, "invalid_row_tolerance"),
])
def test_export_rejects_ambiguous_targets_and_bad_settings(project, options, error):
    assert export(project, **options) == {"status": "rejected", "error": error}


def test_view_identification_returns_the_exact_binding_without_exporting(project):
    assert export(project, identify_view=True, expected_view_uid=None) == {
        "status": "identified",
        "view": {"id": str(VIEW_ID), "uid": "drafting-view-uid", "name": "BEP – 配管"},
    }


def test_unavailable_view_axes_preserve_inventory(project):
    project.view.RightDirection = None
    project.notes = [TextNote(FIRST_NOTE, 1, 20, "Retained")]
    result = export(project)
    assert result["inventory_complete"] is True
    assert result["visual_order"] == "unverified"
    assert [note["text"] for note in result["notes"]] == ["Retained"]


def test_limits_report_inventory_incomplete(project):
    project.notes = [TextNote(FIRST_NOTE + index, 0, index, "") for index in range(501)]
    assert export(project) == {"status": "incomplete", "error": "note_limit",
                               "inventory_complete": False}
    project.notes = [TextNote(FIRST_NOTE + index, 0, index, "中" * 30000) for index in range(10)]
    assert export(project) == {"status": "incomplete", "error": "output_limit",
                               "inventory_complete": False}


def test_legacy_read_preserves_first_line_and_exact_note_mode(project):
    note = TextNote(FIRST_NOTE, 1, 10, "one\nline")
    project.notes = [note, TextNote(FIRST_NOTE + 1, 1, 11, "hidden", cropped=True)]
    result = run(project, {"view_id": VIEW_ID, "note_id": FIRST_NOTE})
    assert result["views"][0]["text_notes"] == [{"id": FIRST_NOTE, "first_line": "one", "paragraphs": 2}]
    assert result["note"] == {"id": FIRST_NOTE, "view_id": VIEW_ID,
                              "view": "BEP – 配管", "text": "one\nline"}
