# Rhino gotchas (rhino-mcp tools and RhinoCommon scripts)

Read this page before you model or script in Rhino 8 through the rhino-mcp tools or the RhinoCode fallback. Each item cost a real session time. Issues: #289, #290, #292, #293, #306.

A **slot** is one Rhino window that the rhino-mcp router controls. `spawn_slot` starts one. `list_slots` lists them.

## Slots (#293)

- Run `list_slots` before the first Rhino call in a session. Run it again after every `spawn_slot`.
- Use the slot id that `list_slots` shows. Do not keep the name that `spawn_slot` returned. The router can rename a slot later (same pid, same port, new name).
- The error `rhino_not_installed: the default Rhino couldn't be auto-spawned` after the slot worked before usually means a stale slot name. Run `list_slots` and use the current id. Do not reinstall anything.
- With more than one slot, pass `slot` on every call. Check the document name with `get_context` before a change.

## After a timeout or a dropped call (#293, #292)

The tool timeout is 300 seconds. A `TaskCanceledException ... 300 seconds` error does not stop Rhino. The script can still run, finish, or fail later.

1. Do not send a second `run_python` to the same slot at once. A second call on a busy slot can crash it (`rhino_crashed`).
2. Read the job's own progress file from Claude's side (Read tool or PowerShell). Do not use a Rhino call for this check.
3. If the script wrote no progress file, wait. Then call `get_context` (read-only) to see if the slot answers.
4. Count the result with a read-only script before you run the change again.

The same applies to "permission stream closed": the call may or may not have run. Never run a change again before a read-only check shows that it did not run.

To prevent this:
- Keep each call under about 2 minutes. Split long work into batches.
- Let each script write a progress file: "started", one line per finished item, then "done" or "failed" with the traceback. `scripts/rhinocode_job.py` shows the pattern.
- Make batch scripts resumable: skip the items that the progress file lists as done.
- Copy large sets of files outside Rhino (PowerShell), not inside `run_python`.

## open_doc: units, tolerance and leftovers (#289, #290, #292)

- `open_doc` into a new slot can keep the new document's default unit (millimeters). A file in feet then comes in scaled by 304.8. The absolute tolerance can also differ from the source file.
- Before you trust any size, compare the document with the source file:

```python
import Rhino
doc = __rhino_doc__
src = Rhino.FileIO.File3dm.Read(r"<full path of the source .3dm>")
s = src.Settings
print("AM_RESULT " + repr({
    "source": [str(s.ModelUnitSystem), s.ModelAbsoluteTolerance, s.ModelAngleToleranceRadians],
    "document": [str(doc.ModelUnitSystem), doc.ModelAbsoluteTolerance, doc.ModelAngleToleranceRadians]}))
src.Dispose()
```

- If the units differ and the document held nothing but the imported file, scale it back: `doc.AdjustModelUnitSystem(<source unit>, True)`. Then set `doc.ModelAbsoluteTolerance` and `doc.ModelAngleToleranceRadians` to the source values. Compare one bounding box with the source again.
- If the document held other geometry before the import, do not scale. Ask the user.
- Not verified: setting the empty document to the source units first (`doc.AdjustModelUnitSystem(<unit>, False)`) and then importing avoids the scale step.
- `clearFirst` deletes objects only. Materials (and their textures) from the previous file stay in the document. Before an import, check `doc.Objects.Count` and `doc.Materials.Count`. Both must be 0 for a clean start.
- A new Rhino can open an old crash-recovery file ("Recovered model"). Then it is not empty. Ask the user to close that model and start a new empty one.
- After each save of a new file, read the saved file with `File3dm.Read` and check its units and absolute tolerance again. Report both files' values.

## Viewport pictures (#289, #306)

- A large `get_viewport_image` result goes over the tool-result limit (seen: 900 x 560 gave about 63,000 characters). The harness then saves it to a file. Keep inline captures small (for example 600 x 400 or less).
- For a larger or a repeatable picture, capture to a PNG file inside `run_python` (code below). Then open the PNG with the Read tool. This avoids the size limit.
- A capture right after a layer visibility change can come back almost blank. Call `doc.Views.Redraw()` before the capture. If the image is still almost empty, capture again once.
- `ViewCapture` after `SetCameraLocations(target, camera)` gave mirrored pictures (text read backwards) with "up" in the wrong direction. Set the camera in steps instead, and set `CameraUp` yourself.
- Every picture goes to the AI provider. Use it only where the project allows it.

Check by picture: save the view, set a fixed camera for each option, capture, then restore the view, the display mode and the layer visibility. The user's view does not change.

```python
import Rhino
import System.Drawing
G = Rhino.Geometry
doc = __rhino_doc__
view = doc.Views.ActiveView
vp = view.ActiveViewport
out_path = r"<full path>.png"
bbox = G.BoundingBox.Empty
for o in doc.Objects:          # or only the option's objects
    bbox.Union(o.Geometry.GetBoundingBox(True))
old_mode = vp.DisplayMode
vp.PushViewProjection()
try:
    direction = G.Vector3d(1, 1, -0.6)
    direction.Unitize()
    target = bbox.Center
    camera = target - direction * bbox.Diagonal.Length
    side = G.Vector3d.CrossProduct(direction, G.Vector3d.ZAxis)
    up = G.Vector3d.CrossProduct(side, direction)   # (direction x Z) x direction; use Y for a plan view
    vp.SetCameraLocation(camera, False)
    vp.SetCameraDirection(direction, False)
    vp.CameraUp = up
    vp.SetCameraTarget(target, False)
    vp.ZoomBoundingBox(bbox)
    doc.Views.Redraw()
    cap = Rhino.Display.ViewCapture()
    cap.Width, cap.Height = 1200, 800
    cap.DrawGrid = False
    cap.DrawAxes = False
    bmp = cap.CaptureToBitmap(view)
    bmp.Save(out_path, System.Drawing.Imaging.ImageFormat.Png)
finally:
    vp.DisplayMode = old_mode
    vp.PopViewProjection()
    doc.Views.Redraw()
print("AM_RESULT " + out_path)
```

Not verified: `DisplayModeDescription.FindByName("Shaded")` did not seem to change the mode. The picture used the viewport's own mode.

## RhinoCommon gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `for o in doc.Objects` skips objects | Objects on hidden layers are not listed. Old design options usually sit on hidden layers. | Use `ObjectEnumeratorSettings` with `HiddenObjects = True` (and `LockedObjects = True`). Add `LayerIndexFilter` for one layer. Pass it to `doc.Objects.GetObjectList(settings)`. |
| `Brep.CreateBooleanUnion` returns None | The solids share faces exactly, or touch only along a tangent. | Move or offset the new solid slightly into the host: seen 0.3 ft for a fill against walls, 0.1 mm for a torus on a letter. Check that the result is one solid. |
| Thin faces (5 to 30 sq ft) hang past a facade | Blocks were drawn from rounded coordinates. | Clip each new footprint to a contour section of the existing solid before you extrude it. Then run `rhino_massing_check.py` (am-rhino-model-health). |
| `Brep.CreateFilletEdges(..., BlendType.Chamfer, ...)` returns None or several pieces on some planar outlines | Not found. It worked only at a smaller distance. | For each loop, use `Brep.CreateFromTaperedExtrude(curve, d, ZAxis, start, +/-45 deg, ExtrudeCornerType.Sharp, tol, angleTol)`. For the outer loop, pick the sign whose top loop area is smaller. For a hole, pick the larger one. Add the wall extrusion and the planar top and bottom faces, then `Brep.JoinBreps`. |
| A ruled surface between a loop and its offset is twisted | The curve seams do not match. | Do not use `NurbsSurface.CreateRuledSurface` for facets. Use the tapered extrude above. Check by picture. |
| `ViewCapture` picture is mirrored, "up" points the wrong way | `SetCameraLocations(target, camera)` leaves `CameraUp` wrong. | Set location, direction, `CameraUp` = (direction x Z) x direction, target, then `ZoomBoundingBox` (code above). |
| `print()` output never arrives (RhinoCode fallback) | RhinoCode does not return script output. | Write the result and the traceback to files, then poll (`scripts/rhinocode_job.py`). |
| `open_doc` geometry is 304.8 times too large | The document kept millimeters. | See "open_doc: units, tolerance and leftovers". |

Candidate pattern, seen to work once (#306): `Material.ToPhysicallyBased()`, then set `PhysicallyBased.Opacity`, `OpacityIOR`, `Metallic` and `Roughness`, then `RenderMaterial.CreateBasicMaterial(m, doc)`. Assign it through `ObjectAttributes.RenderMaterial` with `MaterialSource = MaterialFromObject`.

## One change, one Undo

Rhino has no Keep/Discard approval. Ask the user to save the file first. Put one change in each call, so one Undo in Rhino removes it. In a script, wrap the change in `doc.BeginUndoRecord("<name>")` and `doc.EndUndoRecord(<serial>)`.
