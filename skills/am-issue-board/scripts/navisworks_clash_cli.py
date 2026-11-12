"""Preview Navisworks clash intake and optional Revit section bounds."""

import argparse
import hashlib
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import uuid
import xml.etree.ElementTree as ET

import bcf_cli
import board_cli
import issue_board as ib

LIMIT = 2 * 1024 * 1024
MARKER = "Navisworks clash UUID: "
STATUSES = {"new", "active", "reviewed", "approved", "resolved"}
FRAME = "revit_host_internal_feet"
RESULT = ("exchange", "batchtest", "clashtests", "clashtest", "clashresults")


def local_bytes(raw):
    windows_drive = os.name == "nt" and re.match(r"^[a-zA-Z]:[/\\]", raw)
    if raw.startswith(("//", "\\")) or ":" in (raw[2:] if windows_drive else raw):
        raise ValueError("Use a regular local input file.")
    path = Path(raw)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    original = path.lstat()
    if (not stat.S_ISREG(original.st_mode) or getattr(original, "st_file_attributes", 0) & 0x400
            or original.st_size > LIMIT):
        raise ValueError("Use a regular local input file up to 2 MiB.")
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not os.path.samestat(original, before) or not stat.S_ISREG(before.st_mode):
            raise ValueError("Input identity changed before reading.")
        data = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        if not (len(data) <= LIMIT and len(data) == before.st_size == after.st_size == current.st_size
                and before.st_mtime_ns == after.st_mtime_ns == current.st_mtime_ns
                and os.path.samestat(before, current)):
            raise ValueError("Input changed while reading.")
    return data


def json_input(data):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate JSON field.")
            result[name] = value
        return result

    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)


def text(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("Report text exceeds the supported limit.")
    return value.strip()


def object_side(node):
    candidates = set()
    for attr in node.findall("objectattribute") + node.findall("smarttags/smarttag"):
        if text(attr.findtext("name", "")).casefold() == "element id":
            candidates.add(text(attr.findtext("value", "")))
    item_id = next(iter(candidates)) if len(candidates) == 1 else None
    if item_id and not (
        re.fullmatch(r"[1-9][0-9]{0,18}", item_id) and int(item_id) <= 2**63 - 1
    ):
        item_id = None
    paths = node.findall("pathlink")
    nodes = [text(entry.text or "") for entry in paths[0].findall("node")] if len(paths) == 1 else []
    return {"element_id": item_id, "selection_path": nodes}


def valid_result_path(path):
    trail = tuple(path[len(RESULT):-1])
    return tuple(path[:len(RESULT)]) == RESULT and trail == ("clashgroup", "clashresults") * (len(trail) // 2)


def parse_report(data):
    decoded = data.decode("utf-8-sig")
    if "\0" in decoded or re.search(r"<!\s*(DOCTYPE|ENTITY)\b", decoded, re.I):
        raise ValueError("DTD, entity declarations and NUL bytes are forbidden.")
    path = []
    report = []
    seen = set()
    nodes = 0
    tests = 0
    units = ""
    for event, node in ET.iterparse(io.StringIO(decoded), events=("start", "end")):
        if event == "start":
            path.append(node.tag)
            nodes += 1
            if nodes > 20000 or len(path) > 24:
                raise ValueError("XML exceeds structure limits.")
            if "}" in node.tag or (nodes == 1 and node.tag != "exchange"):
                raise ValueError("Select an unnamespaced Navisworks exchange XML report.")
            if nodes == 1:
                units = text(node.get("units", ""))
            if tuple(path) == RESULT[:-1]:
                tests += 1
            continue
        if node.tag == "clashresult":
            if not valid_result_path(path) or len(report) >= 300:
                raise ValueError("Unexpected result nesting or too many clashes.")
            source_guid = text(node.get("guid", ""))
            try:
                guid = str(uuid.UUID(source_guid))
            except ValueError:
                guid = None
            if guid is not None:
                if guid in seen:
                    raise ValueError("Duplicate clash GUID.")
                seen.add(guid)
            groups = node.findall("clashobjects")
            objects = groups[0].findall("clashobject") if len(groups) == 1 else []
            source_point = node.find("clashpoint/pos3f")
            coordinates = point([float(source_point.attrib[axis]) for axis in "xyz"]) if source_point is not None else None
            report.append({
                "guid": guid, "name": text(node.get("name", "")),
                "source_status": text(node.get("status", "")).casefold(),
                "source_point": coordinates,
                "objects": [object_side(item) for item in objects] if len(objects) == 2 else [],
            })
        path.pop()
    if not tests:
        raise ValueError("No Clash Detective test found in this exchange file.")
    return units, report


class TabularReport(HTMLParser):
    """Read the default Navisworks tabular table as inert text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.nodes = 0
        self.sequential = False

    def handle_decl(self, declaration):
        if declaration.lower().strip() != "doctype html":
            raise ValueError("Unexpected HTML declaration.")

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 20000 or tag in ("script", "iframe", "object", "embed"):
            raise ValueError("Unsupported HTML content or structure limit.")
        fields = dict(attrs)
        css = fields.get("class", "")
        if tag == "div" and css == "animation":
            self.sequential = True
        if tag == "table":
            if self.table is not None:
                raise ValueError("Unexpected nested Navisworks table.")
            if css == "mainTable":
                if len(self.tables) >= 100:
                    raise ValueError("Too many Navisworks tables.")
                self.table = []
        elif self.table is not None and tag == "tr":
            if self.row is not None or css not in ("headerRow", "contentRow"):
                raise ValueError("Unexpected Navisworks row layout.")
            if len(self.table) >= 302:
                raise ValueError("Too many Navisworks table rows.")
            self.row = (css, [])
        elif self.table is not None and tag in ("td", "th"):
            if self.row is None or self.cell is not None or len(self.row[1]) >= 50:
                raise ValueError("Unexpected Navisworks cell layout.")
            if fields.get("rowspan", "1") != "1":
                raise ValueError("Unexpected Navisworks cell span.")
            raw_span = fields.get("colspan", "1")
            if not re.fullmatch(r"[1-9][0-9]?", raw_span) or int(raw_span) > 50:
                raise ValueError("Unexpected Navisworks cell span.")
            self.cell = (css, int(raw_span), [])
        elif self.cell is not None and tag == "br":
            self.cell[2].append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[2].append(data)

    def handle_endtag(self, tag):
        if self.table is None:
            return
        if tag in ("td", "th"):
            if self.cell is None:
                raise ValueError("Unexpected Navisworks cell closing tag.")
            css, span, parts = self.cell
            self.row[1].append((css, span, " ".join("".join(parts).split())))
            self.cell = None
        elif tag == "tr":
            if self.cell is not None or self.row is None:
                raise ValueError("Incomplete Navisworks row.")
            self.table.append(self.row)
            self.row = None
        elif tag == "table":
            if self.row is not None:
                raise ValueError("Incomplete Navisworks table.")
            self.tables.append(self.table)
            self.table = None


HTML_LABELS = {
    "name": {"clash name", "nom de conflit"},
    "status": {"status", "etat", "état"},
    "element": {"element id", "id d'élément"},
}
HTML_ITEM_CLASSES = ({"item1Content", "élément1Contenu"}, {"item2Content", "élément2Contenu"})


def html_element_id(value):
    # An explicitly labelled Element ID column may repeat the label before the value.
    candidate = re.sub(r"^(?:element id|id d'élément)\s*:\s*", "", value, flags=re.I)
    return candidate if re.fullmatch(r"[1-9][0-9]{0,18}", candidate) and int(candidate) <= 2**63 - 1 else None


def tabular_columns(rows):
    if len(rows) < 2 or [row[0] for row in rows[:2]] != ["headerRow", "headerRow"]:
        raise ValueError("Expected the default two-level Navisworks table header.")
    group, columns = rows[0][1], rows[1][1]
    if sum(cell[1] for cell in group) != sum(cell[1] for cell in columns):
        raise ValueError("Navisworks header spans disagree.")
    group_width = {css: span for css, span, _ in group if css in ("item1HeaderGroup", "item2HeaderGroup")}
    if len(group_width) != 2 or len([cell for cell in group if cell[0] in group_width]) != 2:
        raise ValueError("The report must identify both Navisworks item groups.")
    keys = {}
    expected_cells = []
    position = 0
    group_slots = [css for css, span, _ in group for _ in range(span)]
    for index, (css, span, heading) in enumerate(columns):
        field = None
        if css == "generalHeader":
            if any(slot in group_width for slot in group_slots[position:position + span]):
                raise ValueError("General and item columns overlap.")
            expected_cells.append(({"contentCell"}, span))
            field = next((name for name in ("name", "status") if heading.casefold() in HTML_LABELS[name]), None)
        elif css in ("item1Header", "item2Header"):
            side = int(css[4]) - 1
            if group_slots[position:position + span] != [css + "Group"] * span:
                raise ValueError("A Navisworks item column is outside its group.")
            expected_cells.append((HTML_ITEM_CLASSES[side], span))
            if heading.casefold() in HTML_LABELS["element"]:
                field = "element%d" % side
        else:
            raise ValueError("Unknown Navisworks table column.")
        if field is not None:
            if field in keys:
                raise ValueError("The report repeats a Navisworks field.")
            keys[field] = index
        position += span
    if "name" not in keys:
        raise ValueError("The report does not identify individual clash rows.")
    return keys, expected_cells


def tabular_clashes(reader):
    if reader.table is not None or not reader.tables:
        raise ValueError("The report is not a complete default Navisworks tabular export.")
    report = []
    for rows in reader.tables:
        keys, expected = tabular_columns(rows)
        for css, cells in rows[2:]:
            if css != "contentRow" or len(cells) != len(expected) or len(report) >= 300:
                raise ValueError("Unsupported tabular clash row or report size.")
            if any(actual[0] not in layout[0] or actual[1] != layout[1]
                   for actual, layout in zip(cells, expected)):
                raise ValueError("Clash row does not match its Navisworks headers.")
            selected = {field: text(cells[index][2]) for field, index in keys.items()}
            if not selected["name"]:
                raise ValueError("A clash row does not have a name.")
            report.append({"guid": None, "name": selected["name"],
                           "source_status": selected.get("status", "").casefold(), "source_point": None,
                           "objects": [{"element_id": html_element_id(selected.get("element%d" % side, "")),
                                        "selection_path": []} for side in (0, 1)]})
    return report


class SequentialReport(HTMLParser):
    """Read one viewpoint at a time from the default sequential layout."""

    CONTAINER = (("html", ""), ("body", ""), ("div", "animation"))
    VIEWPOINT = ("div", "viewpoint")
    PAIR = ("span", "namevaluepair")
    SIDE = ("h4", "clashobject")
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                 "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.clashes = []
        self.fields = None
        self.items = []
        self.pair = None
        self.parts = None

    def handle_starttag(self, tag, attrs):
        css = dict(attrs).get("class", "")
        node = (tag, css)
        parent = tuple(self.stack)
        viewpoint = self.CONTAINER + (self.VIEWPOINT,)
        if len(parent) >= 24 or self.parts is not None:
            raise ValueError("Unsupported nested sequential clash field.")
        if node == self.VIEWPOINT:
            if parent != self.CONTAINER or len(self.clashes) >= 300:
                raise ValueError("Unexpected clash viewpoint or report size.")
            self.fields, self.items = {}, []
        if parent == viewpoint and node == self.SIDE:
            self.parts = []
        if parent == viewpoint and node == self.PAIR:
            self.pair = []
        if parent == viewpoint + (self.PAIR,):
            if len(self.pair) >= 2 or node != ("span", ("name", "value")[len(self.pair)]):
                raise ValueError("Unexpected sequential property field.")
            self.parts = []
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("Incomplete sequential clash markup.")
        node = self.stack.pop()
        parent = tuple(self.stack)
        viewpoint = self.CONTAINER + (self.VIEWPOINT,)
        if parent == viewpoint + (self.PAIR,):
            self.pair.append(" ".join("".join(self.parts).split()))
            self.parts = None
        elif parent == viewpoint and node == self.PAIR:
            if len(self.pair) != 2:
                raise ValueError("Incomplete sequential clash property.")
            label, value = self.pair
            label = label.casefold()
            if not self.items and label in ("name", "status"):
                if label in self.fields:
                    raise ValueError("The viewpoint repeats a clash field.")
                self.fields[label] = text(value)
            if self.items and label in HTML_LABELS["element"]:
                if "element_id" in self.items[-1]:
                    raise ValueError("The item repeats an Element ID.")
                self.items[-1]["element_id"] = html_element_id(text(value))
            self.pair = None
        elif parent == viewpoint and node == self.SIDE:
            label = " ".join("".join(self.parts).split()).casefold()
            if len(self.items) >= 2 or label != "item %d" % (len(self.items) + 1):
                raise ValueError("Expected ordered Item 1 and Item 2 sections.")
            self.items.append({"selection_path": []})
            self.parts = None
        elif parent == self.CONTAINER and node == self.VIEWPOINT:
            if len(self.items) != 2 or not self.fields.get("name"):
                raise ValueError("The sequential clash lacks a name or two item sections.")
            for item in self.items:
                item.setdefault("element_id", None)
            self.clashes.append({"guid": None, "name": self.fields["name"],
                                 "source_status": self.fields.get("status", "").casefold(),
                                 "source_point": None, "objects": self.items})


def parse_html_report(data):
    decoded = data.decode("utf-8-sig")
    if "\0" in decoded or re.search(r"<!\s*(ENTITY|DOCTYPE[^>]*\[)", decoded, re.I):
        raise ValueError("Unexpected HTML declaration.")
    layout = TabularReport()
    layout.feed(decoded)
    layout.close()
    if not layout.sequential:
        return "html_tabular", tabular_clashes(layout)
    if layout.tables or layout.table is not None:
        raise ValueError("The HTML mixes tabular and sequential layouts.")
    reader = SequentialReport()
    reader.feed(decoded)
    reader.close()
    if reader.stack or not reader.clashes:
        raise ValueError("The report is not a complete default sequential Navisworks export.")
    return "html_sequential", reader.clashes


def point(value):
    if (not isinstance(value, list) or len(value) != 3
            or any(type(n) not in (int, float) or not -1e6 <= n <= 1e6 for n in value)):
        raise ValueError("Bounds must contain three finite host coordinates.")
    return value


def element_snapshot(raw):
    doc = json_input(raw)
    if doc["coordinate_frame"] != FRAME or not text(doc["host_id"]):
        raise ValueError("Use a named Revit host snapshot in internal feet.")
    sources = doc["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise ValueError("Use 1 to 32 source model instances.")
    index = {}
    ids = set()
    total = 0
    for source in sources:
        model = text(source["model_id"])
        prefix = source["path_prefix"]
        if not isinstance(prefix, list) or not 1 <= len(prefix) <= 20:
            raise ValueError("A source needs an exact selection-tree path prefix.")
        prefix = tuple(text(node) for node in prefix)
        if not model or not all(prefix) or prefix in index or model in ids:
            raise ValueError("Duplicate or empty source model mapping.")
        ids.add(model)
        entries = source["elements"]
        if not isinstance(entries, dict):
            raise ValueError("Use an element ID dictionary.")
        total += len(entries)
        if total > 10000:
            raise ValueError("Element snapshot exceeds the supported count.")
        elements = {}
        unique = set()
        for key, item in entries.items():
            low, high = point(item["min"]), point(item["max"])
            uid = text(item["unique_id"])
            if (not re.fullmatch(r"[1-9][0-9]{0,18}", key) or int(key) > 2**63 - 1
                    or not uid or uid in unique or not all(0 <= b - a <= 1000 for a, b in zip(low, high))
                    or low == high):
                raise ValueError("Invalid or duplicate element identity or bounds.")
            unique.add(uid)
            elements[key] = {"model_id": model, "unique_id": uid, "min": low, "max": high}
        index[prefix] = elements
    return doc["host_id"], index


def section_box(objects, index, padding):
    if len(objects) != 2:
        return None
    targets = []
    for item in objects:
        path = tuple(item["selection_path"])
        matched = [elements for prefix, elements in index.items() if path[:len(prefix)] == prefix]
        if len(matched) != 1 or item["element_id"] not in matched[0]:
            return None
        targets.append(matched[0][item["element_id"]])
    if (targets[0]["model_id"], targets[0]["unique_id"]) == (targets[1]["model_id"], targets[1]["unique_id"]):
        return None
    low = [min(a, b) - padding for a, b in zip(targets[0]["min"], targets[1]["min"])]
    high = [max(a, b) + padding for a, b in zip(targets[0]["max"], targets[1]["max"])]
    if not all(-1e6 <= a < b <= 1e6 and b - a <= 1000 for a, b in zip(low, high)):
        return None
    return {"min": low, "max": high, "targets": [
        {key: item[key] for key in ("model_id", "unique_id")} for item in targets
    ]}


def board_rows(path, *, archive=False):
    body, formats = board_cli.decode_note(json_input(local_bytes(path)))
    board = ib.parse_board(body)
    issues = board["issues"]
    numbers = {row["number"] for row in issues}
    markers = set()
    header = board["header"]
    if (header["archive"] != archive or not header["title"] or len(numbers) != len(issues)
            or (not archive and (header["next_number"] or 0) <= max(numbers, default=0))):
        raise ValueError("Invalid board header, role or issue numbers.")
    for row in issues:
        for finding in row["findings"]:
            if finding.startswith(MARKER):
                raw = finding[len(MARKER):]
                if str(uuid.UUID(raw)) != raw or raw in markers:
                    raise ValueError("Board has invalid or duplicate clash provenance.")
                markers.add(raw)
    return body, formats, issues, markers


def board_preview(path, archive, clashes):
    body, formats, existing_rows, seen = board_rows(path)
    if archive:
        _, _, _, archived = board_rows(archive, archive=True)
        if seen & archived:
            raise ValueError("Clash provenance exists on both active and archive notes.")
        seen.update(archived)
    proposals = []
    skipped = []
    for row in clashes:
        guid = row["guid"]
        if guid is None:
            continue
        if guid in seen:
            skipped.append(guid)
            continue
        title = bcf_cli.literal(row["name"])
        title = re.sub(re.escape(MARKER), "Navisworks source reference: ", title, flags=re.I)
        title = "Clash: " + (title or guid)
        issue = {"title": title, "status": "OPEN", "findings": [
            MARKER + guid,
            "Section bounds proposed for review." if row["section_box"] else "Element mapping requires review.",
        ]}
        proposals.append({"op": "add", "issue": issue})
    if not proposals:
        return None, skipped
    plan = ib.plan_batch(body, proposals, formats)
    after = ib.parse_board(plan["final_text"])["issues"]
    by_number = {row["number"]: row for row in after}
    if (len(after) != len(existing_rows) + len(proposals) or len(after) != len(by_number)
            or any(by_number.get(row["number"]) != row for row in existing_rows)):
        raise ValueError("Preview changed existing issues or issue counts.")
    for operation, request in zip(plan["operations"], proposals):
        row = by_number[int(operation.removeprefix("add #"))]
        issue = request["issue"]
        if (row["title"] != issue["title"] or row["status"] != issue["status"]
                or row["findings"] != issue["findings"] or row["assignees"] != ["@TBD"]
                or row["parent"] is not None
                or any(row[key] for key in ("element_ids", "related", "todos", "comments", "severity"))):
            raise ValueError("Preview did not preserve literal clash data.")
    return plan, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--snapshot")
    parser.add_argument("--padding-ft", type=float, default=1.0)
    parser.add_argument("--board")
    parser.add_argument("--archive")
    args = parser.parse_args(argv)
    suffix = Path(args.report).suffix.lower()
    is_html = suffix in (".html", ".htm")
    try:
        if suffix != ".xml" and not is_html:
            raise ValueError("Use a Navisworks XML or default HTML report.")
        if not math.isfinite(args.padding_ft) or not 0.1 <= args.padding_ft <= 20:
            raise ValueError("Use padding from 0.1 to 20 feet.")
        if args.archive and not args.board:
            raise ValueError("Supply an active board with the archive.")
        raw = local_bytes(args.report)
        if is_html:
            source_format, clashes = parse_html_report(raw)
            units = ""
        else:
            source_format = "xml"
            units, clashes = parse_report(raw)
        snapshot = local_bytes(args.snapshot) if args.snapshot else None
        host, index = element_snapshot(snapshot) if snapshot is not None else (None, {})
        for row in clashes:
            row["section_box"] = section_box(row["objects"], index, args.padding_ft)
            row["warnings"] = []
            if row["guid"] is None:
                row["warnings"].append("Missing or invalid clash GUID.")
            if row["source_status"] not in STATUSES:
                row["warnings"].append("Unknown Navisworks status.")
            if row["section_box"] is None:
                row["warnings"].append("Both distinct elements must match one scoped Revit snapshot.")
        plan, skipped = board_preview(args.board, args.archive, clashes) if args.board else (None, [])
        report = {
            "kind": "navisworks-clash-preview", "model_write_approved": False,
            "source_format": source_format,
            "report_sha256": hashlib.sha256(raw).hexdigest(), "source_units": units,
            "snapshot_sha256": hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None,
            "host_id": host, "coordinate_frame": FRAME if host else None,
            "clashes": clashes, "plan": plan, "skipped_guids": skipped,
            "warnings": ["Archive note not checked for duplicates."] if args.board and not args.archive else [],
        }
        if is_html:
            report["warnings"].append("HTML review has no mapped clash identity. Export XML for model mapping and board import.")
        encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2)
        if len(encoded) > LIMIT:
            raise ValueError("Preview exceeds the supported size.")
        print(encoded)
    except OSError:
        print("Navisworks intake failed: a local input could not be opened.", file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, ET.ParseError, ib.BoardError):
        # Parser and board exceptions may include raw report or model content.
        print("Navisworks intake failed: invalid input, format, identity or limit.", file=sys.stderr)
        if suffix != ".xml":
            print("Use a Navisworks XML or a supported default ungrouped HTML report.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
