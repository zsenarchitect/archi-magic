import hashlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_verified_spec_family_rfa.py"
MODULES = ("probe_spec_family_box", "probe_spec_family_placement", "spec_family_box_constraints",
           "spec_family_clearance", "spec_family_source_parameters")


def run_verify(monkeypatch, tmp_path, fault=None):
    file = tmp_path / "Equipment.rfa"
    file.write_bytes(b"reviewed RFA fixture")
    dimensions = {"width": "304.8", "depth": "609.6", "height": "914.4"}
    spec = json.dumps({
        "kind": "confirmed-family-specification", "model_write_approved": False,
        "product": "Unit", "variant": "Example", "pdf": {"file": "Unit.pdf", "sha256": "a" * 64},
        "dimensions_mm": dimensions, "clearance_mm": {}, "connectors": {"connector_service": "Supply"},
        "parameters": {}, "source_rows": [
            {"name": name, "value": value, "unit": "mm", "source": {"page": 1}}
            for name, value in dimensions.items()
        ] + [{"name": "connector_service", "value": "Supply", "source": {"page": 2}}],
    })
    lengths = {"Equipment Width": 1, "Equipment Depth": 2, "Equipment Height": 3}
    texts = {
        "PDF Product": "Unit", "PDF Variant": "Example", "PDF Source File": "Unit.pdf",
        "PDF Source Pages": "1, 2", "PDF Source SHA256": "a" * 64, "PDF Connector service": "Supply",
    }
    if fault == "type":
        lengths["Equipment Depth"] = 2.5
    if fault == "provenance":
        texts["PDF Source Pages"] = "1"
    parameters = []
    for group, values in (("length", lengths), ("text", texts)):
        parameters.extend(NS(Definition=NS(Name=name, GetDataType=lambda group=group: group),
                             IsInstance=False) for name in values)
    family_type = NS(Name="Reviewed", AsDouble=lambda parameter: lengths[parameter.Definition.Name],
                     AsString=lambda parameter: texts[parameter.Definition.Name])

    class Extrusion:
        IsSolid = True
        Subcategory = None

        def get_BoundingBox(self, _):
            return NS(Enabled=True, Min=NS(X=0, Y=0, Z=0),
                      Max=NS(X=1, Y=2, Z=4 if fault == "geometry" else 3),
                      Transform=NS(OfPoint=lambda point: point))

    def file_info(path):
        assert Path(path) == file
        return NS(GetDocumentVersion=lambda: NS(VersionGUID="version", NumberOfSaves=(
            2 if fault == "disk_version" else 1)), Dispose=lambda: None)

    db = NS(
        ElementId=lambda value: NS(Value=value), FamilyPlacementType=NS(OneLevelBased="level"),
        BuiltInCategory=NS(OST_GenericModel=-1), SpecTypeId=NS(Length="length", String=NS(Text="text")),
        FilteredElementCollector=lambda _: NS(OfClass=lambda cls: [Extrusion()]), Extrusion=Extrusion,
        BasicFileInfo=NS(Extract=file_info), Document=NS(GetDocumentVersion=lambda _: NS(
            VersionGUID="version", NumberOfSaves=1)), XYZ=lambda x, y, z: NS(X=x, Y=y, Z=z),
    )
    doc = NS(
        IsFamilyDocument=True, IsModified=fault == "unsaved", PathName=str(file),
        Application=NS(VersionNumber="2026", VersionBuild="fixture-build"),
        OwnerFamily=NS(UniqueId="family-uid", FamilyPlacementType="level", FamilyCategory=NS(Id=NS(Value=-1))),
        FamilyManager=NS(CurrentType=family_type, Types=[family_type], Parameters=parameters),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    for module in MODULES:
        monkeypatch.delitem(sys.modules, module, raising=False)
    try:
        return runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": {
            "family_name": "Equipment", "type_name": "Reviewed", "specification_json": spec,
            "specification_sha256": hashlib.sha256(spec.encode("utf-8")).hexdigest(),
        }})["result"]
    finally:
        for module in MODULES:
            sys.modules.pop(module, None)


def test_saved_rfa_readback_binds_geometry_and_source_text_to_exact_bytes(monkeypatch, tmp_path):
    result = run_verify(monkeypatch, tmp_path)
    assert result["status"] == "verified", result
    assert result["rfa_sha256"] == hashlib.sha256(b"reviewed RFA fixture").hexdigest()
    assert result["rfa_version"] == {"version_guid": "version", "number_of_saves": 1}
    assert result["source_parameters"]["PDF Source Pages"] == "1, 2"
    assert result["equipment_geometry"] == {"min_ft": [0, 0, 0], "max_ft": [1, 2, 3]}
    assert result["pdf_source_sha256"] == "a" * 64


@pytest.mark.parametrize(("fault", "reason"), [
    ("unsaved", "without making any changes"), ("disk_version", "different Revit document versions"),
    ("type", "dimensions differ"), ("provenance", "provenance differs"), ("geometry", "bounds differ"),
])
def test_readback_refuses_unproven_disk_identity_or_family_values(monkeypatch, tmp_path, fault, reason):
    result = run_verify(monkeypatch, tmp_path, fault)
    assert result["status"] == "stopped" and reason in result["message"]
    assert "rfa_sha256" not in result
