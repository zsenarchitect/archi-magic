# Rhino MCP kit for Claude Code (no admin rights)

This kit prepares the current Windows user for McNeel's official **rhino-mcp** Claude Code plugin:
1. **Checks** that Rhino 8 and Node.js are installed. The plugin uses Node.js to start McNeel's MCP router.
2. **Installs the Rhino package** `Rhino-MCP-Platform` per-user with Yak, Rhino's package manager. No admin rights needed.
3. **Adds ask rules.** Claude Code must ask you before every Rhino tool that can change a file or the model.

You install the plugin itself in Claude Code: `/plugin install rhino-mcp@archi-magic`. The archi-magic marketplace lists it on demand, so it is not installed with archi-magic.

Source: https://github.com/mcneel/RhinoAI (MIT, folder `cc-plugin`). Docs: https://mcneel.github.io/RhinoAI/docs/

## Option A: let Claude Code do it

With the archi-magic plugin installed, ask Claude Code to "set up Rhino". The `am-rhino-setup` skill runs the script and walks you through each step.

## Option B: run the script yourself

1. Run `powershell -NoProfile -ExecutionPolicy Bypass -File rhino-setup.ps1`.
2. In Claude Code, type `/plugin install rhino-mcp@archi-magic`.
3. In Rhino 8, type `SetDotNetRuntime`. It must say **NETCore**.
4. Restart the Claude desktop app. In a **new** Code session, ask: *"use the rhino tools to list the objects in my open Rhino document"*.

If Yak cannot install the package, install it in Rhino: type `PackageManager`, search for **Rhino-MCP-Platform**, click Install, and restart Rhino.

If Node.js is missing, download it from https://nodejs.org/en/download. If the installer asks for admin rights, ask your IT team.

## Tools that ask first

Claude Code asks before these tools run. The rules cover both names: `mcp__plugin_rhino-mcp_rhino__<tool>` (plugin) and `mcp__rhino__<tool>` (manual setup, for example with Rhino's `MCPConnect` command).

`run_python`, `run_csharp`, `run_command`, `open_doc`, `save_doc`, `close_doc`, `set_layer_material`, `manage_plugin_commands`, and the Grasshopper tools that change the canvas: `g1_apply_graph`, `g1_clear_canvas`, `g1_delete_component`, `g1_connect`, `g1_connect_many`, `g1_place_component`, `g1_place_slider`, `g1_solve_graph`, `g1_start`, and the same `g2_` tools (`g2_solve_canvas` in place of `g1_solve_graph`).

Ask rules prompt in every permission mode, also in auto mode. Tools that only read or move the view (for example `list_objects`, `get_context`, `get_viewport_image`, `set_camera`) run without a prompt.

## Good to know

- **Approvals:** Rhino has no Keep/Discard step. Read each request. Use Undo in Rhino to reverse a change. Save your file first.
- **Data:** anything Claude reads from the model, and every viewport image, goes to the AI provider. Use it only on projects where that is allowed.
- **Several Rhino windows:** Claude can edit a different document than the one you look at. Keep one Rhino open. If no Rhino is open, a tool call can start one.
- **Rhino 9 WIP/BETA:** the router uses Rhino 8 by default. See McNeel's troubleshooting page to change it.
- **Logs and backups:** each run writes a log under `%TEMP%\rhino-setup\<timestamp>\`. Before it changes `settings.json`, the script writes a backup next to it: `settings.json.am-rhino-backup-<timestamp>`. The script rewrites the file in a standard format. The content stays the same, plus the new rules.
- **Undo:** in Claude Code, type `/plugin uninstall rhino-mcp@archi-magic`. Remove the `mcp__plugin_rhino-mcp_rhino__*` and `mcp__rhino__*` lines from `permissions.ask` in `%USERPROFILE%\.claude\settings.json`. Run `"C:\Program Files\Rhino 8\System\Yak.exe" uninstall Rhino-MCP-Platform`.

## Script switches

`rhino-setup.ps1 [-Install] [-AskRules] [-Verify] [-NoAsk] [-Force] [-RhinoVersion 8|9] [-SettingsPath <file>]`. With no switch, it runs Install, AskRules and Verify. `-NoAsk` turns on do-not-ask mode: it removes the Rhino ask rules and adds allow rules for the two Rhino server names. Use it only when you ask for it. A later default run keeps this choice. `-AskRules` turns the prompts back on. Long rule lists print as a count and the first 3 names; the full list is in a text file in the log folder. `-Force` runs `yak install` again. `-SettingsPath` changes the settings file (default `%USERPROFILE%\.claude\settings.json`). `-Verify` only reads.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Rhino 8 (or the selected version) is not installed |
| 3 | Rhino package install failed |
| 5 | settings.json not changed (invalid JSON, unexpected shape, or `-NoAsk` together with `-AskRules`) |
| 6 | Verify found problems. Normal on a machine that is not set up yet. Each problem is one WARNING line. |
| 7 | Node.js not found |
