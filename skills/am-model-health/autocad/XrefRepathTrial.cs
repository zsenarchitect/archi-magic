using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Microsoft.Win32.SafeHandles;

namespace ArchiMagic.AutoCAD;

public static class XrefRepathTrial
{
    private const int MaxDirectXrefs = 256;
    private const long MaxXrefBytes = 32 * 1024 * 1024;
    private const uint FileReadAttributes = 0x80;
    private const uint ShareRead = 1;
    private const uint OpenExisting = 3;
    private const uint BackupSemantics = 0x02000000;
    private const uint OpenReparsePoint = 0x00200000;

    private sealed record XrefState(string Status, string? LoadedFile, bool Overlay);

    [CommandMethod("AMCADTRYXREFPATH", CommandFlags.Modal)]
    public static void TryXrefRepath() => DrawingTrial.Execute(document =>
    {
        var editor = document.Editor;
        var selection = editor.GetEntity(new PromptEntityOptions("\nSelect one direct Xref insert: "));
        if (selection.Status != PromptStatus.OK)
            return;

        var root = editor.GetString(new PromptStringOptions("\nApproved physical local drawing root: ") { AllowSpaces = true });
        if (root.Status != PromptStatus.OK)
            return;

        var path = editor.GetString(new PromptStringOptions("\nProposed DWG path relative to the host drawing: ") { AllowSpaces = true });
        if (path.Status != PromptStatus.OK)
            return;

        using var directories = HoldDirectories(document.Database.Filename, root.StringResult, path.StringResult);
        using var targetFile = OpenTarget(document.Database.Filename, root.StringResult, path.StringResult);
        var digest = Convert.ToHexString(SHA256.HashData(targetFile));
        var reviewedState = ReadSelected(document, selection.ObjectId).State;
        DrawingTrial.Run(document, () =>
        {
            VerifyTarget(document.Database.Filename, root.StringResult, path.StringResult, targetFile);
            var current = ReadSelected(document, selection.ObjectId);
            if (current.State != reviewedState || current.Path == path.StringResult)
                throw new InvalidOperationException("The Xref state changed or already stores the proposed path.");

            return new DrawingTrial.Target(current.RecordId, current.RecordId.Handle.ToString(), current.Path,
                path.StringResult, "Direct Xref " + selection.ObjectId.Handle + ", " + current.State.Status
                + (current.State.Overlay ? ", overlay" : ", attachment") + ", target SHA-256 " + digest);
        }, (transaction, target) =>
        {
            var record = (BlockTableRecord)transaction.GetObject(target.Id, OpenMode.ForWrite);
            record.PathName = target.After;
            var observed = record.PathName;
            record.DowngradeOpen();
            VerifyTarget(document.Database.Filename, root.StringResult, path.StringResult, targetFile);
            if (ReadState(document.Database, record) != reviewedState)
                throw new InvalidOperationException("The Xref resolved or loaded state changed during the path trial.");
            return observed;
        });
    });

    private static (ObjectId RecordId, string Path, XrefState State) ReadSelected(Document document, ObjectId insertId)
    {
        using var transaction = document.Database.TransactionManager.StartOpenCloseTransaction();
        if (transaction.GetObject(insertId, OpenMode.ForRead) is not BlockReference insert
            || transaction.GetObject(insert.OwnerId, OpenMode.ForRead) is not BlockTableRecord layout || !layout.IsLayout
            || transaction.GetObject(insert.LayerId, OpenMode.ForRead) is not LayerTableRecord layer
            || layer.IsLocked || layer.IsDependent
            || transaction.GetObject(insert.BlockTableRecord, OpenMode.ForRead) is not BlockTableRecord record
            || record.IsDependent || !(record.IsFromExternalReference || record.IsFromOverlayReference)
            || string.IsNullOrWhiteSpace(record.PathName) || record.PathName.Length > 1024)
            throw new InvalidOperationException("Select an editable direct Xref insert with an existing saved path.");

        return (record.ObjectId, record.PathName, ReadState(document.Database, record));
    }

    private static XrefState ReadState(Database database, BlockTableRecord record)
    {
        using var graph = database.GetHostDwgXrefGraph(true);
        var host = graph.HostDrawing;
        if (host is null || host.NumOut > MaxDirectXrefs)
            throw new InvalidOperationException("The host Xref graph is unavailable or exceeds the trial scope.");

        XrefGraphNode? direct = null;
        for (var index = 0; index < host.NumOut; index++)
        {
            if (host.Out(index) is not XrefGraphNode node || node.BlockTableRecordId != record.ObjectId)
                continue;
            if (direct is not null)
                throw new InvalidOperationException("The Xref graph has more than one matching direct node.");
            direct = node;
        }

        if (direct is null)
            throw new InvalidOperationException("The selected record has no exact direct host Xref graph node.");

        // Use existing graph data. No Xref reload or resolve operation is invoked.
        return new XrefState(direct.XrefStatus.ToString(), direct.Database?.Filename, record.IsFromOverlayReference);
    }

    private static FileStream OpenTarget(string host, string root, string path)
    {
        var target = ResolveLocalPath(host, root, path);
        var stream = new FileStream(target, FileMode.Open, FileAccess.Read, FileShare.Read);
        try
        {
            VerifyTarget(host, root, path, stream);
            if (stream.Length <= 0 || stream.Length > MaxXrefBytes)
                throw new InvalidOperationException("Use a proposed local DWG of at most 32 MiB.");
            Span<byte> signature = stackalloc byte[6];
            stream.ReadExactly(signature);
            if (signature[0] != (byte)'A' || signature[1] != (byte)'C'
                || signature[2] < '0' || signature[2] > '9'
                || signature[3] < '0' || signature[3] > '9'
                || signature[4] < '0' || signature[4] > '9'
                || signature[5] < '0' || signature[5] > '9')
                throw new InvalidOperationException("The proposed target does not have a DWG signature.");
            stream.Position = 0;
            return stream;
        }
        catch
        {
            stream.Dispose();
            throw;
        }
    }

    private static void VerifyTarget(string host, string root, string path, FileStream stream)
    {
        var expected = ResolveLocalPath(host, root, path);
        VerifyPhysicalHandle(stream.SafeFileHandle, expected, directory: false);
    }

    internal static void VerifyPhysicalHandle(SafeFileHandle handle, string expected, bool directory)
    {
        var buffer = new StringBuilder(1024);
        var length = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity
            || !buffer.ToString().Equals(@"\\?\" + expected, StringComparison.Ordinal)
            || !GetFileInformationByHandle(handle, out var info)
            || (info.Attributes & FileAttributes.ReparsePoint) != 0
            || ((info.Attributes & FileAttributes.Directory) != 0) != directory
            || (!directory && info.LinkCount != 1))
            throw new InvalidOperationException("The opened handle could not be bound to the exact local file or directory.");
    }

    private sealed class DirectoryHandles : IDisposable
    {
        private readonly List<SafeFileHandle> handles = new();

        internal void Add(string directory)
        {
            // Open the directory itself and withhold write/delete sharing through the trial.
            var handle = OpenDirectory(directory, FileReadAttributes, ShareRead, IntPtr.Zero, OpenExisting,
                BackupSemantics | OpenReparsePoint, IntPtr.Zero);
            handles.Add(handle);
            if (handle.IsInvalid)
                throw new InvalidOperationException("The Xref path directory could not be held for the trial.");
            VerifyPhysicalHandle(handle, directory, directory: true);
        }

        public void Dispose()
        {
            foreach (var handle in handles)
                handle.Dispose();
        }
    }

    private static DirectoryHandles HoldDirectories(string host, string root, string relative)
    {
        var paths = new HashSet<string>(StringComparer.Ordinal);
        ResolveLocalPath(host, root, relative, paths);
        var handles = new DirectoryHandles();
        try
        {
            foreach (var path in paths)
                handles.Add(path);
            return handles;
        }
        catch
        {
            handles.Dispose();
            throw;
        }
    }

    private static string ResolveLocalPath(string host, string root, string relative,
        HashSet<string>? directories = null)
    {
        if (string.IsNullOrWhiteSpace(root) || root.Length > 512 || !Path.IsPathFullyQualified(root)
            || !Path.IsPathFullyQualified(host) || root.StartsWith(@"\\", StringComparison.Ordinal)
            || host.StartsWith(@"\\", StringComparison.Ordinal)
            || string.IsNullOrWhiteSpace(relative) || relative.Length > 512
            || Path.IsPathRooted(relative) || relative.Contains(':'))
            throw new InvalidOperationException("Use a local physical root and a host-relative DWG path.");

        var physicalRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        var current = Path.GetDirectoryName(Path.GetFullPath(host))!;
        if (new DriveInfo(Path.GetPathRoot(physicalRoot)!).DriveType != DriveType.Fixed || !Within(current, physicalRoot))
            throw new InvalidOperationException("The saved host must be within the reviewed local root.");

        for (DirectoryInfo? directory = new DirectoryInfo(current); directory is not null; directory = directory.Parent)
        {
            RejectReparsePoint(directory.FullName);
            if (directory.Parent is not null)
                directories?.Add(directory.FullName);
        }

        var parts = relative.Split(new[] { '\\', '/' });
        if (parts.Length > 32 || !Path.GetExtension(parts[^1]).Equals(".dwg", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Use a bounded relative path to a DWG file.");
        for (var index = 0; index < parts.Length; index++)
        {
            var part = parts[index];
            if (part != ".." && part != "." && !SafePart(part))
                throw new InvalidOperationException("The relative path contains an unsupported Windows path component.");
            current = Path.GetFullPath(Path.Combine(current, part));
            if (!Within(current, physicalRoot))
                throw new InvalidOperationException("The relative path escapes the reviewed local root.");
            RejectReparsePoint(current);
            if (index < parts.Length - 1 && !Path.EndsInDirectorySeparator(current))
            {
                if (!Directory.Exists(current))
                    throw new InvalidOperationException("The relative Xref path has a nondirectory component.");
                directories?.Add(current);
            }
        }

        if (current.Equals(Path.GetFullPath(host), StringComparison.OrdinalIgnoreCase) || !File.Exists(current))
            throw new InvalidOperationException("The proposed target must be an existing DWG distinct from the host.");
        return current;
    }

    private static bool Within(string path, string root) =>
        path == root || path.StartsWith(Path.EndsInDirectorySeparator(root) ? root : root + Path.DirectorySeparatorChar,
            StringComparison.Ordinal);

    private static void RejectReparsePoint(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException("The host or proposed Xref path contains a reparse point.");
    }

    private static bool SafePart(string part)
    {
        if (part.Length == 0 || part.EndsWith(' ') || part.EndsWith('.')
            || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
            return false;
        foreach (var character in part)
            if (character < ' ' || character > '~')
                return false;

        var stem = part.Split('.')[0].ToUpperInvariant();
        return stem is not ("CON" or "PRN" or "AUX" or "NUL")
            && !(stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal)
                || stem.StartsWith("LPT", StringComparison.Ordinal)) && char.IsAsciiDigit(stem[3]));
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileInformation
    {
        internal FileAttributes Attributes;
        internal uint CreationLow, CreationHigh, AccessLow, AccessHigh, WriteLow, WriteHigh;
        internal uint Volume, SizeHigh, SizeLow, LinkCount, IndexHigh, IndexLow;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle OpenDirectory(string path, uint access, uint share,
        IntPtr security, uint creation, uint flags, IntPtr template);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInformation information);

    [DllImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle file, StringBuilder path, uint capacity, uint flags);
}
