# Design

## Direction
Quiet, exact, compact, dark. Essence: "quiet · exact · instrument-panel". Controls read like a control on a test rig. A game-QA operator picks the model that drives the bot, at start or mid-run, and must see the current model and the default model at a glance.

## Tokens
All tokens live in the `:root` block of `ashad/static/app.css`.

| Token | Value | Role |
|---|---|---|
| `color-scheme` | `dark` | Dark only |
| `--bg` | `#141414` | App background |
| `--bg-sidebar` | `#1a1a1a` | Sidebar background |
| `--surface` | `#212121` | Base surface |
| `--surface-2` | `#2a2a2a` | Raised surface, popovers |
| `--surface-3` | `#333333` | Hover, focus and pill background |
| `--border` | `#2e2e2e` | Hairline borders |
| `--text` | `#ececec` | Primary text |
| `--text-2` | `#a8a8a8` | Secondary text |
| `--text-3` | `#949494` | Tertiary text |
| `--accent` | `#7aa2ff` | Focus rings and primary actions |
| `--accent-ink` | `#0d1424` | Text on accent fills |
| `--live` | `#4ade80` | Live or running status |
| `--attention` | `#fbbf24` | Needs attention |
| `--danger` | `#f87171` | Errors and destructive actions |
| `--muted` | `#7d7d7d` | Disabled and muted text |
| `--font` | `"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif` | UI font |
| `--mono` | `"Cascadia Mono", Consolas, ui-monospace, monospace` | Code and log font |
| `--fs` | `13px` | Body size |
| `--fs-meta` | `12px` | Meta text |
| `--fs-title` | `15px` | Titles |
| `--fs-hero` | `24px` | Hero heading |
| `--r-row` | `6px` | Row radius |
| `--r-card` | `10px` | Card radius |
| `--r-big` | `14px` | Large panel radius |
| `--titlebar` | `40px` | Title bar height |
| `--sidebar-w` | `260px` | Sidebar width |
| `--ease` | `120ms ease-out` | Standard transition |

## Rules
- Dark only.
- Every colour comes from `:root`.
- The accent is only for focus and primary actions.
- Use one popover shadow.
- Use the radii `--r-row`, `--r-card`, `--r-big` and the 999px chip/pill.
- 4px spacing base.
- Use the global `:focus-visible` ring.
- No hover lift, gradients or glow.

## Signature move
The selected row's quiet check plus the muted "Default" pill. Nothing else is decorative.

## Components
Model menu: a chip trigger (sliders icon, label, chevron) opens a native popover listbox anchored under it, flipping above if there is no room. Rows show the label, a muted "Default" pill on the default model and a quiet check on the selected one. Keyboard: opening focuses the selected row, ArrowUp and ArrowDown clamp at the ends, Home and End jump, Enter and Space pick, and Escape closes and returns focus to the trigger.
Get controls meter: one `<progress class="progress">` with real `batches_done / batch_count`, the stage line and the stall note; no other decoration.
Instance issues: plain hairline rows at the top of the Actions tab (type in words, title linking to the issue view, counts, status in words, file path in mono). Hits sit in a nested details list showing time, source and kept/counted in words. "View in project Issues" opens the board filtered to the instance. No colour-coded severity.
Issues board: one page per project, with four plain groups in fixed order (Unsupported actions, Crashes, Game bugs, Planner rejections), each a subheading with its count and a hairline list. Rows: title link, status in words, occurrence count, last source instance (+N), first and last seen. Closed rows use --text-2. Empty groups say so; loading, error and Refresh follow the Usage page. An instance filter is a removable chip. No cards, tiles or colour-coded type.
Issue information: one pure component (issueInfoHTML) rendering, in order, what happened, available actions, recent decisions, screenshot (an image or the reason in words), game state, crash log, bug report or rejected proposal, and evidence paths in mono. Sections are hairline-separated with section-title headings; JSON sits in details/pre. Absent sections are omitted; all game text is escaped.
Goals tab: primary goal in words ("Judged by you, not by ASHAD." and the plan revision), one "Working on now" block with a 2px --live left rule (objective, completion, "n of m so far", why, ancestor path), a "Needs review" note in words when planning paused the run, the goal tree with status words (Now, Pending, Completed, Blocked, Not needed, Primary goal) and measured progress, and a hairline "Plan history" list (time, sentence, Activity chip); under Why, the operator's current instruction in words; Plan history also lists hand-back and resume instructions with an Operator chip. The --live rule is the tab's one signature; no breadcrumb chevrons, badges or colour-coded status.

## Changelog
- 2026-10-11: DESIGN.md created from app.css :root; model menu added (feat/model-menu).
- 2026-10-11: setup step 4 'Get controls' batch meter; overview 'Project configurations'; instance Blocked banner.
- 2026-10-11: project configurations can no longer be deleted; the instance Blocked banner is removed and retired rows offer 'Get controls again…' (fix/remove-config-delete).
- 2026-10-11: instance Saves section and Save now (feat/instance-save-load).
- 2026-10-11: instance Issues section with per-hit save links (feat/instance-issues).
- 2026-10-11: Fixes chip, Fixing/Needs you status and Improvement loop section (feat/fixer-loop).
- 2026-10-11: Fixes chip becomes the shared popover menu; unready reasons are visible in the menu (fix/fixes-menu).
- 2026-10-11: fixer removed: no Fixes chip, Fixing/Needs you status or Improvement loop section; the model menu is the only popover listbox again (chore/remove-fixer).
- 2026-10-11: save/load removed: no Save now, Saves section or per-hit Load (chore/remove-save-load).
- 2026-10-11: project Issues board, issue view and Issue information component (feat/project-issues-board).
- 2026-10-12: Issues board gains Planner rejections; issue view shows the rejected proposal (feat/planner-rejected-issues).
- 2026-10-12: Goals tab shows the primary goal, Working on now, review notes and plan history; "Needs your acceptance" and the Currently chasing breadcrumb removed (feat/goals-tab).
- 2026-10-12: Hand back with an instruction; Goals tab shows the operator's instruction and lists it in Plan history (fix/handback-instruction-planner).
