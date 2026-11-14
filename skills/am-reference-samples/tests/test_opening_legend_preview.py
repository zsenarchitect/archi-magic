from copy import deepcopy
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opening_legend_preview.py"
PLANNER = runpy.run_path(str(SCRIPT))
GRID = {"origin_uv_ft": [0, 0], "columns": 3, "cell_width_ft": 10, "cell_height_ft": 10}


def opening(uid, identifier, mark, category="Doors"):
    return {"uid": uid, "id": identifier, "type_mark": mark, "category": category, "placements": 2,
            "width": {"feet": 3, "display": "3 ft"}, "height": {"feet": 7, "display": "7 ft"},
            "instance_dimensions_vary": {"width": False, "height": False}}


def snapshot():
    return {"kind": "placed-opening-type-snapshot", "scope": "host-project",
            "host_identity": {"project_uid": "fixture-project", "path_sha256": "a" * 64},
            "types": [opening("door", "9007199254740993", "A01"), opening("window", "12", "A01", "Windows")],
            "legend": {"id": "25", "uid": "fixture-legend", "scale": 20, "directions_verified_for_year": True,
                       "origin_ft": [0, 0, 0], "right": [1, 0, 0], "up": [0, 1, 0],
                       "components": [], "text_notes": [], "layout": {
                           "text_type": {"id": "50"}, "target_graphics": [], "sheet_placements": [],
                       }}}


def run_cli(tmp_path, state, intent):
    source, request = tmp_path / "read.json", tmp_path / "intent.json"
    source.write_text(json.dumps({"status": "ok", "result": {"status": "ok", "snapshot_json": json.dumps(state)}}))
    request.write_text(json.dumps(intent))
    return subprocess.run([sys.executable, str(SCRIPT), str(source), str(request)], capture_output=True, text=True)


def test_new_legend_outputs_one_row_per_type_and_requests_missing_label(tmp_path):
    state = snapshot()
    state["types"][1]["instance_dimensions_vary"]["height"] = True
    output = run_cli(tmp_path, state, {"grid": GRID})
    assert output.returncode == 1, output.stderr
    blocked = json.loads(output.stdout)
    assert blocked["blocked"] and len(blocked["steps"]) == 2
    assert blocked["ownership_ids"] == ["25"]
    result = run_cli(tmp_path, state, {"grid": GRID, "labels": {"window": "A01 | 3 ft by variable height"}})
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert [(row["type_uid"], row["label"], row["cell_uv_ft"]) for row in preview["steps"]] == [
        ("door", "A01 | 3 ft × 7 ft", [0, -10, 10, 0]),
        ("window", "A01 | 3 ft by variable height", [10, -10, 20, 0]),
    ]
    assert len(preview["warnings"]) == 2 and all(not row["blocking"] for row in preview["warnings"])


def managed_snapshot():
    state = snapshot()
    legend = state["legend"]
    pairs = []
    for slot, row in enumerate(state["types"]):
        offset = slot * 10
        bounds = {"min": [1 + offset, -8, 0], "max": [4 + offset, -1, 0]}
        component = {"id": str(31 + slot * 2), "uid": "component-" + row["uid"], "type_id": row["id"],
                     "type_uid": row["uid"], "direction": "front_elevation", "direction_raw": -7,
                     "opening_category": row["category"], "host_length_ft": None,
                     "position_ft": [2 + offset, -5, 0], "bounds_ft": bounds}
        note = {"id": str(32 + slot * 2), "uid": "note-" + row["uid"], "text": "A01 | 3 ft × 7 ft",
                "position_ft": [2 + offset, -9, 0], "type_id": "50"}
        legend["components"].append(component)
        legend["text_notes"].append(note)
        pair = {"slot": slot, "type_uid": row["uid"]}
        for kind, element in (("component", component), ("note", note)):
            graphic = {"id": element["id"], "uid": element["uid"], "owner_id": "25", "pinned": False, "group_id": "-1",
                       "category_id": "-8", "native_kind": "TextNote" if kind == "note" else "Element",
                       "bounds_ft": bounds}
            if kind == "note":
                graphic["bounds_ft"] = {"min": [1 + offset, -9, 0], "max": [4 + offset, -8.5, 0]}
                graphic["text_appearance"] = {"plain_single_line": True, "leaders": 0, "horizontal_alignment": "Left"}
            legend["layout"]["target_graphics"].append(graphic)
            pair[kind] = PLANNER["paired_state"](element, graphic, kind)
        pairs.append(pair)
    receipt = {"kind": "opening-legend-receipt", "decision": "committed", "approval": "user",
               "host_identity": deepcopy(state["host_identity"]), "legend_uid": legend["uid"], "grid": GRID,
               "run_id": "keep", "readback_run_id": "fresh", "pairs": pairs}
    return state, receipt


def test_refresh_retires_only_receipted_pairs_and_reuses_a_freed_cell():
    state, receipt = managed_snapshot()
    build = PLANNER["build_preview"]
    intent = {"grid": GRID, "receipt": receipt}
    assert build(state, intent)["steps"] == []
    state["types"] = [opening("window", "12", "A02", "Windows"), opening("new", "13", "A03")]
    preview = build(state, intent)
    assert [(row["operation"], row["type_uid"], row["existing_ids"], row["slot"]) for row in preview["steps"]] == [
        ("retire", "door", ["31", "32"], 0), ("add", "new", [], 0), ("refresh", "window", ["33", "34"], 1),
    ]
    assert preview["ownership_ids"] == ["25", "31", "32", "33", "34"]
    assert preview["deletions"] == [
        {"id": "31", "category": "Legend Components", "name": "Front elevation for type ID 9007199254740993"},
        {"id": "32", "category": "Text Notes", "name": "Label: A01 | 3 ft × 7 ft"},
    ]
    assert PLANNER["approved_plan"](preview, state) == preview
    incomplete = deepcopy(preview)
    incomplete["deletions"].pop()
    with pytest.raises(ValueError, match="approved legend preview"):
        PLANNER["approved_plan"](incomplete, state)
    with pytest.raises(ValueError, match="verified prior receipt"):
        build(state, {"grid": GRID})
    state["legend"]["text_notes"][1]["text"] = "Edited by hand"
    with pytest.raises(ValueError, match="changed after its kept receipt"):
        build(state, intent)


def test_sheet_fit_refuses_a_batch_when_one_sheet_overflows():
    def graphic(identifier, high):
        return {"id": identifier, "native_kind": "Viewport", "category_id": "-8",
                "bounds_ft": {"min": [0.1, 0.1, 0], "max": [high, 0.8, 0]},
                "label_bounds_ft": {"min": [0.1, 0.03, 0], "max": [0.4, 0.08, 0]}}

    def sheet(identifier, viewport_id, high):
        return {"id": identifier, "uid": "fixture-sheet-" + identifier, "number": "A" + identifier,
                "outline_ft": {"min": [0, 0], "max": [3, 2]},
                "legend_viewport_ids": [viewport_id], "graphics": [graphic(viewport_id, high)]}

    before = {"layout": {"title_block_category_id": "-9",
                         "sheet_placements": [sheet("81", "91", 0.9), sheet("82", "92", 0.9)]}}
    after = deepcopy(before)
    after["layout"]["sheet_placements"][0]["graphics"] = [graphic("91", 1.2)]
    after["layout"]["sheet_placements"][1]["graphics"] = [graphic("92", 1.2)]
    intent = {"sheet_rectangles_ft": {"81": [0, 0, 1.5, 1], "82": [0, 0, 1, 1]}}
    with pytest.raises(ValueError, match="does not fit on every reviewed sheet"):
        PLANNER["verify_sheet_fit"](before, after, intent)
    intent["sheet_rectangles_ft"]["82"][2] = 1.5
    assert PLANNER["verify_sheet_fit"](before, after, intent) == ["91", "92"]


def test_writer_rolls_back_if_retiring_pair_deletes_an_extra_element(monkeypatch):
    from types import SimpleNamespace as NS

    state, receipt = managed_snapshot()
    state["types"] = []
    layout = state["legend"]["layout"]
    layout.update(source_legend_id="26", seed={"id": "60"})
    plan = PLANNER["build_preview"](state, {"grid": GRID, "receipt": receipt})
    transactions = []

    class ElementId:
        def __init__(self, value):
            self.Value = int(value)

    class Element:
        Pinned = False
        OwnerViewId = ElementId(25)
        GroupId = ElementId(-1)
        Category = NS(Id=ElementId(-8))

        def __init__(self, identifier):
            self.Id = ElementId(identifier)

        def GetDependentElements(self, _filter):
            return []

    class TextNote(Element):
        pass

    class IdList(list):
        Add = list.append

        @classmethod
        def __class_getitem__(cls, _kind):
            return cls

    class Transaction:
        def __init__(self, _document, _name):
            self.state = None
            transactions.append(self)

        def Start(self):
            self.state = "started"
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            self.state = "rolled_back"
            return self.state

    elements = {25: NS(Id=ElementId(25)), 26: NS(Id=ElementId(26)), 31: Element(31), 32: TextNote(32)}
    doc = NS(IsReadOnly=False, IsWorkshared=False, GetElement=lambda identifier: elements.get(identifier.Value),
             Delete=lambda ids: list(ids) + [ElementId(99)])
    db = NS(ElementId=ElementId, TextNote=TextNote, BuiltInCategory=NS(OST_LegendComponents=-8),
            Transaction=Transaction, TransactionStatus=NS(Started="started", RolledBack="rolled_back"))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=IdList))
    monkeypatch.setitem(sys.modules, "opening_legend_preview", NS(**PLANNER))
    monkeypatch.setitem(sys.modules, "read_opening_types", NS(project_snapshot=lambda _doc, _request: state,
                        positive_id=ElementId, element_id=lambda item: str(item.Value)))
    script = SCRIPT.with_name("apply_opening_legend.py")
    with pytest.raises(ValueError, match="outside the reviewed legend pair"):
        runpy.run_path(str(script), init_globals={"doc": doc, "inputs": {"preview": plan}})
    assert [transaction.state for transaction in transactions] == ["rolled_back"]


def test_readback_accepts_verified_refresh_and_preserves_manual_notes(tmp_path):
    state, receipt = managed_snapshot()
    legend = state["legend"]
    legend["revit_year"] = "2026"
    legend["layout"].update(source_legend_id="26", source_legend_uid="fixture-source", source_scale=20,
                            source_frame={"origin_ft": [0, 0, 0]}, seed={"id": "60"}, title_block_category_id="-9",
                            front_elevation_coverage={"missing_type_ids": [], "duplicate_elevation_type_ids": [],
                                                      "unplaced_elevation_type_ids": [], "unresolved_type_ids": []})
    legend["text_notes"].append({"id": "35", "uid": "manual-note", "text": "Manual"})
    legend["layout"]["target_graphics"].append({
        "id": "35", "uid": "manual-note", "native_kind": "TextNote",
        "bounds_ft": {"min": [40, -4, 0], "max": [42, -3, 0]},
    })
    state["types"][0]["type_mark"] = "A02"
    plan = PLANNER["build_preview"](state, {"grid": GRID, "receipt": receipt})
    assert [step["operation"] for step in plan["steps"]] == ["refresh"]
    actual = deepcopy(state)
    actual["legend"]["text_notes"][0]["text"] = "A02 | 3 ft × 7 ft"
    pairs = {"door": {"component_id": "31", "note_id": "32"},
             "window": {"component_id": "33", "note_id": "34"}}
    result = PLANNER["verify_result"](plan, actual, pairs)
    assert [pair["note"]["text"] for pair in result["pairs"]] == ["A02 | 3 ft × 7 ft", "A01 | 3 ft × 7 ft"]
    assert result["modified_pair_ids"] == ["32"]
    def query(snapshot, run_id):
        return {"mode": "query", "status": "ok", "run_id": run_id,
                "result": {"status": "ok", "snapshot_json": json.dumps(snapshot)}}
    changes = {"added_count": 0, "added": [], "deleted_count": 0, "deleted": [],
               "modified_count": 1, "modified": [{"id": 32}]}
    dry = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "run_id": "dry",
           "changes": changes, "result": {"status": "ok", "receipt_json": json.dumps(result)}}
    verified_dry = PLANNER["verify_run"](plan, dry, query(state, "baseline"), "dry-run")
    assert verified_dry["status"] == "verified_dry_run"
    assert verified_dry["changes"] == {"added": 0, "modified": 1, "deleted": 0, "regenerated_viewports": 0}
    discard = {"mode": "modify", "status": "rejected", "decision": "discarded", "approval": "user", "run_id": "discard"}
    assert PLANNER["verify_run"](plan, discard, query(state, "discarded-read"), "discard")["status"] == "verified_discard"
    kept = dict(dry, mode="modify", decision="committed", approval="user", run_id="keep")
    receipt = PLANNER["verify_run"](plan, kept, query(actual, "read"), "keep")
    assert receipt["status"] == "verified_keep" and receipt["pairs"] == result["pairs"]
    paths = [tmp_path / name for name in ("fresh.json", "preview.json", "run.json")]
    for path, data in zip(paths, (query(actual, "read"), plan, kept)):
        path.write_text(json.dumps(data))
    cli = subprocess.run([sys.executable, str(SCRIPT), str(paths[0]), str(paths[1]),
                          "--verify", "keep", "--run", str(paths[2])], capture_output=True, text=True)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == receipt

    with pytest.raises(ValueError, match="separate Revit query"):
        PLANNER["verify_run"](plan, kept, query(actual, "keep"), "keep")
    with pytest.raises(ValueError, match="rolled-back baseline"):
        PLANNER["verify_run"](plan, dry, query(actual, "not-rolled-back"), "dry-run")
    invalid = dict(kept, changes=dict(changes, modified=[{"id": 35}]))
    with pytest.raises(ValueError, match="differ from the reviewed legend scope"):
        PLANNER["verify_run"](plan, invalid, query(actual, "read"), "keep")

    actual["legend"]["text_notes"][2]["text"] = "Unexpected edit"
    with pytest.raises(ValueError, match="Unmanaged graphics changed"):
        PLANNER["verify_result"](plan, actual, pairs)


def test_writer_builds_retires_and_restarts_legend_from_separate_seed(monkeypatch):
    from types import SimpleNamespace as NS

    base = snapshot()
    base["legend"]["revit_year"] = "2026"
    base["legend"]["layout"].update(source_legend_id="26", source_legend_uid="source", source_scale=20,
                                     source_frame={"origin_ft": [0, 0, 0]}, seed={"id": "60"},
                                     title_block_category_id="-9")
    targets = {25: NS(Id=NS(Value=25)), 26: NS(Id=NS(Value=26))}
    items = {}
    sequence = iter(range(101, 120))
    commits = []
    copied = []

    def element_id(value):
        return str(value.Value)

    class ElementId:
        def __init__(self, value):
            self.Value = int(value)

    class IdList(list):
        Add = list.append

        @classmethod
        def __class_getitem__(cls, _kind):
            return cls

    class Graphic:
        Pinned = False
        OwnerViewId = ElementId(25)
        GroupId = ElementId(-1)
        Category = NS(Id=ElementId(-8))

        def __init__(self, width, height):
            self.Id = ElementId(next(sequence))
            self.uid = "fixture-graphic-" + element_id(self.Id)
            self.position = [0.0, 0.0, 0.0]
            self.width, self.height = width, height
            items[self.Id.Value] = self

        def GetDependentElements(self, _filter):
            return []

        def bounds(self):
            x, y, z = self.position
            return {"min": [x, y, z], "max": [x + self.width, y + self.height, z]}

    class Component(Graphic):
        def __init__(self):
            super().__init__(3, 7)
            self.type_id = ElementId(12)
            self.direction = -7

        def get_Parameter(self, key):
            if key == "type":
                return NS(IsReadOnly=False, AsElementId=lambda: self.type_id,
                          Set=lambda value: self.set_value("type_id", value))
            return NS(IsReadOnly=False, AsInteger=lambda: self.direction,
                      Set=lambda value: self.set_value("direction", value))

        def set_value(self, key, value):
            setattr(self, key, value)
            return True

    class TextNote(Graphic):
        def __init__(self, text, type_id):
            super().__init__(2, 0.4)
            self.Text, self.type_id = text, type_id

        @staticmethod
        def Create(_doc, view_id, _origin, text, type_id):
            assert view_id.Value == 25
            return TextNote(text, type_id)

        def GetTypeId(self):
            return self.type_id

    def copy_seed(source, ids, target, _transform, _options):
        assert source is targets[26] and target is targets[25]
        copied.append([element_id(identifier) for identifier in ids])
        return [Component().Id]

    def move(_doc, identifier, delta):
        item = items[identifier.Value]
        item.position = [value + change for value, change in zip(item.position, delta)]

    class Transaction:
        def __init__(self, _doc, _title):
            self.status = None

        def Start(self):
            self.status = "started"
            return self.status

        def GetStatus(self):
            return self.status

        def Commit(self):
            self.status = "committed"
            commits.append(self.status)
            return self.status

        def RollBack(self):
            self.status = "rolled_back"
            return self.status

    def read(_doc, _request):
        state = deepcopy(base)
        legend = state["legend"]
        types = {row["id"]: row for row in original_types}
        for item in items.values():
            identifier = element_id(item.Id)
            row = {"id": identifier, "uid": item.uid, "position_ft": list(item.position)}
            graphic = {"id": identifier, "uid": item.uid, "owner_id": "25", "pinned": False, "group_id": "-1",
                       "category_id": "-8", "bounds_ft": item.bounds()}
            if isinstance(item, TextNote):
                row.update(type_id=element_id(item.type_id), text=item.Text)
                graphic.update(native_kind="TextNote", text_appearance={
                    "plain_single_line": True, "leaders": 0, "horizontal_alignment": "Left",
                })
                legend["text_notes"].append(row)
            else:
                kind = types[element_id(item.type_id)]
                row.update(type_id=kind["id"], type_uid=kind["uid"], opening_category=kind["category"],
                           direction_raw=item.direction, direction="front_elevation", host_length_ft=None,
                           bounds_ft=item.bounds())
                graphic["native_kind"] = "Element"
                legend["components"].append(row)
            legend["layout"]["target_graphics"].append(graphic)
        legend["layout"]["front_elevation_coverage"] = {
            "missing_type_ids": [row["id"] for row in state["types"]
                                 if not any(item["type_id"] == row["id"] for item in legend["components"])],
        }
        return state

    def delete(ids):
        for identifier in ids:
            del items[identifier.Value]
        return ids

    doc = NS(IsReadOnly=False, IsWorkshared=False,
             GetElement=lambda identifier: items.get(identifier.Value) or targets.get(identifier.Value),
             Delete=delete, Regenerate=lambda: None)
    db = NS(ElementId=ElementId, TextNote=TextNote, BuiltInCategory=NS(OST_LegendComponents=-8),
            BuiltInParameter=NS(LEGEND_COMPONENT="type", LEGEND_COMPONENT_VIEW="direction"),
            Transform=NS(Identity=None), CopyPasteOptions=lambda: NS(Dispose=lambda: None), XYZ=lambda *values: values,
            ElementTransformUtils=NS(CopyElements=copy_seed, MoveElement=move), Transaction=Transaction,
            TransactionStatus=NS(Started="started", Committed="committed", RolledBack="rolled_back"))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=IdList))
    monkeypatch.setitem(sys.modules, "opening_legend_preview", NS(**PLANNER))
    monkeypatch.setitem(sys.modules, "read_opening_types", NS(project_snapshot=read, positive_id=ElementId,
                        element_id=element_id, element_bounds=lambda item, _view: item.bounds()))
    original_types = deepcopy(base["types"])

    def apply_and_verify(intent, tag):
        before = read(doc, None)
        plan = PLANNER["build_preview"](before, intent)
        old_ids = set(items)
        script = SCRIPT.with_name("apply_opening_legend.py")
        run = runpy.run_path(str(script), init_globals={"doc": doc, "inputs": {"preview": plan}})["result"]
        added, deleted = set(items) - old_ids, old_ids - set(items)
        changes = {"added": [{"id": value} for value in sorted(added)], "added_count": len(added),
                   "modified": [], "modified_count": 0,
                   "deleted": sorted(map(int, deleted)), "deleted_count": len(deleted)}
        envelope = {"mode": "modify", "status": "ok", "decision": "committed", "approval": "user",
                    "run_id": "keep-" + tag, "changes": changes, "result": run}
        fresh = {"mode": "query", "status": "ok", "run_id": "read-" + tag,
                 "result": {"status": "ok", "snapshot_json": json.dumps(read(doc, None))}}
        if deleted:
            wrong = deepcopy(envelope)
            wrong["changes"]["deleted"][0] = 987654321
            with pytest.raises(ValueError, match="differ from the reviewed legend scope"):
                PLANNER["verify_run"](plan, wrong, fresh, "keep")
        return PLANNER["verify_run"](plan, envelope, fresh, "keep")

    receipt = apply_and_verify({"grid": GRID}, "add")
    assert receipt["changes"]["added"] == 4 and copied == [["60"], ["60"]]
    assert PLANNER["build_preview"](read(doc, None), {"grid": GRID, "receipt": receipt})["steps"] == []
    base["types"] = []
    empty = apply_and_verify({"grid": GRID, "receipt": receipt}, "retire")
    assert empty["pairs"] == [] and empty["changes"]["deleted"] == 4 and items == {}
    base["types"] = [original_types[1]]
    restarted = apply_and_verify({"grid": GRID, "receipt": empty}, "restart")
    assert [pair["type_uid"] for pair in restarted["pairs"]] == ["window"]
    assert copied == [["60"], ["60"], ["60"]] and commits == ["committed"] * 3
