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


class Id:
    def __init__(self, value):
        self.Value = value


def xyz(x, y, z):
    return NS(X=x, Y=y, Z=z)


IDENTITY = NS(OfPoint=lambda point: point)
COS = math.sqrt(0.5)
ROTATED = NS(OfPoint=lambda point: xyz(10 + COS * (point.X - point.Y), COS * (point.X + point.Y), point.Z))
MIRRORED = NS(OfPoint=lambda point: xyz(20 - point.X, point.Y + 2, point.Z))


class Link:
    def __init__(self, uid, linked, transform):
        self.UniqueId, self.linked, self.transform = uid, linked, transform
        self.hidden = False

    def IsHidden(self, _view):
        return self.hidden

    def GetLinkDocument(self):
        return self.linked

    def GetTotalTransform(self):
        return self.transform


def model(uid, elements):
    return NS(PathName="/synthetic/" + uid + ".rvt", ProjectInformation=NS(UniqueId=uid),
              IsFamilyDocument=False, GetElement=lambda item: elements.get(item.Value))


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=Id, XYZ=xyz, Transform=NS(Identity=IDENTITY),
        ElementType=type("ElementType", (), {}), RevitLinkInstance=Link,
    )))
    reader = runpy.run_path(str(SCRIPTS / "read_clash_bounds.py"))["main"]
    box = NS(Min=xyz(0, 0, 0), Max=xyz(2, 1, 3), Transform=IDENTITY, Enabled=True)
    elem = NS(ViewSpecific=False, Category=object(), UniqueId="same-element-in-two-instances",
              get_BoundingBox=lambda _view: box, IsHidden=lambda _view: False)
    linked = model("linked", {42: elem})
    links = [Link("rotated-instance", linked, ROTATED), Link("mirrored-instance", linked, MIRRORED)]
    host = model("host", {6: links[0], 7: links[1]})
    host.ActiveView = NS(UniqueId="view", IsTemplate=False)
    request = {"active_view_uid": "view", "sources": [
        {"path_prefix": ["Rotated"], "link_instance_id": "6", "link_instance_uid": links[0].UniqueId,
         "element_ids": ["42"]},
        {"path_prefix": ["Mirrored"], "link_instance_id": "7", "link_instance_uid": links[1].UniqueId,
         "element_ids": ["42", "43"]},
    ]}
    return reader, host, request, links


def test_reader_bounds_feed_xml_clash_workflow_with_distinct_link_instances(runtime, tmp_path):
    reader, host, request, _ = runtime
    native = reader(host, request)
    snapshot = json.loads(native["snapshot_json"])
    rotated, mirrored = snapshot["sources"]
    assert rotated["elements"]["42"]["min"] == pytest.approx([10 - COS, 0, 0])
    assert rotated["elements"]["42"]["max"] == pytest.approx([10 + 2 * COS, 3 * COS, 3])
    assert mirrored["elements"]["42"]["min"] == [18, 2, 0]
    assert mirrored["elements"]["42"]["max"] == [20, 3, 3]
    assert mirrored["unresolved"] == {"43": "element_missing"}

    record = tmp_path / "full-run.json"
    record.write_text(json.dumps({"response": {"result": native}}))
    xml = tmp_path / "fixture.xml"
    xml.write_text("""<exchange units="m"><batchtest><clashtests><clashtest><clashresults>
<clashresult guid="11111111-0000-4000-8000-000000000001" name="Synthetic clash" status="new">
<clashobjects>
<clashobject><objectattribute><name>Element ID</name><value>42</value></objectattribute>
<pathlink><node>Rotated</node><node>Fixture element</node></pathlink></clashobject>
<clashobject><objectattribute><name>Element ID</name><value>42</value></objectattribute>
<pathlink><node>Mirrored</node><node>Fixture element</node></pathlink></clashobject>
</clashobjects></clashresult></clashresults></clashtest></clashtests></batchtest></exchange>""")
    out = subprocess.run([sys.executable, str(SCRIPTS / "navisworks_clash_cli.py"), str(xml), "--snapshot", str(record)],
                         check=True, text=True, capture_output=True)
    box = json.loads(out.stdout)["clashes"][0]["section_box"]
    assert box["min"] == pytest.approx([9 - COS, -1, -1])
    assert box["max"] == [21, 4, 4]
    assert [target["model_id"] for target in box["targets"]] == ["link:rotated-instance", "link:mirrored-instance"]
    assert box["targets"][0]["unique_id"] == box["targets"][1]["unique_id"]


def test_reader_reports_missing_visibility_and_refuses_stale_identity(runtime):
    reader, host, request, links = runtime
    links[0].linked = None
    links[1].hidden = True
    sources = json.loads(reader(host, request)["snapshot_json"])["sources"]
    assert sources[0]["elements"] == {} and sources[0]["unresolved"] == {"42": "link_unloaded"}
    assert sources[1]["elements"] == {} and sources[1]["unresolved"] == {
        "42": "link_hidden_in_active_view", "43": "link_hidden_in_active_view",
    }
    request["sources"][1]["link_instance_uid"] = "stale-id"
    with pytest.raises(ValueError, match="changed identity"):
        reader(host, request)
    request["active_view_uid"] = "other-view"
    with pytest.raises(ValueError, match="current view differs"):
        reader(host, request)
