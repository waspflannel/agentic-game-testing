# Frontend spec: harness redesign

Status: draft, 2026-10-07. Replaces the whole renderer in `ashad/static/`. Supersedes the "reuse the current renderer" rule in [refactor plan §10](electron-workspace-refactor-plan.md#10-frontend-refactor-boundaries) and the layout parts of the [app guide](ui.md). The rules in those docs that guard behavior still apply (escaping, camera epochs, render guards, draft protection, bounded history). Nothing here changes the API. Every screen maps to an existing route in the [API contract](api-contract.md).

## 1. Goal

ASHAD should look and feel like a coding-agent harness (Claude Code desktop, Codex, DeepSeek Harness), applied to game agents:

- A **project tree with threads** on the left.
- A **single focused workspace** in the centre. The bot's computer replaces the chat transcript, and the composer becomes the instance launcher.
- A **collapsible inspector** on the right.
- **Quiet chrome.** Dark graphite surfaces, one accent colour, dense type, almost no borders or boxes.

None of the current UI is kept: no markup, no CSS and no visual patterns.

## 2. Principles

1. **The live computer is the hero.** On an instance, the camera gets the most pixels and sits above the fold.
2. **Show what was recorded, never invent it.** The timeline and inspector show recorded events and observations. There is no "thinking" text, no guessed explanations and no implied success. When something is unknown, say so and label it.
3. **One action, one place.** Each control appears once, in a predictable spot. Destructive actions ask for confirmation.
4. **Polling is invisible.** Background refresh never moves focus, resets scroll, collapses a section, clears a draft or restarts the camera.
5. **Quiet by default, loud on trouble.** Neutral colours for normal state. Colour and motion are reserved for Live, attention-needed and error.

## 3. Stack and files

- Plain HTML, CSS and JavaScript ES modules (`<script type="module">`). No framework, no build step, no runtime dependencies, no CDN. This satisfies the existing CSP (`script-src 'self'`) and keeps packaging as is.
- `.js` is served as `text/javascript` (set explicitly in `ashad/app.py`), because Windows registry entries can map it to `text/plain`, which browsers refuse for module scripts.
- UI memory (last project, expanded groups, inspector open state) lives in renderer memory. The Electron app partition is memory-only, so browser storage would not outlive the window anyway.
- Icons: one inline SVG `<symbol>` sprite in `index.html`. Paths copied from Lucide (ISC licence), 16px, 1.5 stroke.
- Native elements wherever they fit: `<dialog>` for modals and the narrow-width drawers (free focus trap and Esc), `<details>` for disclosures, `<select>` and `<input>` for forms, `<a href="#/…">` for navigation, `<button>` for actions.

Files (the complete set; add more only when one gets unwieldy):

| File | Owns |
|---|---|
| `ashad/static/index.html` | Shell landmarks, icon sprite, module entry |
| `ashad/static/app.css` | Tokens, layout, every component style |
| `ashad/static/app.js` | Entry: shell wiring, routing, bootstrap polling, banners, toasts, dialogs, dirty-draft bridge |
| `ashad/static/core.js` | Pure helpers with no DOM at import: `esc`, API client, route parsing, render epochs, `patchHTML`, run status. Node tests import it directly |
| `ashad/static/views/sidebar.js` | Sidebar model and markup (pure, tested) |
| `ashad/static/views/home.js` | Empty workspace and the new-instance composer |
| `ashad/static/views/project.js` | Project overview |
| `ashad/static/views/setup.js` | Setup flow |
| `ashad/static/views/instance.js` | Thread header, camera, control bar |
| `ashad/static/views/timeline.js` | Activity timeline: kinds, bounded window, cards |
| `ashad/static/views/inspector.js` | Right inspector |
| `ashad/static/views/settings.js` | Settings |
| `ashad/static/views/usage.js` | Usage page (split from `settings.js` in C9) |

C1 (§16) deletes `style.css`, `workspace.css`, `workspace.js`, `instance-workspace.css`, `instance-workspace.js` and the old `app.js`. Keep `favicon.svg` (or redraw it to match §5).

## 4. Window and shell layout

The Electron main window uses `titleBarStyle: 'hidden'` and a `titleBarOverlay` (colour `--bg-sidebar`, symbols `--text-2`, height 40). Windows draws its own minimize, maximize and close controls. The renderer draws a 40px drag strip (`-webkit-app-region: drag`) holding the sidebar toggle and the window title. Interactive elements inside the strip opt out with `no-drag`. Minimum window size stays 800×600.

```
┌──────────────┬───────────────────────────────────────────┬──────────────┐
│ ≡  ASHAD     │  (drag strip)                     [⊟ ⊞ ✕] │              │
├──────────────┼───────────────────────────────────────────┤              │
│ [+ New inst.]│ Goal text…        ● Running   agent·model ⧉│  INSPECTOR   │
│              ├───────────────────────────────────────────┤  (340px,     │
│ PROJECTS   + │                                           │  collapsible)│
│ ▾ Agronauts  │            LIVE COMPUTER (16:9)           │              │
│   ● Reach the│                                           │ ▸ World state│
│     Craft a… │                                           │ ▸ Activity   │
│   + New inst.├───────────────────────────────────────────┤ ▸ Tokens     │
│ ▸ Other game │ [⏸ Pause] [✋ Take over]        [■ Stop]  │ ▸ Goal       │
│              ├───────────────────────────────────────────┤              │
│              │ ACTIVITY                                  │              │
│              │ ◦ Observed  inventory 4 · 12 entities  3s │              │
│ ● Live · 12m │ ◦ Selected  interact(door_3)         2s   │              │
│ Usage        │ ✓ Outcome   delivered · effect confirmed  │              │
│ Settings     │ [Load older activity]                     │              │
└──────────────┴───────────────────────────────────────────┴──────────────┘
   260px              fluid (min 480)
```

Landmarks: `nav` (sidebar), `main` (centre), `aside` (inspector), plus a skip link to `main`.

## 5. Visual language

Dark only for v1 (`color-scheme: dark`). Light theme is out of scope. All colours live as custom properties on `:root`.

| Token | Value | Use |
|---|---|---|
| `--bg` | `#141414` | Centre workspace |
| `--bg-sidebar` | `#1a1a1a` | Sidebar, title strip, inspector |
| `--surface` | `#212121` | Composer, cards, control bar |
| `--surface-2` | `#2a2a2a` | Hover, selected row, input fill |
| `--border` | `#2e2e2e` | Hairlines only (1px); prefer spacing over borders |
| `--text` | `#ececec` | Primary text |
| `--text-2` | `#a8a8a8` | Secondary, meta |
| `--text-3` | `#949494` | Tertiary; ≥4.5:1 on every surface up to `--surface-2` (C10 raised it from `#8a8a8a`, which was 4.2:1 on `--surface-2`) |
| `--accent` | `#7aa2ff` | Primary button, focus ring, links, active row marker |
| `--live` | `#4ade80` | Running / live camera |
| `--attention` | `#fbbf24` | Paused, investigating, stale, human control |
| `--danger` | `#f87171` | Stop, errors, failed outcomes |
| `--muted` | `#7d7d7d` | Ended, offline, unknown (always paired with a text label); ≥3:1 on `--surface-3` (C10 raised it from `#6b6b6b`, which was 2.7:1 on a selected row) |

- **Type:** `"Segoe UI Variable Text", "Segoe UI", system-ui` at a 13px base and 1.45 line height. Meta text 12px. Section headings 12px/600, sentence case, `--text-2`. Thread title 15px/600. Hero 24px/600. Mono (`"Cascadia Mono", Consolas, monospace`, 12px) for IDs, raw evidence and action calls.
- **Spacing:** 4px grid. Radii are 6px for rows and buttons, 10px for cards, 14px for the composer and camera frame.
- **Buttons:** primary is filled `--accent` with dark text. Secondary is a `--surface-2` fill. Ghost is text only with a hover fill. Danger is a ghost with `--danger` text, turning to a `--danger` fill only inside the confirm dialog. Icon-only buttons always have `aria-label` and a tooltip (`title`).
- **Status dot and badge:** an 8px dot plus a text label, always both. Only Running pulses (opacity 1→0.5, 1.6s). The pulse is disabled under `prefers-reduced-motion`.
- **Focus:** `:focus-visible` draws a 2px `--accent` outline with a 2px offset on every interactive element. Never `outline: none` without a replacement.
- **Motion:** 120ms ease-out for hover and disclosure. No layout animation during polling.

## 6. Routes

Hash routes, rendered into `main`. Unknown routes go to `#/`.

| Route | View |
|---|---|
| `#/` | No projects: empty workspace. Otherwise: composer for the last-opened project (in-memory route memory, falling back to the first project) |
| `#/p/<project>` | Project overview |
| `#/p/<project>/new[?agent=<id>]` | New-instance composer |
| `#/setup[?project=<id>&step=<n>]` | Setup flow |
| `#/i/<run>` | Instance thread (live or ended) |
| `#/usage` | Usage |
| `#/settings[/<section>]` | Settings |
| Legacy `#/sessions`, `#/agents`, `#/new/<agent>` | Redirect to `#/`, `#/`, and that agent's project composer (or `#/` when it has no project) |

## 7. Left sidebar

From top to bottom:

1. **Title strip** with the sidebar toggle (Ctrl+B) and the "ASHAD" wordmark.
2. **New instance** (full-width secondary button). Opens the composer for the current or last project. With no projects it opens setup.
3. **Projects** heading with a `+` button (`aria-label="Set up new game agent"`) that opens `#/setup`.
4. **Project tree.** Each project is a disclosure row (chevron, folder icon, name, then a hover-only `⋯` and `+`). Expanded projects list their instance threads directly underneath, newest first:
   - Row content: status dot, goal text (one line, ellipsis, full text in `title`), relative age (`3m`, `5h`, `2d`) on the right.
   - Selected thread gets a `--surface-2` fill and `aria-current="page"`.
   - The last row is `+ New instance` (ghost, `--text-2`).
   - Show 10 threads, then "Show all (N)" inline.
   - Expanded state is kept per project for the session.
5. **Unassigned** group at the bottom of the tree, only when runs without a project exist.
6. **Active-instance indicator**, pinned above Usage. Shown only when `instance_slot.owners` is non-empty and the current route isn't that thread. It's a compact row: dot, state, truncated goal, elapsed time, linking to `#/i/<run>`.
7. **Usage** and **Settings** links. The Settings row shows a small `--attention` dot when ChatGPT sign-in is not `ready`.

Data comes from the `GET /api/bootstrap` poll (`projects`, `runs`, `agents`, `instance_slot`, `ai`). The sidebar re-renders by stable ID, so expanded state, hover and focus survive polls.

## 8. Screens

### 8.1 Empty workspace (no projects)

A centred column (max 560px):

- **Heading:** "Set up your first game agent".
- **One line of copy:** ASHAD prepares a desktop, tunes instructions from your build, then runs one bot against a goal you give it.
- **Prerequisites checklist,** each with a live status and a fix link:
  - ChatGPT signed in (`ai.status === 'ready'`, otherwise → Settings › Account)
  - At least one worker desktop registered (`workers.length`, otherwise → Settings › Desktops)
  - A build manifest or folder on this computer (informational, checked in setup)
- **Primary button:** **Set up new game agent** (→ `#/setup`). It's enabled even when prerequisites are missing, because setup re-checks each step.

### 8.2 New-instance composer (`#/p/<project>/new`)

The harness "home" screen: hero text vertically centred, composer below it.

- **Hero:** "What should the agent do in **<Project>**?"
- **Context chips** above the composer:
  - Project chip (dropdown to switch project).
  - Readiness chip: `● Desktop ready`, `▲ Not ready: <reason>` (links to the project overview), or `● Instance running`, which links to the owning thread when the slot is taken.
- **Composer card** (`--surface`, 14px radius, max 760px):
  - Goal `<textarea>`: auto-grow, 3–10 rows, placeholder "Describe the goal, e.g. finish the tutorial and craft a stone pickaxe". Required, trimmed.
  - Bottom row, left: **Agent** selector chip (saved configurations for this project, showing name and model) and **Transport** chip (ENet or Steam). Steam is disabled with its reason as visible helper text, not a tooltip only.
  - Bottom row, right: read-only summary `GPT-6 Astra · build <name> · Fresh world`, then **Start** (round primary button, arrow icon, `aria-label="Start instance"`).
  - Shortcut: Ctrl+Enter starts. Plain Enter inserts a newline, because launching is costly and must stay explicit. Hint text "Ctrl+Enter to start" sits under the card.
- **Start behaviour:**
  - The `request_id` is created once when the draft is first edited and reused on retry.
  - `POST /api/instances`. On a network error or timeout, `GET /api/instances/requests/<id>`: `accepted` navigates to the thread, `in_progress` keeps showing Starting, `prepared` or `unknown` shows "Not started" with a Retry button.
  - On a 409 `active_instance_conflict`, keep the draft and link the owning instance.
  - Field errors show under the field and focus the first one.
- **Disabled Start always says why,** inline, under the card: no agent configuration, provider not ready, desktop not ready, or slot taken.
- **No agent configurations yet:** the composer is replaced by "This project has no agent configuration yet" with a primary **Add agent configuration** button.

### 8.3 Project overview (`#/p/<project>`)

A centred column (max 960px). The header shows the project name, with **New instance** (primary) and **Add agent configuration** (secondary) on the right. Sections follow as quiet cards in a two-column grid that drops to one column under 1100px:

- **Builds:**
  - Each row shows name, pinned source revision (mono, truncated; the manifest SHA-256 in its tooltip) and validation state from the last readiness check of that build this session ("Not checked yet" otherwise). Builds have no separate version field.
  - `+ Add manifest…` and `+ Add build folder…` run `ashadDesktop.chooseBuild('manifest'|'folder')`, then `POST /api/builds`. A cancelled chooser changes nothing. A chooser result applies only to the form that opened it, and a newly added build becomes the one readiness checks.
- **Agent configurations:**
  - Each row shows name, model, build and tuned age. Actions: **View instructions** (read-only dialog), **Retune** (confirmed; new `request_id`; tuning writes a new configuration with the same name and keeps the old one, so the list shows both, newest first), **New instance** with this agent. Retune shares setup's waiting and `tuning/<id>` recovery.
- **Desktop readiness:**
  - Per-build checklist from `GET /api/projects/<id>/readiness?build_id=`: provider, build validation, each environment (ready/reason), slot, Steam. With several builds a build picker chooses which one is checked; choosing one probes it.
  - This call probes, so it runs on view open and on an explicit **Re-check** button, never on the poll. The result shows its own "checked 2m ago".
  - **Prepare desktop** opens a confirm dialog that states it creates or reuses the container, images, volumes and credentials and never starts a game. It then calls `POST …/environments/prepare` with `confirmed:true` and a stable `request_id`.
- **Recent instances:** the last 10 threads (same row component as the sidebar, plus end state and duration), then "View all N in sidebar", which expands that project's group with Show all and focuses it.

### 8.4 Setup flow (`#/setup`)

A centred column (max 720px) with a horizontal stepper on top (numbered, current step `aria-current="step"`, completed steps clickable to review). Each step is one card with a Back and Continue pair. Continue is disabled with its reason shown. Each step's draft is kept in memory, and `ashadDesktop.setDirtyDraft(true)` is set while unsaved input exists.

| # | Step | Calls | Done when |
|---|---|---|---|
| 1 | **Project:** name (or pick existing via `?project=`) | `POST /api/projects {name, request_id}` | Project exists |
| 2 | **Build:** choose manifest or folder, show catalog summary | `chooseBuild`, `POST /api/builds`, `GET /api/builds/<id>/catalog` | Build registered and validated |
| 3 | **Compatibility and desktop:** readiness checklist, Prepare desktop | `GET …/readiness`, `POST …/environments/prepare` | `build_validation.ready` and one environment ready |
| 4 | **Agent and model:** configuration name, model `<select>` from `model_options`, sign-in gate with a link to Settings › Account | none | Valid name, `ai.status === 'ready'` |
| 5 | **Tuning:** indeterminate progress, elapsed time, "This prepares instructions, not model weights" | `POST /api/agents/tune`; recovery via `GET /api/agents/tuning/<request_id>` | `status: completed` |
| 6 | **Instruction review:** the saved instructions in a scrollable mono panel (read-only), with model, build and timestamp | none | **Start an instance** goes to the composer with `?agent=` |

- Setup is resumable. Re-entering `#/setup?project=<id>` jumps to the first incomplete step. Steps 1–2 are complete from data (project exists, project has a build). Desktop readiness is a probe, so re-entry re-probes and lands on step 3 at the earliest; a project that already has agents still lands there, because re-entry means adding another configuration. `?step=` is honoured on entry and capped at the first incomplete step. A finished setup (tuned agent) is dropped when you leave it, so the next visit starts a new configuration.
- Step 1 also links to existing projects (`#/setup?project=<id>`). Step 2 lists the project's builds as radio rows (newest preselected) next to the two chooser buttons.
- Create project, prepare and tune each keep one `request_id` until they succeed. A refusal (4xx) frees the ID for corrected input. A lost reply locks the submitted fields, so Retry can only resend the same request: project creation and preparation are idempotent per ID, and tuning recovers through `GET /api/agents/tuning/<request_id>` first.
- Only one tuning can run at a time. If another is running (the backend's "already being tuned" refusal), step 5 says so, retries the same request every 5s and offers **Stop waiting**.
- Prepare and tuning are long operations. The page stays responsive, and leaving the page doesn't cancel them. Their state lives in renderer memory per project, so it survives navigation but not a reload.

### 8.5 Instance thread (`#/i/<run>`)

The centre column is a single scroll container with three parts.

**A. Thread header** (sticky, 52px, `--bg` with a bottom hairline on scroll):
- Left: goal text (one line with ellipsis; clicking expands it to a full-text popover), then the status badge (§9).
- Right: meta chips (agent, model, build, transport), then the inspector toggle (Ctrl+I, `aria-expanded`).

**B. Live computer:**
- A 16:9 frame (14px radius, black letterbox) at the column's full width, capped at `min(64vh, …)` so the control bar stays above the fold at 1080p.
- `<img>` source is `/api/runs/<id>/players/<player>/live?epoch=<epoch>`. It's set once per (run, player, epoch). Polling never touches `src`. On an epoch change, the old `<img>` gets `src=''` and is removed before the new one is created. The image is also cleared on route change and while the document is hidden.
- Overlay pill in the top-left corner: `● Live`, `▲ Paused: bot input held (game may still simulate)`, `✋ Human control`, `Camera reconnecting… (doesn't mean the game froze)` with backoff retry, or `Camera offline`.
- One player in v1. If a run has several, show a small player switcher above the frame.

**C. Control bar** directly under the camera. It's `position: sticky` under the header, so it pins once the camera scrolls away and Stop stays reachable. Contents come from the state matrix in §9. Primary actions sit on the left, **Stop** is always the right-most button, and secondary actions go in a `⋯` menu.

**D. Activity timeline** below. See §10.

**Ended threads:** same layout. The camera frame shows a static "Instance ended · camera offline" panel with end state, reason and time. No live URL is ever requested. The control bar shows only Report, Evidence and Flag issue. Opening, reloading or polling an ended thread never resumes or launches anything.

### 8.6 Right inspector

A 340px `aside` with its own scroll. It's open by default at ≥1280px, and its open state is remembered in memory for the session. It holds four `<details>` sections. Their open state is keyed by section, survives polls, and every section starts open.

1. **Observed world state.** Rendered only from the latest retained structured observation in the run's events. The inspector never calls `GET …/context`, because that probes the game.
   - Header line: observation ID (mono, short), `observed 14s ago`, coverage badge. The age keeps ticking locally.
   - Badges, each with a text label:
     - **Fresh**: age under 30s while Running (`STALE_AFTER_S = 30`, a single tunable constant).
     - **Stale**: older than that, or "as of pause" when not Running.
     - **Partial**: coverage incomplete.
     - **Unknown**: no observation yet.
   - Groups: Player (position, vitals), Inventory and hotbar, Nearby entities (top 12 with their available actions, then "+N more"), Objectives and completed quests, UI/action state (modal, active action).
   - A missing field renders as "Not observed", never as an empty value or zero.
   - **Decision-time vs latest:** when a timeline card is selected, a toggle appears at the top of this section: `Latest | At decision #<seq>`. "At decision" shows only the observation retained for that event. If none was retained, it shows "Not retained for this decision". It never reuses the latest observation as a stand-in.
2. **Recent activity:** the last 8 lifecycle and control events (pause, resume, takeover, incidents) as one-line rows. Clicking one scrolls the timeline to it.
3. **Tokens and cost,** from `run.usage`:
   - Input, cached input, output and reasoning tokens. Cached and reasoning are shown as subsets, not added to the total.
   - Turn count.
   - **API-equivalent estimate** labelled "Estimate, not a charge. Billed through your ChatGPT subscription."
   - Unknown counts show "—" with "unknown" in the tooltip and text.
4. **Goal:**
   - **Currently chasing** (active goal and its ancestry as breadcrumbs).
   - Nested read-only checklist, each row showing a status icon and label (Pending / Active / Completed / Blocked / Not needed), title and observable completion condition.
   - Blocked rows show their reason inline.
   - Each row expands to its planning and verification evidence (links to timeline cards by seq).
   - The primary objective always reads "Needs your acceptance" until manually accepted.
   - Historical runs without a plan show "No goal plan was recorded for this instance."

### 8.7 Usage (`#/usage`)

- **Summary row:** turns, total tokens and API-equivalent estimate for the selected range (7d / 30d / all), with the same "not a charge" label.
- **Table** from `GET /api/usage`: time, purpose, model, instance (link), tokens, estimate, status.
- **Behaviour:** sortable by column, filterable by model and purpose. Rendering is capped at 200 rows with "Show more". Fetched on view open and on a Refresh button, not on the bootstrap poll.
- **Totals:** tokens are `usage.total_tokens` (or input + output); cached input and reasoning output are subsets and never added again. Rows without token counts are counted and named, not treated as zero. The estimate sums only recorded `estimated_cost_usd`; when every row in range is null (subscription billing) it reads "Not available", never `$0`.
- **Unloaded:** before the first fetch the page says Loading; a failed first fetch says "Usage is unavailable" and the summary shows "—". A failed Refresh keeps the last rows and toasts the error.

### 8.8 Settings (`#/settings/<section>`)

A secondary nav on the left (inside `main`) with the section form on the right. Every form saves explicitly with **Save**, and errors show per field.

- **Account:** ChatGPT status (`GET /api/settings/codex/status`, on view open and Refresh status), Sign in, Cancel sign-in, Sign out. Sign in and Sign out are disabled with the reason while tuning or while an instance is active (the backend's sign-in guard, mirrored). Sign in shows an **Open ChatGPT sign-in** link (`target="_blank"`, opened by the main process's allowlist) only when `auth_url` is HTTPS on `auth.openai.com` or `chatgpt.com`; any other address is refused with a message. While sign-in is pending, the open Account page re-checks status every 5s (paused while hidden). The link is held in memory and dropped as soon as status is no longer `login_pending`, on Cancel, and on Sign out. The sidebar's attention dot follows the refreshed status.
- **Desktops:**
  - Each worker row shows name, URL and last probe result and age, with **Probe**, **Edit** and **Delete**.
  - Add or edit form (`#/settings/desktops?edit=<id>` edits): the token field is write-only (`type="password"`, never prefilled, cleared after saving) and its placeholder reads "Stored. Leave blank to keep". A blank token is not sent.
  - Delete and editing are disabled with their reason while the worker is leased.
- **Steam accounts:** list, add form (ID64, App ID; 480 rejected inline as you type), and per-account Start sign-in, Finish, Verify and Delete, all disabled with the reason while the account's desktop is leased. Start sign-in shows the worker's instructions and an **Open desktop** link to its viewer. Finish also refreshes the account's readiness (`steam/status`). Verify asks for a build with the account's App ID (disabled when none is registered) and warns that it launches the game and needs the free slot.
- **Limits:** session duration, spend cap and storage reserve. Hardware readout (CPU, memory, disk free) is read-only from bootstrap. Concurrency shows "1 (fixed in this release)".
- **Maintenance:** workspace import and project migration preview/apply. Each is a guarded form with absolute-path inputs, the required confirmation checkboxes and a preview step before apply.
  - Import has no preview route, so its preview is a review dialog listing exactly what will be sent (paths, derived Codex home `<source_root>\codex`, external inventory) before **Import workspace**.
  - Migration groups are edited as JSON (`[{project_id, name, build_ids}]`), prefilled with each project's builds plus one suggested project per unassigned build. **Preview** calls the preview route; Apply needs that exact grouping (any edit invalidates the reviewed preview), no blockers, an absolute backup folder and the confirmation.

## 9. Status and control matrix

| Run state | Badge | Control bar (left → right) | `⋯` menu |
|---|---|---|---|
| Queued / Starting | `◌ Starting` (muted spinner) | **Stop** | Flag issue |
| Running | `● Running` (live, pulsing) | **Pause**, **Take over** · **Stop** | Recalculate goals, Flag issue |
| Paused | `▲ Paused` | **Resume** (split button: plain, or with a recovery instruction), **Take over** · **Stop** | Recalculate goals, Flag issue |
| Investigating | `▲ Needs review` | Same as Paused, plus a banner above the camera with `pause.reason` | Extend investigation, Recalculate goals, Flag issue |
| Human control (`run.human_control`) | `✋ You have control` | **Hand back** (split: resume, or keep paused) · **Stop** | Flag issue |
| Recalculating goals | `◌ Replanning` | **Stop** only; Resume appears when planning finishes | none |
| Stopping | `◌ Stopping` | none (shows "Waiting for teardown confirmation") | none |
| Ended, teardown unconfirmed | `▲ Teardown unconfirmed` | **Reconcile teardown** | Report, Evidence |
| Ended | `○ Ended · <reason>` | **Report**, **Evidence** | Flag issue |
| Unknown or unrecognised | `? Unknown` | **Stop** (if the slot is still owned) | none |

Rules:

- Every control click generates a `command_id` once and reuses it on retry. The button shows a pending state until the run reflects the command or the request fails. A failure shows an inline error under the bar, not just a toast.
- **Stop** opens a confirm `<dialog>` naming the instance.
- **Take over:** `POST …/takeover` opens the desktop viewer (main process). The password is shown once in the dialog with a Copy button, kept in memory only, and dropped when the dialog closes.
- **Hand back:** `POST …/takeover/finish`. Closing the viewer window never hands back.
- **Resume with instruction** uses a small inline textarea (it becomes `reason`). The UI says it is "used until the next resume".
- `terminate` is not exposed in v1 (open question, §15).

## 10. Activity timeline

A transcript-style list, newest at the bottom, like a harness conversation. Each card is a compact row: icon, kind label, one-line summary, relative time. It expands (`<details>`) to show structured fields, correlated IDs (links to related cards by seq) and a **Raw** block. The raw block is escaped mono JSON, truncated at 4KB with a "Show full (12KB)" option.

| Kind | Event types | Summary shows |
|---|---|---|
| **Observed** | `structured_decision_context`, `subgoal_planning_context` | Observation ID, coverage, entity and inventory counts |
| **Plan** | `subgoal_plan_*`, `subgoal_planning_cancelled`, `subgoals_verified`, `goal_changed`, `goal_recalculation_*` | What changed (expanded, verified, rejected and reason) |
| **Selected** | `decision_selected`, `choice` | The action call in mono, labelled "selected, not yet sent" |
| **Sent** | `structured_action_intent` | Action dispatched |
| **Outcome** | `structured_action_outcome`, `action_result` | "Input delivered" vs "effect confirmed" vs "failed: reason". These are distinct labels, never merged |
| **Tool** | `mcp_tool_result`, `structured_tool_result` | Tool name and result status |
| **Model** | `provider_request_*`, `agent_turn_*`, `inference_usage` | Collapsed by default into one "Model turn · 2.1k tokens · 4.2s" row per turn |
| **Control** | `launch_approved`, `run_paused`, `intervention`, `human_control_*`, `resume_failed`, `stopping`, `run_ended`, `teardown_*` | Who did what |
| **Problem** | `investigation_started`, `incident_flag`, `agent_task_failed`, `agent_task_cancelled`, `decision_cancelled`, any `*_failed` | `--danger` or `--attention` icon with the reason |
| **Other** | anything unmapped | Type name plus raw. Never dropped |

- Cards without attribution are labelled "Instance activity".
- **Filter chips** above the list: All · Actions · Plan · Problems · Model, plus a search box (server `search=`).
- **Window:** start from `GET /api/runs/<id>` (newest 200, render the latest 100).
  - "Load older activity" replaces the window using `before=<first seq>`.
  - "Return to latest" restores it. "N newer" shows when you're away from the latest records.
  - At most 200 cards are rendered, ever.
- **Live edge:** poll `events?after=<next>`. Auto-scroll only if the user is within 80px of the bottom. Otherwise show a floating "↓ N new" pill. Changing route or run resets scroll.
- **Selecting a card** (click, or Enter on a focused card) highlights it and drives the inspector's "At decision" toggle.

## 11. Data and polling

| What | How | When |
|---|---|---|
| Bootstrap | `GET /api/bootstrap` | Every 5s; paused while `document.hidden` |
| Open run | `GET /api/runs/<id>` | Every 2s while active, every 15s when ended, plus immediately after any control |
| New events | `GET /api/runs/<id>/events?after=` | With the run poll, only when at the live edge |
| Readiness | `GET …/readiness` | On view open and on Re-check only |
| Usage | `GET /api/usage` | On view open and on Refresh only |

- **Render guard:** each request captures `{route, runId, epoch}`. A response that no longer matches is dropped, so a late reply never paints another view.
- **Patch, don't replace:** polls update existing nodes keyed by stable IDs (project ID, run ID, event `seq`, goal ID). Forms, focused elements, open `<details>` and scroll containers are never re-created by a poll.
- **Escaping:** every server, game, model and user string goes in via `textContent` or a single `esc()` helper. No `innerHTML` with unescaped data, ever.
- **Mutations:** sent with `X-ASHAD-Request: 1` and JSON. On a 409 drain or import (`workspace_import.interrupted`), show a global banner and disable mutating controls.
- **Backend loss:** if three polls fail in a row, show a top banner "Lost connection to ASHAD. Data shown may be stale". Data stays visible but marked stale. Nothing auto-resumes.
- **Dirty drafts:** composer, setup and settings forms call `ashadDesktop.setDirtyDraft(true/false)`. In-app navigation away from a dirty form asks to confirm.

## 12. Narrow widths

| Width | Layout |
|---|---|
| ≥1280 | Sidebar, centre and inspector |
| 960–1279 | Inspector becomes a right-side drawer (modal `<dialog>`, 360px), toggled from the header. Sidebar stays |
| 800–959 | Sidebar also becomes a left drawer (toggle in the title strip). Header meta chips collapse into a `⋯` popover |

- The control bar wraps rather than scrolling horizontally, and Stop never wraps out of view (it's pinned as the last flex item, `margin-left:auto`).
- No horizontal page scroll at any width ≥800.
- Drawers close on Esc and on navigation, and return focus to their toggle.
- **Mechanics (C10):** a drawer is a `<dialog>` in `index.html` (`#drawer-left`, `#drawer-right`). Opening moves the existing `nav` or `aside` node into it and calls `showModal()`; closing moves the node back, so the inspector's mounted sections and the sidebar's state survive. Drawers sit below the 40px title strip. A click on the backdrop also closes them, and crossing a breakpoint closes any open drawer. The toggle is looked up again on close, because closing re-renders the header that holds the inspector toggle.

## 13. Accessibility

- Keyboard reaches everything in a logical order.
- Shortcuts: Ctrl+B sidebar, Ctrl+I inspector, Ctrl+Enter start (composer only). No single-key shortcuts.
- A single polite live region announces control outcomes, state changes (Running → Paused, Ended) and errors. It never announces telemetry, ticking ages or new timeline cards.
  - **Mechanics (C10):** `#announcer` in `index.html` is the only `aria-live` element. Toasts are visual and pass their text to it. View markup carries no `role="status"` or `role="alert"`; an error block is marked `data-announce` instead, and when a patch newly shows one, its text is announced once. Field errors announce their first message. A thread announces "Instance paused." and similar once per change of its §9 state, never on first render or on an unchanged poll.
- Status is never colour-only: dot plus text, icon plus text.
- Disabled buttons that matter (Start, Continue, Stop) stay focusable via `aria-disabled` and say why in adjacent text.
- Contrast: body and meta text ≥4.5:1, focus ring and status dots ≥3:1 against their background.

## 14. Security invariants (carried over, must not regress)

- No credentials, tokens or desktop passwords in `localStorage`, `sessionStorage`, URLs or the DOM after their dialog closes.
- Only same-origin requests. External links (sign-in, docs) go through main-process allowlisting. No `window.open` to arbitrary URLs.
- Camera URLs carry only the epoch, never auth.
- The renderer never calls owner-only desktop endpoints or `GET …/context`.

## 15. Open questions

1. **`terminate`:** when should the UI offer force-terminate over Stop? Proposal: only in Stopping after the run's deadline passes, behind a second confirm. Hidden until the backend semantics are confirmed.
2. **Light theme:** deferred. The tokens make it a later CSS-only addition.
3. **Multi-player runs:** v1 assumes one player per instance (current backend limit). The switcher in §8.5 is the only provision.

## 16. Delivery in chunks

The redesign ships as ten chunks, done strictly one at a time. Each chunk ends with something you can open in Electron and judge. Feedback from each review changes this spec before the next chunk starts, so later chunks build on what we learned rather than on this first draft.

### 16.1 Loop for every chunk

1. **Re-read:** the chunk's spec sections, plus any notes from earlier reviews.
2. **Build:** only the chunk's scope. Anything outside it stays a placeholder ("Coming in C6"), not a half-built screen.
3. **Check:** run the chunk's checks and the full Python and Node suites.
4. **Review:** open the desktop app and walk through the chunk's review script. Screenshots go in the review notes.
5. **Amend:** fold changes into the relevant spec sections, write a one-line note in the chunk's **Notes**, and mark it Done in the tracker.
6. **Commit:** one commit (or a small series) per chunk, so any chunk can be reverted on its own.

Rules:
- **Look and feel is settled in C1.** Tokens, type and density get tuned there. Revisiting them later is allowed but costs a pass over every finished chunk.
- **Tests come back with their behaviour.** C1 deletes the old renderer and the JS tests that only check the old DOM. Each behaviour those tests protected is re-added in the chunk that rebuilds it (listed under each chunk). No chunk is Done with a red suite.
- **Nothing is half-wired.** A control shown in a chunk calls the real API. Placeholders are visibly labelled and do nothing.

### 16.2 Tracker

| Chunk | Scope | Status |
|---|---|---|
| C1 | Shell, tokens, title bar, router, polling | Done |
| C2 | Sidebar | Done |
| C3 | Empty workspace and composer | Built, awaiting review |
| C4 | Instance thread: header, camera, controls | Built, awaiting review |
| C5 | Activity timeline | Built, awaiting review |
| C6 | Inspector | Built, awaiting review |
| C7 | Project overview | Built, awaiting review |
| C8 | Setup flow | Built, awaiting review |
| C9 | Settings and Usage | Built, awaiting review |
| C10 | Narrow widths, accessibility audit, docs | Built, awaiting review |

The order lets you use the new UI for real as early as possible. After C4 you can start an instance from the composer and drive it. After C6 you can watch it fully. C7–C9 cover setup and admin, which existing data can do without until then.

**Order change (2026-10-08):** C7–C10 are built in the order C8 → C9 → C7 → C10. The workspace under review is empty and ChatGPT is not signed in, so setup (C8) and Settings › Account sign-in (C9) are what unblock the rest of the app.

### 16.3 Chunks

#### C1: Shell and look

- **Spec:** §3, §4, §5, §6, §11 (API client, render guard, polling, escaping, backend-loss banner).
- **Build:**
  - New `index.html` (landmarks, skip link, icon sprite, module entry), `app.css` (all tokens plus base components: buttons, chips, badges, cards, inputs, focus), `app.js` (API client with `X-ASHAD-Request`, `esc()`, hash router with legacy redirects, bootstrap poll paused when hidden, toast region, confirm `<dialog>` helper, render-guard helper).
  - Main window gets `titleBarStyle: 'hidden'` plus `titleBarOverlay` in `desktop/main.cjs`, with a 40px drag strip.
  - Three-pane layout with empty placeholder panes.
  - A temporary `#/styleguide` route showing every token and component. Delete it in C10.
  - Delete the old static files.
- **Tests:**
  - Remove `test_workspace_ui.js`, `test_workspace_integration.js`, `test_instance_workspace.js`, `test_instance_integration.js`, and the old-DOM parts of `test_dashboard_state.js` / `test_desktop_dashboard.js`.
  - Update asset paths in `test_ui_http.py` (`style.css` → `app.css`).
  - Add: `esc()` escapes untrusted text; the router maps legacy links; a late response for an old route is dropped.
- **Review:** open the app. Check the title bar, the empty three-pane shell and the styleguide page. Judge colour, type size, density and focus ring. **This is the main look-and-feel review.**
- **Notes:** Added `core.js` (pure helpers) so tests can import renderer logic without a DOM. `.js` served as `text/javascript`. Inspector hidden below 1280px until C10. Old asset references updated in `scripts/test.ps1` and `scripts/probe-desktop-runtime.py`; the old-dashboard baseline capture script was deleted. `test_ui_http.py` no longer asserts ChatGPT sign-in text in the renderer; that comes back in C9.

#### C2: Sidebar

- **Spec:** §7.
- **Build:** New instance button, Projects heading with `+`, project disclosure rows, thread rows (dot, goal, age, selected state), "Show all", Unassigned group, active-instance indicator, Usage and Settings links with sign-in dot, Ctrl+B collapse. Thread rows route to `#/i/<run>` (still a placeholder pane).
- **Tests (re-added):** polls patch rows in place, keeping expanded state and focus; project names and goals are escaped; real unassigned history is grouped; the active indicator hides on its own thread.
- **Review:** your real projects and history show up correctly. Expanding, collapsing and keyboard navigation feel right. The indicator appears when you're elsewhere.
- **Notes:** Re-render is whole-sidebar but only when its markup changes. Expanded and Show-all state live in JS; focus (by `data-key`) and scroll are restored. Thread dots carry hidden text plus a tooltip instead of a visible label to keep rows compact. Revisit if the dots alone are unclear.

#### C3: Empty workspace and composer

- **Spec:** §8.1, §8.2.
- **Build:**
  - `views/home.js`: empty workspace with live prerequisites.
  - Composer with hero, context chips, goal textarea, agent and transport chips (Steam disabled with reason), read-only summary, Start, Ctrl+Enter, disabled-with-reason states.
  - Start flow with a stable `request_id` and lost-reply recovery.
  - 409 conflict handling and dirty-draft bridge.
- **Tests (re-added):** a 409 keeps goal, transport and request ID and links the owner; a lost reply recovers through `requests/<id>`; a changed goal after an accepted start isn't silently resubmitted; Steam never enables from stale rows or App ID 480; the draft bridge sends only booleans.
- **Review:** start a real instance from the new composer. Check the blocked-slot and not-ready states. Afterwards the app lands on the C4 placeholder.
- **Notes:** Composer mounts a skeleton once per project and patches named regions, so polls never re-create the textarea. Readiness probes on view open, on agent change and from a small Re-check button next to the readiness chip; Start stays disabled while it runs. A request ID is bound to one payload: a changed goal first checks `requests/<id>`, and only a never-launched request gets a new ID. Leaving a typed, unsent goal confirms and then discards it. Steam enables only from a fresh probe for the selected build.

#### C4: Instance thread (header, camera, controls)

- **Spec:** §8.5 A–C, Ended threads, §9.
- **Build:**
  - `views/instance.js` with sticky header, camera frame with epoch lifecycle and overlay states, and the sticky control bar implementing the full §9 matrix.
  - Confirm dialogs, take-over password dialog, hand back, resume with instruction, recalculate goals, flag issue, reconcile, report and evidence.
  - Ended-thread offline panel.
  - Run poll at 2s/15s.
- **Tests (re-added):**
  - Camera: an epoch change tears down the old `<img>` before creating a new one; polling never rewrites `src`; a hidden page stops the feed; a failed feed retries when visible; an ended run never requests `/live`.
  - Controls: each one uses a single `command_id` across retries; recalculate pauses once and ignores late GETs.
  - Ownership: paused, ended and unconfirmed ownership display correctly.
- **Review:** drive a live instance through pause, resume, take over, hand back and stop. Open an ended thread and confirm nothing launches. Check that Stop stays reachable while scrolling.
- **Notes:** Extend moved from Running to Investigating's menu: the backend extends only an open investigation. Recalculate goals shows only when the backend would accept it (one launched player, no human or external control). The thread mounts once per run and patches regions; the camera owns its `<img>` outside the patched markup, retries with exponential backoff (cap 30s) and stops while hidden. Take over shows the password and an **Open desktop** link in a dialog that empties on close; the main process opens the viewer. Split-button menus and `⋯` use the native `popover` attribute. Report and Evidence open read-only dialogs.

#### C5: Activity timeline

- **Spec:** §10.
- **Build:** card component with kind mapping, expandable fields and escaped bounded raw block; filter chips and search; latest-100 window; older-page replace; return to latest; live-edge polling with the "↓ N new" pill; card selection state (consumed by C6).
- **Tests (re-added):** 100 heavy events render a bounded page each poll; older pages replace rather than accumulate (≤200 cards); a late history response can't override "return to latest"; unknown event types render, not drop; "selected" vs "delivered" vs "confirmed" labels never merge.
- **Review:** read a long real run. Judge whether the cards tell you what happened at a glance, and tune the summaries and density here.
- **Notes:** The timeline got its own `views/timeline.js`, because `instance.js` was getting unwieldy. Cards are reconciled by key (seq, or `m<seq>` for a model row), so polls never re-create an unchanged card and open cards stay open. A card's body is built only when it is first opened, so heavy events cost one row each. The live window grows to 200 events, then drops the oldest; history pages are 100 and replace the window. Filters apply to the loaded window; search goes to the server. Adjacent model events merge into one "Model turn" row. Lifecycle milestones (`player_launched`, `world_ready`, `session_running` and similar) map to Control rather than Other. `resume_failed` stays Control; every other `*_failed` is a Problem. Outcome icons: a check only for a confirmed effect; delivered and unknown outcomes get a plain circle. The "↓ N new" pill is sticky inside the Activity section, so it doesn't sit over the camera. Relative times use the sidebar's `ago()` format (`now`, `3m`).

#### C6: Inspector

- **Spec:** §8.6.
- **Build:** `views/inspector.js` with four persistent `<details>` sections; world state from retained observations only; age ticker and Fresh/Stale/Partial/Unknown badges; latest vs decision-time toggle driven by C5 selection; tokens and estimate with "not a charge"; goal tree with statuses, blocked reasons, evidence links and primary acceptance; Ctrl+I toggle; open state remembered.
- **Tests (re-added):** the inspector makes no `/context` request; stale, partial and unknown render from fixtures; a decision without a retained observation says so rather than showing the latest; cached and reasoning tokens aren't double-counted; blocked goals are escaped and explained.
- **Review:** watch a live instance with the inspector open. Is the world state readable, and is stale state obvious? Does the goal tree make the bot's intent clear?
- **Notes:** The inspector is built only from the run and its loaded events: the run detail's newest 200 plus the timeline window. The latest observation is remembered per thread once seen, so model-heavy stretches don't lose it. Freshness uses the controller's receipt time (`event.timestamp`), not the game clock. The section `<details>` mount once, so collapsed sections stay collapsed across polls, threads and Ctrl+I; goal rows remember their open state by goal ID. The decision toggle starts on Latest and keeps your choice as the selection changes. A decision's observation is found from its own event or by `observation_id`/`source_observation`; otherwise it reads "Not retained for this decision". The world groups are Player (every scalar field the game reported), Inventory, Hotbar, Nearby entities (interactive ones first, top 12), Objectives, Completed quests, and UI and action state; any missing group reads "Not observed". The API-equivalent estimate sums `estimated_cost_usd` and reads "not available" when every row is null (subscription billing); no price is invented. Recent activity covers only loaded events, so a long run's older control events appear only after loading older activity. Ages tick each second locally.

#### C7: Project overview

- **Spec:** §8.3.
- **Build:** `views/project.js` with builds (add via native chooser), agent configurations (view instructions, retune, new instance), readiness checklist with Re-check and "checked N ago", Prepare desktop with confirm, recent instances.
- **Tests (re-added):** readiness never runs on the poll; a cancelled chooser does nothing; a late chooser result can't change another form; prepare sends `confirmed:true` with a stable ID.
- **Review:** open each real project. Re-check readiness and add a build. Check whether the page answers "can I run this project right now, and if not, why?"
- **Notes:** Built third (see §16.2). Reuses setup's `readinessHTML`, `registerBuild` and `prepareDesktop`. Overview state (selected build, readiness, per-build validation, preparation, retunes) lives in memory per project, so long operations survive leaving the page. Readiness is probed when the overview opens, on Re-check, after a preparation and when a build is added or picked; never by the poll (`readinessDue`). Retunes count as tuning for the Account page's sign-in guard.

#### C8: Setup flow

- **Spec:** §8.4.
- **Build:** `views/setup.js` with stepper, six steps, resumable entry via `?project=`, tuning progress and recovery, instruction review, hand-off to the composer.
- **Tests (re-added):** re-entry jumps to the first incomplete step; tuning recovery via `tuning/<id>`; one-tuning-at-a-time messaging; closing setup restores draft protection.
- **Review:** set up a new game agent end to end from an empty project.
- **Notes:** Built before C7 (see §16.2). `core.js` gained `newOp`/`submitOnce`, the shared one-ID-per-operation helper used by create project, prepare and tune. Each step mounts its inputs once per (project, step) and patches `stepper`, `main` and `nav` regions. Readiness markup (`readinessHTML`) lives in `setup.js` for reuse by C7. The dirty-draft guard in `app.js` is now one `dirtyGuard(route)` covering the composer, setup and settings forms. Steam shows as "(optional)" in the checklist, because ENet needs no Steam account. Tuning progress uses a native indeterminate `<progress>` and an elapsed time ticking locally.

#### C9: Settings and Usage

- **Spec:** §8.7, §8.8.
- **Build:** `views/settings.js` with the Account, Desktops, Steam accounts, Limits and Maintenance sections, plus the Usage page.
- **Tests (re-added):** ChatGPT login offers only the official HTTPS link and clears after sign-in; the worker token is write-only; leased resources show disabled-with-reason; usage polling and totals; an unloaded usage page reports unavailable; changed migration grouping invalidates a reviewed preview.
- **Review:** sign out and in, probe a desktop, review usage numbers against expectations.
- **Notes:** Built second (see §16.2). Usage got its own `views/usage.js`. Each settings section mounts its forms once per section (and edited desktop), so polls only patch lists and status regions; a typed form is dirty until saved and leaving it confirms. The Account page's model shows its label. Limits takes duration in minutes and the reserve in GB, converted to the API's seconds and bytes; blank duration or spend means no limit.

#### C10: Narrow widths, accessibility, docs

- **Spec:** §12, §13, §14.
- **Build:** inspector and sidebar drawers at the §12 breakpoints; header chip overflow; live-region audit; contrast check of every token pair; delete `#/styleguide`; rewrite `docs/ui.md`; mark §10 of the refactor plan superseded.
- **Tests:** Stop is visible with no horizontal scroll at 800px across every §9 state; drawers return focus; only state changes are announced.
- **Review:** resize from 1920 down to 800 on a live instance. Then do a keyboard-only pass of the whole app. Finish with a desktop smoke run from the packaged build.
- **Notes:** `createDrawer` lives in `core.js` and `stateAnnouncement` in `views/instance.js`, so both are unit-tested; the narrow-width, drawer, live-region and contrast checks are in `tests/test_shell.js`. The 800px check is a markup and CSS invariant test; the actual layout was measured in a browser at 800px for every §9 state (no horizontal scroll, Stop on screen and right-most). The contrast audit raised `--text-3` and `--muted` (§5). `#/styleguide` and its CSS are gone; the route now redirects home. `docs/ui.md` was rewritten for this renderer, and §10 of the refactor plan is marked superseded.
