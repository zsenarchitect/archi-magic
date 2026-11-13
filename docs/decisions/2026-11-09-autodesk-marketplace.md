# Autodesk Marketplace listing decision for Archi Magic

**Decision** · Defer the Autodesk Marketplace listing for the current package
**Decision owner** · `zsenarchitect`, repository owner
**Decision status** · Confirmed by the repository owner on 2026-11-13
**Repository baseline** · `921f04327f9a4fb0f2c71f605066edaa7059f1a0`

Keep the current GitHub and Claude Code distribution. Revisit an Autodesk listing after an accountable publisher, a supported Revit package, and real Revit acceptance evidence are available. This decision covers whether to submit the current package. [Issue #177 and owner direction](https://github.com/zsenarchitect/archi-magic/issues/177)

## Evidence

| Area | Current evidence | Condition for a go decision |
|---|---|---|
| Publisher and support | GitHub lists `zsenarchitect` as the repository owner. The project documents GitHub feedback. The Autodesk publisher account, support contact, and update responsibility still need confirmation. | Name the submitting account, support contact, and maintainer responsible for compatibility and update triage. |
| Product and installation | The current package installs as Claude Code skills and configures the upstream pyRevit Agent Runtime. The setup guide lists Revit 2024–2026. Autodesk's current Revit plug-in guide calls for Revit 2027 compatibility, a ready-to-run install, and a ribbon entry point. | Confirm the correct submission category and installation contract with Autodesk. Qualify the chosen package and uninstall path on the required Revit version. |
| IP and distribution | The repository has an MIT license and separate third-party notices. The Claude Code plugin names additional dependencies. The current license files establish terms for the files they cover. | Inventory the exact installer contents, notices, external dependencies, and assets. Confirm redistribution rights for each item before bundling. |
| MCP and data | The configured MCP server comes from pyRevit. The skill list describes Archi Magic workflows. Autodesk requires an MCP manifest for the actual tools, resources, prompts, endpoints, APIs, and AI providers, plus a publisher declaration. | Pin the distributed runtime and inspect its exposed tools and data flow. Verify tool permissions, external destinations and the consent path against the declaration. |
| Evidence for users | The issue owner asks for genuine Revit recordings of preview, dry run, and Keep or Discard, separated from illustrations. | Record the shortlisted workflows on disposable projects. Show the before and after for kept changes and confirm that Discard preserves the baseline. |

[Repository setup](https://github.com/zsenarchitect/archi-magic/blob/921f04327f9a4fb0f2c71f605066edaa7059f1a0/README.md) · [Autodesk Revit publisher guidelines](https://aps.autodesk.com/marketplace/publisher-center/revit-publisher-guidelines) · [Autodesk MCP publisher guide](https://aps.autodesk.com/marketplace/mcp-publisher-guide)

## Shortlist for a future listing

1. **Check worksets.** Report model organization problems before a handoff.
2. **Check naming standards.** Show which names violate the project rules and what a proposed correction would change.
3. **Review a fill or texture change.** Demonstrate preview, dry run, and Keep or Discard on a disposable model.

These candidates reflect the owner's architecture QA and standards direction. Verify the installed commands before describing them as one-click actions.
