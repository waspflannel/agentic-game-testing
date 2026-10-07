# ASHAD Bot

ASHAD runs a game-playing bot in an isolated desktop. Tune a reusable agent profile from the pinned game's controls, then start a fresh instance with that agent, one primary goal, and ENet or Steam. The bot receives its saved instructions and filtered JSON game state, chooses an action, and checks the next observation.

**Tuning writes instructions; it does not train model weights.** A profile records the selected model and build. Each run freezes its profile so later tuning cannot silently change a running bot.

Tuning saves each completed source batch, so retrying a failed setup reuses matching notes for the same model, source and prompt version. Oversized notes or manuals receive one compression retry; both requests count toward usage. An agent is saved only after its complete manual passes validation.

## Repository scope

This repository contains only the ASHAD web app, controller, worker services, supporting Python utilities, and app tests/documentation. Agronauts game source, assets, builds, Godot AI-layer edits, and game-patching tools are not included. Local credentials, runtime data, and validation history also stay out of Git.

The dashboard can start without game files. Agent tuning and gameplay require a separately supplied compatible game build, pinned source/control evidence, and a registered manifest. The default local manifest location is `builds/agronauts-decisions/manifest.json`; game files remain local and ignored.

## Planned desktop refactor

The [Electron workspace refactor plan](<docs/electron-workspace-refactor-plan.md>) records the agreed Codex-style desktop direction, project/instance model, implementation milestones, migration safeguards, and acceptance checks. It tracks implemented milestones separately from remaining release gates; the instructions below describe the current supported workflow.

## Desktop shell (Windows x64)

The Electron shell, private-runtime build tooling, project/migration contracts and fixed global instance admission are implemented; project-workspace navigation/setup and installed/live release acceptance are still in progress. The shell currently opens the existing dashboard. No legacy data, worker registrations, game builds or credentials are imported automatically.

Build locally with Node.js 22.12+ and npm (no system Python required for the desktop runtime):

```powershell
npm --prefix desktop ci
npm --prefix desktop run build:dir
.\desktop\output\win-unpacked\ASHAD.exe
# Optional unsigned, per-user installer:
npm --prefix desktop run build:nsis
```

Build preparation downloads verified private Python 3.13.16 and pinned dependencies; game files and Docker Desktop remain separately supplied prerequisites. The application writes to `%APPDATA%\ASHAD\controller`, not its installation directory. For isolated diagnostic testing only, pass `--user-data-dir="C:\absolute\fresh test data"`; this changes the store, not authentication or runtime safeguards. Do not point it at live legacy data as a substitute for migration.

Quit lists owning instances and in-flight work. **Cancel** leaves them unchanged; **Stop/cancel and quit** waits for confirmed teardown and clean controller exit. Failed teardown keeps the app open for reconciliation. Closing a human viewer never hands control back automatically. See the [execution log](<docs/electron-workspace-refactor-plan.md#p1-execution-record--2026-10-07-offline-packaged-scope-complete>) for precise verified scope and remaining gates.

## Start locally

From PowerShell in your clone directory:

```powershell
.\scripts\start.ps1
```

Open [ASHAD](http://127.0.0.1:8740). Keep the controller running during a session; closing the dashboard does not stop the game. Stop active runs and confirm teardown before restarting after a code update.

For a fresh environment, run `scripts/setup.ps1` first. The controller requires Python 3.11+; local game desktops require Docker Desktop's Linux engine. The dashboard has no npm build step. Build registration, desktop health and Steam account setup remain in Settings; see [worker setup](docs/workers.md) for infrastructure details.

## Connect your ChatGPT subscription

Use **Settings → AI connection → Sign in with ChatGPT** and finish the official browser sign-in. ASHAD uses its own managed Codex connection, separate from desktop-app and global CLI sign-in. There is no API-key fallback or manual token copying. Pause or stop active work before changing accounts.

Profile tuning and bot decisions require account access to the selected model. Model selection belongs to agent setup; ASHAD does not silently substitute a different model. Settings, saved records and local diagnostics remain available while signed out.

**Usage & capacity** retains token totals, cost estimates, hardware measurements, session capacity and desktop availability. Equivalent API-price estimates are comparison values, not ChatGPT subscription charges or spending limits. Missing usage remains marked incomplete; historical charges retain their original meaning.

## Project ownership and retained history

Create a game project in **Settings → Builds**, then register its pinned manifest under that project. Duplicate names remain independent IDs. Name each tuning configuration if useful; retries reconcile the same tuning request rather than silently retuning. Exactly **one occupied instance globally** is allowed, including queued, paused, human-owned and ended/unconfirmed work. Existing higher saved caps remain historical settings, not effective capacity.

Optional **Import existing workspace…** requires an empty owned target, a stopped source controller/watchdog, confirmed idle worker/account pool, a fresh verified SQLite/evidence backup and explicit separate external backups/inventory. It does not copy Codex auth, OS keyring values, registrations, external builds or Docker volumes, and does not infer current readiness. Project grouping is a separate explicit preview/backup/apply operation; unassigned history remains readable. Interrupted imports block editing/admission until operator reconciliation: retain the journal, evidence and backup; do not remove them blindly. See the [migration contracts and verification](<docs/electron-workspace-refactor-plan.md#p2-execution-record--2026-10-07-controller-contracts-complete>).

## Tune an agent, then run it

1. Open **Tune agent**, choose the pinned build and model, and generate the profile. Codex uses the build's source and control evidence to produce reusable gameplay instructions. Review the saved instructions before using the profile.
2. Open **New instance**, select an agent, enter the primary goal, and choose ENet or Steam.
3. Start the instance. ASHAD validates readiness and automatically allocates an available compatible desktop for one host bot in a fresh world. Steam additionally requires a verified, entitled account on an available desktop.
4. Follow the live view. Pause, resume, stop, or take over if the bot needs help. Each instance retains its own run ID and evidence.

Profile generation does not launch a game. A new instance always uses fresh world and character setup; multiplayer planning, saved starts, seeds and advanced scenario editing are deferred. Existing desktops and Steam profiles are retained between runs.

The primary goal is free text. New tuned-agent instances maintain an automatic **goal hierarchy** and a **currently chasing** goal. The planner works backward from the desired outcome through missing prerequisites, then the existing game-action loop pursues one achievable goal at a time. Completing a child does not automatically complete its parent.

Plans belong to the instance, not the reusable agent. They survive refreshes and controller recovery; resume/takeover handback rechecks fresh observations before acting. The live instance sidebar shows the hierarchy, current branch, completion conditions, blocked reasons and recorded evidence. Setup remains agent + primary goal + ENet/Steam.

The planner uses observed quests, visible recipes, inventory and game controls knowledge, not a guessed tutorial walkthrough. Each proposed goal cites supplied evidence. Completion checks are controller-evaluated quest IDs, current inventory quantities, matching observed tiles/entities/recipes, or world readiness. An unknown condition stays unverified. Inventory quantity does not prove cumulative harvesting, delivered input is not success, and the observation's `complete` flag only means coverage was not truncated.

Planning expands the current branch as needed (up to 8 levels / 64 goals), reviews failed actions or eight actions without measured progress, and requests help after repeated unproductive planning. Missing recipes or unsupported observations may require discovery or operator assistance; the planner cannot create unavailable controls. Historical runs without a generated plan retain their existing behavior.

Supplying a goal still does **not** create a reliable automatic whole-game success detector. The free-text primary objective retains manual acceptance; concrete observable outcomes live in its child goals. When no further grounded work is available, the planner requests operator review. Inspect results and Stop when appropriate. An exhausted checklist alone never proves tutorial completion. Citations and checks constrain proposals but do not prove arbitrary natural-language intent. Full autonomous DEMO completion remains unverified.

## Observe and recover

The live camera is for the human operator. Routine model decisions use the filtered JSON observation, profile instructions, objective and recent action outcomes. The executor sends bounded normal game controls; it does not directly grant inventory, complete quests or modify saves. Input delivery is not proof that an intended gameplay effect occurred.

**Pause** holds bot input and inference; the game world may continue simulating. **Take over** pauses the bot and opens that desktop for human control. **Hand back to bots** closes human access and resumes from fresh observations, with optional instructions. Closing the viewer alone does not hand control back.

**Recalculate goals** rebuilds the checklist from fresh player state under the same primary objective, model and profile. It holds input, archives the old plan/evidence (unfinished goals are retired, not completed), and leaves the run paused for inspection and a separate Resume. It is unavailable during human/external control. **Cancel recalculation** stops planning without replacing the old plan. The planner can also retire obsolete setup goals automatically rather than insist on returning to a screen already passed.

**Stop** attempts normal save/exit and confirms process teardown. Failed teardown retains its desktop/account reservation until reconciliation succeeds. Existing diagnostics, evidence and operational recovery remain available; removing advanced setup does not remove these safeguards.

## Scope and retained data

The supported workflow is agent tuning and fresh, one-bot instances. Old scenarios, runs, saves, usage and evidence remain stored and readable for operational compatibility. Retired feature endpoints are unavailable; preserving a record does not make its old editing or launch workflow available.

[Future work](docs/future-work.md) records deferred features and the prerequisites for restoring them. Older architecture, API and validation documents contain historical implementation details; use this README, [dashboard guide](docs/ui.md) and [structured testing](docs/structured-testing.md) for the current workflow.

Default controller data lives in `.data/controller`; worker volumes contain game and Steam state. Back up both when preserving local history. Keep Steam profiles and credentials out of shared evidence exports.

## Verify changes

Install Node.js for the dashboard checks, in addition to the Python environment created by setup.

```powershell
.\scripts\test.ps1
```

See [structured testing](docs/structured-testing.md) for app checks and separate live acceptance gates. Mocked tests do not establish live account access, Steam gameplay, UI acceptance or autonomous campaign completion. Game-adapter checks and historical validation records are maintained separately and are not included here.

Code map: `ashad/` contains the controller, agents and storage; `ashad/static/` the dashboard; `worker/` isolated desktop services; `integration/fixtures.py` Python-side save/archive utilities required by the controller; `tests/` app automated checks; `scripts/` local app setup and verification tools.
