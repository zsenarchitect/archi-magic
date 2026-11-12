"""Preview Navisworks clash intake and optional Revit section bounds."""

import argparse
from collections import Counter
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
from clash_section import FRAME, element_snapshot, point, section_box, section_view_preview, text
import issue_board as ib

LIMIT = 2 * 1024 * 1024
MARKER = "Navisworks clash UUID: "
BOUNDS_PENDING = "Section bounds proposed for review."
MAPPING_PENDING = "Element mapping requires review."
STATUSES = {"new", "active", "reviewed", "approved", "resolved"}
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
    group_names = []
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
            if node.tag == "clashgroup":
                group_names.append(text(node.get("name", "")))
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
            if group_names and group_names[-1]:
                report[-1]["source_group"] = group_names[-1]
        if node.tag == "clashgroup":
            group_names.pop()
        path.pop()
    if not tests:
        raise ValueError("No Clash Detective test found in this exchange file.")
    return units, report


TABULAR_ROWS = {"headerRow", "contentRow", "clashGroupRow", "childRow", "childRowLast"}


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
            if self.row is not None or css not in TABULAR_ROWS:
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


def html_element_id(value, *, labelled=False):
    # An explicitly labelled Element ID column may repeat the label before the value.
    candidate, labels = re.subn(r"^(?:element id|id d'élément)\s*:\s*", "", value, flags=re.I)
    if labelled and not labels:
        return None
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
            if heading.casefold() == "clash group":
                field = "group"
        elif css in ("item1Header", "item2Header"):
            side = int(css[4]) - 1
            if group_slots[position:position + span] != [css + "Group"] * span:
                raise ValueError("A Navisworks item column is outside its group.")
            expected_cells.append((HTML_ITEM_CLASSES[side], span))
            if heading.casefold() in HTML_LABELS["element"]:
                field = "element%d" % side
            elif heading.casefold() == "item id":
                field = "item%d" % side
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


def tabular_cells(cells, expected, row_class):
    summary = row_class == "clashGroupRow"
    layout = [({"clashGroupCell"} if summary and classes == {"contentCell"} else classes, span)
              for classes, span in expected]
    missing = [set()]
    if row_class == "contentRow" and len(cells) < len(layout):
        # A default report can omit an entire unavailable item, while retaining later columns.
        missing = [{index for index, (allowed, _) in enumerate(layout) if allowed == classes}
                   for classes in HTML_ITEM_CLASSES]
    matches = []
    for omitted in missing:
        present = [index for index in range(len(layout)) if index not in omitted]
        if len(cells) == len(present) and all(
                cell[0] in layout[index][0] and cell[1] == layout[index][1]
                for cell, index in zip(cells, present)):
            matches.append(dict(zip(present, cells)))
    if len(matches) != 1:
        raise ValueError("Clash row does not match its Navisworks headers.")
    return matches[0]


def tabular_clashes(reader):
    if reader.table is not None or not reader.tables:
        raise ValueError("The report is not a complete default Navisworks tabular export.")
    report = []
    summaries = 0
    incomplete_groups = 0
    for rows in reader.tables:
        keys, expected = tabular_columns(rows)
        group_name = None
        child_started = False
        for css, cells in rows[2:]:
            if css == "headerRow":
                raise ValueError("Unsupported tabular clash row or report size.")
            summary = css == "clashGroupRow"
            aligned = tabular_cells(cells, expected, css)
            selected = {field: text(aligned[index][2]) for field, index in keys.items() if index in aligned}
            if not selected["name"]:
                raise ValueError("A clash row does not have a name.")
            row_group = selected.get("group")
            if css in ("childRow", "childRowLast"):
                if group_name is None or ("group" in keys and selected["group"] != group_name):
                    raise ValueError("A child clash is outside its reported group.")
                row_group = group_name
                child_started = css == "childRow"
                if not child_started:
                    group_name = None
            else:
                if child_started:
                    raise ValueError("A clash group is missing its final child row.")
                incomplete_groups += int(group_name is not None)
                group_name = selected["name"] if summary else None
            if summary:
                summaries += 1
                continue
            if len(report) >= 300:
                raise ValueError("Too many individual clashes.")
            objects = []
            for side in (0, 1):
                element_key = "element%d" % side
                value = (html_element_id(selected[element_key]) if element_key in selected
                         else html_element_id(selected.get("item%d" % side, ""), labelled=True))
                objects.append({"element_id": value, "selection_path": []})
            report.append({"guid": None, "name": selected["name"],
                           "source_status": selected.get("status", "").casefold(), "source_point": None,
                           "objects": objects})
            if row_group:
                report[-1]["source_group"] = row_group
        if child_started:
            raise ValueError("A clash group is missing its final child row.")
        incomplete_groups += int(group_name is not None)
    warnings = []
    if summaries:
        warnings.append("Skipped %d group summary rows." % summaries)
    if incomplete_groups:
        warnings.append("Some groups have no individual rows. Export Everything to review all clashes.")
    return report, warnings


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
        self.warnings = []
        self.test_number = 0
        self.test_start = 0
        self.test_totals = []

    def handle_starttag(self, tag, attrs):
        css = dict(attrs).get("class", "")
        node = (tag, css)
        parent = tuple(self.stack)
        viewpoint = self.CONTAINER + (self.VIEWPOINT,)
        if len(parent) >= 24 or self.parts is not None:
            raise ValueError("Unsupported nested sequential clash field.")
        if parent + (node,) == self.CONTAINER:
            self.test_number += 1
            self.test_start = len(self.clashes)
            self.test_totals = []
        if node == self.VIEWPOINT:
            if parent != self.CONTAINER:
                raise ValueError("Unsupported nested sequential report. Export Individual Clashes Only or HTML (Tabular).")
            if len(self.clashes) >= 300:
                raise ValueError("Too many individual clashes.")
            self.fields, self.items = {}, []
        if parent == viewpoint and node == self.SIDE:
            self.parts = []
        if parent in (self.CONTAINER, viewpoint) and node == self.PAIR:
            self.pair = []
        if parent in (self.CONTAINER + (self.PAIR,), viewpoint + (self.PAIR,)):
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
        if parent in (self.CONTAINER + (self.PAIR,), viewpoint + (self.PAIR,)):
            self.pair.append(" ".join("".join(self.parts).split()))
            self.parts = None
        elif parent in (self.CONTAINER, viewpoint) and node == self.PAIR:
            if len(self.pair) != 2:
                raise ValueError("Incomplete sequential clash property.")
            label, value = self.pair
            label = label.casefold()
            if parent == self.CONTAINER:
                if label == "total":
                    self.test_totals.append(value)
            elif not self.items and label in ("name", "status", "clash group"):
                if label in self.fields:
                    raise ValueError("The viewpoint repeats a clash field.")
                self.fields[label] = text(value)
            if parent == viewpoint and self.items and label in HTML_LABELS["element"]:
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
                raise ValueError("A sequential entry lacks a name or two items. Export Individual Clashes Only with item data.")
            for item in self.items:
                item.setdefault("element_id", None)
            self.clashes.append({"guid": None, "name": self.fields["name"],
                                 "source_status": self.fields.get("status", "").casefold(),
                                 "source_point": None, "objects": self.items})
            if self.fields.get("clash group"):
                self.clashes[-1]["source_group"] = self.fields["clash group"]
        elif parent + (node,) == self.CONTAINER and self.test_totals:
            if len(self.test_totals) != 1 or not re.fullmatch(r"[0-9]{1,9}", self.test_totals[0]):
                self.warnings.append(f"Sequential test {self.test_number} has an unreadable clash total.")
            else:
                declared = int(self.test_totals[0])
                present = len(self.clashes) - self.test_start
                if declared != present:
                    suffix = "" if present == 1 else "s"
                    self.warnings.append(f"Sequential test {self.test_number} reports {declared} total clashes "
                                         f"and contains {present} individual row{suffix}. Check the report filters.")


def parse_html_report(data):
    decoded = data.decode("utf-8-sig")
    if "\0" in decoded or re.search(r"<!\s*(ENTITY|DOCTYPE[^>]*\[)", decoded, re.I):
        raise ValueError("Unexpected HTML declaration.")
    layout = TabularReport()
    layout.feed(decoded)
    layout.close()
    if not layout.sequential:
        clashes, warnings = tabular_clashes(layout)
        return "html_tabular", clashes, warnings
    if layout.tables or layout.table is not None:
        raise ValueError("The HTML mixes tabular and sequential layouts.")
    reader = SequentialReport()
    reader.feed(decoded)
    reader.close()
    if reader.stack or not reader.clashes:
        raise ValueError("The report is not a complete default sequential Navisworks export.")
    return "html_sequential", reader.clashes, reader.warnings


def paired_clashes(html_rows, xml_rows):
    translated = {"nouveau": "new", "activé": "active", "vérifié": "reviewed",
                  "approuvé": "approved", "résolu": "resolved"}

    def key(row):
        sides = tuple(item["element_id"] for item in row["objects"])
        status = translated.get(row["source_status"], row["source_status"])
        if row["name"] and status in STATUSES and len(sides) == 2 and all(sides):
            return row["name"], status, sides
        return None

    counts = Counter(key(row) for row in html_rows)
    indexed = {}
    for row in xml_rows:
        match_key = key(row)
        if match_key is not None:
            indexed.setdefault(match_key, []).append(row)
    result = []
    for row in html_rows:
        match_key = key(row)
        candidates = indexed.get(match_key, [])
        status = "unmatched"
        paired = dict(row)
        if match_key is None:
            status = "incomplete_html_identity"
        elif counts[match_key] != 1 or len(candidates) > 1:
            status = "ambiguous"
        elif candidates:
            match = candidates[0]
            if row.get("source_group") and row["source_group"] != match.get("source_group"):
                status = "different_group"
            elif not match["guid"] or not all(
                item["selection_path"] and all(item["selection_path"]) for item in match["objects"]
            ):
                status = "incomplete_xml_identity"
            else:
                paired.update(guid=match["guid"], objects=match["objects"], source_point=match["source_point"],
                              source_status=match_key[1])
                if row["source_status"] != match_key[1]:
                    paired["html_source_status"] = row["source_status"]
                status = "matched"
        result.append(dict(paired, xml_identity=status))
    return result


def snapshot_document(raw):
    doc = json_input(raw)
    doc = doc.get("response", doc)
    if "result" in doc:
        if doc.get("status", "ok") != "ok" or doc.get("error"):
            raise ValueError("The Revit clash snapshot did not complete.")
        doc = doc["result"]
    if "snapshot_json" in doc:
        if doc.get("status") != "ok":
            raise ValueError("The Revit clash snapshot did not complete.")
        encoded = doc["snapshot_json"]
        if not isinstance(encoded, str) or not encoded.isascii() or len(encoded) > LIMIT:
            raise ValueError("The Revit clash snapshot is unavailable or exceeds its limit.")
        doc = json_input(encoded.encode("ascii"))
    return doc


def board_rows(source, *, archive=False):
    body, formats = board_cli.decode_note(source)
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


def board_preview(source, archive, clashes, *, verified_finding=None):
    body, formats, existing_rows, seen = board_rows(source)
    if archive is not None:
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
            verified_finding or (BOUNDS_PENDING if row["section_box"] else MAPPING_PENDING),
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


def runtime_record(path):
    data = json_input(local_bytes(path))
    return data.get("response", data)


RUNTIME_OUTCOMES = {"dry-run": ("dry_run", "ok", "rolled_back", None),
                    "keep": ("modify", "ok", "committed", "user"),
                    "discard": ("modify", "rejected", "discarded", "user")}


def runtime_changes(record, mode, status, decision, approval=None):
    if (record["mode"] != mode or record["status"] != status or record["decision"] != decision
            or record.get("approval") != approval or not text(record["run_id"])
            or record.get("reckless") or any(record.get(key) for key in (
                "error", "failures", "dialogs", "blocked", "warnings", "closed_documents", "unclosed_documents"))):
        raise ValueError("The guarded pyRevit record does not establish the requested outcome.")
    changes = record["changes"]
    if (changes.get("other_documents") or changes["deleted"] != []
            or type(changes["deleted_count"]) is not int or changes["deleted_count"] != 0):
        raise ValueError("The run reports unexpected model changes.")
    return changes


def matches_changes(changes, *, added=None, modified=None):
    for field, expected in (("added", added), ("modified", modified)):
        rows = changes[field]
        count = changes[field + "_count"]
        if type(count) is not int or not isinstance(rows, list) or count != len(rows):
            return False
        if expected is None:
            if count:
                return False
        elif count != 1 or (str(rows[0]["id"]), rows[0]["class"]) != expected:
            return False
    return True


def runtime_payload(record, field):
    result = record["result"]
    if result["status"] != "ok":
        raise ValueError("The clash script stopped before producing evidence.")
    return json_input(result[field].encode("ascii"))


def verify_view_run(plan, outcome, run, readback):
    if plan["blocked"] or run["run_id"] == readback["run_id"]:
        raise ValueError("Use an unblocked plan and an independent readback.")
    changes = runtime_changes(run, *RUNTIME_OUTCOMES[outcome])
    fresh_changes = runtime_changes(readback, "query", "ok", "rolled_back")
    if not matches_changes(fresh_changes):
        raise ValueError("The readback must leave the model unchanged.")
    receipt = runtime_payload(run, "receipt_json")
    view_id = receipt["view_id"]
    if (not re.fullmatch(r"[1-9][0-9]{0,18}", view_id) or int(view_id) >= 2**63
            or not matches_changes(changes, added=(view_id, "View3D"))):
        raise ValueError("The guarded run must report only the exact added 3D view.")
    expected = {"clash_guid": plan["view"]["clash_guid"], "view_name": plan["view"]["name"],
                "view_type_id": plan["view"]["view_type_id"], "basis_sha256": plan["basis_sha256"],
                "host_identity": plan["host_identity"]}
    if (not text(receipt["view_uid"]) or any(receipt[key] != value for key, value in expected.items())
            or any(not abs(actual - approved) <= 1e-7
                   for field, bound in (("box_min", "min"), ("box_max", "max"))
                   for actual, approved in zip(point(receipt[field]), plan["box"][bound]))):
        raise ValueError("The created view differs from the reviewed plan.")
    if outcome == "keep":
        observed = runtime_payload(readback, "verification_json")
        if observed != {"view": receipt, "potentially_visible": plan["targets"]}:
            raise ValueError("The independent query did not verify the kept view and both mapped targets.")
        return {"status": "verified_keep", "view": receipt, "visual_inspection_required": True}
    snapshot = runtime_payload(readback, "snapshot_json")
    basis = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if hashlib.sha256(basis.encode("ascii")).hexdigest() != plan["basis_sha256"]:
        raise ValueError("The original model readback changed after rollback.")
    return {"status": "verified_dry_run" if outcome == "dry-run" else "verified_discard"}


def board_identity(source, host):
    if source["host_identity"] != host:
        raise ValueError("The board is not in the verified project.")
    note = source["note"]
    note_id = note["id"]
    body, _ = board_cli.decode_note(source)
    if (not re.fullmatch(r"[1-9][0-9]{0,18}", note_id) or int(note_id) >= 2**63
            or not text(note["unique_id"]) or note["sha1"] != ib.text_sha1(body)):
        raise ValueError("Read the board again with its saved project and note identities.")
    return {key: note[key] for key in ("id", "unique_id", "sha1")}


def kept_board_preview(source, archive, clash, view):
    host = view["host_identity"]
    active = board_identity(source, host)
    archived = board_identity(archive, host) if archive is not None else None
    if archived and (active["id"] == archived["id"] or active["unique_id"] == archived["unique_id"]):
        raise ValueError("Choose distinct active and archive notes.")
    guid = clash["guid"]
    marker = MARKER + guid
    finding = "Verified Revit section view " + view["view_name"] + "."
    body, formats, issues, seen = board_rows(source)
    plan, skipped = board_preview(source, archive, [clash], verified_finding=finding)
    if guid in seen:
        issue = next(row for row in issues if marker in row["findings"])
        if finding in issue["findings"]:
            return None, skipped, "already_recorded"
        if issue["findings"] not in ([marker, BOUNDS_PENDING], [marker, MAPPING_PENDING]):
            raise ValueError("The existing clash findings were edited. Review the issue before updating it.")
        plan = ib.plan_replace_field(body, issue["number"], "finding", [marker, finding], formats)
        old_paragraph = "Finding: " + issue["findings"][1]
        new_paragraph = "Finding: " + finding
        original = ib.split_paragraphs(body)
        changed = ib.split_paragraphs(plan["final_text"])
        expected = [line for _, line in original]
        status_rows = [row["index"] for row in issue["lines"] if row["text"] == old_paragraph]
        if len(status_rows) != 1:
            raise ValueError("Cannot identify one original clash status paragraph.")
        expected[status_rows[0]] = new_paragraph
        if [line for _, line in changed] != expected:
            raise ValueError("The board preview changed more than the clash status paragraph.")
        skipped = []
    if plan is None:
        return None, skipped, "archived"
    plan["structural_target"] = {"host_identity": host, "active": active, "archive": archived,
                                 "view": {key: view[key] for key in (
                                     "view_id", "view_uid", "view_name", "box_min", "box_max")}}
    return plan, skipped, "planned"


def verify_board_run(plan, outcome, run, readback):
    if plan is None or run["run_id"] == readback["run_id"]:
        raise ValueError("Use a planned board handoff and an independent note readback.")
    guard = plan["structural_target"]
    note = guard["active"]
    changes = runtime_changes(run, *RUNTIME_OUTCOMES[outcome])
    if not matches_changes(changes, modified=(note["id"], "TextNote")):
        raise ValueError("The guarded board run must change only the reviewed text note.")
    fresh_changes = runtime_changes(readback, "query", "ok", "rolled_back")
    if not matches_changes(fresh_changes):
        raise ValueError("The independent board readback must leave the model unchanged.")
    intended_sha1 = ib.text_sha1(plan["final_text"])
    receipt = run["result"]
    if (receipt["status"] != "ok" or str(receipt["note_id"]) != note["id"]
            or receipt["sha1_after"] != intended_sha1):
        raise ValueError("The board write receipt differs from the plan.")
    expected = dict(note, sha1=intended_sha1) if outcome == "keep" else note
    if board_identity(readback["result"], guard["host_identity"]) != expected:
        raise ValueError("The independent note readback differs from the expected text or identity.")
    return {"status": "verified_" + outcome.replace("-", "_"), "note_id": note["id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--matching-xml", help="XML export from the same test as the HTML report")
    parser.add_argument("--snapshot")
    parser.add_argument("--padding-ft", type=float, default=1.0)
    parser.add_argument("--board")
    parser.add_argument("--archive")
    parser.add_argument("--view-clash", help="Preview one section view for a verified Navisworks clash GUID")
    parser.add_argument("--verify-view", choices=("dry-run", "keep", "discard"))
    parser.add_argument("--view-run", help="Complete guarded pyRevit run record")
    parser.add_argument("--view-readback", help="Complete independent pyRevit query record")
    parser.add_argument("--confirm-visible", action="store_true", help="Both clash targets were visible in the kept view")
    parser.add_argument("--verify-board", choices=tuple(RUNTIME_OUTCOMES))
    parser.add_argument("--board-run", help="Complete guarded board write record")
    parser.add_argument("--board-readback", help="Complete independent board query record")
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
        if args.matching_xml and (not is_html or Path(args.matching_xml).suffix.lower() != ".xml"):
            raise ValueError("Pair one HTML report with an XML export of the same test.")
        if args.view_clash and ((is_html and not args.matching_xml) or not args.snapshot):
            raise ValueError("Use verified XML clash identity and a Revit snapshot for a section-view preview.")
        if any((args.verify_view, args.view_run, args.view_readback)) and not all(
                (args.verify_view, args.view_run, args.view_readback, args.view_clash)):
            raise ValueError("Supply the view GUID, outcome, full run record and independent readback together.")
        kept_board = bool(args.board and args.view_clash and args.verify_view == "keep")
        if args.confirm_visible != kept_board or (args.board and args.view_clash and not kept_board):
            raise ValueError("Use a verified Keep and visual confirmation for a kept-view board preview.")
        if any((args.verify_board, args.board_run, args.board_readback)) and not all(
                (kept_board, args.verify_board, args.board_run, args.board_readback)):
            raise ValueError("Supply a kept-view board handoff and both board run records.")
        raw = local_bytes(args.report)
        report_digest = hashlib.sha256(raw).hexdigest()
        identity_digest = None
        source_warnings = []
        if is_html:
            source_format, clashes, source_warnings = parse_html_report(raw)
            units = ""
            if args.matching_xml:
                identity_raw = local_bytes(args.matching_xml)
                identity_digest = hashlib.sha256(identity_raw).hexdigest()
                units, xml_rows = parse_report(identity_raw)
                clashes = paired_clashes(clashes, xml_rows)
                unresolved = sum(row["xml_identity"] != "matched" for row in clashes)
                if unresolved:
                    source_warnings.append(f"{unresolved} HTML clash rows could not be bound uniquely to XML identity.")
        else:
            source_format = "xml"
            units, clashes = parse_report(raw)
        snapshot = local_bytes(args.snapshot) if args.snapshot else None
        snapshot_data = snapshot_document(snapshot) if snapshot is not None else None
        host, index = element_snapshot(snapshot_data) if snapshot_data is not None else (None, {})
        for row in clashes:
            row["section_box"] = section_box(row["objects"], index, args.padding_ft)
            row["warnings"] = []
            if row["guid"] is None:
                row["warnings"].append("Missing or invalid clash GUID.")
            if row["source_status"] not in STATUSES:
                row["warnings"].append("Unknown Navisworks status.")
            if row["section_box"] is None:
                row["warnings"].append("Both distinct elements must match one scoped Revit snapshot.")
        board = runtime_record(args.board) if args.board else None
        archive = runtime_record(args.archive) if args.archive else None
        if kept_board:
            board = board.get("result", board)
            archive = archive.get("result", archive) if archive is not None else None
            if args.archive and not isinstance(archive, dict):
                raise ValueError("The archive query did not return a note.")
        plan, skipped = board_preview(board, archive, clashes) if args.board and not kept_board else (None, [])
        report = {
            "kind": "navisworks-clash-preview", "model_write_approved": False,
            "source_format": source_format,
            "report_sha256": report_digest, "source_units": units,
            "snapshot_sha256": hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None,
            "host_id": host, "coordinate_frame": FRAME if host else None,
            "clashes": clashes, "plan": plan, "skipped_guids": skipped,
            "warnings": source_warnings + (
                ["Archive note not checked for duplicates."] if args.board and not args.archive else []),
        }
        if identity_digest is not None:
            report["identity_report_sha256"] = identity_digest
        if args.view_clash:
            plan_report_digest = report_digest if identity_digest is None else hashlib.sha256(
                ("navisworks-html+xml:" + report_digest + ":" + identity_digest).encode("ascii")
            ).hexdigest()
            report["view_plan"], report["view_preview"] = section_view_preview(
                args.view_clash, clashes, snapshot_data, args.padding_ft, plan_report_digest
            )
        if args.verify_view:
            report["view_verification"] = verify_view_run(
                report["view_plan"], args.verify_view, runtime_record(args.view_run), runtime_record(args.view_readback)
            )
        if kept_board:
            clash = next(row for row in clashes if row["guid"] == args.view_clash)
            report["plan"], report["skipped_guids"], report["board_handoff"] = kept_board_preview(
                board, archive, clash, report["view_verification"]["view"]
            )
        if args.verify_board:
            report["board_verification"] = verify_board_run(
                report["plan"], args.verify_board, runtime_record(args.board_run), runtime_record(args.board_readback)
            )
        if is_html and not args.matching_xml:
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
            print("Use a Navisworks XML or a supported default HTML report.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
