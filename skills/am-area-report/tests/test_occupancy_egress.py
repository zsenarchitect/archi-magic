"""Exercise the public room-snapshot and calculation entry points."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def execute(tmp_path, snapshot, criteria, *, bind=True):
    room_file = tmp_path / "rooms.json"
    room_file.write_text(json.dumps(snapshot), encoding="utf-8")
    if bind:
        criteria["snapshot_sha256"] = hashlib.sha256(room_file.read_bytes()).hexdigest()
    criteria_file = tmp_path / "criteria.json"
    criteria_file.write_text(json.dumps(criteria), encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPTS / "occupancy_egress.py"), str(room_file),
                           str(criteria_file)], capture_output=True, text=True)


@pytest.fixture
def example():
    snapshot = {"kind": "occupancy-room-snapshot", "area_basis": "net_room", "phase_id": "1",
                "level_ids": ["2"], "room_count": 2, "rooms": [
                    {"id": "11", "level_id": "2", "status": "placed", "occupancy": "Office", "area_ft2": 401},
                    {"id": "12", "level_id": "2", "status": "placed", "occupancy": "Training", "area_ft2": 180},
                ]}

    def segment(identifier, kind, value, unit):
        return {"id": identifier, "kind": kind,
                "measurement": {"basis": "clear_width", "value": value, "unit": unit, "source": "Fixture"},
                "minimum": {"value": "38", "unit": "in"},
                "width_per_person": {"value": "1", "unit": "in"}, "criteria_source": "Fixture"}

    criteria = {"occupancy_factors": [
        {"occupancy": "office", "basis": "net_room", "area_per_person": "100", "unit": "ft2", "source": "Fixture"},
        {"occupancy": "training", "basis": "net_room", "area_per_person": "5.5741824", "unit": "m2", "source": "Fixture"},
    ], "segments": [segment("51", "door", "38", "in"), segment("52", "stair", "965.1", "mm"),
                    segment("53", "door", "40", "in")],
        "routes": [{"room_id": "11", "occupants": 3, "segment_ids": ["51", "52"]},
                   {"room_id": "11", "occupants": 2, "segment_ids": ["53", "51", "52"]},
                   {"room_id": "12", "occupants": 3, "segment_ids": ["51", "52"]}]}
    return snapshot, criteria


def test_hand_calculation_and_exact_width_boundary(tmp_path, example):
    result = execute(tmp_path, *example)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [room["occupants"] for room in report["rooms"]] == [5, 3]
    assert report["floors"] == [{"level_id": "2", "known_occupants": 8, "placed_rooms": 2,
                                 "excluded_rooms": 0, "unresolved_rooms": 0, "complete": True}]
    assert report["complete_width_inputs"]
    assert [(row["assigned_occupants"], row["comparison"], row["margin_mm"]) for row in report["segments"]] == [
        (8, "meets_supplied_width", "0.000000"),
        (8, "below_supplied_width", "-0.100000"),
        (2, "meets_supplied_width", "50.800000"),
    ]


def test_room_element_id_cannot_be_used_as_an_egress_segment(tmp_path, example):
    snapshot, criteria = example
    room_id = snapshot["rooms"][0]["id"]
    criteria["segments"][0]["id"] = room_id
    for route in criteria["routes"]:
        route["segment_ids"] = [room_id if value == "51" else value for value in route["segment_ids"]]

    result = execute(tmp_path, snapshot, criteria)

    assert result.returncode == 2 and not result.stdout
    assert "belongs to a room" in result.stderr


def test_ceiling_uses_exact_ratio_before_rounding(tmp_path, example):
    snapshot, criteria = example
    snapshot["rooms"][0]["area_ft2"] = "400.00000000000000000000000000001"
    report = json.loads(execute(tmp_path, snapshot, criteria).stdout)
    assert report["rooms"][0]["occupants"] == 5


@pytest.mark.parametrize("incomplete", ["unknown_occupancy", "underallocated"])
def test_incomplete_room_withholds_every_segment_verdict(tmp_path, example, incomplete):
    snapshot, criteria = example
    if incomplete == "unknown_occupancy":
        snapshot["rooms"][1]["occupancy"] = None
    else:
        criteria["routes"][0]["occupants"] = 2
    report = json.loads(execute(tmp_path, snapshot, criteria).stdout)
    assert not report["complete_width_inputs"]
    assert all(row["comparison"] == "incomplete_allocation" and row["margin_mm"] is None
               for row in report["segments"])


@pytest.mark.parametrize("invalid", ["digest", "repeated", "nominal", "units", "truncated"])
def test_invalid_evidence_cannot_emit_comparisons(tmp_path, example, invalid):
    snapshot, criteria = example
    if invalid == "repeated":
        criteria["routes"][0]["segment_ids"].append("51")
    elif invalid == "nominal":
        criteria["segments"][0]["measurement"]["basis"] = "nominal_width"
    elif invalid == "units":
        criteria["segments"][0]["measurement"]["unit"] = "cm"
    elif invalid == "truncated":
        snapshot["rooms"].pop()
    criteria["snapshot_sha256"] = "0" * 64
    result = execute(tmp_path, snapshot, criteria, bind=invalid != "digest")
    assert result.returncode == 2 and result.stdout == ""


class Id:
    def __init__(self, value):
        self.Value = value


class Level:
    Id = Id(2)


class Phase:
    Id = Id(1)


class Room:
    Area = 401.002345
    LevelId = Level.Id
    DesignOption = None

    def __init__(self, identifier, phase_id, use):
        self.Id = Id(identifier)
        self.phase = phase_id
        self.use = use

    def get_Parameter(self, key):
        if key == "phase":
            return NS(StorageType="ElementId", AsElementId=lambda: Id(self.phase))
        assert key == "occupancy"
        return NS(StorageType="String", AsString=lambda: self.use)


def test_native_query_snapshot_round_trips_unicode_and_uses_phase(monkeypatch):
    text = "Café 🙂 " + chr(92) + "u0041"
    rooms = [Room(11, 1, text), Room(12, 3, "other phase")]
    collector = NS(OfClass=lambda kind: NS(GetElementCount=lambda: 0),
                   OfCategory=lambda category: NS(WhereElementIsNotElementType=lambda: iter(rooms)))
    db = NS(FilteredElementCollector=lambda doc: collector,
            Level=Level, Phase=Phase, ElementId=Id, DesignOption=object,
            BuiltInCategory=NS(OST_Rooms="rooms"), SpatialElementBoundaryOptions=object,
            BuiltInParameter=NS(ROOM_PHASE_ID="phase", ROOM_OCCUPANCY="occupancy"),
            StorageType=NS(ElementId="ElementId", String="String"))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    doc = NS(IsFamilyDocument=False, GetElement=lambda identifier: {1: Phase(), 2: Level()}.get(identifier.Value))
    result = runpy.run_path(str(SCRIPTS / "area_report.py"), init_globals={
        "doc": doc, "inputs": {"occupancy_snapshot": True, "phase_id": "1", "level_ids": ["2"]},
    })["result"]
    snapshot = json.loads(result["snapshot_json"])
    assert result["snapshot_json"].isascii()
    assert snapshot["room_count"] == 1
    assert snapshot["rooms"] == [
        {"id": "11", "level_id": "2", "status": "placed", "occupancy": text, "area_ft2": 401.002345}
    ]
