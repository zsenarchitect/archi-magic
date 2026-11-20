# -*- coding: utf-8 -*-
"""Checks for the code snippets and error rows in SKILL.md (issues #251, #255, #259).

The snippets run inside Revit, so these tests use small fake objects in place
of the Revit API. They check the Python logic only, not the Revit behavior.
"""

import io
import os
import re
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_MD = os.path.join(HERE, "..", "SKILL.md")


def _skill_text():
    with io.open(SKILL_MD, encoding="utf-8") as f:
        return f.read()


def _python_blocks():
    text = _skill_text()
    blocks = []
    for m in re.finditer(r"```python\n(.*?)```", text, re.S):
        body = m.group(1)
        lines = body.splitlines()
        indent = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
        blocks.append("\n".join(l[indent:] for l in lines) + "\n")
    return blocks


def _block_with(marker):
    found = [b for b in _python_blocks() if marker in b]
    assert len(found) == 1, "expected one python block with %r, found %d" % (marker, len(found))
    return found[0]


# --- fakes -------------------------------------------------------------------


class _Param(object):
    def __init__(self, value):
        self.value = value

    def AsString(self):
        return self.value


class _IronNameDescriptor(object):
    """IronPython style: DB.Element.Name has GetValue."""

    def GetValue(self, e):
        return e._hidden_name


class _GetsetDescriptor(object):
    """IronPython style seen on a floor type: no GetValue, but __get__ works."""

    def __get__(self, e, owner=None):
        return e._hidden_name


class _PropertyObject(object):
    """CPython (pythonnet) style: neither GetValue nor a usable __get__."""

    def __getattribute__(self, name):
        if name in ("GetValue", "__get__"):
            raise AttributeError("'PropertyObject' object has no attribute '%s'" % name)
        return object.__getattribute__(self, name)


class _Elem(object):
    def __init__(self, symbol_name=None, name=None, name_raises=False, hidden_name=None):
        self._symbol_name = symbol_name
        self._name = name
        self._name_raises = name_raises
        self._hidden_name = hidden_name

    def get_Parameter(self, bip):
        if bip == "SYMBOL_NAME_PARAM" and self._symbol_name is not None:
            return _Param(self._symbol_name)
        return None

    @property
    def Name(self):
        if self._name_raises:
            raise AttributeError("Name")
        return self._name


def _fake_db(name_descriptor):
    db = types.SimpleNamespace()
    db.BuiltInParameter = types.SimpleNamespace(SYMBOL_NAME_PARAM="SYMBOL_NAME_PARAM")
    db.Element = types.SimpleNamespace(Name=name_descriptor)
    return db


def _element_name(name_descriptor):
    ns = {"DB": _fake_db(name_descriptor)}
    exec(compile(_block_with("def element_name("), "<element_name>", "exec"), ns)
    return ns["element_name"]


# --- element_name --------------------------------------------------------------


def test_element_name_reads_symbol_name_param_first():
    fn = _element_name(_GetsetDescriptor())
    floor_type = _Elem(symbol_name="Floor Type A", name_raises=True, hidden_name="other")
    assert fn(floor_type) == "Floor Type A"


def test_element_name_uses_name_on_cpython_without_getvalue():
    fn = _element_name(_PropertyObject())
    view = _Elem(name="Level 1")
    assert fn(view) == "Level 1"


def test_element_name_falls_back_to_getvalue_on_ironpython():
    fn = _element_name(_IronNameDescriptor())
    group_type = _Elem(symbol_name="", name_raises=True, hidden_name="Group A")
    assert fn(group_type) == "Group A"


def test_element_name_falls_back_to_get_when_getvalue_is_missing():
    fn = _element_name(_GetsetDescriptor())
    elem = _Elem(name_raises=True, hidden_name="Type B")
    assert fn(elem) == "Type B"


def test_element_name_returns_none_when_nothing_works():
    fn = _element_name(_PropertyObject())
    assert fn(_Elem(name_raises=True)) is None


# --- step marks ------------------------------------------------------------------


class _Room(object):
    Number = "101"


class _Door(object):
    def __init__(self, value, fail=False):
        self.Id = types.SimpleNamespace(Value=value)
        self.fail = fail

    def get_ToRoom(self, phase):
        if self.fail:
            raise AttributeError("'NoneType' object has no attribute 'Number'")
        return _Room()


class _Collector(object):
    doors = []

    def __init__(self, doc):
        pass

    def OfCategory(self, cat):
        return self

    def WhereElementIsNotElementType(self):
        return list(_Collector.doors)


def _run_step_snippet(doors):
    _Collector.doors = doors
    db = types.SimpleNamespace(
        FilteredElementCollector=_Collector,
        BuiltInCategory=types.SimpleNamespace(OST_Doors="OST_Doors"),
    )
    doc = types.SimpleNamespace(Phases=_Phases(["Phase 1"]))
    ns = {"DB": db, "doc": doc}
    exec(compile(_block_with('step = "start"'), "<steps>", "exec"), ns)
    return ns


class _Phases(list):
    @property
    def Size(self):
        return len(self)


def test_step_marks_return_result_when_nothing_fails():
    ns = _run_step_snippet([_Door(11), _Door(12)])
    assert ns["result"] == {"doors": 2, "rooms": {"11": "101", "12": "101"}}


def test_step_marks_name_the_step_and_raise_again():
    with pytest.raises(Exception) as err:
        _run_step_snippet([_Door(11), _Door(12, fail=True)])
    assert "read the room of door 12" in str(err.value)


# --- loader --------------------------------------------------------------------


def test_loader_runs_the_file_in_the_same_names(tmp_path):
    target = tmp_path / "change.py"
    target.write_text(u"result = {'n': inputs['n'] * 2, 'doc': doc}\n", encoding="utf-8")
    ns = {"inputs": {"script_file": str(target), "n": 21}, "doc": "the doc"}
    exec(compile(_block_with('inputs["script_file"]'), "<loader>", "exec"), ns)
    assert ns["result"] == {"n": 42, "doc": "the doc"}


# --- whole document --------------------------------------------------------------


def test_no_bare_except_in_snippets():
    for block in _python_blocks():
        assert not re.search(r"^\s*except\s*:", block, re.M), block
        assert not re.search(r"^\s*except\s+BaseException", block, re.M), block


def test_no_f_strings_in_snippets():
    for block in _python_blocks():
        assert not re.search(r"(?<![A-Za-z0-9_])[fF][\"']", block), block


@pytest.mark.parametrize(
    "error_text",
    [
        "OST_ReferencePlanes",
        "The input curve is not bound.",
        "CreateInstance() takes no arguments (1 given)",
        "'getset_descriptor' object has no attribute 'GetValue'",
        "'PropertyObject' object has no attribute 'GetValue'",
        "'indexer#' object has no attribute 'Id'",
        "Modification of the document is forbidden",
        "'NoneType' object has no attribute 'Point'",
        "View cannot be duplicated",
        "The type typeId is not valid for this element",
        "invalid_params: 'name' is required.",
        "skill_not_found: No skill named ''",
        "NameError: name '<var>' is not defined",
        "internal_error: Internal error",
        "view_not_found: No view named '<sheet number>'",
        "BoundingBoxIntersectsFilter",
        "ElementIntersectsSolidFilter",
        "One level first?",
        "exceeds maximum allowed tokens",
        "other_document_modified",
        "discarded_on_close",
    ],
)
def test_field_error_texts_are_searchable(error_text):
    assert error_text in _skill_text()


def test_result_size_snippet_writes_the_file_and_returns_a_summary(tmp_path):
    """The "Result size" snippet (issue #278) writes real text to out_path."""
    import json
    block = _block_with('inputs["out_path"]')
    block = block.replace("rows = []", "rows = [{'id': 1, 'name': 'Caf\u00e9'}]")
    out = tmp_path / "read.json"
    names = {"inputs": {"out_path": str(out)}}
    exec(compile(block, "<snippet>", "exec"), names)
    assert names["result"] == {"out_path": str(out), "count": 1}
    assert json.loads(out.read_text(encoding="utf-8")) == {"rows": [{"id": 1, "name": "Café"}]}


def test_query_budget_has_the_result_size_rule():
    text = _skill_text()
    budget = text[text.index("## Query budget"):text.index("## Rules")]
    assert "20,000 characters" in budget
    assert "absolute path" in budget and "%TEMP%" in budget
