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
Saves list: plain hairline rows in the Actions tab (time, trigger in words, game-save state in words, secondary Load). Load is never the accent; a disabled Load always shows its reason as text.

## Changelog
- 2026-10-11: DESIGN.md created from app.css :root; model menu added (feat/model-menu).
- 2026-10-11: setup step 4 'Get controls' batch meter; overview 'Project configurations'; instance Blocked banner.
- 2026-10-11: project configurations can no longer be deleted; the instance Blocked banner is removed and retired rows offer 'Get controls again…' (fix/remove-config-delete).
- 2026-10-11: instance Saves section and Save now (feat/instance-save-load).
