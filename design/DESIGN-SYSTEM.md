# archi-magic design system: "Visual Research"

Every archi-magic page uses this system: the 3D viewer, the schedule and markup tables, the change preview, and the model-health dashboard. It comes from a monochrome poster study: a soft beam of light, film grain, 1 px construction geometry, and small monospace type. It reads like an architect's drawing, not like software.

The tokens live in `design/tokens.css`. That file is the only source. `scripts/sync-design-tokens.ps1` copies it into each page template between the `am-tokens:start` and `am-tokens:end` markers, and `check-skills.ps1` fails if any copy drifts.

## Principles

1. **Value, not hue.** Use only black, white and grays (`--v0` to `--v7`). Do not use brand color, and do not encode meaning with color.
2. **One focal dot.** Each view has at most one solid dot (`.am-dot`). It marks the one thing that matters most: the selected element, the current item, or a blocking problem.
3. **Light is the material, except behind the model.** Page chrome (headers, hero bands, empty states) uses a soft vertical beam (`--beam`) inside a vignette (`--vignette`), with fine grain (`.am-surface::after`). **The 3D viewport is the exception.** It is a flat, very clean space: `--bg` with no beam, glare, highlight, vignette or grain over the canvas, so the model is the only thing that carries light. Do not use drop shadows anywhere.
4. **Construction lines, not boxes.** Draw structure with 1 px hairlines (`--line`). Use circles, axes and tangents for empty states, loaders and dividers. Rectangles have square corners. Round things are fully round.
5. **Monospace only, quiet hierarchy.** Use one family (IBM Plex Mono). Rank by tone: primary in `--ink`, secondary in `--ink-2`. Use size only for the single lead line. Labels are lowercase. Section headers are uppercase micro text, tracked (`.am-micro`), with an index such as `13 / 30` (`.am-index`).
6. **Axis and air.** Compose along a centered vertical axis where the layout allows it. Leave generous empty space. Put fewer things on screen.
7. **Slow and calm motion.** Use fades and eases of about 420 ms (`--slow`, `--ease`). Do not bounce.

## Named exceptions

- **Pet mode (am-bim-viewer).** The walking companion cat is drawn in color (orange body with green and cream marks), with pink hearts when petted and a rounded thought cloud. This is a deliberate exception to principle 1 and to the "rounded cards" don't, decided by the maintainer (PR #45, 2026-10-28).
- The exception holds because the cat is a companion character, not data. Its color is its identity. It encodes no state, status, or category. Everything that carries meaning (status, categories, selection, changes) stays in monochrome value steps.
- The exception covers only the pet and its effects (speech bubble, thought cloud, zzz, hearts) in the viewer. It does not extend to page chrome, tables, or model categories.

## Tokens (summary)

| Token | Value | Use |
|---|---|---|
| `--v0` / `--v7` | `#0a0a0a` / `#f4f4f1` | ink / paper |
| `--ink`, `--ink-2`, `--ink-3` | theme-dependent | primary, secondary, tertiary text |
| `--line`, `--line-strong` | ink at 14% / 42% | hairlines, construction geometry |
| `--beam`, `--vignette` | radial gradients | the light column and the dark edge |
| `--grain`, `--grain-opacity` | SVG luminance noise, 0.16 (paper) / 0.22 (night), overlay blend | texture over the whole page |
| `--font` | IBM Plex Mono 300/400/500 | all text |
| `--fs-micro`, `--fs-body`, `--fs-lead` | 10.5 / 12.5 / 14 px | label, body, the one lead line |
| `--radius`, `--round` | 0 / 999px | square rectangles, fully round dots and rings |

Themes follow the **time of day** (`design/theme.js`, copied into every page between the `// am-theme` markers):
- **paper** (day) runs from 07:00 to 19:00 local time, and **night** (`.am-night`) runs at all other hours. The page checks every minute and switches by itself.
- A manual switch (`amTheme.toggle()` or `amTheme.set('night'|'paper'|'auto')`) holds until the next 07:00 or 19:00. After that the page is automatic again.
- On each switch the page sends an `am-theme-change` event. The 3D viewer listens for it and recolors its scene.
- Without JavaScript, pages follow the OS light/dark setting through the CSS tokens.
- `design/tests/theme.test.js` tests the boundaries, the override and its expiry. CI runs it.

## Functional states without color

| State | Mark |
|---|---|
| ok | hollow ring (`.am-status`) |
| warning | half-filled ring (`.am-status.warn`) |
| problem / blocked | solid dot (`.am-status.bad`); a blocking banner is shown inverted (ink background, paper text) |
| selected (3D) | the element turns pure paper on the night scene with an ink hairline outline; one dot marks its label |
| changed (preview) | old value in `--ink-3` with a hairline strike, new value in `--ink` |
| hover | the hairline goes from `--line` to `--line-strong` |

3D categories use value steps (`--v2` to `--v6`), not hue. A category is told apart by its value and its name in the legend.

## Do and don't

- Do: square tables with hairline rules, a micro uppercase header with an index (`schedules  04 / 40`), and lowercase labels.
- Do: an empty state drawn as two tangent hairline circles with a dot where they meet, plus one lowercase line ("click an element / to see its data").
- Don't: colored chips, colored status pills, `border-left` accent bars, gradients with hue, rounded cards, shadows, emoji.

## Logo

Files are in `design/logo/`. `index.html` shows them all.

| File | Use |
|---|---|
| `archi-magic-mark.svg` | The mark on paper: two tangent circles and the focal dot at the point where they meet. |
| `archi-magic-mark-paper.svg` | The same mark for night backgrounds. |
| `archi-magic-icon.svg` | The app icon tile: the mark on the light beam with vignette and grain. |
| `archi-magic-mark-small.svg`, `-small-paper.svg` | 32 px and below (favicon, pane, badge). It shows one circle and the dot, like an "o." Below 48 px the two circles of the full mark run together and its dot disappears. |

Rules:
- Use only these files. Do not recolor them (they use ink and paper only), stretch them, or add effects.
- Give the mark clear space of at least half its width on every side.
- Use the full mark only at 48 px and above. Below that, use the small mark.

## Accessibility

- Text contrast (measured with the WCAG formula): `--ink` on paper is 17.97:1. `--ink-2` on paper is 4.85:1 and on night 7.01:1, so both pass AA for body text. `--ink-3` is 2.56:1 on paper and 3.70:1 on night, which is below AA. Use `--ink-3` only for supplementary marks that repeat information shown elsewhere (units, the struck-out old value next to the new value, counts in chips). Never use it for text the user must read.
- Status is never shown by tone alone. Each status has a shape (ring, half, dot) and a text label.
- Grain is luminance noise blended with `overlay`, at 0.16 opacity on paper and 0.22 on night. Overlay barely changes flat ink or flat paper, so text contrast stays as measured. Grain has `pointer-events: none`.
