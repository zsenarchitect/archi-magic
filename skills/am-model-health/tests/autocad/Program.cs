using System;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using ArchiMagic.AutoCAD;

const string drawing = """
    "drawing_fingerprint":"aabbccdd-0000-0000-0000-000000000001",
    "drawing_version":"aabbccdd-0000-0000-0000-000000000002",
    "file_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """;
const string rename = """{"kind":"rename_layer","handle":"FFFFFFFFFFFFFFFF","native_class":"LAYER","before":"Old","after":"New"}""";
const string map = """{"kind":"map_entity","handle":"B","native_class":"LINE","before":"A","after":"C"}""";
const string attribute = """{"kind":"fill_attribute","handle":"D","native_class":"ATTRIB","before":"","after":"A-101","owner_handle":"E","tag":"ROOM"}""";
const string block = """{"kind":"purge_block","handle":"F","native_class":"BLOCK_RECORD","before":"Unused","owned_handles":["11","12"]}""";
const string xref = """{"kind":"repath_xref","handle":"14","native_class":"BLOCK_RECORD","before":"Old.dwg","after":"Refs/New.dwg","approved_root":"C:\\Fixtures"}""";
const string emptyText = """{"kind":"erase_empty_text","handle":"15","native_class":"TEXT","before":""}""";

static string Request(params string[] rows) => "{" + drawing + ",\"edits\":[" + string.Join(",", rows) + "]}";
var valid = Encoding.UTF8.GetBytes(Request(rename, map, attribute, block, xref, emptyText));
var parsed = EditRequest.Parse(valid);
if (parsed.Edits.Length != 6 || parsed.Edits[0].Handle != ulong.MaxValue
    || parsed.RequestSha256 != Convert.ToHexString(SHA256.HashData(valid))
    || parsed.Edits[2].OwnerHandle != 0xE || !parsed.Edits[3].OwnedHandles.SequenceEqual(new ulong[] { 0x11, 0x12 }))
    throw new Exception("Valid mixed request lost reviewed identity or content.");

var invalid = new (string Name, string Json)[]
{
    ("invalid JSON", "{"),
    ("duplicate top-level field", Request(rename).Replace("\"edits\"", "\"edits\":[],\"edits\"")),
    ("duplicate row field", Request(rename.Replace("\"after\"", "\"after\":\"other\",\"after\""))),
    ("unknown field", Request(rename.Replace("\"before\"", "\"surprise\":true,\"before\""))),
    ("unknown kind", Request(rename.Replace("rename_layer", "run_command"))),
    ("duplicate edit target", Request(rename, rename)),
    ("noncanonical handle", Request(rename.Replace("FFFFFFFFFFFFFFFF", "0a"))),
    ("rename swap", Request(rename, rename.Replace("FFFFFFFFFFFFFFFF", "A").Replace("\"Old\",\"after\":\"New\"", "\"New\",\"after\":\"old\""))),
    ("layer dependency", Request(rename.Replace("FFFFFFFFFFFFFFFF", "A"), map)),
    ("owner dependency", Request(rename.Replace("FFFFFFFFFFFFFFFF", "E"), attribute)),
    ("owned purge dependency", Request(rename.Replace("FFFFFFFFFFFFFFFF", "11"), block)),
    ("attribute field marker", Request(attribute.Replace("A-101", "%<unsafe>%"))),
    ("missing attribute tag", Request(attribute.Replace(",\"tag\":\"ROOM\"", ""))),
    ("rooted proposed path", Request(xref.Replace("Refs/New.dwg", "C:/New.dwg"))),
    ("nonempty text", Request(emptyText.Replace("\"\"", "\" \""))),
    ("zero rows", Request()),
    ("too many rows", Request(Enumerable.Repeat(rename, 101).ToArray())),
    ("oversize bytes", Request(rename).PadRight(EditRequest.MaxBytes + 1)),
};
foreach (var test in invalid)
{
    try
    {
        EditRequest.Parse(Encoding.UTF8.GetBytes(test.Json));
    }
    catch (Exception error) when (error is InvalidOperationException or JsonException)
    {
        continue;
    }
    throw new Exception("Accepted invalid request: " + test.Name);
}
Console.WriteLine($"Passed one valid mixed request and {invalid.Length} invalid request cases.");
