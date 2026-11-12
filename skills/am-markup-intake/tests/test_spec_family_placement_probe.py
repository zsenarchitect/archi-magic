import hashlib
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_spec_family_placement.py"


def setup(monkeypatch, tmp_path, fault):
    class Level(NS):
        pass

    class Family(NS):
        pass

    class Symbol(NS):
        pass

    class LocationPoint(NS):
        pass

    states = NS(Uninitialized="new", Started="started", RolledBack="rolled_back")
    level = Level(Id=NS(Value=1), UniqueId="level", ProjectElevation=50.0)
    records = {1: level}
    calls = []
    copies = []
    dimensions = {"width": 1.0, "depth": 2.0, "height": 3.0}
    metadata = {"PDF Source File": "Equipment.pdf", "PDF Source Pages": "1, 2", "PDF Source SHA256": "a" * 64,
                "PDF Product": "Equipment", "PDF Variant": "Standard", "PDF Connector service": "Supply"}
    monkeypatch.syspath_prepend(str(SCRIPT.parent))

    class Transaction:
        def __init__(self, *_):
            self.state = states.Uninitialized
            calls.append("transaction")

        def Start(self):
            self.state = states.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            self.state = states.RolledBack
            calls.append("rollback")
            if fault not in ("retained", "wrong_retained"):
                for key in list(records):
                    if key != 1:
                        del records[key]
            return self.state

    def load(path, name):
        path = Path(path)
        assert path != source and path.name == source.name and path.read_bytes() == b"fixture RFA"
        copies.append(path)
        calls.append(("load", name))
        if fault == "load_false":
            return False
        family = Family(Id=NS(Value=2), UniqueId="family", FamilyPlacementType="level", Name=(
            "Wrong" if fault in ("wrong_name", "wrong_retained") else "Equipment"),
            FamilyCategory=NS(Id=NS(Value=-1)), GetFamilySymbolIds=lambda: [NS(Value=3)])
        symbol = Symbol(Id=NS(Value=3), UniqueId="symbol", IsActive=False, Name="Reviewed")
        symbol.Activate = lambda: calls.append("activate")
        def parameters(name):
            if name in metadata:
                actual = metadata[name]
                if (fault, name) == ("provenance", "PDF Source SHA256"):
                    actual = "b" * 64
                if (fault, name) == ("source_pages", "PDF Source Pages"):
                    actual = "3"
                return [NS(StorageType="string", AsString=lambda: actual,
                           Definition=NS(GetDataType=lambda: "text"))]
            actual = dimensions[name.removeprefix("Equipment ").lower()]
            if (fault, name) == ("width", "Equipment Width"):
                actual += 0.5
            return [NS(StorageType="double", AsDouble=lambda: actual,
                       Definition=NS(GetDataType=lambda: "length"))]

        symbol.GetParameters = parameters
        records.update({2: family, 3: symbol})
        return True

    def place(point, symbol, native_level, structure):
        assert native_level is level and structure == "non-structural" and symbol.UniqueId == "symbol"
        calls.append(("place", point.X, point.Y, point.Z))
        if fault == "late":
            raise RuntimeError("after load")
        instance = NS(Id=NS(Value=4), UniqueId="instance", Symbol=symbol, LevelId=level.Id,
                      Location=LocationPoint(Point=NS(X=point.X, Y=point.Y, Z=(
                          point.Z if fault == "world_z" else point.Z + level.ProjectElevation))))
        records[4] = instance
        return instance

    def collector(_):
        return NS(OfClass=lambda cls: [entry for entry in records.values() if isinstance(entry, cls)])

    db = NS(
        Transaction=Transaction, TransactionStatus=states, ElementId=lambda value: NS(Value=value),
        Level=Level, Family=Family, FamilySymbol=Symbol, LocationPoint=LocationPoint,
        FilteredElementCollector=collector, FamilyPlacementType=NS(OneLevelBased="level"),
        BuiltInCategory=NS(OST_GenericModel=-1), Element=NS(Name=NS(GetValue=lambda symbol: symbol.Name)),
        StorageType=NS(String="string", Double="double"), SpecTypeId=NS(Length="length", String=NS(Text="text")), XYZ=lambda x, y, z: NS(X=x, Y=y, Z=z), Structure=NS(StructuralType=NS(NonStructural="non-structural")),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    doc = NS(
        Application=NS(VersionNumber="2026", VersionBuild="fixture-build"), IsFamilyDocument=False,
        IsReadOnly=False, IsWorkshared=False, IsModelInCloud=False, PathName="fixture.rvt",
        ProjectInformation=NS(UniqueId="project"), GetElement=lambda id_: records.get(id_.Value),
        LoadFamilySymbol=load, Create=NS(NewFamilyInstance=place), Regenerate=lambda: calls.append("regenerate"),
    )
    source = tmp_path / "equipment.rfa"
    source.write_bytes(b"fixture RFA")
    query = runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": {
        "mode": "read", "level_id": "1", "family_name": "Equipment",
    }})["result"]
    assert query["status"] == "ok"
    args = {
        "baseline": query["baseline"], "type_name": "Reviewed", "rfa_path": str(source),
        "rfa_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "placement_level_relative_ft": [3, 4, 2.5],
    }
    args["verified_rfa"] = {"status": "verified", "kind": "verified-spec-family-rfa",
                            "family_name": "Equipment", "type_name": "Reviewed",
                            "rfa_sha256": args["rfa_sha256"], "pdf_source_sha256": "a" * 64,
                            "revit_build": "different-build" if fault == "receipt" else "fixture-build",
                            "dimensions_ft": dimensions, "source_parameters": metadata}
    if fault == "incomplete_receipt":
        del args["verified_rfa"]["dimensions_ft"]
    if fault == "digest":
        source.write_bytes(b"other file")
    if fault == "collision":
        records[5] = Family(Id=NS(Value=5), UniqueId="other", Name="equipment")
    return doc, args, calls, records, copies


def test_placement_uses_pinned_copy_and_reads_level_relative_world_position(monkeypatch, tmp_path):
    doc, args, calls, records, copies = setup(monkeypatch, tmp_path, None)
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": args})["result"]
    assert result["status"] == "measured", result
    assert result["placement"]["position_ft"] == [3, 4, 52.5]
    assert result["placement"]["dimensions_ft"] == args["verified_rfa"]["dimensions_ft"]
    assert result["placement"]["source_parameters"] == args["verified_rfa"]["source_parameters"]
    assert result["rollback_verified"] and list(records) == [1]
    assert calls == ["transaction", ("load", "Reviewed"), "activate", "regenerate",
                     ("place", 3, 4, 2.5), "regenerate", "rollback"]
    assert len(copies) == 1 and not copies[0].exists()


@pytest.mark.parametrize(("fault", "status"), [
    ("load_false", "stopped"), ("late", "stopped"), ("world_z", "stopped"), ("wrong_name", "stopped"),
    ("retained", "transaction_unresolved"), ("wrong_retained", "transaction_unresolved"),
    ("provenance", "stopped"), ("source_pages", "stopped"), ("width", "stopped"),
])
def test_late_failure_or_wrong_family_still_requires_verified_rollback(monkeypatch, tmp_path, fault, status):
    doc, args, calls, records, copies = setup(monkeypatch, tmp_path, fault)
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": args})["result"]
    assert result["status"] == status
    assert calls[-1] == "rollback"
    if fault in ("provenance", "source_pages", "width"):
        assert not any(isinstance(call, tuple) and call[0] == "place" for call in calls)
    assert not copies[0].exists()
    if status == "stopped":
        assert result["rollback_verified"] and list(records) == [1]


@pytest.mark.parametrize(("fault", "message"), [
    ("digest", "reviewed SHA-256"), ("collision", "already exists"), ("receipt", "verification receipt"),
    ("incomplete_receipt", "lacks complete dimensions"),
])
def test_changed_file_or_existing_family_stops_before_load(monkeypatch, tmp_path, fault, message):
    doc, args, calls, _, copies = setup(monkeypatch, tmp_path, fault)
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": args})["result"]
    assert result["status"] == "stopped" and message in result["message"]
    assert not calls and not copies
