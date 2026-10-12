# ASHAD Bot

ASHAD runs a game-playing bot in an isolated desktop. Get game controls for a project configuration from the pinned game's source, then start a fresh instance with that configuration and one primary goal using ENet. Steam remains unavailable without fresh licensed-identity proof; see the limitation below. The bot receives its saved instructions and filtered JSON game state, chooses an action, and checks the next observation.

**Get game controls writes a versioned controls file; it does not train model weights.** A Claude Sonnet swarm reads the pinned source in parallel. Each instance picks its own model (Codex or Claude) and freezes the controls file it started with.

Get game controls saves each completed source batch, so retrying a failed run reuses matching notes for the same model, source and prompt version. Oversized notes or manuals receive one compression retry; both requests count toward usage. A project configuration is saved only after its complete controls file passes validation.

## Repository scope

ASHAD is a desktop app (Electron) for Windows x64 and Linux x64. There is no browser version: the window talks only to its own private controller, which rejects any client without the per-launch key Electron injects.

This repository contains the desktop shell, the controller and its UI, worker services, supporting Python utilities, and tests/documentation. Agronauts game source, assets, builds, Godot AI-layer edits, and game-patching tools are not included. Local credentials, runtime data, and validation history also stay out of Git.

The app opens without game files. Getting game controls and gameplay require a separately supplied compatible game build, pinned source/control evidence, and a registered manifest; game files remain local and ignored.

## Build and run (Windows x64)

Build with Node.js 22.12+ and npm (no system Python is required at runtime):

```powershell
npm --prefix desktop ci
npm --prefix desktop run build:dir
.\desktop\output\win-unpacked\ASHAD.exe
# Optional unsigned, per-user installer:
npm --prefix desktop run build:nsis
```

Build preparation requires Git (only tracked sources are packaged) and downloads verified private Python 3.13.16 plus hash-locked runtime wheels from PyPI, ignoring local pip configuration; pip and test tooling are not shipped. Game files and Docker Desktop's Linux engine remain separately supplied prerequisites. The app writes to `%APPDATA%\ASHAD\controller`, not its installation directory. For isolated diagnostic testing only, pass `--user-data-dir="C:bsoluteresh test data"`; this changes the store, not authentication or runtime safeguards.

Chromium's sandbox needs ordinary user read access to the app's files. If `ASHAD.exe` exits immediately when started inside a folder with restricted permissions, install it with the NSIS build or copy `win-unpacked` to a normal per-user location such as `%LOCALAPPDATA%`.

Quit lists owning instances and in-flight work. **Cancel** leaves them unchanged; **Stop/cancel and quit** waits for confirmed teardown and clean controller exit. Failed teardown keeps the app open for reconciliation. If the controller has already exited, Quit asks for confirmation rather than trapping the window; the watchdog keeps its authority. Closing a human viewer never hands control back automatically.

Data from an older checkout-based controller (`.data\controller`) is not loaded automatically. Desktop preparation, build registration and Steam account setup live in the app.

## Build and run (Linux x64)

Build with Node.js 22.12+ and npm (no system Python is required at runtime):

```sh
npm --prefix desktop ci
npm --prefix desktop run build:linux
./desktop/output/linux-unpacked/ashad
# Optional Debian package:
npm --prefix desktop run build:deb
sudo apt install ./desktop/output/<file>.deb
```

Prerequisites: Git (only tracked sources are packaged), `tar`, Docker Engine with `/usr/bin/docker` or `/usr/local/bin/docker` (snap Docker is not supported) and your user in the `docker` group, and a running Secret Service provider (gnome-keyring or KWallet) in the desktop session. Without a Secret Service provider, credential storage and Codex sign-in fail, by design. Data lives in `~/.config/ASHAD/controller`.

For `linux-unpacked` on Ubuntu 24.04, give Chromium's sandbox helper its setuid bit, or install the `.deb` instead. Never run with `--no-sandbox`.

```sh
sudo chown root:root desktop/output/linux-unpacked/chrome-sandbox && sudo chmod 4755 desktop/output/linux-unpacked/chrome-sandbox
```

## Connect your ChatGPT subscription

Use **Settings → AI connection → Sign in with ChatGPT** and finish the official browser sign-in. ASHAD uses its own managed Codex connection, separate from desktop-app and global CLI sign-in. There is no API-key fallback or manual token copying. Pause or stop active work before changing accounts.

Bot decisions require account access to the instance's model, and Get game controls requires Claude. Each instance picks its own model; ASHAD does not silently substitute a different one. Settings, saved records and local diagnostics remain available while signed out.

**Usage & capacity** retains token totals, cost estimates, hardware measurements, session capacity and desktop availability. Equivalent API-price estimates are comparison values, not ChatGPT subscription charges or spending limits. Missing usage remains marked incomplete; historical charges retain their original meaning.

## Connect your Claude subscription

1. Install Claude Code 2.1.280 or later with Anthropic's native installer. ASHAD looks only in the installer's default location (`%USERPROFILE%\.local\bin\claude.exe`, or `~/.local/bin/claude` on Linux).
2. In a terminal run `claude setup-token`, finish the browser step and copy the printed token.
3. Use **Settings → Account → Claude**, paste the token and Save. The token goes to the OS credential store, never to ASHAD's database or logs. Remove deletes it.
4. Get game controls always runs Claude Sonnet (**Projects + → Setup new game agent**, step 4). Each instance picks its own model, Claude Sonnet, Opus or Haiku here or Codex, at start and may change it within the same provider while live.
5. Terms: this is personal, local use of your own subscription through the unmodified Claude CLI. Read https://code.claude.com/docs/en/legal-and-compliance. Products built for other people should use API keys; ASHAD has no API-key fallback. Do not share a build containing a token or run it for other people's subscriptions.

## Project ownership and retained history

Use **Projects + → Setup new game agent** to create a game project, select/register a supported pinned manifest, explicitly inspect/prepare its desktop, get game controls for a named project configuration and review its saved controls. Selection never imports, prepares, gets controls or starts automatically. Duplicate names remain independent IDs. Completed retries reconcile the same request rather than silently running again. Exactly **one occupied instance globally** is allowed, including queued, paused, human-owned and ended/unconfirmed work. Existing higher saved caps remain historical settings, not effective capacity.

Interrupted imports from older ASHAD versions block editing and admission until operator reconciliation: retain the journal, evidence and backup; do not remove them blindly.

## Get game controls, then run it

1. Complete the four **Projects +** setup steps: Project, Build, Desktop, then Get controls. Get controls explicitly runs a Claude Sonnet swarm over the pinned source/control evidence; it performs inference but does not launch a game. Review the saved controls.
2. Open the project's **New instance**, select a project configuration and enter the primary goal. Its build is fixed by the configuration. Choose the instance's model, Codex or Claude; change it from the instance header within the same provider while the instance is live (it applies from the next decision). The controls file stays the one the instance started with. Check readiness without inference/gameplay if needed.
3. Explicitly **Start instance**. ASHAD freshly revalidates authentication, exact supported build pins and actual idle desktop/render/capture/input capabilities, then accepts one host bot in a fresh world. Failed/blocked drafts never auto-launch. Lost replies reconcile the same request; restoring prepared inputs asks first and still requires another Start click.
4. Follow the live view. Pause, resume, stop, or take over if the bot needs help. Each instance retains its own run ID and evidence.

Configurations from earlier versions are converted once on upgrade to controls files; any that cannot be converted are retired. The conversion is one-way.

**Desktop prerequisites:** Docker Desktop's Linux engine (Windows) or Docker Engine (Linux), and separately supplied supported game files are required. Confirmed preparation may build a content-addressed bundled worker image and create one owner-labelled desktop, persistent volumes and local service/desktop credential references; it never starts gameplay or recreates/adopts unrelated containers. Changed/legacy artifacts lacking exact supported pins require a new registration. Each Prepare request reconciles one desktop per project and build. Provisioning completion alone is not readiness.

**Steam limitation:** Steam is unavailable until fresh actual matching identity, real App ID and licence are proven. The current idle passive worker cannot supply that proof without application initialization, so copied account readiness or an old verification never enables it. ASHAD never substitutes App ID 480, ENet or offline transport. Supported owned Steam-start/integration work and live acceptance remain separate gates; ENet is an explicit user choice.

Getting game controls does not launch a game. A new instance always uses fresh world and character setup; multiplayer planning, seeds and advanced scenario editing are deferred. Existing desktops and Steam profiles are retained between runs.

The primary goal is free text. New instances maintain an automatic **goal hierarchy** and a **currently chasing** goal. The planner works backward from the desired outcome through missing prerequisites, then the existing game-action loop pursues one achievable goal at a time. Completing a child does not automatically complete its parent.

Plans belong to the instance, not the reusable agent. They survive refreshes and controller recovery; resume/takeover handback rechecks fresh observations before acting. The live instance sidebar shows the hierarchy, current branch, completion conditions, blocked reasons and recorded evidence. Setup remains agent + primary goal + ENet/Steam.

The planner uses observed quests, visible recipes, inventory and game controls knowledge, not a guessed tutorial walkthrough. Each proposed goal cites supplied evidence. Completion checks are controller-evaluated quest IDs, current inventory quantities, matching observed tiles/entities/recipes, or world readiness. An unknown condition stays unverified. Inventory quantity does not prove cumulative harvesting, delivered input is not success, and the observation's `complete` flag only means coverage was not truncated.

Planning expands the current branch as needed (up to 8 levels / 64 goals), reviews failed actions or eight actions without measured progress, and requests help after repeated unproductive planning. Missing recipes or unsupported observations may require discovery or operator assistance; the planner cannot create unavailable controls. Historical runs without a generated plan retain their existing behavior.

Supplying a goal still does **not** create a reliable automatic whole-game success detector. The free-text primary objective retains manual acceptance; concrete observable outcomes live in its child goals. When no further grounded work is available, the planner requests operator review. Inspect results and Stop when appropriate. An exhausted checklist alone never proves tutorial completion. Citations and checks constrain proposals but do not prove arbitrary natural-language intent. Full autonomous DEMO completion remains unverified.

## Observe and recover

The instance workspace has four tabs: **Bot view** (the live bot computer), **Goals**, **World state** (retained observed world state) and **Actions** (bounded evidence-based agent activity and model usage). Status, run controls and the resume form stay visible on every tab, and the camera stream runs only while Bot view is open. Load older activity replaces a bounded page; returning to latest does not discard the ledger. Observation age/coverage and decision-time context are explicit; monitor-only diagnostics are not model knowledge. Polling preserves current-page reading position, focus and disclosures.

The live camera is for the human operator. Routine model decisions use the filtered JSON observation, profile instructions, objective and recent action outcomes. The executor sends bounded normal game controls; it does not directly grant inventory, complete quests or modify saves. Input delivery is not proof that an intended gameplay effect occurred.

**Pause** holds bot input and inference; the game world may continue simulating. **Take over** pauses the bot and opens that desktop for human control. **Hand back to bots** closes human access and resumes from fresh observations, with optional instructions. Closing the viewer alone does not hand control back.

**Recalculate goals** rebuilds the checklist from fresh player state under the same primary objective, model and profile. It holds input, archives the old plan/evidence (unfinished goals are retired, not completed), and leaves the run paused for inspection and a separate Resume. It is unavailable during human/external control. **Cancel recalculation** stops planning without replacing the old plan. The planner can also retire obsolete setup goals automatically rather than insist on returning to a screen already passed.

**Stop** attempts normal save/exit and confirms process teardown. Failed teardown retains its desktop/account reservation until reconciliation succeeds. Existing diagnostics, evidence and operational recovery remain available; removing advanced setup does not remove these safeguards.

## Scope and retained data

The supported workflow is getting game controls and fresh, one-bot instances. Old scenarios, runs, usage and evidence (including old save archives under each instance folder) remain stored and readable for operational compatibility. Deleting an instance from the sidebar, or a project from setup, removes its records and run files from this computer; usage totals are kept. Retired feature endpoints are unavailable; preserving a record does not make its old editing or launch workflow available.

[Future work](docs/future-work.md) records deferred features and the prerequisites for restoring them.

Controller data lives in `%APPDATA%\ASHAD\controller` (Windows) or `~/.config/ASHAD/controller` (Linux); worker volumes contain game and Steam state. Back up both when preserving local history. Keep Steam profiles and credentials out of shared evidence exports.

## Verify changes

Development checks need Node.js and a Python environment; run `.\scripts\setup.ps1` (Windows) or `./scripts/setup.sh` (Linux) once to create `.venv`.

```powershell
.\scripts\test.ps1
```

```sh
./scripts/test.sh
```

Mocked tests do not establish live account access, Steam gameplay, UI acceptance or autonomous campaign completion. Game-adapter checks and historical validation records are maintained separately and are not included here.

Code map: `ashad/` contains the controller, agents and storage; `ashad/static/` the app UI rendered by Electron; `desktop/` the Electron shell and packaging; `worker/` isolated desktop services; `integration/fixtures.py` Python-side save/archive utilities required by the controller; `tests/` app automated checks; `scripts/` development setup, desktop provisioning and verification tools.
