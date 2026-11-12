using System;
using System.IO;
using System.Text.Json;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;

namespace ArchiMagic.AutoCAD;

public static class EditRequestInspection
{
    [CommandMethod("AMCADCHECKREQUEST", CommandFlags.Modal)]
    public static void CheckRequest() => DrawingTrial.Execute(document =>
    {
        var selected = document.Editor.GetFileNameForOpen(new PromptOpenFileOptions("\nSelect a local edit request JSON file: ")
        {
            Filter = "JSON files (*.json)|*.json",
        });
        if (selected.Status != PromptStatus.OK)
            return;

        EditRequest request;
        try
        {
            request = EditRequest.Parse(ReadRequest(selected.StringResult));
        }
        catch (JsonException)
        {
            throw new InvalidOperationException("The edit request is not valid bounded JSON.");
        }

        var active = DrawingTrial.ReadDrawing(document);
        if (Guid.Parse(active.Fingerprint) != request.DrawingFingerprint || Guid.Parse(active.Version) != request.DrawingVersion
            || active.Digest != request.FileSha256)
            throw new InvalidOperationException("The request does not match the clean active drawing and saved bytes.");

        var database = document.Database;
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        foreach (var row in request.Edits)
            CheckBaseline(database, transaction, row);

        document.Editor.WriteMessage("\n" + JsonSerializer.Serialize(new
        {
            status = "request_checked",
            request_sha256 = request.RequestSha256,
            row_count = request.Edits.Length,
            transaction_started_for_write = false,
        }));
    });

    private static byte[] ReadRequest(string path)
    {
        if (!Path.IsPathFullyQualified(path) || path.StartsWith(@"\\", StringComparison.Ordinal)
            || !Path.GetExtension(path).Equals(".json", StringComparison.OrdinalIgnoreCase)
            || new DriveInfo(Path.GetPathRoot(path)!).DriveType != DriveType.Fixed)
            throw new InvalidOperationException("Select a local JSON file.");
        var file = new FileInfo(path);
        for (FileSystemInfo? item = file; item is not null;
            item = item is FileInfo leaf ? leaf.Directory : ((DirectoryInfo)item).Parent)
            if ((item.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("The edit request path contains a reparse point.");

        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        var buffer = new byte[EditRequest.MaxBytes + 1];
        var size = stream.ReadAtLeast(buffer, buffer.Length, false);
        if (size > EditRequest.MaxBytes)
            throw new InvalidOperationException("The edit request exceeds 64 KiB.");
        return buffer[..size];
    }

    private static void CheckBaseline(Database database, Transaction transaction, EditRow row)
    {
        var id = Resolve(database, row.Handle);
        var target = transaction.GetObject(id, OpenMode.ForRead);
        if (target.GetRXClass().DxfName != row.NativeClass)
            throw new InvalidOperationException("The native class changed for handle " + row.Handle.ToString("X") + ".");

        string? actual = row.Kind switch
        {
            "rename_layer" or "purge_layer" when target is LayerTableRecord layer => layer.Name,
            "purge_block" when target is BlockTableRecord block
                && Resolve(database, row.OwnedHandles[0]) == block.BlockBeginId
                && Resolve(database, row.OwnedHandles[1]) == block.BlockEndId => block.Name,
            "purge_dim_style" when target is DimStyleTableRecord style => style.Name,
            "map_entity" when target is Entity entity && transaction.GetObject(
                Resolve(database, EditRequest.Handle(row.After!)), OpenMode.ForRead) is LayerTableRecord => entity.LayerId.Handle.ToString(),
            "repath_xref" when target is BlockTableRecord xref && (xref.IsFromExternalReference || xref.IsFromOverlayReference)
                => xref.PathName,
            "fill_attribute" when target is AttributeReference attribute
                && attribute.OwnerId == Resolve(database, row.OwnerHandle!.Value) && attribute.Tag == row.Tag => attribute.TextString,
            "erase_empty_text" when target is DBText text && target is not (AttributeReference or AttributeDefinition) => text.TextString,
            "erase_empty_text" when target is MText multiline => multiline.Contents,
            _ => null,
        };
        if (actual is null || actual != row.Before)
            throw new InvalidOperationException("The reviewed original value or owner changed for handle " + row.Handle.ToString("X") + ".");
    }

    private static ObjectId Resolve(Database database, ulong handle) =>
        database.GetObjectId(false, new Handle(unchecked((long)handle)), 0);
}
