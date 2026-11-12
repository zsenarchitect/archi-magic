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

internal static class DrawingTrial
{
    private const long MaxFixtureBytes = 32 * 1024 * 1024;
    private const int MaxEvents = 256;

    internal sealed record Target(ObjectId Id, string Handle, string Before, string After, string Context,
        ObjectId RelatedId = default, bool ErasesTarget = false, ObjectId OwnedBegin = default,
        ObjectId OwnedEnd = default)
    {
        internal bool Includes(ObjectId id) =>
            !id.IsNull && (id == Id || id == OwnedBegin || id == OwnedEnd);
    }

    private sealed record Baseline(string DrawingFingerprint, string DrawingVersion, string FileName,
        string FileDigest, Target Target);

    internal static void Run(Document document, Func<Target> inspect, Func<Transaction, Target, string> edit)
    {
        var editor = document.Editor;
        var baseline = ReadBaseline(document, inspect);
        Write(editor, new { status = "preview", handle = baseline.Target.Handle, before = baseline.Target.Before,
            after = baseline.Target.After, context = baseline.Target.Context });
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

        if (ReadBaseline(document, inspect) != baseline)
            throw new InvalidOperationException("The drawing or reviewed target changed after preview.");

        RunTrial(document, baseline, inspect, edit);
    }

    internal static void Execute(Action<Document> command)
    {
        var document = AcadApplication.DocumentManager.MdiActiveDocument;
        if (document is null)
            return;

        try
        {
            command(document);
        }
        catch (AcadException error)
        {
            Write(document.Editor, new { status = "stopped", reason = error.ErrorStatus.ToString() });
        }
        catch (System.Exception error) when (error is InvalidOperationException or IOException or UnauthorizedAccessException)
        {
            Write(document.Editor, new { status = "stopped", reason = error.Message });
        }
    }

    private static Baseline ReadBaseline(Document document, Func<Target> inspect)
    {
        var identity = ReadDrawing(document);
        return new Baseline(identity.Fingerprint, identity.Version, identity.FileName, identity.Digest, inspect());
    }

    internal static (string Fingerprint, string Version, string FileName, string Digest) ReadDrawing(Document document)
    {
        if (AcadApplication.DocumentManager.MdiActiveDocument != document
            || Convert.ToInt32(AcadApplication.GetSystemVariable("DBMOD")) != 0)
            throw new InvalidOperationException("Open the clean saved disposable drawing as the active document.");

        var database = document.Database;
        var fingerprint = database.FingerprintGuid;
        var version = database.VersionGuid;
        if (!Guid.TryParse(fingerprint, out var fingerprintId) || fingerprintId == Guid.Empty
            || !Guid.TryParse(version, out var versionId) || versionId == Guid.Empty)
            throw new InvalidOperationException("The fixture has no verifiable drawing fingerprint or version.");

        return (fingerprint, version, database.Filename, HashDrawing(database.Filename));
    }

    internal static bool ReservedLayer(string name) =>
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

    private static void RunTrial(Document document, Baseline baseline, Func<Target> inspect,
        Func<Transaction, Target, string> edit)
    {
        var database = document.Database;
        var events = new List<string>();
        var overflow = false;
        var unexpected = false;
        var saveObserved = false;
        var trialName = "";
        var rollbackStarted = false;
        var rollbackCompleted = false;
        var observedTargets = new HashSet<ObjectId>();
        var failure = "";
        void Record(string operation, ObjectId id)
        {
            if (events.Count >= MaxEvents)
            {
                overflow = true;
                return;
            }

            var target = baseline.Target;
            var permittedEvent = target.ErasesTarget
                ? operation == "modified" || operation == (rollbackStarted ? "unerased" : "erased")
                : operation == "modified";
            if (!target.Includes(id) || !permittedEvent)
                unexpected = true;
            else if (!rollbackStarted && operation == (target.ErasesTarget ? "erased" : "modified"))
                observedTargets.Add(id);

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
                trialName = edit(transaction, baseline.Target);
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
            restored = ReadBaseline(document, inspect) == baseline;
        }
        catch (System.Exception error) when (error is AcadException or InvalidOperationException or IOException or UnauthorizedAccessException)
        {
            failure = error.Message;
        }

        var reviewed = baseline.Target;
        var expectedEvents = observedTargets.Contains(reviewed.Id)
            && (reviewed.OwnedBegin.IsNull || observedTargets.Contains(reviewed.OwnedBegin))
            && (reviewed.OwnedEnd.IsNull || observedTargets.Contains(reviewed.OwnedEnd));
        var qualified = restored && trialName == reviewed.After && expectedEvents
            && !overflow && !unexpected && !saveObserved && failure.Length == 0;
        var status = !restored || !rollbackCompleted || unexpected || overflow || saveObserved ? "unresolved"
            : qualified ? "measured" : "unqualified";
        Write(document.Editor, new { status, baseline_restored = restored, expected = baseline.Target.After,
            drawing_fingerprint = baseline.DrawingFingerprint, drawing_version = baseline.DrawingVersion,
            file_sha256_before = baseline.FileDigest, handle = reviewed.Handle,
            owned_begin = reviewed.OwnedBegin.IsNull ? null : reviewed.OwnedBegin.Handle.ToString(),
            owned_end = reviewed.OwnedEnd.IsNull ? null : reviewed.OwnedEnd.Handle.ToString(),
            observed = trialName, rollback_completed = rollbackCompleted, save_observed = saveObserved,
            expected_events_observed = expectedEvents, unexpected, overflow, events, failure });
    }

    private static void Write(Editor editor, object message) =>
        editor.WriteMessage("\n" + JsonSerializer.Serialize(message));
}
