from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_health.py"
WALL = "11111111-0000-4000-8000-000000000001"
DOOR = "11111111-0000-4000-8000-000000000002"


def failure(kind, message, failing, additional=()):
    return NS(GetFailureDefinitionId=lambda: NS(Guid=kind), GetDescriptionText=lambda: message,
              GetFailingElements=lambda: [NS(Value=item) for item in failing],
              GetAdditionalElements=lambda: [NS(Value=item) for item in additional])


def run_query(monkeypatch, warnings, names, workshared=True):
    queries = []

    def tooltip(_, element_id):
        queries.append(element_id.Value)
        creator = names[element_id.Value]
        return NS(Creator=creator, Dispose=lambda: None)

    monkeypatch.setitem(sys.modules, "System", NS())
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        WorksharingUtils=NS(GetWorksharingTooltipInfo=tooltip)
    )))
    document = NS(IsFamilyDocument=False, IsWorkshared=workshared, GetWarnings=lambda: warnings)
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": {"warning_triage": True}})
    return result["result"], queries


def test_warning_types_rank_by_instance_and_creator_attribution_uses_only_failing_ids(monkeypatch):
    large_id = 9223372036854775807
    warnings = [failure(WALL, "First wall description", [2, 3, large_id], [3, 9]),
                failure(DOOR, "First wall description", [3]),
                failure(WALL, "Second wall description", [2, 4])]
    result, queries = run_query(monkeypatch, warnings, {2: "A", 3: "A", large_id: "B"})
    assert result["status"] == "ok" and result["warning_count"] == 3
    assert result["unavailable_creator_count"] == 1
    assert sorted(queries) == [2, 3, 4, large_id]
    walls, doors = result["groups"]
    assert [walls["failure_id"], doors["failure_id"]] == [WALL, DOOR]
    assert walls["count"] == 2 and doors["count"] == 1
    assert walls["descriptions"] == ["First wall description", "Second wall description"]
    assert walls["creator_counts"] == [{"name": "A", "count": 2}, {"name": "B", "count": 1}]
    assert walls["warnings_with_unknown_creator"] == 1
    assert walls["instances"][0] == {"failing_ids": ["2", "3", str(large_id)], "additional_ids": ["9"],
                                      "creators": ["A", "B"], "has_unknown_creator": False}


def test_non_workshared_warning_report_discloses_unavailable_creators(monkeypatch):
    result, queries = run_query(monkeypatch, [failure(WALL, "Duplicate wall", [1])], {}, workshared=False)
    assert queries == []
    assert result["workshared"] is False
    assert result["unavailable_creator_count"] == 1
    assert result["groups"][0]["creator_counts"] == []
    assert result["groups"][0]["warnings_with_unknown_creator"] == 1


def test_warning_count_cap_refuses_to_report_partial_counts(monkeypatch):
    warnings = [failure(WALL, "Duplicate wall", [1])] * 5001
    result, queries = run_query(monkeypatch, warnings, {})
    assert result["status"] == "stopped"
    assert "5,000" in result["message"]
    assert "groups" not in result and queries == []
