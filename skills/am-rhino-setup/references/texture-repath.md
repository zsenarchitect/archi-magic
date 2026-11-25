# Recipe: move Rhino textures to a project folder and repath (#292)

Use this recipe when the user asks to "repath Rhino textures", to collect the texture images of a `.3dm` into one folder, or when Rhino shows its temporary unpack folder as the texture path. Read `rhino-gotchas.md` first. Tested on Rhino 8 only.

## Pitfalls

1. **A normal save embeds the images again.** On open, Rhino unpacks the embedded copy and repoints the texture to its temporary folder. `FileWriteOptions.IncludeBitmapTable = False` did not help. Save with `-_SaveAs SaveTextures=No` instead.
2. **`open_doc` with `clearFirst` keeps materials.** It deletes objects only. A repath then also touches the previous file's materials and copies their images into the wrong folder.
3. **A new Rhino can open a crash-recovery file.** Then the document is not empty.
4. **Long repath calls hit the 300 second timeout.** The script keeps running, Rhino can crash, and all work in memory is lost.

## Steps

1. Use one new, empty Rhino session for each model file. Before `open_doc`, check that `doc.Objects.Count` and `doc.Materials.Count` are both 0. Do not rely on `clearFirst`. If the document is not empty, ask the user to start a new empty model.
2. Use one texture folder for each model file, for example `<project textures>\<model name>\`. Image names such as `Unnamed-0-3.png` repeat across files, so a shared folder can keep the wrong image.
3. List the current texture paths: run `rhino_texture_check.py` (am-rhino-model-health) with `INPUTS = {"folder": "<new folder>", "max_items": 10000}`. Every current path is in `outside_folder`. Stop if `name_clashes` is not empty, and ask the user.
4. Copy the files outside Rhino, with PowerShell, in small batches. When a file with the same name exists in the target, compare the sizes. Same size: keep it. Different size: stop and ask. Write each copied file to a progress file, so a rerun skips it.
5. Change the texture paths in Rhino in batches of less than about 2 minutes each. Write progress to a file (see "After a timeout" in `rhino-gotchas.md`).
6. Save a NEW file next to the original with `run_command`: `-_SaveAs SaveTextures=No "<full path of the new file>"`. Never overwrite the original file.
7. Check the saved file: run `rhino_texture_check.py` with `INPUTS = {"path": "<new file>", "folder": "<new folder>"}`. `ok` must be true: 0 paths outside the folder, 0 missing files, 0 embedded files. `embedded_files` is `null` when this Rhino cannot count them; then say so.
8. Report the counts for each file.

Not verified: whether `SaveTextures=No` works the same in Rhino 9.
