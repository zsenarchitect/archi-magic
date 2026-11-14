using System;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;

namespace ArchiMagic.AutoCAD;

public static class PurgeTrial
{
    private const string Erased = "Erased";

    [CommandMethod("AMCADTRYPURGE", CommandFlags.Modal)]
    public static void TryPurge() => DrawingTrial.Execute(document =>
    {
        var editor = document.Editor;
        var choice = new PromptKeywordOptions("\nChoose the exact kind of purge trial:");
        choice.Keywords.Add("Layer");
        choice.Keywords.Add("Block");
        choice.Keywords.Add("DimStyle");
        choice.Keywords.Add("EmptyText");
        var kind = editor.GetKeywords(choice);
        if (kind.Status != PromptStatus.OK)
            return;

        if (kind.StringResult == "EmptyText")
        {
            var selection = editor.GetEntity(new PromptEntityOptions("\nSelect one empty text entity: "));
            if (selection.Status == PromptStatus.OK)
                DrawingTrial.Run(document, () => InspectEmptyText(document, selection.ObjectId), EraseTarget);
            return;
        }

        var name = editor.GetString(new PromptStringOptions("\nExact existing symbol name: ") { AllowSpaces = true });
        if (name.Status == PromptStatus.OK)
            DrawingTrial.Run(document, () => InspectSymbol(document, kind.StringResult, name.StringResult), EraseTarget);
    });

    private static DrawingTrial.Target InspectSymbol(Document document, string kind, string name)
    {
        if (string.IsNullOrWhiteSpace(name) || name.Length > 255 || name.Contains('|'))
            throw new InvalidOperationException("Use an independent named symbol.");

        var database = document.Database;
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var tableId = kind switch
        {
            "Layer" => database.LayerTableId,
            "Block" => database.BlockTableId,
            "DimStyle" => database.DimStyleTableId,
            _ => throw new InvalidOperationException("Choose Layer, Block or DimStyle."),
        };
        var table = (SymbolTable)transaction.GetObject(tableId, OpenMode.ForRead);
        if (!table.Has(name))
            throw new InvalidOperationException("The selected named symbol is missing.");

        var id = table[name];
        var record = (SymbolTableRecord)transaction.GetObject(id, OpenMode.ForRead);
        if (record.IsDependent || !record.ExtensionDictionary.IsNull)
            throw new InvalidOperationException("Dependent symbols and symbols with extension dictionaries require separate review.");

        var begin = ObjectId.Null;
        var end = ObjectId.Null;
        if (record is LayerTableRecord layer
            && (layer.IsLocked || id == database.Clayer || DrawingTrial.ReservedLayer(layer.Name)))
            throw new InvalidOperationException("The selected layer is reserved, locked or current.");

        if (record is DimStyleTableRecord && id == database.Dimstyle)
            throw new InvalidOperationException("The selected dimension style is current.");

        if (record is BlockTableRecord block)
        {
            if (block.IsLayout || block.IsFromExternalReference || block.IsFromOverlayReference
                || block.IsAnonymous || block.IsDynamicBlock)
                throw new InvalidOperationException("Select an ordinary independent block definition.");
            foreach (ObjectId _ in block)
                throw new InvalidOperationException("The block contains owned drawing entities that require separate review.");

            begin = block.BlockBeginId;
            end = block.BlockEndId;
            if (begin.IsNull || end.IsNull || begin == end
                || transaction.GetObject(begin, OpenMode.ForRead) is not BlockBegin opening
                || transaction.GetObject(end, OpenMode.ForRead) is not BlockEnd closing
                || opening.OwnerId != id || closing.OwnerId != id
                || !opening.ExtensionDictionary.IsNull || !closing.ExtensionDictionary.IsNull)
                throw new InvalidOperationException("The empty block's native boundary objects could not be verified.");
        }

        var candidates = new ObjectIdCollection { id };
        database.Purge(candidates);
        if (candidates.Count != 1 || candidates[0] != id)
            throw new InvalidOperationException("AutoCAD reports the selected symbol is ineligible for purge.");

        var ownership = begin.IsNull ? "" : ", block boundary handles " + begin.Handle + " and " + end.Handle;
        return new DrawingTrial.Target(id, record.Handle.ToString(), record.Name, Erased,
            kind + " purge" + ownership, ErasesTarget: true, OwnedBegin: begin, OwnedEnd: end);
    }

    private static DrawingTrial.Target InspectEmptyText(Document document, ObjectId id)
    {
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity
            || transaction.GetObject(entity.OwnerId, OpenMode.ForRead) is not BlockTableRecord owner
            || !owner.IsLayout || owner.IsFromExternalReference || entity.HasFields || !entity.ExtensionDictionary.IsNull)
            throw new InvalidOperationException("Select a field-free text entity directly owned by the drawing layout.");

        var text = entity switch
        {
            AttributeReference => null,
            AttributeDefinition => null,
            DBText singleLine => singleLine.TextString,
            MText multiline => multiline.Contents,
            _ => null,
        };
        if (text is null || text.Length != 0)
            throw new InvalidOperationException("Select a DBText or MText entity with an empty stored value.");

        var layer = (LayerTableRecord)transaction.GetObject(entity.LayerId, OpenMode.ForRead);
        if (layer.IsLocked || layer.IsDependent)
            throw new InvalidOperationException("The text layer is locked or dependent.");

        return new DrawingTrial.Target(id, entity.Handle.ToString(), "Empty text", Erased,
            entity.GetRXClass().DxfName + ", layout " + owner.Handle + ", layer " + layer.Handle,
            ErasesTarget: true);
    }

    private static string EraseTarget(Transaction transaction, DrawingTrial.Target target)
    {
        var value = transaction.GetObject(target.Id, OpenMode.ForWrite);
        value.Erase();
        value.DowngradeOpen();
        var observed = target.Id.IsErased
            && (target.OwnedBegin.IsNull || target.OwnedBegin.IsEffectivelyErased)
            && (target.OwnedEnd.IsNull || target.OwnedEnd.IsEffectivelyErased);
        return observed ? Erased : "Erase not verified";
    }
}
