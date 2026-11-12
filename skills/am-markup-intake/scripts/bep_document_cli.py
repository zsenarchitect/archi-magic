"""Compare model references in a private local DOCX with a Revit facts export."""

import argparse
from collections import Counter
import io
from itertools import chain, islice
import json
import ntpath
import posixpath
import re
import sys
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from document_tables import local_bytes, validate_ooxml


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_PARTS = 32
MAX_PHYSICAL = {"p": 5000, "tbl": 100, "tr": 1000, "tc": 5000}
MAX_SPAN = 64
MAX_TEXT = 1024 * 1024
MAX_REFERENCES = 500
MAX_FACTS = 1024 * 1024
MAX_VALUE = 4096
RV = r"\.rvt(?![\w.])"
REFERENCES = re.compile(
    r'["“](?P<quoted>[^"”<>\r\n]{1,1000}' + RV + r')["”]'
    r'|(?<![\w/\\])(?P<path>(?:[A-Z]:[/\\]|[/\\]{2}|(?:RSN|ACC|BIM 360|Autodesk Docs)://)'
    r'[^"“”<>|;\r\n]{1,1000}?' + RV + r')'
    r'|(?<![\w./\\])(?P<file>[\w().-]{1,250}' + RV + r')',
    re.IGNORECASE,
)
FIELD_REFERENCE = re.compile(r'[^"<>|;\r\n]{1,1000}' + RV, re.IGNORECASE)
UNSCANNED = {
    "txbxContent": "text_box", "ins": "tracked_revision", "del": "tracked_revision",
    "moveFrom": "tracked_revision", "moveTo": "tracked_revision", "sdt": "content_control",
    "altChunk": "embedded_document", "footnoteReference": "footnote",
    "endnoteReference": "endnote", "commentReference": "comment", "fldSimple": "field",
    "instrText": "field_code", "object": "embedded_object", "drawing": "drawing",
    "pict": "picture", "sym": "symbol",
}


def xml_part(archive, name):
    try:
        data = archive.read(name)
    except KeyError:
        raise ValueError("missing internal DOCX part") from None
    return ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def relationships(archive, name):
    root = xml_part(archive, name)
    if root.tag != REL + "Relationships":
        raise ValueError("invalid DOCX relationships part")
    result = {}
    for node in root:
        key = node.get("Id")
        if node.tag != REL + "Relationship" or not key or key in result:
            raise ValueError("invalid or duplicate DOCX relationship")
        result[key] = node
    return result


def target_part(source, relationship, expected_kind):
    target = relationship.get("Target", "")
    if (relationship.get("TargetMode", "Internal") != "Internal"
            or not relationship.get("Type", "").endswith("/" + expected_kind)
            or not target or any(char in target for char in "%:#?\\")
            or target.startswith("//")):
        raise ValueError("unsupported DOCX part relationship")
    name = posixpath.normpath(
        target[1:] if target.startswith("/") else posixpath.join(posixpath.dirname(source), target)
    )
    if name.startswith("../") or name in (".", "..") or not name.endswith(".xml"):
        raise ValueError("invalid DOCX part path")
    return name


def section_refs(body):
    sections = body.findall(W + "sectPr")
    sections.extend(section for paragraph in body.findall(W + "p")
                    for section in paragraph.findall(W + "pPr/" + W + "sectPr"))
    for section in sections:
        for node in section:
            if node.tag in (W + "headerReference", W + "footerReference"):
                yield node.tag[len(W):-len("Reference")], node.get(R + "id")


def read_parts(archive):
    relations = relationships(archive, "_rels/.rels")
    roots = [node for node in relations.values() if node.get("Type", "").endswith("/officeDocument")]
    if len(roots) != 1:
        raise ValueError("expected one DOCX main document part")
    main = target_part("", roots[0], "officeDocument")
    root = xml_part(archive, main)
    body = root.find(W + "body")
    if root.tag != W + "document" or body is None:
        raise ValueError("expected a DOCX document body")
    parts = [("body", body)]
    refs = section_refs(body)
    first = next(refs, None)
    if first is None:
        return parts
    relname = posixpath.join(posixpath.dirname(main), "_rels", posixpath.basename(main) + ".rels")
    relations = relationships(archive, relname)
    seen = set()
    for kind, key in chain((first,), refs):
        if key not in relations:
            raise ValueError("missing DOCX header or footer relationship")
        name = target_part(main, relations[key], kind)
        if name in seen:
            continue
        seen.add(name)
        if len(seen) > MAX_PARTS:
            raise ValueError("DOCX header and footer part limit exceeded")
        root = xml_part(archive, name)
        if root.tag != W + ("hdr" if kind == "header" else "ftr"):
            raise ValueError("invalid DOCX header or footer")
        parts.append((kind + ":" + name, root))
    return parts


def preflight(parts):
    counts = Counter()
    for _, root in parts:
        for node in root.iter():
            if not node.tag.startswith(W):
                continue
            name = node.tag[len(W):]
            if name in MAX_PHYSICAL:
                counts[name] += 1
                if counts[name] > MAX_PHYSICAL[name]:
                    raise ValueError("DOCX physical " + name + " limit exceeded")
            if name in ("gridSpan", "gridBefore", "gridAfter"):
                value = node.get(W + "val", "")
                if (not value.isascii() or not value.isdigit() or len(value) > 2
                        or int(value) > MAX_SPAN or (name == "gridSpan" and int(value) == 0)):
                    raise ValueError("DOCX grid span limit exceeded")


def paragraph_text(paragraph):
    pieces = []
    for child in paragraph:
        if child.tag == W + "r":
            runs = [child]
        elif child.tag == W + "hyperlink":
            runs = child.findall(W + "r")
        else:
            pieces.append("\n")
            continue
        for run in runs:
            for node in run:
                if node.tag == W + "t":
                    pieces.append(node.text or "")
                elif node.tag in (W + "tab", W + "br", W + "cr"):
                    pieces.append("\n")
                elif node.tag == W + "noBreakHyphen":
                    pieces.append("-")
                elif node.tag != W + "rPr":
                    pieces.append("\n")
    return "".join(pieces).strip()


def paragraphs(parts, skipped):
    for part, root in parts:
        for index, node in enumerate(root, 1):
            location = f"{part}/block:{index}"
            if node.tag == W + "p":
                yield location, node
            elif node.tag == W + "tbl":
                yield from table_paragraphs(node, location, skipped)
            elif node.tag != W + "sectPr":
                skipped(location, "unsupported_block")


def table_paragraphs(table, location, skipped):
    for row_index, row in enumerate(table.findall(W + "tr"), 1):
        row_location = f"{location}/row:{row_index}"
        if any(child.tag not in (W + "tc", W + "trPr", W + "tblPrEx") for child in row):
            skipped(row_location, "unsupported_row_content")
        for name in ("gridBefore", "gridAfter"):
            omitted = row.find(W + "trPr/" + W + name)
            if omitted is not None and omitted.get(W + "val") != "0":
                skipped(row_location, "omitted_table_cells")
        for cell_index, cell in enumerate(row.findall(W + "tc"), 1):
            cell_location = f"{row_location}/cell:{cell_index}"
            properties = cell.find(W + "tcPr")
            if properties is not None:
                span = properties.find(W + "gridSpan")
                if (span is not None and span.get(W + "val") != "1"
                        or properties.find(W + "vMerge") is not None
                        or properties.find(W + "hMerge") is not None):
                    skipped(cell_location, "merged_table_cell")
                    continue
            for index, child in enumerate(cell, 1):
                block = f"{cell_location}/block:{index}"
                if child.tag == W + "p":
                    yield block, child
                elif child.tag != W + "tcPr":
                    skipped(block, "nested_table" if child.tag == W + "tbl" else "unsupported_cell_block")
    if any(child.tag not in (W + "tr", W + "tblPr", W + "tblGrid") for child in table):
        skipped(location, "unsupported_table_content")


def normalize(reference):
    return ntpath.normpath(reference.replace("/", "\\")).casefold()


def file_alias(value):
    if not isinstance(value, str) or len(value) > MAX_VALUE or not value.lower().endswith(".rvt"):
        return None
    return normalize(value)


def names_for(paths):
    return {ntpath.basename(path) for path in paths}


def model_aliases(path):
    _, raw = local_bytes(path)
    if len(raw) > MAX_FACTS:
        raise ValueError("model facts export exceeds size limit")
    data = json.loads(raw.decode("utf-8-sig"))
    if (not isinstance(data, dict) or data.get("kind") != "revit-model-facts"
            or data.get("status") != "complete" or data.get("issues") != []
            or not isinstance(data.get("model"), dict) or not isinstance(data.get("links"), list)
            or len(data["links"]) > 500):
        raise ValueError("choose a complete model-facts JSON export")
    model = data["model"]
    if any(not isinstance(model.get(key), str) or not model[key]
           for key in ("title", "revit_year", "revit_build")):
        raise ValueError("model facts lack project or runtime identity")
    roles = {}
    for name in ("current", "central", "cloud"):
        if name + "_path" not in model:
            raise ValueError("incomplete model-facts paths")
        value = model[name + "_path"]
        if value is not None and (not isinstance(value, str) or len(value) > MAX_VALUE):
            raise ValueError("invalid model-facts path")
        alias = file_alias(value)
        if alias:
            roles[name] = {alias}
    if not roles:
        raise ValueError("model facts lack a comparable host path")
    link_paths = {}
    for row in data["links"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"].isascii() or not row["id"].isdigit() or row["id"] in link_paths
                or not isinstance(row.get("status"), str) or not row["status"]):
            raise ValueError("invalid link identity in model facts")
        paths = set()
        for name in ("resolved_path", "provider_session_path", "stored_path"):
            if name not in row:
                raise ValueError("incomplete model-facts link paths")
            value = row[name]
            if value is not None and (not isinstance(value, str) or len(value) > MAX_VALUE):
                raise ValueError("invalid link path in model facts")
            alias = file_alias(value)
            if alias:
                paths.add(alias)
        link_paths[row["id"]] = paths
    return roles, link_paths


def compare_reference(reference, host_roles, link_paths, former_paths, reviewed_role):
    alias = normalize(reference)
    has_folder = bool(ntpath.dirname(alias))
    def aliases(paths):
        return paths if has_folder else names_for(paths)

    host_matches = sorted(role for role, paths in host_roles.items() if alias in aliases(paths))
    links = sorted(identifier for identifier, paths in link_paths.items() if alias in aliases(paths))
    if reviewed_role is not None:
        if reviewed_role in host_matches:
            return {"status": "current_host", "host_roles": [reviewed_role]}
        expected = host_roles[reviewed_role]
    elif host_matches and not links:
        return {"status": "current_host", "host_roles": host_matches}
    elif links and not host_matches and len(links) == 1:
        return {"status": "current_link", "link_id": links[0]}
    elif host_matches or links:
        return {"status": "unresolved", "reason": "ambiguous_alias"}
    elif alias in aliases(former_paths):
        expected = set().union(*host_roles.values())
    else:
        return {"status": "unresolved", "reason": "unrecognized_reference"}
    differences = []
    if ntpath.basename(alias) not in names_for(expected):
        differences.append("file_name")
    if has_folder and ntpath.dirname(alias) not in {ntpath.dirname(path) for path in expected}:
        differences.append("folder")
    if not differences:
        differences.append("path_combination")
    return {"status": "mismatch", "differences": differences,
            "evidence": "reviewed_field" if reviewed_role is not None else "confirmed_former_alias"}


def paragraph_references(text, reviewed, known_names):
    if reviewed:
        # The caller identified this physical paragraph as a specific host field.
        # Accept one entire reference or a label followed by that reference.
        value = text.strip().strip('"“”')
        labeled = re.match(r"^[^:\r\n\\/]{2,80}:\s+(.+)$", value)
        if labeled:
            value = labeled[1].strip().strip('"“”')
        if FIELD_REFERENCE.fullmatch(value) and len(re.findall(RV, value, re.I)) == 1:
            return [value]
    if len(text) <= MAX_VALUE and normalize(text) in known_names:
        return [text]
    matches = islice(REFERENCES.finditer(text), MAX_REFERENCES + 1)
    return [match.group(match.lastgroup) for match in matches]


def compare(docx, facts, reviewed_fields, former):
    host_roles, link_paths = model_aliases(facts)
    if any(role not in host_roles for role in reviewed_fields.values()):
        raise ValueError("reviewed field requires an observed path for that host role")
    former_paths = {file_alias(value) for value in former}
    if None in former_paths:
        raise ValueError("former aliases must end in .rvt")
    filename, data = local_bytes(docx)
    if filename.suffix.casefold() != ".docx":
        raise ValueError("choose a local DOCX file")
    validate_ooxml(data)
    with ZipFile(io.BytesIO(data)) as archive:
        parts = read_parts(archive)
        preflight(parts)

    known_names = names_for(former_paths)
    for paths in chain(host_roles.values(), link_paths.values()):
        known_names.update(names_for(paths))
    references = []
    skipped_examples = []
    skipped_counts = Counter()
    reviewed_seen = set()
    text_size = 0
    output_size = 0

    def skipped(location, reason):
        skipped_counts[reason] += 1
        if len(skipped_examples) < 100:
            skipped_examples.append({"location": location, "reason": reason})

    for location, node in paragraphs(parts, skipped):
        reasons = {UNSCANNED[item.tag[len(W):]] for item in node.iter()
                   if item.tag.startswith(W) and item.tag[len(W):] in UNSCANNED}
        if any(item.tag.endswith("}AlternateContent") for item in node.iter()):
            reasons.add("alternate_content")
        for reason in sorted(reasons):
            skipped(location, reason)
        text = paragraph_text(node)
        text_size += len(text.encode("utf-8"))
        if text_size > MAX_TEXT:
            raise ValueError("DOCX text scan exceeds size limit")
        role = reviewed_fields.get(location)
        candidates = paragraph_references(text, role is not None, known_names)
        if role is not None:
            reviewed_seen.add(location)
            if reasons:
                skipped(location, "reviewed_field_partly_unscanned")
                continue
            if len(candidates) != 1:
                skipped(location, "reviewed_field_requires_one_reference")
                continue
        for reference in candidates:
            finding = compare_reference(reference, host_roles, link_paths, former_paths, role)
            start = max(0, text.find(reference) - 48)
            finding.update(location=location, reference=reference, excerpt=text[start:start + 160])
            references.append(finding)
            output_size += len(json.dumps(finding, ensure_ascii=True)) + 2
            if len(references) > MAX_REFERENCES or output_size > MAX_TEXT:
                raise ValueError("DOCX comparison exceeds output limit")
    for location in sorted(set(reviewed_fields) - reviewed_seen):
        skipped(location, "reviewed_field_not_scanned")
    return {
        "status": "incomplete" if skipped_counts else "complete",
        "document": filename.name, "references": references,
        "reference_status": "found" if references else "none_found",
        "skipped_counts": dict(sorted(skipped_counts.items())), "skipped_examples": skipped_examples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", nargs="?")
    parser.add_argument("--facts", help="complete model-facts JSON saved from the selected Revit model")
    parser.add_argument("--field", action="append", default=[], metavar="LOCATION=ROLE",
                        help="reviewed physical paragraph, with role current, central or cloud")
    parser.add_argument("--former-host", action="append", default=[], metavar="RVT_PATH",
                        help="confirmed former host path or filename")
    args = parser.parse_args()
    if args.docx is None:
        print(json.dumps({"status": "not_run", "reason": "no_docx"}))
        return 0
    try:
        if not args.facts:
            raise ValueError("provide a complete model-facts JSON export with --facts")
        if len(args.field) > 100 or len(args.former_host) > 100:
            raise ValueError("too many reviewed fields or former aliases")
        fields = {}
        for entry in args.field:
            location, separator, role = entry.rpartition("=")
            if not separator or not location or location in fields or len(location) > 400:
                raise ValueError("use one LOCATION=ROLE per reviewed field")
            fields[location] = role
        result = compare(args.docx, args.facts, fields, args.former_host)
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True)
        if len(encoded) > MAX_TEXT:
            raise ValueError("DOCX comparison exceeds output limit")
    except (ValueError, OSError, BadZipFile, ElementTree.ParseError, DefusedXmlException) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=True))
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
