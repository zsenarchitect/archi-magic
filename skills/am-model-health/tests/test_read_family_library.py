import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import discover, manifest_digest
from read_family_library import WorkerUnresolved, read_family
import stage_family_library as staging

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Use the POSIX staging fixture with Revit API doubles.")


class FamilyType:
    Name = "600 × 900"

    def HasValue(self, parameter):
        return parameter.value is not None

    def AsString(self, parameter):
        return parameter.value

    AsInteger = AsDouble = AsElementId = AsString


def parameter(name, number, storage, value, instance=False):
    definition = SimpleNamespace(Name=name, GetDataType=lambda: SimpleNamespace(TypeId=storage))
    return SimpleNamespace(Definition=definition, Id=SimpleNamespace(Value=number), IsShared=False,
                           IsInstance=instance, StorageType=storage, value=value)


class Worker:
    VersionNumber = "2026"
    VersionBuild = "fixture"

    def __init__(self):
        self.Documents = []
        self.opened = []
        self.closed = []
        self.close_ok = True
        self.on_close = lambda: None

    def OpenDocumentFile(self, path):
        self.opened.append(path)

        def close(save):
            self.closed.append(save)
            self.on_close()
            if self.close_ok:
                self.Documents.remove(document)
            return self.close_ok

        family = SimpleNamespace(Name="Fixture Family",
                                 FamilyCategory=SimpleNamespace(Id=SimpleNamespace(Value=-2000011), Name="Walls"))
        manager = SimpleNamespace(Parameters=[
            parameter("Manufacturer", 42, "String", "Fixture"),
            parameter("Width", 43, "Double", 2.0),
            parameter("Width", 44, "Double", 3.0, instance=True),
            parameter("Width", 45, "Integer", None),
        ], Types=[FamilyType()])
        document = SimpleNamespace(IsFamilyDocument=True, IsModified=False, PathName=path,
                                   OwnerFamily=family, FamilyManager=manager, Close=close)
        self.Documents.append(document)
        return document


@pytest.fixture
def staged(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "library"
    destination = tmp_path.resolve() / "staging"
    root.mkdir()
    destination.mkdir()
    (root / "Fixture.rfa").write_bytes(b"reviewed family")
    (root / "Fixture.txt").write_bytes(b"reviewed catalog")
    monkeypatch.setattr(staging, "free_bytes", lambda parent: 10 * staging.HEADROOM)
    receipt = staging.stage(discover(str(root)), ["Fixture.rfa"], str(destination))
    pair = Path(receipt["workspace"]) / receipt["pairs"][0]["directory"]
    return receipt, pair


def read(worker, receipt, source="Fixture.rfa"):
    ui = SimpleNamespace(Application=worker, ActiveUIDocument=None)
    return read_family(ui, receipt, source, ["Manufacturer", "Width"])


def test_read_embedded_types_and_close_without_saving(staged):
    receipt, pair = staged
    worker = Worker()
    result = read(worker, receipt)
    assert worker.opened == [str(pair / "Fixture.rfa")]
    assert worker.closed == [False]
    assert worker.Documents == []
    assert result["category"] == {"id": "-2000011", "name": "Walls"}
    assert result["catalog_coverage"] == "pending"
    assert result["staging_sha256"] == receipt["staging_sha256"]
    assert result["embedded_types"] == [{"name": "600 × 900", "values": {
        "42": {"state": "value", "value": "Fixture"},
        "43": {"state": "value", "value": 2.0},
        "45": {"state": "unset"},
    }}]
    digest = result.pop("metadata_sha256")
    assert digest == manifest_digest(result)


@pytest.mark.parametrize("mutation", ["file", "catalog", "receipt", "selection", "directory"])
def test_reject_invalid_staging_before_revit_opens(staged, mutation):
    receipt, pair = staged
    worker = Worker()
    source = "Fixture.rfa"
    if mutation == "file":
        (pair / source).write_bytes(b"changed")
    elif mutation == "catalog":
        (pair / "Fixture.txt").unlink()
    elif mutation == "receipt":
        receipt["discovery_sha256"] = "0" * 64
    elif mutation == "selection":
        source = "fixture.rfa"
    else:
        pair.rename(pair.with_suffix(".old"))
        pair.mkdir()
    with pytest.raises(ValueError, match="staged|staging"):
        read(worker, receipt, source)
    assert worker.opened == []


def test_reject_pair_changed_by_the_time_revit_closes(staged):
    receipt, pair = staged
    worker = Worker()
    worker.on_close = lambda: (pair / "Fixture.txt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="staged file bytes"):
        read(worker, receipt)
    assert worker.closed == [False]
    assert worker.Documents == []


@pytest.mark.parametrize("condition", ["open_at_start", "unexpected_document", "failed_close"])
def test_stop_worker_when_document_state_is_unresolved(staged, condition):
    receipt, _ = staged
    worker = Worker()
    other = object()
    if condition == "open_at_start":
        worker.Documents.append(other)
    elif condition == "unexpected_document":
        worker.on_close = lambda: worker.Documents.append(other)
    else:
        worker.close_ok = False
    with pytest.raises(WorkerUnresolved):
        read(worker, receipt)
    assert worker.closed == ([] if condition == "open_at_start" else [False])
    assert len(worker.Documents) == 1
