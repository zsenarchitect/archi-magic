import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import discover, manifest_digest
from read_family_library import WorkerUnresolved, collect_worker, prepare_worker, read_family, run_worker
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

        family = SimpleNamespace(Name="Fixture Family", FamilyPlacementType="OneLevelBased",
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


def stage_fixture(tmp_path, monkeypatch, *, catalog=True):
    root = tmp_path.resolve() / "library"
    destination = tmp_path.resolve() / "staging"
    root.mkdir()
    destination.mkdir()
    (root / "Fixture.rfa").write_bytes(b"reviewed family")
    if catalog:
        (root / "Fixture.txt").write_bytes(catalog if isinstance(catalog, bytes) else b"reviewed catalog")
    monkeypatch.setattr(staging, "free_bytes", lambda parent: 10 * staging.HEADROOM)
    receipt = staging.stage(discover(str(root)), ["Fixture.rfa"], str(destination))
    pair = Path(receipt["workspace"]) / receipt["pairs"][0]["directory"]
    return receipt, pair


@pytest.fixture
def staged(tmp_path, monkeypatch):
    return stage_fixture(tmp_path, monkeypatch)

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


def test_worker_handoff_binds_build_and_cannot_be_replayed(staged):
    receipt, _ = staged
    directory = prepare_worker(receipt, "Fixture.rfa", ["Width"], "2026", "fixture", "pyrevit-fixture", thumbnails=False)
    worker = Worker()
    ui = SimpleNamespace(Application=worker, ActiveUIDocument=None)

    with pytest.raises(ValueError, match="build differs"):
        run_worker(ui, "unexpected-build", str(directory))
    assert not worker.opened
    assert not (directory / "worker.started").exists()
    with pytest.raises(FileNotFoundError):
        collect_worker(str(directory))

    run_worker(ui, "pyrevit-fixture", str(directory))
    report = collect_worker(str(directory))
    assert report["runtime"]["pyrevit_build"] == "pyrevit-fixture"
    assert report["metadata"]["revit_build"] == "fixture"
    assert report["metadata"]["requested_parameters"] == ["Width"]
    assert report["metadata"]["embedded_types"] == [{"name": "600 × 900", "values": {
        "43": {"state": "value", "value": 2.0},
        "45": {"state": "unset"},
    }}]
    assert worker.closed == [False] and worker.Documents == []
    with pytest.raises(FileExistsError):
        run_worker(ui, "pyrevit-fixture", str(directory))
    assert len(worker.opened) == 1


def test_collect_rejects_partial_receipt_and_changed_pair(staged):
    receipt, pair = staged
    directory = prepare_worker(receipt, "Fixture.rfa", [], "2026", "fixture", "pyrevit-fixture", thumbnails=False)
    worker = Worker()
    ui = SimpleNamespace(Application=worker, ActiveUIDocument=None)
    run_worker(ui, "pyrevit-fixture", str(directory))

    receipt_path = directory / "receipt.json"
    complete = receipt_path.read_bytes()
    receipt_path.write_bytes(complete[:-1])
    with pytest.raises(ValueError):
        collect_worker(str(directory))

    receipt_path.write_bytes(complete)
    (pair / "Fixture.txt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="staged file bytes"):
        collect_worker(str(directory))


def test_unresolved_close_cannot_publish_worker_receipt(staged):
    receipt, _ = staged
    directory = prepare_worker(receipt, "Fixture.rfa", [], "2026", "fixture", "pyrevit-fixture", thumbnails=False)
    worker = Worker()
    worker.close_ok = False
    ui = SimpleNamespace(Application=worker, ActiveUIDocument=None)
    with pytest.raises(WorkerUnresolved):
        run_worker(ui, "pyrevit-fixture", str(directory))
    assert (directory / "worker.started").is_file()
    with pytest.raises(FileNotFoundError):
        collect_worker(str(directory))


def fixture_png(red):
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
            + chunk(b"IEND", b""))


def test_collect_binds_thumbnail_bytes_to_exact_type_and_source(tmp_path, monkeypatch):
    from family_library_thumbnails import png_record

    receipt, pair = stage_fixture(tmp_path, monkeypatch, catalog=False)
    directory = prepare_worker(receipt, "Fixture.rfa", [], "2026", "fixture", "pyrevit-fixture")
    worker = Worker()
    ui = SimpleNamespace(Application=worker, ActiveUIDocument=None)
    data = fixture_png(100)
    image = png_record(data)
    row = {"type": "600 × 900", "status": "available", "image": image}

    def thumbnail_reader(current_ui, path, metadata, deadline):
        assert current_ui is ui and worker.Documents == []
        assert path == pair / "Fixture.rfa"
        assert metadata["family"] == "Fixture Family"
        return [row], {image["file"]: data}

    run_worker(ui, "pyrevit-fixture", str(directory), thumbnail_reader)
    assert collect_worker(str(directory))["thumbnails"] == [row]
    (directory / image["file"]).write_bytes(fixture_png(101))
    with pytest.raises(ValueError, match="thumbnail does not match"):
        collect_worker(str(directory))


def test_render_loads_exact_symbol_and_preserves_missing_preview():
    from family_library_thumbnails import render_thumbnails
    from time import monotonic

    status = SimpleNamespace(Started=1, Committed=2, RolledBack=3)
    transactions = []
    loads = []
    symbols = {}

    class Transaction:
        def __init__(self, scratch, label):
            self.status = None
            self.disposed = False
            transactions.append(self)

        def Start(self):
            self.status = status.Started
            return self.status

        def Commit(self):
            self.status = status.Committed
            return self.status

        def RollBack(self):
            self.status = status.RolledBack
            return self.status

        def GetStatus(self):
            return self.status

        def Dispose(self):
            self.disposed = True

    family = SimpleNamespace(Name="Fixture Family",
                             FamilyCategory=SimpleNamespace(Id=SimpleNamespace(Value=-2000011)))

    def load_symbol(path, name):
        loads.append((path, name))
        symbol = SimpleNamespace(Name=name, Family=family)
        symbols[name] = symbol
        return True, symbol

    db = SimpleNamespace(Transaction=Transaction, TransactionStatus=status, Family=type(family),
                         FilteredElementCollector=lambda scratch: SimpleNamespace(OfClass=lambda cls: []))
    scratch = SimpleNamespace(LoadFamilySymbol=load_symbol)
    metadata = {"family": family.Name, "category": {"id": "-2000011"},
                "embedded_types": [{"name": "A"}, {"name": "B"}]}
    rows, assets = render_thumbnails(scratch, db, "fixture.rfa", metadata, monotonic() + 30,
                                     lambda symbol: fixture_png(1) if symbol is symbols["A"] else None)
    assert loads == [("fixture.rfa", "A"), ("fixture.rfa", "B")]
    assert all(tx.status == status.Committed and tx.disposed for tx in transactions)
    assert [row["status"] for row in rows] == ["available", "preview_unavailable"]
    assert assets == {rows[0]["image"]["file"]: fixture_png(1)}

    db.FilteredElementCollector = lambda scratch: SimpleNamespace(OfClass=lambda cls: [family])
    rows, assets = render_thumbnails(scratch, db, "different/fixture.rfa", metadata, monotonic() + 30)
    assert [row["status"] for row in rows] == ["scratch_name_collision", "scratch_name_collision"]
    assert assets == {} and len(loads) == 2


def test_catalog_pair_reports_previews_pending_without_opening_scratch(staged):
    receipt, _ = staged
    directory = prepare_worker(receipt, "Fixture.rfa", [], "2026", "fixture", "pyrevit-fixture")
    worker = Worker()
    ui = SimpleNamespace(Application=worker, ActiveUIDocument=None)
    run_worker(ui, "pyrevit-fixture", str(directory))
    result = collect_worker(str(directory))
    assert result["metadata"]["catalog_coverage"] == "partial"
    assert result["metadata"]["catalog_blocked_embedded"] == ["600 × 900"]
    assert result["thumbnails"] == [{"type": "600 × 900", "status": "catalog_pending"}]
    assert len(worker.opened) == 1 and not list(directory.glob("*.png"))


def test_publish_and_search_keep_same_named_families_distinct(tmp_path, monkeypatch):
    from family_library_index import begin, publish, search, select_for_placement
    from family_library_thumbnails import png_record

    root = tmp_path.resolve() / "library"
    work = tmp_path.resolve() / "staging"
    index = str(tmp_path.resolve() / "index")
    root.mkdir()
    work.mkdir()
    for name in ("A", "B"):
        (root / name).mkdir()
        (root / name / "Fixture.rfa").write_bytes(name.encode())
    monkeypatch.setattr(staging, "free_bytes", lambda parent: 10 * staging.HEADROOM)
    manifest = discover(str(root))
    token = begin(index, manifest, ["Width"], "fixture", "pyrevit-fixture")["token"]
    paths = ["A/Fixture.rfa", "B/Fixture.rfa"]
    receipt = staging.stage(manifest, paths, str(work))
    workers = []
    for position, path in enumerate(paths):
        directory = prepare_worker(receipt, path, ["Width"], "2026", "fixture", "pyrevit-fixture", generation=token)
        ui = SimpleNamespace(Application=Worker(), ActiveUIDocument=None)
        data = fixture_png(position)
        image = png_record(data)
        row = {"type": "600 × 900", "status": "available", "image": image}
        run_worker(ui, "pyrevit-fixture", str(directory), lambda *_: ([row], {image["file"]: data}))
        workers.append(str(directory))

    publish(index, token, workers)
    found = search(index, name="ＦＩＸＴＵＲＥ", category="walls", parameter="43", equals="2")
    assert [row["path"] for row in found["matches"]] == paths
    assert [row["parameter"]["value"] for row in found["matches"]] == [2.0, 2.0]
    assert [Path(row["thumbnail"]["path"]).read_bytes() for row in found["matches"]] == [
        fixture_png(0), fixture_png(1),
    ]
    assert search(index, parameter="Width")["ambiguous_parameter_paths"] == paths
    selection = select_for_placement(index, token, paths[0], "600 × 900")
    assert selection["source"]["path"] == paths[0]
    assert selection["type_values"]["43"] == {"state": "value", "value": 2.0}
    assert selection["placement_type"] == "OneLevelBased"
    with pytest.raises(ValueError, match="exact verified type"):
        select_for_placement(index, token, paths[0], "600 x 900")
    first = Path(found["matches"][0]["thumbnail"]["path"])
    first.write_bytes(fixture_png(200))
    assert search(index)["matches"][0]["thumbnail"] == {"status": "asset_unavailable"}

    newer = begin(index, manifest, ["Width"], "fixture", "pyrevit-fixture")["token"]
    assert search(index)["generation"] == token
    with pytest.raises(ValueError, match="no longer owns"):
        publish(index, token, workers)
    with pytest.raises(ValueError, match="does not belong"):
        publish(index, newer, workers)
    (root / "A" / "Fixture.rfa").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed after indexing"):
        select_for_placement(index, token, paths[0], "600 × 900")
    assert select_for_placement(index, token, paths[1], "600 × 900")["source"]["path"] == paths[1]
    with pytest.raises(ValueError, match="discovery changed"):
        publish(index, newer, [])
    assert search(index)["generation"] == token
    (root / "A").rename(tmp_path / "removed")
    (root / "Link").symlink_to(tmp_path / "removed", target_is_directory=True)
    partial = discover(str(root))
    latest = begin(index, partial, ["Width"], "fixture", "pyrevit-fixture")["token"]
    publish(index, latest, [])
    found = search(index)
    assert found["discovery_complete"] is False
    assert found["matches"] == []
    assert found["unavailable"] == [
        {"path": "A/Fixture.rfa", "status": "source_unconfirmed"},
        {"path": "B/Fixture.rfa", "status": "worker_unavailable"},
    ]
    assert not list(Path(index).glob("thumbnail-*.png"))


def test_catalog_candidates_preserve_exact_names_and_report_collisions():
    from family_library_catalog import catalog_candidates

    text = ';Width##LENGTH##FEET\r\n48"" x 60"";5\r\n"Style; B";6\r\nSame;3\r\nsame;4\r\nEmbedded;2\r\n'
    names, blocked, issues = catalog_candidates(text.encode("utf-16"), ["Embedded"], 10)
    assert names == ['48" x 60"', "Style; B"]
    assert blocked == ["Embedded"]
    assert [item["line"] for item in issues if item["reason"] == "type_name_collision"] == [4, 5, 6]


def test_only_native_verified_catalog_types_can_be_selected(tmp_path, monkeypatch):
    from family_library_index import begin, publish, search, select_for_placement
    from read_family_library import capture_catalog

    data = ",Width##LENGTH##FEET\n600 × 900,2\nVerified catalog type,4\nMissing catalog type,5\n".encode()
    receipt, pair = stage_fixture(tmp_path, monkeypatch, catalog=data)
    root = str(tmp_path.resolve() / "library")
    index = str(tmp_path.resolve() / "index")
    manifest = discover(root)
    refresh = begin(index, manifest, ["Width"], "fixture", "pyrevit-fixture")
    directory = prepare_worker(receipt, "Fixture.rfa", ["Width"], "2026", "fixture", "pyrevit-fixture",
                               generation=refresh["token"])
    ui = SimpleNamespace(Application=Worker(), ActiveUIDocument=None)

    def native(current_ui, path, metadata, names, deadline):
        assert current_ui is ui and path == pair / "Fixture.rfa"
        assert names == ["Verified catalog type", "Missing catalog type"]
        row = {"name": names[0], "values": {
            "43": {"state": "value", "value": 4.0}, "45": {"state": "unset"},
        }}
        return [row], [{"type": names[0], "status": "preview_unavailable"}], {}, [
            {"type": names[1], "reason": "native_type_unavailable"},
        ]

    run_worker(ui, "pyrevit-fixture", str(directory),
               catalog_reader=lambda *args: capture_catalog(*args, native=native))
    publish(index, refresh["token"], [str(directory)])
    found = search(index, parameter="43", equals="4")
    assert [(row["type"], row["type_origin"], row["selectable"]) for row in found["matches"]] == [
        ("Verified catalog type", "catalog", True),
    ]
    assert found["matches"][0]["catalog_coverage"] == "partial"
    selected = select_for_placement(index, refresh["token"], "Fixture.rfa", "Verified catalog type")
    assert selected["source"]["catalog"]["bytes"] == len(data)
    assert selected["type_origin"] == "catalog"
    for rejected in ("600 × 900", "Missing catalog type"):
        with pytest.raises(ValueError, match="exact verified type"):
            select_for_placement(index, refresh["token"], "Fixture.rfa", rejected)
