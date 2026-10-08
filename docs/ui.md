# App guide

ASHAD is a Windows Electron app; there is no browser version. It looks like a coding-agent harness applied to game agents: a project tree with instance threads on the left, one focused workspace in the centre, and a collapsible inspector on the right. The renderer is plain HTML, CSS and JavaScript modules with no framework, build step, runtime dependency or CDN. The design and the reasons behind it are in the [frontend spec](frontend-spec.md). Navigation and inspection never launch gameplay.

## Layout

- **Sidebar** (Ctrl+B): **New instance**, the **Projects** tree (each project lists its threads newest first, ten at a time), an **Unassigned** group for older runs without a project, the active-instance pill when the one instance slot is busy elsewhere, then **Usage** and **Settings**. Settings shows an attention dot until ChatGPT is signed in.
- **Workspace:** the current route. Unknown links go home; legacy `#/sessions`, `#/agents` and `#/new/<agent>` redirect to their new homes.
- **Inspector** (Ctrl+I): open by default on wide windows, shown only on an instance thread.
- **Narrow windows:** below 1280px the inspector opens as a right-side drawer from the thread header. Below 960px the sidebar opens as a left drawer from the title strip, and the header's meta chips move into a `⋯` popover. Drawers are modal dialogs: Esc, a click outside or any navigation closes them, and focus returns to the button that opened them. Nothing scrolls sideways at 800px or wider.

## Empty workspace and setup

With no projects, the workspace shows **Set up your first game agent**, with live prerequisites (ChatGPT signed in, a desktop registered, a build on this computer) and fix links. **Set up new game agent**, the sidebar's **+** and **New instance** all open setup.

Setup (`#/setup`) has six steps:

1. **Project:** name it. Existing projects are linked.
2. **Build:** choose `manifest.json` or a build folder with the native chooser. A cancelled chooser changes nothing. The build's catalog is summarised.
3. **Desktop:** a readiness checklist (model, build validation, each desktop, the instance slot, Steam). **Prepare desktop** is explicit and confirmed. It creates or reuses the project's managed desktop (container, images, volumes, credentials) and never starts the game.
4. **Agent and model:** a configuration name and a model. Continuing requires ChatGPT sign-in, with a link to Settings › Account.
5. **Tuning:** writes a controls manual from the build's source. This prepares instructions, not model weights. Only one tuning runs at a time; if another is running, setup says so and waits.
6. **Instructions:** the saved instructions, read-only, then **Start an instance** opens the composer with that agent.

Setup is resumable. `#/setup?project=<id>` lands on the first incomplete step; readiness is probed again, so a project with a build starts at step 3. Creating a project, preparing and tuning each keep one request ID until they succeed. A lost reply is recovered (tuning through `GET /api/agents/tuning/<id>`) or retried as the same request, never resent with changed inputs. Leaving setup does not cancel preparation or tuning. Their state lives in memory, so it survives navigation but not a reload.

## Project overview

`#/p/<project>` answers "can I run this project now, and if not, why?":

- **Builds:** each with its pinned revision and validation state.
- **Agent configurations:** View instructions, Retune (a new configuration; the old one stays) and New instance.
- **Desktop readiness:** the same checklist as setup, with Re-check and Prepare desktop.
- **Recent instances:** the last ten threads.

Readiness probes the machine, so it runs only when the page opens, on Re-check, after a preparation and when a build is added or picked; never on the background poll.

## New instance

The composer (`#/p/<project>/new`, also the home screen once projects exist) asks "What should the agent do in <project>?". Pick a saved configuration and a transport, type the goal and press **Start** (Ctrl+Enter; plain Enter adds a newline). The readiness chip shows Desktop ready, Not ready with its reason, or Instance running with a link to the owner. A disabled Start always says why underneath.

ENet is the explicit default. Steam stays unavailable until a fresh readiness probe for that build proves a licensed identity; stored account rows, App ID 480 and an offline fallback never enable it. A start keeps one request ID. A lost reply is recovered through `GET /api/instances/requests/<id>`, and a goal changed after an accepted start is not silently resubmitted.

## Instance thread

`#/i/<run>` puts the bot's computer first: a 16:9 live camera, then a sticky control bar, then the activity timeline.

- **Camera:** the feed URL carries only the binding epoch. Polls never restart it; an epoch change tears down the old feed first; it stops while the window is hidden and retries with backoff. Overlay pills say Live, Paused (bot input held; the game may still simulate), Human control, Reconnecting (which does not mean the game froze) or Offline. Ended threads show a static panel and never request a feed.
- **Controls:** follow the spec's §9 matrix (Pause, Resume or Resume with an instruction, Take over, Hand back, Recalculate goals, Extend investigation, Flag issue, Reconcile teardown, Report, Evidence). **Stop** is always the right-most control and asks first. Each control keeps one command ID across retries, and failures show under the bar. Take over shows the desktop password once, in a dialog that empties when it closes; closing the viewer never hands back.
- **Activity:** cards for what was recorded: observed, plan, selected (not yet sent), sent, outcome (delivered vs effect confirmed vs failed, never merged), tool, model turns, control, problems, and any unmapped type. At most 200 cards are rendered; older pages replace the window. The view follows the live edge only when you are near the bottom; otherwise a "↓ N new" pill appears.
- **Inspector:**
  - **Observed world state:** from the latest retained observation only, with Fresh, Stale, Partial and Unknown badges and a Latest vs At-decision toggle.
  - **Recent activity.**
  - **Tokens and cost:** cached and reasoning tokens are subsets and are not counted twice. The API-equivalent estimate is not a charge.
  - **Goal:** checklist, blocked reasons, and a primary objective that always needs your acceptance.

  The inspector never asks the game for fresh state.

## Usage and Settings

**Usage** (`#/usage`) is fetched when the page opens and on Refresh, never by the poll. It shows turns, total tokens and the API-equivalent estimate for 7 days, 30 days or all time, labelled "Estimate, not a charge". Subscription rows have no price, so an all-subscription range reads "Not available", not $0. The table sorts by column, filters by model and purpose, and renders 200 rows at a time.

**Settings** (`#/settings/<section>`):

- **Account:** ChatGPT status, Sign in, Cancel sign-in and Sign out. Sign-in offers only an HTTPS link on `auth.openai.com` or `chatgpt.com`, opened through the main process's allowlist and dropped once sign-in settles. Changing sign-in is disabled, with the reason, while tuning or while an instance is active. There are no API keys.
- **Desktops:** probe, edit and delete. The service token is write-only ("Stored. Leave blank to keep"). A leased desktop cannot be edited or deleted, and says why.
- **Steam accounts:** add (App ID 480 is rejected as you type), Start sign-in, Finish, Verify. Verify launches the game, so it needs the free slot.
- **Limits:** session duration, spend cap, storage reserve, and a read-only hardware readout. Concurrency is fixed at one.
- **Maintenance:** workspace import (absolute paths, three confirmations and a review dialog) and project migration (edit groups as JSON, preview, then apply; any edit invalidates the reviewed preview).

## Behaviour guarantees

- **Escaping:** every server, game, model and user string is escaped.
- **Polling never disturbs you:** the 5s bootstrap poll never re-creates a field, clears a draft, moves focus, collapses a disclosure or restarts the camera. A late response for another view is dropped.
- **Unsent input is protected:** a typed goal, setup input or a settings form asks before you navigate away, and the desktop shell asks before reload or quit.
- **One live region:** it announces control outcomes, state changes (Running → Paused, Ended) and errors, never telemetry, ticking ages or new cards. Status is never colour-only.
- **Credentials stay out of storage:** no credentials, tokens or desktop passwords in browser storage, URLs, or the DOM after their dialog closes.
- **Keyboard:** reaches everything, with a visible focus ring. The only shortcuts are Ctrl+B, Ctrl+I and Ctrl+Enter.
- **Contrast:** body text is at least 4.5:1 and status marks at least 3:1 on every surface; the Node suite checks the token pairs.

## Desktop shell

The Electron shell renders this interface from an authenticated, owned loopback controller running on bundled Python; the controller refuses any client without the shell's per-launch key. The renderer's only bridge is `window.ashadDesktop`:

- `chooseBuild('manifest' | 'folder')`
- `setDirtyDraft(boolean)`
- `requestQuit()`

External links (ChatGPT sign-in, desktop viewers) open only through the main process's allowlist. Human viewers use an isolated session.

Quit checks queued, live or unresolved ownership and in-flight tuning, sign-in or setup operations. Cancel stops nothing. Confirmed Stop/cancel-and-quit uses normal teardown and waits for a clean controller exit; unknown teardown keeps the app open for recovery. View → Reload asks before discarding an unsent draft. See the [desktop build instructions](<../README.md#desktop-shell-windows-x64>).

## Compatibility and verification

Advanced scenarios, multiplayer scheduling, saved starts, balancing, run comparison, reviews and custom success predicates are deferred ([future work](future-work.md)). Existing records and evidence are preserved, and retired mutation endpoints stay unavailable. The [API contract](api-contract.md) lists every route the renderer uses. The controller answers only loopback `Host` headers.

Use [structured testing](structured-testing.md) for verification. Automated API and renderer checks cannot establish successful live tuning, human UI acceptance or autonomous gameplay.
