using System;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;

namespace ArchiMagic.AutoCAD;

public static class EntityEditTrials
{
    private const int MaxAttributes = 100;
    private const int MaxAttributeBytes = 255;

    [CommandMethod("AMCADTRYLAYERMAP", CommandFlags.Modal)]
    public static void TryLayerMap() => DrawingTrial.Execute(document =>
    {
        var selection = document.Editor.GetEntity(new PromptEntityOptions("\nSelect one entity to map: "));
        if (selection.Status != PromptStatus.OK)
            return;

        var layer = document.Editor.GetString(new PromptStringOptions("\nExisting destination layer: ") { AllowSpaces = true });
        if (layer.Status != PromptStatus.OK)
            return;

        DrawingTrial.Run(document, () => InspectMap(document, selection.ObjectId, layer.StringResult),
            (transaction, target) =>
            {
                var entity = (Entity)transaction.GetObject(target.Id, OpenMode.ForWrite);
                entity.LayerId = target.RelatedId;
                var actual = (LayerTableRecord)transaction.GetObject(entity.LayerId, OpenMode.ForRead);
                var observed = DescribeLayer(actual);
                entity.DowngradeOpen();
                return observed;
            });
    });

    [CommandMethod("AMCADTRYATTRFILL", CommandFlags.Modal)]
    public static void TryAttributeFill() => DrawingTrial.Execute(document =>
    {
        var selection = document.Editor.GetEntity(new PromptEntityOptions("\nSelect one block insert: "));
        if (selection.Status != PromptStatus.OK)
            return;

        var tag = document.Editor.GetString(new PromptStringOptions("\nExisting attribute tag: "));
        if (tag.Status != PromptStatus.OK)
            return;

        var text = document.Editor.GetString(new PromptStringOptions("\nProposed literal text: ") { AllowSpaces = true });
        if (text.Status != PromptStatus.OK)
            return;

        DrawingTrial.Run(document, () => InspectAttribute(document, selection.ObjectId, tag.StringResult, text.StringResult),
            (transaction, target) =>
            {
                var attribute = (AttributeReference)transaction.GetObject(target.Id, OpenMode.ForWrite);
                attribute.TextString = target.After;
                var observed = attribute.TextString;
                attribute.DowngradeOpen();
                return observed;
            });
    });

    private static DrawingTrial.Target InspectMap(Document document, ObjectId entityId, string layerName)
    {
        if (string.IsNullOrWhiteSpace(layerName) || layerName.Length > 255 || DrawingTrial.ReservedLayer(layerName))
            throw new InvalidOperationException("Select an existing nonreserved destination layer.");

        var database = document.Database;
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var entity = ReadLayoutEntity(transaction, entityId);
        if (entity is AttributeReference)
            throw new InvalidOperationException("Select a layout entity or its owning insert.");

        var layers = (LayerTable)transaction.GetObject(database.LayerTableId, OpenMode.ForRead);
        if (!layers.Has(layerName))
            throw new InvalidOperationException("The reviewed destination layer is missing.");

        var source = ReadEditableLayer(transaction, entity.LayerId);
        var destination = ReadEditableLayer(transaction, layers[layerName]);
        if (source.ObjectId == destination.ObjectId)
            throw new InvalidOperationException("The entity is already on the destination layer.");

        return new DrawingTrial.Target(entityId, entity.Handle.ToString(), DescribeLayer(source),
            DescribeLayer(destination), entity.GetRXClass().DxfName + " layer map, layout " + entity.OwnerId.Handle,
            destination.ObjectId);
    }

    private static DrawingTrial.Target InspectAttribute(Document document, ObjectId insertId, string tag, string text)
    {
        if (string.IsNullOrWhiteSpace(tag) || tag.Length > 255 || !IsLiteral(text))
            throw new InvalidOperationException("Use an existing tag and nonempty printable ASCII text without AutoCAD formatting codes.");

        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        if (ReadLayoutEntity(transaction, insertId) is not BlockReference insert)
            throw new InvalidOperationException("Select a native block insert.");

        ReadEditableLayer(transaction, insert.LayerId);
        var definition = (BlockTableRecord)transaction.GetObject(insert.BlockTableRecord, OpenMode.ForRead);
        if (definition.IsFromExternalReference || definition.IsDependent)
            throw new InvalidOperationException("The insert belongs to an external or dependent block.");

        AttributeReference? match = null;
        var count = 0;
        foreach (ObjectId id in insert.AttributeCollection)
        {
            if (++count > MaxAttributes
                || transaction.GetObject(id, OpenMode.ForRead) is not AttributeReference attribute
                || attribute.OwnerId != insertId)
                throw new InvalidOperationException("The insert's native attribute inventory is incomplete or too large.");

            if (!attribute.Tag.Equals(tag, StringComparison.OrdinalIgnoreCase))
                continue;
            if (match is not null)
                throw new InvalidOperationException("The insert has more than one attribute with that tag.");
            match = attribute;
        }

        if (match is null || match.IsConstant || match.IsMTextAttribute || match.HasFields
            || match.TextString == text)
            throw new InvalidOperationException("Choose a changed, editable single-line attribute with no field.");

        ReadEditableLayer(transaction, match.LayerId);
        return new DrawingTrial.Target(match.ObjectId, match.Handle.ToString(), match.TextString, text,
            "Attribute " + match.Tag + ", insert " + insert.Handle + ", layer " + match.LayerId.Handle);
    }

    private static Entity ReadLayoutEntity(Transaction transaction, ObjectId id)
    {
        if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity || entity is ProxyEntity
            || transaction.GetObject(entity.OwnerId, OpenMode.ForRead) is not BlockTableRecord owner
            || !owner.IsLayout || owner.IsFromExternalReference)
            throw new InvalidOperationException("Select an entity directly owned by an active drawing layout.");

        return entity;
    }

    private static LayerTableRecord ReadEditableLayer(Transaction transaction, ObjectId id)
    {
        var layer = (LayerTableRecord)transaction.GetObject(id, OpenMode.ForRead);
        if (layer.IsDependent || layer.IsLocked)
            throw new InvalidOperationException("The source or destination layer is dependent or locked.");

        return layer;
    }

    private static string DescribeLayer(LayerTableRecord layer) => layer.Name + " [" + layer.Handle + "]";

    private static bool IsLiteral(string text)
    {
        if (string.IsNullOrWhiteSpace(text) || text.Length > MaxAttributeBytes)
            return false;

        foreach (var character in text)
            if (character < ' ' || character > '~' || character is '%' or '\\' or '{' or '}')
                return false;

        return true;
    }
}
