using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using AcadApplication = Autodesk.AutoCAD.ApplicationServices.Core.Application;
using AcadException = Autodesk.AutoCAD.Runtime.Exception;

namespace ArchiMagic.AutoCAD;

public static class LayerRenameTrial
{
    private const long MaxFixtureBytes = 32 * 1024 * 1024;
    private const int MaxEvents = 256;

    private sealed record Baseline(
        string DrawingFingerprint, string DrawingVersion, string FileName, string FileDigest, ObjectId LayerId,
        string Handle, string OldName, string NewName);

    [CommandMethod("AMCADTRYLAYERRENAME", CommandFlags.Modal)]
    public static void TryLayerRename()
    {
        var document = AcadApplication.DocumentManager.MdiActiveDocument;
        if (document is null)
            return;

        var editor = document.Editor;
        try
        {
            var from = editor.GetString(new PromptStringOptions("\nExisting fixture layer: ") { AllowSpaces = true });
            if (from.Status != PromptStatus.OK)
                return;

            var to = editor.GetString(new PromptStringOptions("\nProposed layer name: ") { AllowSpaces = true });
            if (to.Status != PromptStatus.OK)
                return;

            var baseline = ReadBaseline(document, from.StringResult, to.StringResult);
            Write(editor, new { status = "preview", handle = baseline.Handle, before = baseline.OldName,
                after = baseline.NewName });
            var prompt = new PromptKeywordOptions("\nRun the rollback-only trial on this disposable drawing?")
            {
                AllowNone = true,
            };
            prompt.Keywords.Add("Trial");
            prompt.Keywords.Add("Cancel");
            prompt.Keywords.Default = "Cancel";
            var answer = editor.GetKeywords(prompt);
            if (answer.Status != PromptStatus.OK || answer.StringResult != "Trial")
                return;

            if (ReadBaseline(document, baseline.OldName, baseline.NewName) != baseline)
                throw new InvalidOperationException("The drawing or layer changed after preview.");

            RunTrial(document, baseline);
        }
        catch (AcadException error)
        {
            Write(editor, new { status = "stopped", reason = error.ErrorStatus.ToString() });
        }
        catch (System.Exception error) when (error is InvalidOperationException or IOException or UnauthorizedAccessException)
        {
            Write(editor, new { status = "stopped", reason = error.Message });
        }
    }

    private static Baseline ReadBaseline(Document document, string oldName, string newName)
    {
        if (AcadApplication.DocumentManager.MdiActiveDocument != document
            || Convert.ToInt32(AcadApplication.GetSystemVariable("DBMOD")) != 0)
            throw new InvalidOperationException("Open the clean saved disposable drawing as the active document.");

        if (string.IsNullOrWhiteSpace(oldName) || string.IsNullOrWhiteSpace(newName)
            || oldName.Length > 255 || newName.Length > 255
            || oldName.Equals(newName, StringComparison.OrdinalIgnoreCase)
            || Reserved(oldName) || Reserved(newName))
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

        var fingerprint = database.FingerprintGuid;
        var version = database.VersionGuid;
        if (!Guid.TryParse(fingerprint, out var fingerprintId) || fingerprintId == Guid.Empty
            || !Guid.TryParse(version, out var versionId) || versionId == Guid.Empty)
            throw new InvalidOperationException("The fixture has no verifiable drawing fingerprint or version.");

        return new Baseline(fingerprint, version, database.Filename, HashDrawing(database.Filename),
            layer.ObjectId, layer.Handle.ToString(), layer.Name, newName);
    }

    private static bool Reserved(string name) =>
        name.Equals("0", StringComparison.OrdinalIgnoreCase)
        || name.Equals("Defpoints", StringComparison.OrdinalIgnoreCase) || name.Contains('|');

    private static string HashDrawing(string filename)
    {
        if (string.IsNullOrEmpty(filename) || !Path.IsPathFullyQualified(filename)
            || filename.StartsWith(@"\\", StringComparison.Ordinal)
            || !Path.GetExtension(filename).Equals(".dwg", StringComparison.OrdinalIgnoreCase)
            || new DriveInfo(Path.GetPathRoot(filename)!).DriveType != DriveType.Fixed)
            throw new InvalidOperationException("Use a local saved DWG fixture.");

        var file = new FileInfo(filename);
        for (FileSystemInfo? component = file; component is not null;
            component = component is FileInfo item ? item.Directory : ((DirectoryInfo)component).Parent)
        {
            if ((component.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("The fixture path contains a reparse point.");
        }

        if (file.Length <= 0 || file.Length > MaxFixtureBytes)
            throw new InvalidOperationException("Use a DWG fixture of at most 32 MiB.");

        var length = file.Length;
        var modified = file.LastWriteTimeUtc;
        using var stream = new FileStream(file.FullName, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[64 * 1024];
        int count;
        while ((count = stream.Read(buffer)) != 0)
        {
            if (stream.Position > MaxFixtureBytes)
                throw new InvalidOperationException("The fixture exceeded 32 MiB while being read.");
            hash.AppendData(buffer, 0, count);
        }

        if (stream.Position != length || stream.Length != length
            || File.GetLastWriteTimeUtc(file.FullName) != modified)
            throw new InvalidOperationException("The fixture changed while it was being read.");

        return Convert.ToHexString(hash.GetHashAndReset());
    }

    private static void RunTrial(Document document, Baseline baseline)
    {
        var database = document.Database;
        var events = new List<string>();
        var overflow = false;
        var unexpected = false;
        var saveObserved = false;
        var trialName = "";
        var rollbackStarted = false;
        var rollbackCompleted = false;
        var intendedModificationObserved = false;
        var failure = "";
        void Record(string operation, ObjectId id)
        {
            if (events.Count >= MaxEvents)
            {
                overflow = true;
                return;
            }

            if (operation != "modified" || id != baseline.LayerId)
                unexpected = true;
            else if (!rollbackStarted)
                intendedModificationObserved = true;

            events.Add((rollbackStarted ? "rollback:" : "trial:") + operation + ":"
                + (id.IsNull ? "unavailable" : id.Handle.ToString()));
        }

        void OnModified(object sender, ObjectEventArgs args) => Record("modified", args.DBObject.ObjectId);
        void OnAppended(object sender, ObjectEventArgs args) => Record("appended", args.DBObject.ObjectId);
        void OnReappended(object sender, ObjectEventArgs args) => Record("reappended", args.DBObject.ObjectId);
        void OnUnappended(object sender, ObjectEventArgs args) => Record("unappended", args.DBObject.ObjectId);
        void OnErased(object sender, ObjectErasedEventArgs args) =>
            Record(args.Erased ? "erased" : "unerased", args.DBObject.ObjectId);
        void OnSave(object sender, DatabaseIOEventArgs args) => saveObserved = true;

        database.ObjectModified += OnModified;
        database.ObjectAppended += OnAppended;
        database.ObjectReappended += OnReappended;
        database.ObjectUnappended += OnUnappended;
        database.ObjectErased += OnErased;
        database.BeginSave += OnSave;
        database.SaveComplete += OnSave;
        try
        {
            using var transaction = database.TransactionManager.StartTransaction();
            try
            {
                var layer = (LayerTableRecord)transaction.GetObject(baseline.LayerId, OpenMode.ForWrite);
                layer.Name = baseline.NewName;
                trialName = layer.Name;
                layer.DowngradeOpen();
            }
            finally
            {
                rollbackStarted = true;
                transaction.Abort();
                rollbackCompleted = true;
            }
        }
        catch (System.Exception error)
        {
            failure = error is AcadException native ? native.ErrorStatus.ToString() : error.GetType().Name;
        }
        finally
        {
            database.ObjectModified -= OnModified;
            database.ObjectAppended -= OnAppended;
            database.ObjectReappended -= OnReappended;
            database.ObjectUnappended -= OnUnappended;
            database.ObjectErased -= OnErased;
            database.BeginSave -= OnSave;
            database.SaveComplete -= OnSave;
        }

        var restored = false;
        try
        {
            restored = ReadBaseline(document, baseline.OldName, baseline.NewName) == baseline;
        }
        catch (System.Exception error) when (error is AcadException or InvalidOperationException or IOException or UnauthorizedAccessException)
        {
            failure = error.Message;
        }

        var qualified = restored && trialName == baseline.NewName && intendedModificationObserved
            && !overflow && !unexpected && !saveObserved && failure.Length == 0;
        var status = !restored || !rollbackCompleted || unexpected || overflow || saveObserved ? "unresolved"
            : qualified ? "measured" : "unqualified";
        Write(document.Editor, new { status, baseline_restored = restored, expected = baseline.NewName,
            drawing_fingerprint = baseline.DrawingFingerprint, drawing_version = baseline.DrawingVersion,
            file_sha256_before = baseline.FileDigest, handle = baseline.Handle,
            observed = trialName, rollback_completed = rollbackCompleted, save_observed = saveObserved,
            unexpected, overflow, events, failure });
    }

    private static void Write(Editor editor, object message) =>
        editor.WriteMessage("\n" + JsonSerializer.Serialize(message));
}
