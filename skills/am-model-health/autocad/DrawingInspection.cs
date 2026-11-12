using System;
using System.Collections.Generic;
using System.Text.Json;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using AcadApplication = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace ArchiMagic.AutoCAD;

public static class DrawingInspection
{
    private const int MaxAttributes = 100;
    private const int MaxTextLength = 4096;

    [CommandMethod("AMCADINSPECT", CommandFlags.Modal)]
    public static void InspectSelectedEntity()
    {
        var document = AcadApplication.DocumentManager.MdiActiveDocument;
        if (document is null)
            return;

        var editor = document.Editor;
        var selection = editor.GetEntity(new PromptEntityOptions("\nSelect one entity or block insert: "));
        if (selection.Status != PromptStatus.OK)
            return;

        try
        {
            using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
            if (transaction.GetObject(selection.ObjectId, OpenMode.ForRead) is not Entity entity
                || transaction.GetObject(entity.LayerId, OpenMode.ForRead) is not LayerTableRecord layer)
            {
                editor.WriteMessage("\nArchi Magic could not verify the selected entity and layer.");
                return;
            }

            var attributes = new List<object>();
            if (entity is BlockReference insert)
            {
                foreach (ObjectId id in insert.AttributeCollection)
                {
                    if (attributes.Count >= MaxAttributes
                        || transaction.GetObject(id, OpenMode.ForRead) is not AttributeReference attribute
                        || attribute.OwnerId != insert.ObjectId
                        || attribute.TextString.Length > MaxTextLength)
                    {
                        editor.WriteMessage("\nArchi Magic stopped because the attribute inventory was incomplete or too large.");
                        return;
                    }

                    attributes.Add(new
                    {
                        handle = attribute.Handle.ToString(),
                        tag = attribute.Tag,
                        text = attribute.TextString,
                        constant = attribute.IsConstant,
                        multiline = attribute.IsMTextAttribute,
                        has_fields = attribute.HasFields,
                    });
                }
            }

            var inspection = new
            {
                kind = "autocad-entity-inspection",
                drawing_fingerprint = document.Database.FingerprintGuid,
                drawing_version = document.Database.VersionGuid,
                handle = entity.Handle.ToString(),
                native_class = entity.GetRXClass().DxfName,
                owner_handle = entity.OwnerId.IsNull ? null : entity.OwnerId.Handle.ToString(),
                layer = new
                {
                    handle = layer.Handle.ToString(),
                    name = layer.Name,
                    locked = layer.IsLocked,
                    dependent = layer.IsDependent,
                },
                attributes,
            };
            editor.WriteMessage("\n" + JsonSerializer.Serialize(inspection));
        }
        catch (Autodesk.AutoCAD.Runtime.Exception error)
        {
            editor.WriteMessage("\nArchi Magic could not read the selected entity (" + error.ErrorStatus + ").");
        }
    }
}
