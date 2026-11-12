using System;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;

namespace ArchiMagic.AutoCAD;

public static class LayerRenameTrial
{
    [CommandMethod("AMCADTRYLAYERRENAME", CommandFlags.Modal)]
    public static void TryLayerRename() => DrawingTrial.Execute(document =>
    {
        var from = document.Editor.GetString(new PromptStringOptions("\nExisting fixture layer: ") { AllowSpaces = true });
        if (from.Status != PromptStatus.OK)
            return;

        var to = document.Editor.GetString(new PromptStringOptions("\nProposed layer name: ") { AllowSpaces = true });
        if (to.Status != PromptStatus.OK)
            return;

        DrawingTrial.Run(document, () => Inspect(document, from.StringResult, to.StringResult), (transaction, target) =>
        {
            var layer = (LayerTableRecord)transaction.GetObject(target.Id, OpenMode.ForWrite);
            layer.Name = target.After;
            var observed = layer.Name;
            layer.DowngradeOpen();
            return observed;
        });
    });

    private static DrawingTrial.Target Inspect(Document document, string oldName, string newName)
    {
        if (string.IsNullOrWhiteSpace(oldName) || string.IsNullOrWhiteSpace(newName)
            || oldName.Length > 255 || newName.Length > 255
            || oldName.Equals(newName, StringComparison.OrdinalIgnoreCase)
            || DrawingTrial.ReservedLayer(oldName) || DrawingTrial.ReservedLayer(newName))
            throw new InvalidOperationException("Use distinct, nonreserved layer names.");

        SymbolUtilityServices.ValidateSymbolName(newName, false);
        var database = document.Database;
        using var transaction = database.TransactionManager.StartOpenCloseTransaction();
        var layers = (LayerTable)transaction.GetObject(database.LayerTableId, OpenMode.ForRead);
        if (!layers.Has(oldName) || layers.Has(newName))
            throw new InvalidOperationException("The source layer is missing or the destination layer already exists.");

        var layer = (LayerTableRecord)transaction.GetObject(layers[oldName], OpenMode.ForRead);
        if (layer.IsDependent || layer.IsLocked || layer.ObjectId == database.Clayer)
            throw new InvalidOperationException("The source layer is dependent, locked or current.");

        return new DrawingTrial.Target(layer.ObjectId, layer.Handle.ToString(), layer.Name, newName, "Layer rename");
    }

}
