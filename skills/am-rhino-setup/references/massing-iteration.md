# Recipe: next massing option in a new Rhino file (#290)

Use this recipe when the user asks to "make the next massing option", "make opt N.1 from opt N", or to change tower heights or arm angles in a new iteration file. Read `rhino-gotchas.md` first.

## 1. Ask the key questions once

Ask in one AskUserQuestion round (max 4 questions). Let the user type a free answer for each, because answers like "flip the current stepping" or "opt1 already meets the rule" do not fit fixed options.

1. Base option: which file and which option layer.
2. What to keep (cores, podium, footprint).
3. What to change: heights, flip the stepping, rotate an arm, or another change.
4. File name and folder of the new file. Propose `<base file name>-<yyyy-mm-dd>.3dm` in the same folder.

Do not ask for facts you can read from the model (layer names, units, current heights).

## 2. Make the new file

1. Ask the user to save the base file.
2. Read the source file settings with `File3dm.Read`: units, absolute tolerance, angle tolerance (code in `rhino-gotchas.md`).
3. Copy the base file to the new name with PowerShell `Copy-Item`. Never overwrite a file. If the name exists, stop and ask.
4. Open the copy with `open_doc`. Run `list_slots` first, and use the slot id it shows.
5. Compare the document units and tolerances with the source values. Fix them if they differ. Compare one bounding box with the source.

## 3. Name the new option

- The version subnumber goes up: `opt1` -> `opt1.1`, `opt2.1` -> `opt2.2`. A new option never reuses an old option name.
- Get the name from `rhino_massing_check.py` (am-rhino-model-health) with `INPUTS = {"base_option": "<base leaf name>"}`. It reads every layer, also hidden layers, and returns `next_option_name`.
- Copy the base option's objects to the new layer. Hide the old option layers. Never delete them.
- To read objects on hidden layers, use `ObjectEnumeratorSettings` with `HiddenObjects = True` and `LayerIndexFilter`.

## 4. Edit

- Put one change in each `run_python` call, inside one undo record, so one Undo in Rhino removes it.
- Before a Boolean union, move the fill solid slightly into the host walls (seen: 0.3 ft). Solids that share faces exactly make `CreateBooleanUnion` return None.
- Clip each new footprint to a contour section of the existing solid before you extrude it. Blocks from rounded coordinates otherwise hang past the facade as thin faces.
- Check that each Boolean returns exactly one solid.

## 5. Check

1. Run `rhino_massing_check.py` with `INPUTS = {"layers": ["<new option layer>"], "base_layers": ["<base option layer>"], "sliver_area": <small area>}`.
2. Report `NOT_SOLID`, `SLIVER_FACES` and `DOWNWARD_FACES`, and the volume change against the base option.
3. Check by picture with a fixed camera (code in `rhino-gotchas.md`). Restore the view after.

## 6. Save and verify

1. Save the new file with `save_doc`.
2. Read the saved file with `File3dm.Read`. Its units and absolute tolerance must equal the source file's values.
3. Report both files' units and tolerances, the new layer names, and the check results.
