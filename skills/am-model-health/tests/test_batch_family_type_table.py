import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from batch_family_type_table import bind_table
from family_library_discovery import discover, manifest_digest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Use the ordinary POSIX staging fixture.")


@pytest.fixture
def discovery(tmp_path):
    root = tmp_path.resolve() / "library"
    root.mkdir()
    (root / "A").mkdir()
    (root / "A" / "Fixture.rfa").write_bytes(b"reviewed family")
    (root / "A" / "Fixture.txt").write_bytes(b"reviewed catalog")
    (root / "B.rfa").write_bytes(b"another family")
    return discover(str(root))


def test_cli_binds_exact_names_units_and_source_pairs(tmp_path, discovery):
    table = (
        'source_path,seed_type,type,Width[mm],Ratio[1]\r\n'
        'A/Fixture.rfa,"Seed, tall","Wide ""A""",1200.50,.75\r\n'
        'B.rfa,Existing,Existing,450,1\r\n'
    ).encode("utf-8-sig")
    table_path = tmp_path.resolve() / "types.csv"
    manifest = tmp_path.resolve() / "discovery.json"
    table_path.write_bytes(table)
    manifest.write_text(json.dumps(discovery))
    response = subprocess.run([sys.executable, str(SCRIPTS / "batch_family_type_table.py"),
                               "--table", str(table_path), "--manifest", str(manifest)],
                              capture_output=True, text=True, check=True)
    result = json.loads(response.stdout)
    assert result["discovery_sha256"] == discovery["manifest_sha256"]
    assert result["table_sha256"] == hashlib.sha256(table).hexdigest()
    assert result["sources"] == discovery["files"]
    assert result["rows"] == [
        {"line": 2, "source_path": "A/Fixture.rfa", "seed_type": "Seed, tall", "type": 'Wide "A"',
         "sizes": [{"parameter": "Width", "unit": "mm", "value": "1200.50"},
                   {"parameter": "Ratio", "unit": "1", "value": ".75"}]},
        {"line": 3, "source_path": "B.rfa", "seed_type": "Existing", "type": "Existing",
         "sizes": [{"parameter": "Width", "unit": "mm", "value": "450"},
                   {"parameter": "Ratio", "unit": "1", "value": "1"}]},
    ]
    assert result.pop("request_sha256") == manifest_digest(result)


@pytest.mark.parametrize(("table", "reason"), [
    ("source_path,seed_type,type,Width[kg]\nB.rfa,Seed,New,3", "explicit unit"),
    ("source_path,seed_type,type,Width[mm],width[ft]\nB.rfa,Seed,New,3,4", "duplicate name"),
    ("source_path,seed_type,type,Width[mm]\nB.rfa,Seed,New,NaN", "finite decimal"),
    ("source_path,seed_type,type,Width[mm]\nB.rfa,Seed,New,2\nB.rfa,Other,new,3", "repeats"),
    ("source_path,seed_type,type,Width[mm]\nb.rfa,Seed,New,2", "unavailable"),
    ("source_path,seed_type,type,Width[mm]\n../B.rfa,Seed,New,2", "root-relative"),
])
def test_rejects_ambiguous_unbound_or_malformed_rows(discovery, table, reason):
    with pytest.raises(ValueError, match=reason):
        bind_table(table.encode(), discovery)


def test_read_and_preview_skip_existing_and_block_unqualified_sibling(discovery, monkeypatch):
    from types import SimpleNamespace
    import batch_family_type_preview as preview
    from read_batch_family_types import read_project

    table = b"""source_path,seed_type,type,Width[ft]
A/Fixture.rfa,Seed,Existing,4
A/Fixture.rfa,Seed,Missing sibling,5
B.rfa,Seed,New type,6
"""
    request = bind_table(table, discovery)
    field = {"id": "42", "name": "Width", "storage": "Double", "data_type": "length", "shared_guid": None,
             "read_only": False, "formula_driven": False, "reporting": False}
    sources = {row["path"]: row for row in request["sources"]}

    def read_worker(path):
        source = sources[path]
        return {"metadata": {"source_path": path, "discovery_sha256": request["discovery_sha256"],
                             "rfa": source["rfa"], "catalog": source["catalog"], "revit_year": "2026",
                             "revit_build": "fixture", "requested_parameters": ["Width"],
                             "family": "Fixture A" if path.startswith("A/") else "Fixture B",
                             "category": {"id": "-1000"}, "embedded_types": [{"name": "Seed"}],
                             "catalog_types": [], "catalog_coverage": "complete" if source["catalog"] else "absent",
                             "embedded_type_coverage": "complete", "catalog_blocked_embedded": [],
                             "parameters": [field]}}

    monkeypatch.setattr(preview, "collect_worker", read_worker)
    lookup = preview.prepare(request, list(sources))

    class Spec:
        def __init__(self, name):
            self.TypeId = name

        def Equals(self, other):
            return self.TypeId == other.TypeId

    class Family:
        Name = "Fixture A"
        FamilyCategory = SimpleNamespace(Id=SimpleNamespace(Value=-1000))
        Id = SimpleNamespace(Value=40)
        UniqueId = "family-40"

        def GetFamilySymbolIds(self):
            return [41]

    family = Family()

    class Symbol:
        Name = "Existing"
        Id = SimpleNamespace(Value=41)
        UniqueId = "symbol-41"
        Family = family

        def GetParameters(self, name):
            assert name == "Width"
            definition = SimpleNamespace(GetDataType=lambda: Spec("length"))
            return [SimpleNamespace(Definition=definition, StorageType="Double", HasValue=True, AsDouble=lambda: 3)]

    document = SimpleNamespace(PathName="/fixture/Project.rvt", IsFamilyDocument=False, IsReadOnly=False,
                               IsWorkshared=False, IsModelInCloud=False, IsModified=False,
                               ProjectInformation=SimpleNamespace(UniqueId="project"),
                               Application=SimpleNamespace(VersionNumber="2026", VersionBuild="fixture"),
                               GetElement={41: Symbol()}.get)
    db = SimpleNamespace(Family=Family, FamilySymbol=Symbol, ForgeTypeId=Spec,
                         FilteredElementCollector=lambda _: SimpleNamespace(OfClass=lambda _: [family]),
                         SpecTypeId=SimpleNamespace(Number=Spec("number"), Length=Spec("length")),
                         UnitTypeId=SimpleNamespace(Feet="ft"),
                         UnitUtils=SimpleNamespace(IsValidUnit=lambda spec, unit: unit == "ft",
                                                   ConvertToInternalUnits=lambda value, unit: value))
    snapshot = read_project(document, db, lookup)
    result = preview.build_preview(lookup, {"result": {"snapshot_json": json.dumps(snapshot)}})
    outcomes = result["review"]["outcomes"]
    assert [row["status"] for row in outcomes] == ["skip_existing", "blocked", "ready_for_derivation"]
    assert outcomes[0]["existing"]["symbol_uid"] == "symbol-41"
    assert outcomes[2]["sizes"][0]["internal_value"] == 6.0
    assert "project 3.0, requested 4.0" in result["after"][0][-1]
    assert result["blocked"] is True

    lookup["rows"][2]["sizes"][0]["unit"] = "1"
    lookup.pop("lookup_sha256")
    lookup["lookup_sha256"] = manifest_digest(lookup)
    snapshot = read_project(document, db, lookup)
    assert "not dimensionless" in snapshot["rows"][2]["issues"][0]
    assert preview.build_preview(lookup, snapshot)["review"]["outcomes"][2]["status"] == "blocked"


@pytest.fixture
def derivation_input(discovery, tmp_path, monkeypatch):
    import prepare_batch_family_derivation as derivation
    import stage_family_library as staging
    from batch_family_type_preview import build_preview

    stage_parent = tmp_path.resolve() / "staging"
    work_parent = tmp_path.resolve() / "work"
    stage_parent.mkdir()
    work_parent.mkdir()
    monkeypatch.setattr(staging, "free_bytes", lambda parent: 10 * staging.HEADROOM)
    monkeypatch.setattr(derivation, "free_bytes", lambda parent: 10 * staging.HEADROOM)
    path = "A/Fixture.rfa"
    request = bind_table(b"source_path,seed_type,type,Width[ft]\nA/Fixture.rfa,Seed,New,4\n", discovery)
    field = {"id": "42", "name": "Width", "storage": "Double", "data_type": "length", "shared_guid": None,
             "read_only": False, "formula_driven": False, "reporting": False}
    sizes = [dict(request["rows"][0]["sizes"][0], definition=field)]
    rows = [{"line": 2, "source_path": path, "seed_type": "Seed", "type": "New",
             "family": "Fixture", "category": {"id": "-1000"}, "build": "fixture", "sizes": sizes, "issues": []}]
    lookup = {"kind": "batch-family-type-lookup", "version": 1, "request": request, "rows": rows}
    lookup["lookup_sha256"] = manifest_digest(lookup)
    read = {"kind": "batch-family-type-project-read", "version": 1, "lookup_sha256": lookup["lookup_sha256"],
            "project": {"project_uid": "fixture"}, "revit_year": "2026", "revit_build": "fixture",
            "rows": [{"line": 2, "existing": None, "sizes": [dict(sizes[0], internal_value=4.0)],
                      "differences": [], "issues": []}]}
    preview = build_preview(lookup, read)
    staged = staging.stage(discovery, [path], str(stage_parent))
    return lookup, read, preview, staged, path, str(work_parent)


def test_working_copy_preserves_source_and_excludes_companion(derivation_input):
    from prepare_batch_family_derivation import create_working_copy, verified_working_copy

    lookup, _, preview, staged, source_path, _ = derivation_input
    result = create_working_copy(*derivation_input)
    assert result["preview_sha256"] == manifest_digest(preview)
    assert result["rows"][0]["sizes"][0]["internal_value"] == 4.0
    protected = Path(staged["workspace"]) / staged["pairs"][0]["directory"]
    original = Path(lookup["request"]["root"]) / source_path
    with verified_working_copy(result) as copy:
        assert copy.name == original.name
        assert {item.name for item in copy.parent.iterdir()} == {"Fixture.rfa"}
        assert copy.read_bytes() == original.read_bytes()
        assert copy.stat().st_ino != (protected / copy.name).stat().st_ino
    copy.with_suffix(".txt").write_bytes(b"unexpected catalog")
    with pytest.raises(ValueError, match="unexpected files"):
        with verified_working_copy(result):
            pytest.fail("Unreviewed sidecar reached the native handoff.")
    assert (protected / "Fixture.txt").read_bytes() == b"reviewed catalog"
    assert original.read_bytes() == b"reviewed family"


def test_stale_library_source_and_changed_preview_cannot_create_a_copy(derivation_input):
    from prepare_batch_family_derivation import create_working_copy

    lookup, read, preview, staged, source_path, destination = derivation_input
    with pytest.raises(ValueError, match="matching unblocked preview"):
        create_working_copy(lookup, read, dict(preview, title="Changed"), staged, source_path, destination)
    (Path(lookup["request"]["root"]) / source_path).write_bytes(b"changed RFA")
    with pytest.raises(ValueError, match="source, catalog, or ancestry changed"):
        create_working_copy(*derivation_input)
    assert list(Path(destination).iterdir()) == []


def test_existing_only_does_not_create_working_copy(derivation_input):
    from batch_family_type_preview import build_preview
    from prepare_batch_family_derivation import create_working_copy

    lookup, read, _, staged, source_path, destination = derivation_input
    read["rows"][0]["existing"] = {"family_id": "40", "symbol_id": "41"}
    preview = build_preview(lookup, read)
    result = create_working_copy(lookup, read, preview, staged, source_path, destination)
    assert result["status"] == "no_derivation_needed"
    assert list(Path(destination).iterdir()) == []


def test_working_family_read_binds_exact_seed_and_writable_definition(derivation_input):
    from types import SimpleNamespace
    from prepare_batch_family_derivation import create_working_copy
    from read_batch_family_derivation import read_working_family

    plan = create_working_copy(*derivation_input)
    family_type = SimpleNamespace(Name="Seed", HasValue=lambda _: True, AsDouble=lambda _: 3.0,
                                  AsString=lambda _: None, AsInteger=lambda _: None, AsElementId=lambda _: None)
    parameter = SimpleNamespace(Id=SimpleNamespace(Value=42), IsShared=False, StorageType="Double",
                                Definition=SimpleNamespace(Name="Width", GetDataType=lambda: SimpleNamespace(TypeId="length")),
                                IsReadOnly=False, IsDeterminedByFormula=False, IsReporting=False, IsInstance=False)
    family = SimpleNamespace(Name="Fixture", FamilyCategory=SimpleNamespace(Id=SimpleNamespace(Value=-1000)))
    document = SimpleNamespace(PathName=str(Path(plan["workspace"]) / "Fixture.rfa"), IsFamilyDocument=True,
                               IsReadOnly=False, IsModified=False, IsWorkshared=False, IsModelInCloud=False,
                               IsModifiable=False, Application=SimpleNamespace(VersionNumber="2026", VersionBuild="fixture"),
                               OwnerFamily=family,
                               FamilyManager=SimpleNamespace(Parameters=[parameter], Types=[family_type],
                                                             CurrentType=family_type))
    record = read_working_family(document, plan)
    assert record["derivation_sha256"] == plan["derivation_sha256"]
    assert record["state"]["types"] == {"Seed": {"42": {"state": "value", "value": 3.0}}}
    assert record["state"]["current_type"] == "Seed"
    family_type.Name = "Different seed"
    with pytest.raises(ValueError, match="reviewed seed is missing"):
        read_working_family(document, plan)
    family_type.Name = "Seed"
    parameter.IsDeterminedByFormula = True
    parameter.Formula = "Height / 2"
    with pytest.raises(ValueError, match="changed or is not writable"):
        read_working_family(document, plan)
    document.PathName = str(Path(derivation_input[0]["request"]["root"]) / plan["source"]["path"])
    with pytest.raises(ValueError, match="exact saved writable family copy"):
        read_working_family(document, plan)
