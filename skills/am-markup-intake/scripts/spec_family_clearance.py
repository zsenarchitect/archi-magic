# -*- coding: utf-8 -*-
"""Build a separate solid clearance envelope driven by equipment dimensions."""

from decimal import Decimal
import re

from pyrevit.api import DB

from spec_family_box_constraints import PARAMETER_NAMES, constrain_box


CATEGORY = "Equipment Clearance"
SIDES = (("left", "right"), ("front", "back"), ("below", "above"))
AXES = ("Width", "Depth", "Height")
TEXT = (str, type(u""))


def clearance_offsets(spec):
    values = spec["clearance_mm"]
    rows = {row["name"]: row["value"] for row in spec["source_rows"] if row["name"].startswith("clearance_")}
    if not values and not rows:
        return None
    keys = {"clearance_" + side for pair in SIDES for side in pair}
    if not isinstance(values, dict) or set(values) != keys or values != rows:
        raise ValueError("Confirm all six clearance offsets from the PDF source rows.")
    result = []
    for axis, pair in zip(AXES, SIDES):
        offsets = []
        for side in pair:
            raw = values["clearance_" + side]
            if (not isinstance(raw, TEXT) or not re.match(r"[0-9]{1,5}(?:\.[0-9]{1,12})?\Z", raw)
                    or not Decimal(0) <= Decimal(raw) <= Decimal(30000)):
                raise ValueError("A reviewed clearance offset is invalid.")
            offsets.append(Decimal(raw))
        if Decimal(spec["dimensions_mm"][axis.lower()]) + sum(offsets) > Decimal(60000):
            raise ValueError("The reviewed clearance envelope exceeds 60,000 mm.")
        result.append(tuple(float(value / Decimal("304.8")) for value in offsets))
    return result


def clearance_bounds(dimensions, offsets):
    return ([-negative for negative, _ in offsets],
            [dimension + positive for dimension, (_, positive) in zip(dimensions, offsets)])


def existing_clearance_category(document):
    return any(str(category.Name).lower() == CATEGORY.lower()
               for category in document.OwnerFamily.FamilyCategory.SubCategories)


def constrain_clearance(document, view, extrusion, minimum, maximum, offsets, created):
    manager = document.FamilyManager
    category = document.Settings.Categories.NewSubcategory(document.OwnerFamily.FamilyCategory, CATEGORY)
    extrusion.Subcategory = category
    if not extrusion.IsSolid or extrusion.Subcategory.Id.Value != category.Id.Value:
        raise ValueError("The clearance envelope must be a solid in its own subcategory.")
    parameters = []
    for axis, equipment, (negative, positive) in zip(AXES, PARAMETER_NAMES, offsets):
        allowance_name = "Clearance " + axis + " Allowance"
        allowance = manager.AddParameter(allowance_name, DB.GroupTypeId.Geometry, DB.SpecTypeId.Length, False)
        manager.Set(allowance, negative + positive)
        extent = manager.AddParameter("Clearance " + axis, DB.GroupTypeId.Geometry, DB.SpecTypeId.Length, False)
        manager.SetFormula(extent, equipment + " + " + allowance_name)
        parameters.append(extent)
    constrain_box(document, view, extrusion, minimum, maximum, parameters, created)
    document.Regenerate()
