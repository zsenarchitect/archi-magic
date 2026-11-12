"""Place and inspect one reviewed Revit MEP route, then always roll it back."""

import json
import math

from Autodesk.Revit.Exceptions import RegenerationFailedException
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from duplicate_views import capture_changes
import mep_route_trial_preflight as preflight
import mep_route_preview as planner


TRANSACTION_NAME = preflight.TRANSACTION_NAME
DISTANCE_TOLERANCE = 1e-6
MAX_ADDITIONS = 49
PHYSICAL = ("End", "Curve", "Physical")


def matches_size(actual, expected):
    return math.isfinite(actual) and abs(actual - expected) <= DISTANCE_TOLERANCE


def port_key(port):
    return str(port.Owner.Id.Value), str(port.Owner.UniqueId), int(port.Id)


def find_port(reader, owner, clock, budget, expected_id=None, position=None):
    ports = []
    for port in reader.connector_collection(owner):
        reader.route_visit(clock, budget)
        if str(port.ConnectorType) != "End":
            continue
        if expected_id is not None and int(port.Id) != expected_id:
            continue
        if position is not None and math.dist(reader.xyz(port.Origin), position) > DISTANCE_TOLERANCE:
            continue
        ports.append(port)
    if len(ports) != 1:
        raise ValueError("A reviewed route port is missing or ambiguous.")
    return ports[0]


def physical_refs(reader, port, clock, budget):
    refs = []
    for other in port.AllRefs:
        reader.route_visit(clock, budget)
        kind = str(other.ConnectorType)
        if kind in ("Logical", "Reference") or other.Owner.Id.Value == port.Owner.Id.Value:
            continue
        if kind not in PHYSICAL or not (port.IsConnectedTo(other) and other.IsConnectedTo(port)):
            raise ValueError("A trial port has an unresolved physical connection.")
        refs.append(port_key(other))
    if len(refs) != 1 or not port.IsConnected:
        raise ValueError("A trial port does not have exactly one physical connection.")
    return refs[0]


def ports_of(reader, item, clock, budget):
    ends = []
    for port in reader.connector_collection(item):
        reader.route_visit(clock, budget)
        kind = str(port.ConnectorType)
        if kind in ("Logical", "Reference"):
            continue
        if kind != "End":
            raise ValueError("A placed route element has an unsupported physical connector.")
        ends.append(port)
    if len(ends) != 2 or int(ends[0].Id) == int(ends[1].Id):
        raise ValueError("A placed route element does not have two distinct end ports.")
    return ends


def check_bounds(reader, item, data, owner_snapshot):
    box = item.get_BoundingBox(None)
    if box is None:
        raise ValueError("A trial route element has no measurable bounds.")
    low, high = reader.xyz(box.Min), reader.xyz(box.Max)
    clearance = data["clearance_ft"]
    limits = data["routing_bounds_ft"]
    if any(a >= b or a - clearance < lower - DISTANCE_TOLERANCE or b + clearance > upper + DISTANCE_TOLERANCE
           for a, b, lower, upper in zip(low, high, limits["min_ft"], limits["max_ft"])):
        raise ValueError("A trial route envelope extends outside the reviewed routing area.")
    for record in data.get("obstacle_records", []):
        for obstacle in record["obstacles"]:
            if all(a - clearance <= d and b + clearance >= c
                   for a, b, c, d in zip(low, high, obstacle["min_ft"], obstacle["max_ft"])):
                raise ValueError("A trial route envelope intersects a reviewed obstacle or its clearance.")
    for owner in owner_snapshot["owners"]:
        if all(min(b, d) - max(a, c) > DISTANCE_TOLERANCE
               for a, b, c, d in zip(low, high, owner["min_ft"], owner["max_ft"])):
            raise ValueError("A trial route envelope penetrates a reviewed endpoint owner.")
    return [low, high]


def place(document, reader, route, types, original, created, clock, budget):
    fitting = types["fitting"]
    diameter = fitting["diameter_ft"]
    is_duct = fitting["domain"] == "DomainHvac"
    curve_class = DB.Mechanical.Duct if is_duct else DB.Plumbing.Pipe
    diameter_param = (DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM if is_duct
                      else DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    type_id, system_id, level_id = [reader.element_id(item) for item in (
        fitting["curve_type_id"], types["system_type"]["id"], types["level"]["id"],
    )]
    points = route["points_ft"]
    curves, elbows = [], []
    for start, end in zip(points, points[1:]):
        reader.check_time(clock)
        curve = curve_class.Create(document, system_id, type_id, level_id, DB.XYZ(*start), DB.XYZ(*end))
        created.add(str(curve.Id.Value))
        parameter = curve.get_Parameter(diameter_param)
        if parameter is None or parameter.IsReadOnly or parameter.StorageType != DB.StorageType.Double:
            raise ValueError("The new curve diameter is unavailable or read-only.")
        if (not parameter.HasValue or not matches_size(parameter.AsDouble(), diameter)) and (
                not parameter.Set(float(diameter))):
            raise ValueError("Revit refused the reviewed curve diameter.")
        curves.append(curve.Id)
    document.Regenerate()
    for curve_id in curves:
        reader.check_time(clock)
        curve = document.GetElement(curve_id)
        parameter = curve.get_Parameter(diameter_param)
        if (not parameter.HasValue or not matches_size(parameter.AsDouble(), diameter)
                or any(str(port.Shape) != "Round" or not matches_size(2 * port.Radius, diameter)
                       for port in ports_of(reader, curve, clock, budget))):
            raise ValueError("Revit did not establish the reviewed diameter before connecting the route.")

    for index, point in enumerate(points[1:-1]):
        reader.check_time(clock)
        before = find_port(reader, document.GetElement(curves[index]), clock, budget, position=point)
        after = find_port(reader, document.GetElement(curves[index + 1]), clock, budget, position=point)
        elbow = document.Create.NewElbowFitting(before, after)
        created.add(str(elbow.Id.Value))
        elbows.append(elbow.Id)
        document.Regenerate()

    for reviewed, curve_id in zip(original["endpoints"], (curves[0], curves[-1])):
        reader.check_time(clock)
        owner = document.GetElement(reader.element_id(reviewed["element_id"]))
        original_port = find_port(reader, owner, clock, budget, expected_id=reviewed["connector_id"])
        new_port = find_port(reader, document.GetElement(curve_id), clock, budget, position=reviewed["origin_ft"])
        original_port.ConnectTo(new_port)
        document.Regenerate()
    return curves, elbows, diameter_param


def verify_route(document, reader, route, types, original, data, placed, created, clock, budget):
    curves, elbows, diameter_param = placed
    fitting = types["fitting"]
    diameter = fitting["diameter_ft"]
    system_param = (DB.BuiltInParameter.RBS_DUCT_SYSTEM_TYPE_PARAM if fitting["domain"] == "DomainHvac"
                    else DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM)
    ordered, observations = [], []
    points = route["points_ft"]
    for index, curve_id in enumerate(curves):
        reader.check_time(clock)
        curve = document.GetElement(curve_id)
        parameter = curve.get_Parameter(diameter_param)
        actual_type = document.GetElement(curve.GetTypeId())
        system = curve.get_Parameter(system_param)
        level = curve.ReferenceLevel
        if (str(curve.GetTypeId().Value) != fitting["curve_type_id"]
                or str(actual_type.UniqueId) != fitting["curve_type_uid"]
                or system is None or not system.HasValue
                or str(system.AsElementId().Value) != types["system_type"]["id"]
                or level is None or str(level.Id.Value) != types["level"]["id"]
                or str(level.UniqueId) != types["level"]["uid"] or parameter is None
                or not parameter.HasValue or not matches_size(parameter.AsDouble(), diameter)):
            raise ValueError("A placed route curve differs from its reviewed type, system, level or size.")
        start, end = points[index:index + 2]
        direction = planner.unit(planner.delta(start, end))
        length = math.dist(start, end)
        ends = []
        for port in ports_of(reader, curve, clock, budget):
            origin = reader.xyz(port.Origin)
            distance = planner.dot(planner.delta(start, origin), direction)
            if (distance < -DISTANCE_TOLERANCE or distance > length + DISTANCE_TOLERANCE
                    or math.dist(origin, planner.along(start, direction, distance)) > DISTANCE_TOLERANCE
                    or str(port.Shape) != "Round" or not matches_size(2 * port.Radius, diameter)):
                raise ValueError("Revit displaced or resized a reviewed route segment.")
            ends.append((distance, port))
        ends.sort(key=lambda row: row[0])
        if ends[1][0] - ends[0][0] < 1 / 120 - DISTANCE_TOLERANCE:
            raise ValueError("A fitted route segment is too short.")
        left, right = [row[1] for row in ends]
        ordered.append((left, right))
        observations.append({"id": str(curve_id.Value), "uid": str(curve.UniqueId),
                             "ends_ft": [reader.xyz(left.Origin), reader.xyz(right.Origin)],
                             "bounds_ft": check_bounds(reader, curve, data, original)})

    expected = []
    for index, elbow_id in enumerate(elbows):
        reader.check_time(clock)
        elbow = document.GetElement(elbow_id)
        selected_type = document.GetElement(elbow.GetTypeId())
        if (str(elbow.GetTypeId().Value) != fitting["elbow"]["id"]
                or str(selected_type.UniqueId) != fitting["elbow"]["uid"]):
            raise ValueError("Revit placed an elbow other than the reviewed fitting type.")
        left, right = ordered[index][1], ordered[index + 1][0]
        ports = ports_of(reader, elbow, clock, budget)
        matched = {physical_refs(reader, port, clock, budget): port for port in ports}
        if set(matched) != {port_key(left), port_key(right)}:
            raise ValueError("An elbow did not connect the two reviewed segments.")
        normals = [reader.xyz(port.CoordinateSystem.BasisZ) for port in ports]
        if any(abs(math.hypot(*normal) - 1) > DISTANCE_TOLERANCE for normal in normals):
            raise ValueError("An elbow has an invalid port axis.")
        cosine = max(-1.0, min(1.0, -planner.dot(*normals)))
        angle = math.acos(cosine)
        if abs(angle - math.radians(route["bends_degrees"][index])) > 1e-5:
            raise ValueError("An elbow differs from its reviewed bend angle.")
        if any(str(port.Shape) != "Round" or not matches_size(2 * port.Radius, diameter)
               for port in ports):
            raise ValueError("An elbow differs from the reviewed connector size.")
        expected.extend(((left, matched[port_key(left)]), (right, matched[port_key(right)])))
        observations.append({"id": str(elbow_id.Value), "uid": str(elbow.UniqueId),
                             "bend_degrees": math.degrees(angle),
                             "bounds_ft": check_bounds(reader, elbow, data, original)})

    requested = [{key: row[key] for key in ("element_id", "element_uid", "connector_id")}
                 for row in original["endpoints"]]
    for selected, terminal in zip(requested, (ordered[0][0], ordered[-1][1])):
        owner = document.GetElement(reader.element_id(selected["element_id"]))
        expected.append((find_port(reader, owner, clock, budget, expected_id=selected["connector_id"]), terminal))
    for left, right in expected:
        if (math.dist(reader.xyz(left.Origin), reader.xyz(right.Origin)) > DISTANCE_TOLERANCE
                or physical_refs(reader, left, clock, budget) != port_key(right)
                or physical_refs(reader, right, clock, budget) != port_key(left)):
            raise ValueError("The physical route is not the reviewed unbranched chain.")

    _, transform, _ = reader.resolve_source(document, None, None)
    actual = reader.read_route_trial_owners(document, transform, requested, clock, budget, require_open=False)
    for selected, owner in zip(requested, actual["owners"]):
        endpoint = next(port for port in owner["connectors"] if port["id"] == selected["connector_id"])
        endpoint["references"] = []
    for system in actual["systems"]:
        system["members"] = [member for member in system["members"] if member[0] not in created]
    if actual != original:
        raise ValueError("An existing owner, sibling connector or system changed outside the reviewed edges.")
    return observations


def run(document, args, reader=None):
    reader = reader or preflight.installed_reader()
    accepted = preflight.prepare(document, args, reader)
    if accepted != args["preflight"]:
        raise ValueError("The reviewed route preflight does not match the live fixture.")
    data = json.loads(args["route_input_json"])
    route = args["preview"]["route"]
    original = args["owners_record"]["snapshot"]
    types = args["types_record"]["types"]
    clock = Stopwatch.StartNew()
    budget = {"connectors_visited": 0}
    created, observed, events = set(), set(), []
    untrusted = False
    rolling_back = False

    def record(_sender, event):
        nonlocal untrusted
        try:
            if not event.GetDocument().Equals(document):
                untrusted = True
                return
            op = str(event.Operation)
            added = event.GetAddedElementIds()
            if added.Count > MAX_ADDITIONS:
                untrusted = True
            for index, item in enumerate(added):
                if index >= MAX_ADDITIONS:
                    break
                observed.add(str(item.Value))
            if rolling_back and op in ("TransactionRolledBack", "TransactionGroupRolledBack"):
                if added.Count:
                    untrusted = True
                return
            modified, deleted = event.GetModifiedElementIds(), event.GetDeletedElementIds()
            if modified.Count > 2 or deleted.Count:
                untrusted = True
            if (rolling_back or events or op != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [TRANSACTION_NAME] or untrusted):
                untrusted = True
                events.append(None)
            else:
                events.append((set(observed), {str(item.Value) for item in modified}, set()))
        except Exception:
            untrusted = True

    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    application = document.Application
    receipt, failure, regeneration_failed, rolled_back = None, None, False, False
    application.DocumentChanged += record
    try:
        if group.Start() != DB.TransactionStatus.Started or transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the route trial transaction.")
        placed = place(document, reader, route, types, original, created, clock, budget)
        if transaction.Commit() != DB.TransactionStatus.Committed or transaction.GetStatus() != DB.TransactionStatus.Committed:
            raise ValueError("The route trial transaction did not reach a verified commit.")
        changes = capture_changes(events)
        allowed = {row["element_id"] for row in original["endpoints"]}
        if set(changes["added"]) != created or set(changes["modified"]) != allowed:
            raise ValueError("Revit changed elements outside the reviewed route.")
        measurements = verify_route(document, reader, route, types, original, data, placed, created, clock, budget)
        receipt = {"preflight": accepted, "changes": changes, "measurements": measurements}
        encoded = json.dumps(receipt, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        envelope = json.dumps({"status": "mep_route_trial_rolled_back", "receipt": receipt}, ensure_ascii=True)
        if len(encoded) > 512 * 1024 or len(envelope) > 1024 * 1024:
            raise ValueError("The measured route receipt exceeds its size limit.")
    except RegenerationFailedException:
        regeneration_failed = True
        failure = "Revit regeneration failed. Independently reconcile the fixture before retrying."
    except Exception as error:
        failure = str(error)
    finally:
        rolling_back = True
        try:
            state = transaction.GetStatus()
            if state == DB.TransactionStatus.Started:
                transaction.RollBack()
                state = transaction.GetStatus()
            if state in (DB.TransactionStatus.Committed, DB.TransactionStatus.RolledBack,
                         DB.TransactionStatus.Uninitialized) and group.GetStatus() == DB.TransactionStatus.Started:
                rolled_back = (group.RollBack() == DB.TransactionStatus.RolledBack
                               and group.GetStatus() == DB.TransactionStatus.RolledBack)
        except Exception:
            untrusted = True
        finally:
            application.DocumentChanged -= record

    ids = sorted(created | observed, key=int)
    if regeneration_failed or not rolled_back or untrusted:
        return {"status": "transaction_unresolved", "observed_additions": ids,
                "message": failure or "Revit did not establish a clean route rollback."}
    try:
        if any(document.GetElement(reader.element_id(item)) is not None for item in ids):
            raise ValueError("Revit still resolves a trial addition after rollback.")
        if preflight.prepare(document, args, reader) != accepted:
            raise ValueError("The route baseline differs after rollback.")
    except Exception as error:
        return {"status": "transaction_unresolved", "observed_additions": ids, "message": str(error)}
    if failure is not None:
        return {"status": "stopped_and_rolled_back", "observed_additions": ids, "message": failure}
    return {"status": "mep_route_trial_rolled_back", "receipt": receipt}


if "inputs" in globals():
    try:
        result = run(doc, inputs)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, ImportError, RecursionError) as error:
        result = {"status": "stopped", "message": str(error)}
