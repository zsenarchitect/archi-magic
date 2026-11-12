"""Render exact family symbols in one disposable Revit project."""

import hashlib
import os
import struct
from time import monotonic

MAX_IMAGE_DIMENSION = 256
MAX_IMAGE_BYTES = 256 * 1024


def png_record(data):
    if (not 45 <= len(data) <= MAX_IMAGE_BYTES
            or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
            or data[-12:] != b"\x00\x00\x00\x00IEND\xae\x42\x60\x82"):
        raise ValueError("Expected one bounded PNG thumbnail.")
    width, height = struct.unpack(">II", data[16:24])
    if not 1 <= width <= MAX_IMAGE_DIMENSION or not 1 <= height <= MAX_IMAGE_DIMENSION:
        raise ValueError("The PNG thumbnail dimensions exceed the limit.")
    digest = hashlib.sha256(data).hexdigest()
    return {"file": f"thumbnail-{digest}.png", "sha256": digest,
            "bytes": len(data), "width": width, "height": height}


def preview_bytes(symbol):
    import clr
    clr.AddReference("System.Drawing.Common")
    from System.Drawing import Size
    from System.Drawing.Imaging import ImageFormat
    from System.IO import MemoryStream

    bitmap = symbol.GetPreviewImage(Size(MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    if bitmap is None:
        return None
    try:
        stream = MemoryStream()
        try:
            bitmap.Save(stream, ImageFormat.Png)
            return bytes(stream.ToArray()) if stream.Length <= MAX_IMAGE_BYTES else b""
        finally:
            stream.Dispose()
    finally:
        bitmap.Dispose()


def exact_symbol(scratch, DB, path, type_name, metadata):
    transaction = DB.Transaction(scratch, "Load one family preview")
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise RuntimeError("Revit could not start the scratch load.")
        loaded, symbol = scratch.LoadFamilySymbol(os.fspath(path), type_name)
        if not loaded or symbol is None:
            return None
        category = symbol.Family.FamilyCategory
        if (str(symbol.Name) != type_name or str(symbol.Family.Name) != metadata["family"]
                or str(category.Id.Value) != metadata["category"]["id"]):
            raise RuntimeError("The loaded symbol does not match the staged family and type.")
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise RuntimeError("Revit could not commit the scratch load.")
        return symbol
    finally:
        try:
            if (transaction.GetStatus() == DB.TransactionStatus.Started
                    and transaction.RollBack() != DB.TransactionStatus.RolledBack):
                raise RuntimeError("Revit could not roll back the scratch load.")
        finally:
            transaction.Dispose()


def render_thumbnails(scratch, DB, path, metadata, deadline, encode=preview_bytes):
    names = [item["name"] for item in metadata["embedded_types"]]
    families = DB.FilteredElementCollector(scratch).OfClass(DB.Family)
    if any(str(family.Name).casefold() == metadata["family"].casefold() for family in families):
        return [{"type": name, "status": "scratch_name_collision"} for name in names], {}
    records, assets = [], {}
    for name in names:
        record = {"type": name}
        if monotonic() >= deadline:
            record["status"] = "time_limit"
        else:
            symbol = exact_symbol(scratch, DB, path, name, metadata)
            data = encode(symbol) if symbol is not None else None
            if data is None:
                record["status"] = "preview_unavailable" if symbol is not None else "type_unavailable"
            else:
                try:
                    image = png_record(data)
                except ValueError:
                    record["status"] = "invalid_preview"
                else:
                    record.update(status="available", image=image)
                    assets[image["file"]] = data
        records.append(record)
    return records, assets
