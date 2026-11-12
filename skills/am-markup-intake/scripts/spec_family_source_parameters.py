# -*- coding: utf-8 -*-
"""Store and verify confirmed product PDF provenance in Revit family type parameters."""

import re

from pyrevit.api import DB


TEXT = (str, type(u""))


def text_value(value, maximum):
    if (not isinstance(value, TEXT) or not 0 < len(value) <= maximum or value != value.strip()
            or any(ord(character) < 32 for character in value)):
        raise ValueError("A confirmed PDF source field is missing, too long, or contains control characters.")
    return value


def source_values(spec):
    pdf = spec["pdf"]
    filename = text_value(pdf["file"], 250)
    if not filename.lower().endswith(".pdf") or any(character in filename for character in "/\\:"):
        raise ValueError("Use the PDF basename without a file path.")
    digest = pdf["sha256"]
    if not isinstance(digest, TEXT) or not re.match(r"[0-9a-f]{64}\Z", digest):
        raise ValueError("The confirmed PDF has no valid source digest.")
    rows, pages = {}, set()
    for row in spec["source_rows"]:
        name, page = row["name"], row["source"]["page"]
        if type(page) is not int or not 1 <= page <= 200 or name in rows:
            raise ValueError("A confirmed PDF field has an invalid or repeated source row.")
        rows[name] = row["value"]
        pages.add(page)
    output = {
        "PDF Source File": filename,
        "PDF Source Pages": ", ".join(str(page) for page in sorted(pages)),
        "PDF Source SHA256": digest,
        "PDF Product": text_value(spec["product"], 160),
        "PDF Variant": text_value(spec["variant"], 160),
    }
    for group, prefix, label in (("connectors", "connector_", "PDF Connector "),
                                 ("parameters", "parameter_", "PDF Value ")):
        values = spec[group]
        expected = {key: value for key, value in rows.items() if key.startswith(prefix)}
        if values != expected:
            raise ValueError("The confirmed {} values disagree with their PDF source rows.".format(group))
        for key, value in sorted(values.items()):
            suffix = key[len(prefix):]
            if not re.match(r"[a-z][a-z0-9_]{0,39}\Z", suffix):
                raise ValueError("A confirmed PDF parameter name is invalid.")
            output[label + suffix] = text_value(value, 500)
    return output


def set_source_values(manager, expected):
    existing = {str(parameter.Definition.Name).lower() for parameter in manager.Parameters}
    if any(name.lower() in existing for name in expected):
        raise ValueError("The fixture already contains a PDF provenance parameter.")
    parameters = {}
    for name, value in sorted(expected.items()):
        parameter = manager.AddParameter(name, DB.GroupTypeId.IdentityData, DB.SpecTypeId.String.Text, False)
        manager.Set(parameter, value)
        parameters[name] = parameter
    return parameters


def read_source_values(manager, parameters, expected):
    current = manager.CurrentType
    if current is None:
        raise ValueError("Revit did not return the candidate family type for provenance readback.")
    actual = {name: current.AsString(parameter) for name, parameter in parameters.items()}
    if actual != expected:
        raise ValueError("The Revit type PDF provenance differs from the confirmed source fields.")
    return actual
