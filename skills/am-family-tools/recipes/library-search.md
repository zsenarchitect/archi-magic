# Recipe: search a local family library

Example request: "Find a single flush door, 900 mm wide, in our family library."

This recipe finds families without Revit. Each `.rfa` file holds a short summary that Revit writes when it saves the family. The summary is called `PartAtom`. It gives the category, the Revit version, the type names and the type parameter values. Each `.rfa` file also holds a small thumbnail image. A type catalog is a `.txt` file next to the `.rfa` file with the same name. It lists more types. The index reads all three. It does not open Revit.

Files (paths relative to the am-family-tools skill folder):

| Step | File | Where it runs |
|---|---|---|
| 2 Index | `scripts/family_library.py index` | Terminal, CPython 3 |
| 3 Search | `scripts/family_library.py search` | Terminal, CPython 3 |
| 4 Model check (optional) | `scripts/read_loaded_families.py` | Revit, `run_query` (read only) |
| 4 Mark results | `scripts/family_library.py search --in-model` | Terminal, CPython 3 |

## Step 1: ask

Ask only what the request leaves open. Use AskUserQuestion.

1. **Which folder is the library?** First read the saved office library location: `family_library` in `%LOCALAPPDATA%\archi-magic\office-paths.json` (see "Office files" in `am-welcome`). If it is saved, offer it as the first option. Otherwise ask for the folder path. Do not guess, and do not search a network drive for it. A local folder or a synced folder (OneDrive, Autodesk Desktop Connector) is fine.
2. **Where does the index go?** Recommend a private work folder outside the library, for example `%LOCALAPPDATA%\archi-magic\family-index\<library name>\`. Never write into the library folder.

## Step 2: index

```powershell
python "<skill dir>\scripts\family_library.py" index "<library folder>" --out "<work>\index.json" --thumbnails "<work>\thumbs"
```

- Add `--previous "<work>\index.json"` to refresh an index. Unchanged files keep their earlier rows. Only new or changed files are read again.
- Exit code 0 means the index is complete. Exit code 1 means the index is partial. Exit code 2 means the command could not run.
- Show the printed summary to the user.

Skip reasons in a partial index:

| Reason | Meaning | What to do |
|---|---|---|
| `cloud_only` | The file is online only. Reading it would start a download. | Make the folder available offline (for example "Always keep on this device"), then index again. |
| `link_not_followed` | The item is a shortcut link or a junction. | Index the target folder on its own if the user needs it. |
| `too_deep`, limit `entries` or `families` | The folder is very deep or very large. | Index a subfolder. |
| `unreadable_file`, `unreadable_folder`, `loop` | Windows refused access, or a folder repeats. | Tell the user. Check the access rights. |

Each family row also has a status:

- `ok`: the summary was read.
- `no_metadata`: the file has no summary (some old families). Search finds it by file name only.
- `unreadable`: the file is damaged or is not a family file. Search hides it unless you add `--include-unreadable`.

A file name like `Door.0001.rfa` is a Revit backup. Search hides backups unless you add `--include-backups`.

## Step 3: search

```powershell
python "<skill dir>\scripts\family_library.py" search "<work>\index.json" --text "single flush" --category door --param "Width=900"
```

| Option | Matches |
|---|---|
| `--text "words"` | All words, in the family name, the folder or the type name |
| `--category name` | Part of the Revit category, for example `door` |
| `--type text` | Part of the type name |
| `--param "Name=Value"` | A type parameter with that value. Use `--param Name` to find types that have the parameter. |
| `--limit 50` | Number of rows (max 500) |
| `--out results.json` | Saves all columns, with the full path and thumbnail file |

How values match:

- Matching ignores upper and lower case and extra spaces.
- Numbers match by value: `900` matches `900.0` and `900 mm`.
- The search does not convert units. The index keeps the value text and the unit that the family stores (see `units` in `results.json`). Search with the unit of the library. A fraction such as `1/2"` matches as text only.

Show the printed table in chat. To show a thumbnail, read its PNG file from `results.json` with the Read tool.

- "(same name elsewhere)" means two files have the same family name. Show both folders. Ask the user which file they mean.
- `type_origin` `catalog` means the type comes from the type catalog. In Revit, the user picks it in the Specify Types dialog when they load the family.

## Step 4 (optional): check the open model

1. Call `get_context` first. Follow the data rule before the first model read.
2. Run `scripts/read_loaded_families.py` with `run_query`. It is read only. Optional inputs: `{"max_families": 3000, "max_types": 20000}`.
3. Save the full result as `loaded.json`. Use `get_run` if the result is long. If `result.error` is `no_project_document`, ask the user to open a project model, not a family.
4. Search again with `--in-model "<work>\loaded.json"`. The "In model" column shows `type loaded`, `family loaded, type not`, or `no`.

The match uses the family name. A family with the same name in the model can be a different version of the file. Compare the category (`model_category` in `results.json`) and tell the user what you did not compare.

## Step 5: hand off

This recipe does not load or place families.

- Give the user the full path and the type name of the chosen row.
- If the type is already in the model, tell the user to place the existing type. Do not load the family again. A second load can replace the project family and its type values.
- If the family is not in the model, tell the user to load it in Revit: **Insert > Load Family**, then select the file. For a catalog type, select the type in the Specify Types dialog.
- To place or change elements after the load, use `am-safe-change`.

## Limits

- The index shows the values that Revit wrote in the file summary at the last save. A formula result or an instance value can differ in the model.
- The index reads two small parts of each file with bounded reads. It does not read the rest of the file. It does not check file hashes.
- Tested on synthetic files and on the sample families that install with Revit 2024 and 2026. Not tested on very large office libraries or on Autodesk Docs folders.
