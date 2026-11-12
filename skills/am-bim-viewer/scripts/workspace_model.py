"""Validate team model snapshots and build private, self-contained viewer files."""

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory


MAX_MODEL_BYTES = 256 * 1024 * 1024
MAX_ELEMENTS = 5000
MAX_TRIANGLES = 1500000
BUILDER = Path(__file__).resolve().with_name("build-viewer.mjs")
VIEWER_POLICY = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' data:; "
    "style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def finite_number(value):
    return type(value) in (int, float) and abs(value) <= 3.4e38 and math.isfinite(value)


def validate_model(data, normalize=False):
    error = "Use an am-bim-viewer/1 export with valid elements and geometry."
    if (not isinstance(data, dict) or data.get("schema") != "am-bim-viewer/1"
            or not isinstance(data.get("elements"), list) or len(data["elements"]) > MAX_ELEMENTS):
        raise ValueError(error)
    source = data.get("source", {})
    if not isinstance(source, dict) or any(key in source and not isinstance(source[key], str)
                                          for key in ("title", "view", "generator")):
        raise ValueError(error)
    annotations = data.get("annotations", [])
    if not isinstance(annotations, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("text"), str)
            or not isinstance(item.get("position"), list) or len(item["position"]) != 3
            or not all(finite_number(value) for value in item["position"]) for item in annotations):
        raise ValueError(error)
    ids = set()
    triangles = 0
    for element in data["elements"]:
        if not isinstance(element, dict):
            raise ValueError(error)
        identifier = element.get("id")
        if type(identifier) is int and normalize and abs(identifier) > 2**53 - 1:
            identifier = element["id"] = str(identifier)
        if not (type(identifier) is int and abs(identifier) <= 2**53 - 1
                or isinstance(identifier, str) and re.fullmatch(r"-?[0-9]{1,19}", identifier)):
            raise ValueError(error)
        numeric_id = int(identifier)
        if numeric_id in ids or not -(2**63) <= numeric_id < 2**63:
            raise ValueError(error)
        ids.add(numeric_id)
        if any(key in element and not isinstance(element[key], str)
               for key in ("category", "name", "family", "type", "level")):
            raise ValueError(error)
        if not isinstance(element.get("params", {}), dict):
            raise ValueError(error)
        mesh = element.get("mesh")
        if not isinstance(mesh, dict):
            raise ValueError(error)
        vertices, indices = mesh.get("vertices"), mesh.get("indices")
        if (not isinstance(vertices, list) or not vertices or len(vertices) % 3
                or not isinstance(indices, list) or not indices or len(indices) % 3):
            raise ValueError(error)
        triangles += len(indices) // 3
        vertex_count = len(vertices) // 3
        if (triangles > MAX_TRIANGLES or not all(finite_number(value) for value in vertices)
                or any(type(index) is not int or not 0 <= index < vertex_count for index in indices)):
            raise ValueError(error)


@dataclass(frozen=True)
class ModelViewer:
    digest: str
    size: int
    directory: TemporaryDirectory = field(compare=False, repr=False)

    @property
    def path(self):
        return Path(self.directory.name) / "viewer.html"


def build_model(data):
    directory = TemporaryDirectory(prefix="archi-workspace-viewer-")
    source = Path(directory.name) / "model.json"
    output = source.with_name("viewer.html")
    try:
        with source.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        subprocess.run(["node", str(BUILDER), "--offline", str(source), str(output)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        source.unlink()
        size = output.stat().st_size
        if size > MAX_MODEL_BYTES:
            raise ValueError("The generated viewer exceeds 256 MiB.")
        digest = hashlib.sha256()
        with output.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return ModelViewer(digest.hexdigest(), size, directory)
    except BaseException:
        directory.cleanup()
        raise
