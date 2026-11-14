using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json;

namespace ArchiMagic.AutoCAD;

internal sealed record EditRow(string Kind, ulong Handle, string NativeClass, string Before, string? After,
    ulong? OwnerHandle, string? Tag, string? ApprovedRoot, ulong[] OwnedHandles);

internal sealed record EditRequest(Guid DrawingFingerprint, Guid DrawingVersion, string FileSha256,
    string RequestSha256, EditRow[] Edits)
{
    internal const int MaxBytes = 64 * 1024;
    private const int MaxRows = 100;

    internal static EditRequest Parse(ReadOnlyMemory<byte> bytes)
    {
        if (bytes.Length == 0 || bytes.Length > MaxBytes)
            throw new InvalidOperationException("The edit request must be 1 to 65,536 bytes.");
        using var json = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 8 });
        var data = Fields(json.RootElement);
        ExactFields(data, "drawing_fingerprint", "drawing_version", "file_sha256", "edits");
        var fingerprint = DrawingId(Text(data, "drawing_fingerprint", 40));
        var version = DrawingId(Text(data, "drawing_version", 40));
        var fileHash = Text(data, "file_sha256", 64);
        if (fileHash.Length != 64 || fileHash.Any(character => !Uri.IsHexDigit(character)))
            throw new InvalidOperationException("Use the exact 64-character drawing SHA-256.");

        var edits = data["edits"];
        if (edits.ValueKind != JsonValueKind.Array || edits.GetArrayLength() is < 1 or > MaxRows)
            throw new InvalidOperationException("Supply 1 to 100 edit rows.");

        var rows = edits.EnumerateArray().Select(ParseRow).ToArray();
        var targets = new HashSet<ulong>();
        foreach (var row in rows)
        {
            if (!targets.Add(row.Handle) || row.OwnedHandles.Any(handle => !targets.Add(handle)))
                throw new InvalidOperationException("The request repeats an edit or owned purge handle.");
        }

        var renameNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var row in rows)
        {
            if (row.Kind == "rename_layer" && (!renameNames.Add(row.Before) || !renameNames.Add(row.After!)))
                throw new InvalidOperationException("Layer rename names collide across the batch.");
            if (row.Kind == "map_entity"
                && (targets.Contains(Handle(row.Before)) || targets.Contains(Handle(row.After!))))
                throw new InvalidOperationException("An entity mapping depends on a layer edited in the same batch.");
            if (row.OwnerHandle is ulong owner && targets.Contains(owner))
                throw new InvalidOperationException("An attribute depends on an insert edited in the same batch.");
        }

        return new EditRequest(fingerprint, version, fileHash.ToUpperInvariant(),
            Convert.ToHexString(SHA256.HashData(bytes.Span)), rows);
    }

    private static EditRow ParseRow(JsonElement json)
    {
        var data = Fields(json);
        var kind = Text(data, "kind", 24);
        var expected = new List<string> { "kind", "handle", "native_class", "before" };
        if (kind is "rename_layer" or "map_entity" or "repath_xref" or "fill_attribute")
            expected.Add("after");
        else if (kind is not ("purge_layer" or "purge_block" or "purge_dim_style" or "erase_empty_text"))
            throw new InvalidOperationException("The request contains an unsupported edit kind.");
        if (kind == "fill_attribute")
            expected.AddRange(new[] { "owner_handle", "tag" });
        if (kind == "repath_xref")
            expected.Add("approved_root");
        if (kind == "purge_block")
            expected.Add("owned_handles");
        ExactFields(data, expected.ToArray());

        var handle = Handle(Text(data, "handle", 16));
        var nativeClass = Text(data, "native_class", 80);
        var before = Text(data, "before", 4096, allowEmpty: kind is "fill_attribute" or "erase_empty_text");
        var after = data.ContainsKey("after") ? Text(data, "after", 1024) : null;
        if (kind == "map_entity" && Handle(before) == Handle(after!))
            throw new InvalidOperationException("The source and destination layer handles must differ.");
        if (kind == "rename_layer" && (before.Length > 255 || after!.Length > 255
            || before.Equals(after, StringComparison.OrdinalIgnoreCase)))
            throw new InvalidOperationException("Use distinct bounded layer names.");
        if (kind == "fill_attribute" && (!LiteralAttribute(after!) || before == after))
            throw new InvalidOperationException("Supply a changed, printable literal attribute value.");
        if (kind == "repath_xref" && (before == after || after!.Contains(':') || after[0] is '\\' or '/'))
            throw new InvalidOperationException("Supply a changed host-relative Xref path.");
        if (kind == "erase_empty_text" && before.Length != 0)
            throw new InvalidOperationException("An empty text erase requires the exact empty value.");

        ulong[] owned = Array.Empty<ulong>();
        if (kind == "purge_block")
        {
            var handles = data["owned_handles"];
            if (handles.ValueKind != JsonValueKind.Array || handles.GetArrayLength() != 2)
                throw new InvalidOperationException("An empty block purge must review its two boundary handles.");
            owned = handles.EnumerateArray().Select(item => item.ValueKind == JsonValueKind.String
                ? Handle(item.GetString()!) : throw new InvalidOperationException("Use hexadecimal handle strings.")).ToArray();
        }

        return new EditRow(kind, handle, nativeClass, before, after,
            kind == "fill_attribute" ? Handle(Text(data, "owner_handle", 16)) : null,
            kind == "fill_attribute" ? Text(data, "tag", 255) : null,
            kind == "repath_xref" ? Text(data, "approved_root", 512) : null, owned);
    }

    internal static bool LiteralAttribute(string text) => !string.IsNullOrWhiteSpace(text) && text.Length <= 255
        && text.All(character => character is >= ' ' and <= '~' && character is not ('%' or '\\' or '{' or '}'));

    internal static ulong Handle(string text)
    {
        if (text.Length == 0 || text.Length > 16 || text[0] == '0'
            || !ulong.TryParse(text, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out var handle)
            || text != handle.ToString("X", CultureInfo.InvariantCulture))
            throw new InvalidOperationException("Use a canonical nonzero uppercase 64-bit hexadecimal handle.");
        return handle;
    }

    private static Guid DrawingId(string text) => Guid.TryParse(text, out var value) && value != Guid.Empty
        ? value : throw new InvalidOperationException("The request has an invalid drawing identity.");

    private static Dictionary<string, JsonElement> Fields(JsonElement element)
    {
        if (element.ValueKind != JsonValueKind.Object)
            throw new InvalidOperationException("Use a JSON object for the request and every edit row.");
        var result = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
            if (!result.TryAdd(property.Name, property.Value))
                throw new InvalidOperationException("The request repeats a JSON property.");
        return result;
    }

    private static void ExactFields(Dictionary<string, JsonElement> data, params string[] expected)
    {
        if (data.Count != expected.Length || expected.Any(name => !data.ContainsKey(name)))
            throw new InvalidOperationException("The request or edit row has missing or unsupported fields.");
    }

    private static string Text(Dictionary<string, JsonElement> data, string name, int max, bool allowEmpty = false)
    {
        if (!data.TryGetValue(name, out var element) || element.ValueKind != JsonValueKind.String)
            throw new InvalidOperationException("The request field " + name + " must be text.");
        var text = element.GetString()!;
        if (text.Length > max || (!allowEmpty && string.IsNullOrWhiteSpace(text)) || text.Any(char.IsControl))
            throw new InvalidOperationException("The request field " + name + " is blank, too long or contains controls.");
        return text;
    }
}
