# -*- coding: utf-8 -*-
"""Read model facts for a BIM execution plan through pyRevit run_query."""

import json
import ntpath
import platform
import time

try:
    TEXT = unicode
except NameError:
    TEXT = str

MAX_TEXT = 4096
MAX_JSON = 1024 * 1024
IRONPYTHON = platform.python_implementation() == "IronPython"


def element_id(value):
    if hasattr(value, "Value"):
        return int(value.Value)
    return int(value.IntegerValue)


def ironpython_json_text(value):
    """Follow model_health.jsonable for the IronPython 2.7 encoder."""
    if isinstance(value, dict):
        return dict((key, ironpython_json_text(item)) for key, item in value.items())
    if isinstance(value, list):
        return [ironpython_json_text(item) for item in value]
    if isinstance(value, (str, TEXT)) and any(ord(char) > 127 for char in value):
        return value.encode("utf-8").decode("latin-1")
    return value


def encode(value):
    if IRONPYTHON:
        value = ironpython_json_text(value)
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)


class BudgetExceeded(Exception):
    pass


class Facts:
    def __init__(self, DB):
        self.DB = DB
        self.issues = set()
        self.clock = getattr(time, "monotonic", time.time)
        self.deadline = self.clock() + 10
        self.link_bytes = 0

    def check_time(self):
        if self.clock() >= self.deadline:
            raise BudgetExceeded("time_limit")

    def read(self, getter, missing=None):
        self.check_time()
        try:
            value = getter()
        except Exception:
            value = None
        if value is None and missing:
            self.issues.add(missing)
        return value

    def text(self, getter, missing=None):
        value = self.read(getter, missing)
        if value is None:
            return None
        value = TEXT(value)
        if len(value) > MAX_TEXT:
            self.issues.add("string_limit")
            return None
        return value

    def path(self, getter):
        return self.text(lambda: self.DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(
            getter()))

    def rows(self, getter, limit, name):
        self.check_time()
        try:
            rows = iter(getter())
            for index, row in enumerate(rows):
                self.check_time()
                if index >= limit:
                    raise BudgetExceeded(name + "_limit")
                yield row
            self.check_time()
        except BudgetExceeded:
            raise
        except Exception:
            raise BudgetExceeded(name + "_unavailable")


def filename(path):
    leaf = ntpath.basename(path) if path else ""
    return leaf if leaf.lower().endswith(".rvt") else None


def host_facts(doc, app, facts, title, path):
    workshared = facts.read(lambda: bool(doc.IsWorkshared), "worksharing_unavailable")
    detached = facts.read(lambda: bool(doc.IsDetached), "detached_state_unavailable")
    cloud = facts.read(lambda: bool(doc.IsModelInCloud), "cloud_state_unavailable")
    central_state = "unavailable"
    central = None
    central_path = None
    if workshared is False:
        central_state = "not_applicable"
    elif workshared and detached is not None:
        central = facts.read(lambda: doc.GetWorksharingCentralModelPath())
        if central is not None:
            central_path = facts.path(lambda: central)
            central_state = "available" if central_path else "unavailable"
            if not central_path:
                facts.issues.add("central_path_unavailable")
        elif detached:
            central_state = "not_applicable"
        else:
            facts.issues.add("central_path_unavailable")
    cloud_path = facts.path(lambda: doc.GetCloudModelPath()) if cloud else None
    if cloud and not cloud_path:
        facts.issues.add("cloud_path_unavailable")
    server = facts.read(lambda: bool(central.ServerPath), "server_state_unavailable") if central else None
    if detached:
        location = "detached"
    elif cloud:
        location = "cloud"
    elif server:
        location = "revit_server"
    elif path.startswith(("\\\\", "//")):
        location = "network_path"
    elif ntpath.splitdrive(path)[0]:
        location = "drive_path"
    elif path:
        location = "unknown"
    elif workshared is False and detached is False and cloud is False:
        location = "unsaved"
    else:
        location = "unknown"
    if location == "unknown":
        facts.issues.add("model_location_unavailable")
    file_name = filename(path)
    central_file_name = filename(central_path)
    cloud_file_name = filename(cloud_path)
    observed_name = file_name or cloud_file_name
    if location == "revit_server" and not path:
        observed_name = central_file_name
    if location not in ("unsaved", "detached") and not observed_name:
        facts.issues.add("file_name_unavailable")
    return {
        "title": title, "file_name": file_name, "current_path": path, "location": location,
        "workshared": workshared, "detached": detached, "is_cloud": cloud,
        "central_path": central_path, "central_file_name": central_file_name,
        "central_state": central_state, "cloud_path": cloud_path,
        "cloud_file_name": cloud_file_name,
        "revit_year": facts.text(lambda: app.VersionNumber, "revit_year_unavailable"),
        "revit_build": facts.text(lambda: app.VersionBuild, "revit_build_unavailable"),
    }


def workset_facts(doc, facts):
    total = 0
    user = 0
    for workset in facts.rows(lambda: facts.DB.FilteredWorksetCollector(doc), 1000, "worksets"):
        total += 1
        user += int(workset.Kind == facts.DB.WorksetKind.UserWorkset)
    return {"total": total, "user": user}


def link_facts(doc, facts):
    DB = facts.DB
    links = {}
    for link in facts.rows(lambda: DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkType),
                           500, "link_types"):
        nested = facts.read(lambda: bool(link.IsNestedLink), "link_scope_unavailable")
        if nested is not False:
            continue
        identifier = element_id(link.Id)
        ref = facts.read(lambda: link.GetExternalFileReference())
        stored = facts.path(lambda: ref.GetPath()) if ref is not None else None
        resolved = facts.path(lambda: ref.GetAbsolutePath()) if ref is not None else None
        provider = facts.text(lambda: link.GetExternalResourceReference(
            DB.ExternalResourceTypes.BuiltInExternalResourceTypes.RevitLink).InSessionPath)
        status = facts.text(lambda: link.GetLinkedFileStatus())
        if status is None and ref is not None:
            status = facts.text(lambda: ref.GetLinkedFileStatus())
        if not (stored or resolved or provider):
            facts.issues.add("link_path_unavailable")
        if not status:
            facts.issues.add("link_status_unavailable")
        row = {
            "id": str(identifier), "type_name": facts.text(lambda: link.Name, "link_name_unavailable"),
            "status": status, "stored_path": stored, "resolved_path": resolved,
            "provider_session_path": provider, "loaded_document_titles": [], "instance_count": 0,
        }
        facts.link_bytes += len(encode(row)) + 2
        if facts.link_bytes > MAX_JSON - 65536:
            raise BudgetExceeded("output_limit")
        links[identifier] = row

    for instance in facts.rows(
            lambda: DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance),
            2000, "link_instances"):
        identifier = facts.read(lambda: element_id(instance.GetTypeId()), "link_instance_type_unavailable")
        if identifier not in links:
            continue
        row = links[identifier]
        row["instance_count"] += 1
        linked_doc = facts.read(lambda: instance.GetLinkDocument())
        if linked_doc is None:
            if row["status"] == "Loaded":
                facts.issues.add("loaded_link_document_unavailable")
            continue
        title = facts.text(lambda: linked_doc.Title, "loaded_link_title_unavailable")
        if title and title not in row["loaded_document_titles"]:
            facts.link_bytes += len(encode(title)) + 2
            if facts.link_bytes > MAX_JSON - 65536:
                raise BudgetExceeded("output_limit")
            row["loaded_document_titles"].append(title)

    for row in links.values():
        row["loaded_document_titles"].sort()
    return [links[identifier] for identifier in sorted(links)]


def read_model_facts(doc, app, DB, inputs):
    if doc is None or doc.IsFamilyDocument:
        return {"status": "rejected", "error": "no_project_document"}
    title = inputs.get("expected_title")
    path = inputs.get("expected_path")
    uid = inputs.get("expected_project_uid")
    if not isinstance(title, (str, TEXT)) or not title:
        return {"status": "rejected", "error": "document_identity_required"}
    if inputs.get("identify") is True:
        facts = Facts(DB)
        try:
            if facts.text(lambda: doc.Title) != title:
                return {"status": "rejected", "error": "document_identity_mismatch"}
            path = facts.text(lambda: doc.PathName, "document_path_unavailable")
            uid = facts.text(lambda: doc.ProjectInformation.UniqueId, "project_uid_unavailable")
        except BudgetExceeded as failure:
            return {"status": "incomplete", "issues": [str(failure)]}
        if facts.issues:
            return {"status": "incomplete", "issues": sorted(facts.issues)}
        return {"status": "identified", "title": title, "path": path, "project_uid": uid}
    if not isinstance(path, (str, TEXT)):
        return {"status": "rejected", "error": "document_identity_required"}
    if uid is not None and (not isinstance(uid, (str, TEXT)) or not uid):
        return {"status": "rejected", "error": "invalid_project_uid"}
    if not path and not uid:
        return {"status": "rejected", "error": "project_uid_required_for_empty_path"}
    facts = Facts(DB)
    try:
        actual_title = facts.text(lambda: doc.Title)
        actual_path = facts.text(lambda: doc.PathName)
        actual_uid = facts.text(lambda: doc.ProjectInformation.UniqueId) if uid else None
        if actual_title != title or actual_path != path or (uid and uid != actual_uid):
            return {"status": "rejected", "error": "document_identity_mismatch"}
        result = {"kind": "revit-model-facts",
                  "model": host_facts(doc, app, facts, actual_title, actual_path),
                  "worksets": None, "links": None}
        facts.link_bytes = len(encode(result))
        for section, collect in (("worksets", workset_facts), ("links", link_facts)):
            try:
                result[section] = collect(doc, facts)
            except BudgetExceeded as failure:
                facts.issues.add(str(failure))
            except Exception:
                facts.issues.add(section + "_unavailable")
        result["status"] = "incomplete" if facts.issues else "complete"
        result["issues"] = sorted(facts.issues)
        if len(encode(result)) > MAX_JSON:
            raise BudgetExceeded("output_limit")
        return result
    except BudgetExceeded as failure:
        return {"status": "incomplete", "issues": [str(failure)]}


if "doc" in globals():
    from pyrevit.api import DB

    result = encode(read_model_facts(doc, app, DB, globals().get("inputs") or {}))
