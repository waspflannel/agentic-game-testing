# Electron Game-Testing Workspace Refactor Plan

**Status:** Planning baseline. The product direction below is agreed; implementation has not started under this plan.

**Target:** The ASHADBOT application in this repository, not the DeepSeek Harness GUI or the Codex application.

**Purpose:** Provide a durable reference for moving ASHADBOT from its current browser dashboard to a Codex-inspired Electron game-testing workspace without discarding the existing controller, agent runtime, safeguards, or history.

This document describes the **planned state**, not capabilities already delivered. The [README](<../README.md>), [current dashboard guide](<ui.md>), and [structured testing guide](<structured-testing.md>) continue to describe the supported application until the corresponding implementation milestones pass acceptance.

## Contents

1. [Agreed direction and scope](#1-agreed-direction-and-scope)
2. [Existing foundation and actual gaps](#2-existing-foundation-and-actual-gaps)
3. [Target workspace and user journeys](#3-target-workspace-and-user-journeys)
4. [Data ownership and persistence](#4-data-ownership-and-persistence)
5. [API changes and compatibility](#5-api-changes-and-compatibility)
6. [Electron architecture and lifecycle](#6-electron-architecture-and-lifecycle)
7. [Game onboarding and desktop preparation](#7-game-onboarding-and-desktop-preparation)
8. [One-active-instance invariant](#8-one-active-instance-invariant)
9. [Activity, world state, and usage](#9-activity-world-state-and-usage)
10. [Frontend refactor boundaries](#10-frontend-refactor-boundaries)
11. [Implementation milestones](#11-implementation-milestones)
12. [Verification matrix](#12-verification-matrix)
13. [Migration, rollout, and rollback](#13-migration-rollout-and-rollback)
14. [Risks and decisions to validate](#14-risks-and-decisions-to-validate)
15. [Release definition of done](#15-release-definition-of-done)
16. [Execution and decision log](#16-execution-and-decision-log)

## 1. Agreed direction and scope

### 1.1 Product decisions

| ID | Decision | Consequence |
| --- | --- | --- |
| D01 | Ship an Electron desktop app, Windows first. | A desktop shell, local runtime ownership, native file selection, packaging, and safe quit behavior are part of the refactor. |
| D02 | Use Codex as an interaction and layout reference. | Dark, restrained workspace; project sidebar; nested instance threads; prominent central work area. Do not copy unrelated chat features or branding. |
| D03 | One project represents one game. | A project owns build registrations, multiple agent configurations, and its instance history. A build revision is not automatically a new game project. |
| D04 | A game can have multiple saved agent configurations. | Users choose a configuration when starting an instance. The initial running workflow remains one host bot using one selected configuration. |
| D05 | One instance/thread represents one execution. | Reuse the existing run ID and run history. Do not introduce a second thread database or a many-runs-per-thread abstraction. |
| D06 | Allow one active instance globally in the initial release. | The controller enforces this across all projects, including queued/starting work, pauses, takeover, stopping, and unresolved ownership. |
| D07 | The Projects **+** opens **Setup new game agent**. | Onboard the game/build, prepare a compatible desktop, and configure/tune the first agent. Existing projects can add further configurations. |
| D08 | **New instance** asks for the goal and supported launch settings. | Agent + primary goal + ENet/Steam remain the minimum contract. Display the selected build/model and fresh-world behavior before launch. |
| D09 | The central instance workspace shows the bot's computer. | Reuse the live desktop feed. Keep pause, resume, takeover, handback, stop, and recovery discoverable. |
| D10 | Show Codex-like agent activity. | Present real observations, planning status, selected actions, recorded explanations, outcomes, and errors in an agent-labelled timeline. |
| D11 | Expose price/usage, recent activity, and the AI world state. | Provide a persistent inspector; distinguish observed state from privileged diagnostics and estimates from actual charges. |
| D12 | Preserve the current execution and safety foundation. | No React rewrite, replacement database, replacement agent loop, or weakening of input ownership and teardown safeguards. |

### 1.2 Initial release boundaries

- **Supported game execution:** Retain the existing Linux/Docker desktops and current compatible, instrumented Godot integration. The workspace becomes project-oriented; this does not establish support for every Godot game or arbitrary game executable.
- **Future framework direction:** Make project/build ownership explicit and remove hard-coded game branding from the shell. Keep existing game-specific behavior identifiable. Introduce another runtime or adapter only when a concrete second game requires it.
- **Fresh execution:** Each new instance starts a fresh world using the current supported workflow. Reopening an ended thread opens its records, not a restored game world.
- **Tuning:** Tuning prepares source-grounded instructions. It does not train weights and does not prove gameplay competence. A saved profile can be ready for use while live gameplay remains unverified.
- **Success:** A natural-language goal is not automatically verifiable. Preserve explicit evidence and manual acceptance of the overall objective.
- **Human camera versus model input:** The computer view is for the operator. Routine model decisions continue to use the filtered structured observation, not screenshots.

### 1.3 Explicit non-goals

Do not add these merely to make the application look more like Codex:

- Concurrent instances, multiple bots per new instance, multiplayer scheduling, or cloud workers.
- Saved-world continuation, deterministic replay, seed controls, or run comparison.
- Arbitrary Windows `.exe` support, arbitrary ZIP import, a universal game adapter, or a plugin marketplace.
- A new free-form chat-to-control path that bypasses the existing action and lifecycle contracts.
- Raw model-internal thought traces, fabricated explanations, or extra model calls solely to narrate activity.
- A frontend framework migration, event-bus framework, new database service, or generalized job scheduler.
- Background/tray execution, automatic updating, or macOS/Linux desktop packaging in the first release.
- Re-enabling retired scenario, comparison, review, or saved-start authoring endpoints.

See [deferred work](<future-work.md>) for the prerequisites of those runtime features.

## 2. Existing foundation and actual gaps

The baseline below comes from source review, not a new live gameplay acceptance run. Recheck the referenced implementation before each milestone; line numbers will move as work lands.

| Area | Existing foundation | Refactor gap |
| --- | --- | --- |
| Application stack | [Architecture](<architecture.md#L5-L9>): Python/FastAPI, SQLite metadata, durable JSONL evidence, native HTML/CSS/JS. | No Electron shell, desktop build, or packaged controller lifecycle. |
| Frontend shell | [Navigation and session views](<../ashad/static/app.js#L103-L108>) use global Sessions and Agent setup. | No persistent game-project hierarchy; shell contains game-specific branding. |
| Agent setup | [Agent profiles](<../ashad/agent_profiles.py#L84-L159>) generate and validate immutable build/model-grounded instructions. | Profiles need project membership, useful names, project-scoped selection, and integrated onboarding. |
| Instance creation | [Instance launch facade](<../ashad/instances.py#L11-L84>) validates a goal, chooses a desktop, and creates a durable run. | No project association; admission is not a fixed global one-instance invariant. |
| Live workspace | [Instance rendering](<../ashad/static/app.js#L337-L343>) combines camera, usage, recent events, goals, and lifecycle controls. | Recompose the existing features into the Codex-style workspace rather than recreate them. |
| Activity evidence | [Provider records](<../ashad/ai.py#L205-L299>) and [selected actions](<../ashad/ai.py#L340-L377>) already capture request boundaries and decisions. | Generic event rows need readable, correlated activity cards. Not every old event has agent/player attribution. |
| World state | [Structured inspector](<../ashad/static/app.js#L632>) exposes a limited collapsed JSON view. | Add readable groups, freshness, absence/partial states, and the distinction between latest observation and decision context. |
| Usage | [Usage rendering](<../ashad/static/app.js#L109-L160>) distinguishes subscription usage and API-equivalent estimates. | Move compact totals into the inspector; retain unknown values and correct the planning-usage partial-label edge case. |
| Storage | [Store](<../ashad/store.py#L30-L105>) already supports generic record kinds, transactions, and append-only run events. | Add project records and membership metadata without a new persistence system. |
| Capacity | [Settings](<../ashad/app.py#L333-L347>) allow 1–32 sessions; [admission](<../ashad/coordinator.py#L201-L219>) uses that setting. | Fix the initial product limit at one, handle competing launches atomically, and block while teardown ownership is unresolved. |
| Build setup | [Build registration](<../ashad/app.py#L380-L382>) saves a validated manifest; [worker provisioning](<../scripts/provision-workers.ps1>) mounts the supplied directory. | Registering a build does not deploy it to a desktop. The wizard must connect those steps explicitly. |
| Shutdown/recovery | [Controller entry point](<../ashad/__main__.py#L21-L38>) preserves its watchdog for active runs; [recovery](<../ashad/coordinator.py#L261-L300>) holds input after restart. | Closing an Electron window must not simply kill the controller and abandon a game. |
| Tests | [Python and Node checks](<../scripts/test.ps1>) already cover runtime and dashboard behavior. | Extend them for projects, admission, migration, and Electron. A packaged live smoke is a separate gate. |

Important limits already present:

- [Source extraction](<../ashad/agent_profiles.py#L15-L81>) is Godot/game-specific; accepting a path alone does not make a different game tunable.
- [Build validation](<../ashad/validation.py#L15-L43>) requires a compatible structured interface, source revision/catalog, and artifacts.
- [Worker launch](<../worker/runtime.py#L224-L247>) requires an actual executable under approved runtime/build roots on the worker.
- [Provider completion](<../ashad/codex_backend.py#L233-L260>) currently returns completed schema-constrained output; the app does not already expose a streaming thought feed.

## 3. Target workspace and user journeys

### 3.1 Desktop layout

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Native Electron window / ASHAD                                              │
├───────────────────┬────────────────────────────────────┬────────────────────┤
│ Projects      [+] │ Instance title / goal / state      │ Inspector          │
│                   │ Agent · model · build              │                    │
│ ▾ Game A          ├────────────────────────────────────┤ World state        │
│   Explore map     │                                    │ Recent activity    │
│   Test crafting   │          Bot's computer            │ Usage / cost       │
│   + New instance  │                                    │ Goals / evidence   │
│                   ├────────────────────────────────────┤                    │
│ ▸ Game B          │ Pause / Resume · Take over · Stop  │ Collapsible        │
│                   ├────────────────────────────────────┤                    │
│                   │ Agent activity                     │                    │
│ Usage             │ Observing → Planning → Acting      │                    │
│ Settings          │ Recorded outcomes and blockers     │                    │
└───────────────────┴────────────────────────────────────┴────────────────────┘
```

Layout rules:

- Keep the computer view visually dominant. The activity area and inspector may collapse without stopping the instance.
- Put instance links directly beneath the game project. Manage agent configurations in the project overview, not an additional mandatory sidebar nesting level.
- Use a compact active-instance indicator when the user is viewing another project, an ended instance, or Settings.
- Collapse the inspector into an accessible drawer/tab area at narrower widths. Do not require horizontal scrolling to reach Stop.
- Preserve readable contrast, visible keyboard focus, reduced-motion support, and native window behavior. A custom frameless title bar is not required for v1.
- Long game names and goals must wrap or truncate with an accessible way to inspect the full value.

### 3.2 Empty workspace and project overview

**Empty workspace:** Show **Setup new game agent** as the primary action. Explain prerequisites without requiring Docker or AI sign-in just to open the application and read history.

**Project overview:** Show the game name, registered build revisions, saved agent configurations, desktop readiness, recent instances, **Add agent configuration**, and **New instance**. Readiness must describe what was actually checked and when.

**Incomplete setup:** A project can exist before a usable profile or compatible desktop exists. Show the incomplete step and a clear way to continue; do not disguise it as an empty successful setup.

### 3.3 Setup new game agent

1. **Create or select project.** The global Projects **+** starts a new game project; the project-level action adds an agent configuration to that existing game.
2. **Select build.** Use an Electron native file/folder chooser for a supported manifest/build directory. Show the resolved build identity and requirements before import.
3. **Check compatibility and desktop readiness.** Validate source/control evidence, structured interface, launch command, artifact identity, and a matching desktop. Explain missing prerequisites inline.
4. **Prepare desktop when necessary.** Reuse a compatible idle desktop or explicitly provision an ASHAD-managed desktop for the selected read-only build. See [desktop preparation](#7-game-onboarding-and-desktop-preparation).
5. **Configure agent.** Choose a useful name and an account-supported model. Keep AI connection management in Settings, with a return path to setup.
6. **Tune.** Show actual request/setup status, errors, and retry behavior. Preserve valid source-batch reuse. Do not invent percentage progress if the backend cannot measure it.
7. **Review and finish.** Successful tuning already saves a complete validated profile. Show its instructions, pinned build/model, and limitations before offering **Finish setup** or **New instance**. Finishing setup must not retune, duplicate the profile, or launch a game; source analysis is not a live gameplay test.

Rules:

- Creating a project, selecting a build, or tuning a profile does not start an instance.
- Persist the project only after an explicit create/save action. A later setup failure may leave an honest incomplete project, not an apparently ready agent.
- Save an executable agent profile only after the existing full validation succeeds.
- An explicit retune produces a new immutable profile/configuration record. Existing instances keep their frozen profile.
- Cancelling or leaving review after successful tuning retains the already saved profile; it does not delete the profile or start gameplay.
- Give each tuning submission a stable request ID. After a lost response, reconcile that ID to any completed saved profile before repeating inference. An explicit retune uses a new ID; a failed/incomplete attempt retains the existing source-batch reuse behavior.
- Cancelled native file dialogs do not import anything; retrying a confirmed creation must not duplicate projects or builds.
- Do not promise arbitrary editing of a controls manual without preserving its integrity and validation contract.

### 3.4 New instance

The new-instance composer is project-scoped and contains:

- Agent configuration selector, restricted to that project's valid profiles.
- Primary goal text area.
- Supported transport: ENet or Steam, with its actual readiness requirements.
- Read-only summary of the frozen model, build, and **Fresh world** setup.
- A clear **Start instance** action with request-in-flight and field-local error states.

Only expose launch settings that the backend accepts. The current duration limit is operational configuration; do not add a per-instance duration or budget field without intentionally extending its contract.

If another instance occupies the global slot, retain the draft and show **Return to active instance**. Do not silently stop, switch, or queue gameplay. Do not automatically launch a blocked draft when the slot later becomes free.

### 3.5 Active and ended instances

- Opening a thread is navigation only; it never starts, resumes, or transfers control.
- Active instance: show the camera, current goal, activity, inspector, and state-appropriate controls.
- Paused instance: distinguish **bot inputs paused** from the game world's simulation state.
- Human takeover: preserve explicit handback. Closing the viewer alone does not resume bots.
- Ended instance: show retained activity, evidence, usage, objective, and ending reason; show the camera as offline.
- Unconfirmed teardown: show reconciliation and retain ownership; an Ended label alone does not mean the slot is available.
- Starting a similar test creates a new instance through the normal composer. It does not resume the ended world's save.

## 4. Data ownership and persistence

### 4.1 Minimal entity model

```text
Project (one game)
├── Builds (pinned revisions / compatible artifacts)
├── Agents (named, immutable tuned profiles tied to a build)
└── Runs = Instances = Sidebar threads
    ├── Frozen build / agent / model / goal inputs
    ├── Player binding and lifecycle state
    ├── Persisted goal hierarchy
    ├── Activity / observations / evidence
    └── Usage records
```

Use the current generic record store. Do not introduce an ORM, separate thread table, or generalized workspace service.

| Record | Proposed change | Ownership rule |
| --- | --- | --- |
| Project | New record kind with stable `id`, `name`, `created_at`, and `updated_at`. | Name is presentation, not identity. Duplicate names must not merge projects. |
| Build | Add `project_id` to registration metadata. | A registered pinned build belongs to one project. A changed artifact becomes a new build record rather than silently replacing a referenced one. |
| Agent | Add `project_id`; accept a useful configuration name. | Project must match the referenced build. Instructions, model, and build identity remain immutable for that saved profile. |
| Run | Add top-level `project_id` metadata; keep existing run ID. | New runs derive membership from the validated agent/build. Their execution snapshots remain unchanged by later project/profile edits. |
| Usage | Keep existing run linkage and billing meaning. | Per-instance totals use `run_id`, never display names. Unassigned setup usage stays visibly separate. |
| Events/evidence | Reuse current run ledger and sequence IDs. | Do not rewrite raw events to make new UI labels or retrofit missing model explanations. |
| Settings/migration marker | Record workspace schema/migration version using the existing store. | Migration is explicit and repeatable; this is not a new migration framework. |

### 4.2 Membership validation

- Validate all cross-record relationships in the controller, not only dropdown filtering.
- Reject a project/agent/build mismatch before reserving a desktop or making a gameplay request.
- Derive membership from trusted stored records; do not trust a renderer-supplied project name or arbitrary association.
- Keep project labels separate from immutable execution inputs. Renaming a project later must not rewrite approved plans or evidence.
- Do not add project deletion in the initial navigation work. A future deletion design must address builds, profiles, history, evidence, and active ownership explicitly.

### 4.3 Existing records

- Preserve current IDs, run directories, evidence hashes, profile manuals, account references, worker registrations, and historical charges.
- Offer an import mapping for the current known game and its build revisions. Automatically reuse mappings only when stable IDs establish the association.
- Do not assume all existing builds represent the same game because they share a name or directory pattern.
- Keep ambiguous or orphaned history visible in an **Unassigned history** group until it can be associated safely.
- Legacy run membership may be derived from its frozen build/reference when available. Missing current catalog records must not make old runs unreadable.
- Backfill top-level metadata only; do not rewrite frozen plans, raw ledger entries, or evidence to simulate that projects always existed.

## 5. API changes and compatibility

These are proposed contract changes; implement and test them before the new UI relies on them.

| Operation | Plan |
| --- | --- |
| Bootstrap | Extend `GET /api/bootstrap` with projects and membership data. Preserve the current agents, runs, builds, usage, leases, and settings fields. Include a truthful active-instance/conflict summary. |
| Create project | Add a small validated project-create operation, proposed `POST /api/projects`. Use a stable request ID and the existing command-deduplication mechanism. Reject empty/oversized names. |
| Register build | Extend `POST /api/builds` with project membership. Validate the manifest and selected path before committing. Registration alone must not report desktop readiness. |
| Tune agent | Extend `POST /api/agents/tune` with a configuration name, project consistency checks, and a stable request ID for new callers. Derive the authoritative project from the selected build. Deduplicate completed retries to the saved profile using the existing command store; retain selected-model validation and source-cache behavior. Successful tuning saves the validated profile before presenting review. |
| Start instance | Extend `POST /api/instances` for project consistency. The new UI sends project context; compatible old callers may omit it only when the controller can infer it unambiguously from the agent. Preserve the existing idempotency key semantics. |
| Admission conflict | Return a specific conflict with the owning run ID, lifecycle/teardown reason, and a recoverable message. A competing launch must not create a second queued run as a side effect. |
| Run detail / events | Keep the existing run and cursor-based event contracts. Add only fields needed for reliable observation/activity attribution. |
| Run control | Reuse pause, resume, stop, takeover, handback, goal recalculation, and reconciliation. Do not introduce an alternate desktop control path. |
| Desktop preparation | Add only the operations needed to inspect/reuse/provision the supported local desktop. Keep Docker/service credentials backend-only and validate every argument. |
| Desktop shutdown | Provide a main-process-controlled graceful shutdown handshake/drain path. Do not expose arbitrary process execution through IPC. The concrete transport is validated in P1. |

Compatibility rules:

1. Keep existing run URLs as working aliases when project-aware routes are introduced.
2. Validate the global admission rule at the shared controller boundary so old clients cannot bypass it.
3. Keep retired mutation endpoints unavailable.
4. Additive fields must tolerate legacy records with missing project/observation metadata.
5. Repeated requests with the same key and payload return the same result; conflicting payload reuse returns an error.
6. On ambiguous network failure, reconcile the request/run before offering a new launch key.
7. Emit only real lifecycle transitions. Do not mark a game running merely because the start request was accepted.

## 6. Electron architecture and lifecycle

### 6.1 Process responsibilities

```text
Electron main
  ├── One application instance / native window
  ├── Native file chooser and narrowly scoped preload bridge
  ├── Owned controller process and readiness/shutdown handshake
  └── External sign-in / isolated human desktop viewer

Renderer: existing HTML/CSS/JS from the owned loopback controller origin
  └── Existing validated HTTP API and live camera feed

Python controller
  ├── Projects, agents, runs, storage, credentials, Codex integration
  └── Existing authenticated Docker worker interfaces

Worker desktop
  └── Supported game, player-scoped AI layer, capture, input ownership
```

- Keep the controller as the owner of application data and gameplay decisions.
- Keep Electron main focused on desktop concerns. Do not mirror application state into an Electron-only database.
- Load the current renderer through the controller origin so relative API, CSP, and camera URLs retain their normal behavior. Do not switch to `file://` and compensate by disabling browser security.
- Keep a developer browser mode for diagnosis where practical; it is not a separate product or an unauthenticated bypass around desktop-mode security.

### 6.2 Startup

1. Acquire Electron's single-instance lock; a second application launch focuses the existing window.
2. Resolve writable user-data, immutable application-resource, log, and runtime locations without depending on the checkout directory or current working directory.
3. Detect an existing controller/data lock. Do not silently kill or adopt an unrelated process because it answers on the expected port.
4. Launch only the owned, configured Python runtime with explicit environment and argument boundaries.
5. Wait for authenticated backend readiness and expected application identity/version before loading the workspace.
6. Show actionable startup failures: missing runtime resource, locked data store, port conflict, migration issue, or unavailable service.
7. Preserve the existing controller/watchdog locking and recovery behavior. Recovered bots remain paused until explicitly resumed.

The standalone development entry point may retain its current port. The desktop should support an available loopback port selected through a race-safe bind/readiness mechanism rather than assuming a fixed port is free.

### 6.3 Renderer and IPC security

- Enable context isolation and renderer sandboxing; disable Node integration.
- Expose only narrow, validated bridge operations such as choosing a supported build path and requesting application quit.
- Never expose raw filesystem APIs, arbitrary shell execution, unrestricted process spawning, service tokens, or generic IPC forwarding.
- Validate sender/frame identity for privileged IPC; remote/authentication/viewer content must not receive the app preload bridge.
- Bind the owned controller to loopback and authenticate the desktop session. Use a per-launch secret/handshake, not merely an `ok` health response or the existing application header as proof of ownership.
- Keep authentication out of URLs, renderer local storage, model context, and evidence. The selected mechanism must work for fetches and the MJPEG camera, not just JSON requests.
- Preserve same-origin mutation checks and a restrictive content security policy. Do not enable insecure web preferences to make camera or viewer integration work.
- Deny unexpected navigation and popups. Send approved external HTTPS login links to the system browser; retain the existing login-host allowlist.
- Open human desktop access in an isolated viewer/browser context approved for the configured worker URL. Keep the main application origin and its privileged bridge out of that page.
- Retain the OS credential store. Do not replace keyring-backed credentials with plaintext configuration for packaging convenience.

### 6.4 Close, quit, crash, and recovery

| Situation | Required behavior |
| --- | --- |
| No active run, tuning, setup mutation, or dirty draft | Drain the owned backend, close resources, and quit. Do not terminate unrelated controllers/workers. |
| Only an unsent draft is dirty | Offer **Discard draft and quit** or **Cancel** before draining. This does not require a separate persisted-draft subsystem. |
| Active or paused instance | Prompt **Stop and quit** or **Cancel**. Closing the window is not permission to leave gameplay unattended in the background. |
| Stop and quit confirmed | Block new admission, use normal stop/teardown, wait for confirmed ownership release, then drain the controller and exit. |
| Teardown fails or is unknown | Keep the app open with the owning run and recovery actions visible. Do not report success or free the global slot. |
| Tuning/provider request is in flight | Require explicit cancellation or waiting. Interrupt the provider through the existing cancellation path and retain honest partial/error usage. |
| Desktop preparation is in flight | Wait for or safely cancel/reconcile the operation; do not leave an untracked container or partially overwritten registration. |
| Renderer reload/crash | Recreate the view without launching/resuming gameplay or duplicating the controller. |
| Controller crash | Show disconnected/stale state; retain input/deadline safeguards. Recovery cannot silently resume the bot. |
| External OS force-close | Treat it as failure, not a successful Stop. On restart show and reconcile real persisted ownership. |

Before the fixed single-instance migration is complete, older data may contain multiple owners. **Stop and quit** must list and stop/reconcile all of those owned runs, not only the currently selected thread.

Do not suppress the independent watchdog to make Electron exit look clean. If a deadline or teardown remains unresolved, preserve the existing safety authority.

### 6.5 Packaging and writable data

- Package the Electron shell, renderer/controller resources, a private Python runtime with pinned dependencies, and the Codex SDK's required platform resources.
- Keep executables and subprocess resources outside ASAR where execution requires real filesystem paths.
- A packaged installation must not depend on the developer checkout, a global Python/Node installation, or a manually activated virtual environment.
- Docker Desktop in Linux-container mode remains an explicit game-execution prerequisite. The app may open and display local history without it.
- Store mutable controller data and logs under the application's user-data location; keep game builds external/read-only unless an explicit import strategy is added.
- Preserve existing worker volumes and keyring references. Moving the managed Codex home may require reauthentication; do not promise that copying a directory transfers keyring-bound login state.
- Use one standard Electron packaging tool. Select and pin it after the early packaged-runtime spike; do not build a custom installer/updater framework.
- Extend the [repository publish allowlist](<../.gitignore>) narrowly for the new desktop sources, package/lock manifests, and build configuration. Keep downloaded runtimes, dependencies, installer output, user data, credentials, and signing keys out of source control.
- Test installation and runtime under paths containing spaces and a non-administrator Windows account.
- Do not silently delete user data, external builds, worker volumes, or Steam profiles on uninstall/update.

## 7. Game onboarding and desktop preparation

A game project is not ready merely because its manifest was accepted.

### 7.1 Readiness checks

| Layer | Check | What it must not imply |
| --- | --- | --- |
| Build | Files exist; selected manifest/source/artifact identities pass validation. | Arbitrary executable compatibility or automatic staging. |
| Agent interface | Supported structured schema, source/control evidence, and advertised action coverage. | Complete world visibility or availability of every action described in source. |
| Desktop | Compatible independent desktop, correct mounted build, health, and no conflicting lease/setup session. | Guaranteed gameplay performance or GPU acceleration. |
| Provider | Connected account and access to the chosen model. | Automatic model fallback or permission to make an inference during a passive status check. |
| Steam, if selected | Correct verified identity, game App ID, entitlement, and eligible desktop. | Automatic account creation, purchases, login, or offline fallback. |
| Profile | Complete validated manual matching the selected pinned build/model. | Live gameplay competence or completion of the user's goal. |

### 7.2 Smallest supported preparation flow

1. Prefer a known compatible idle desktop.
2. If none exists, offer an explicit **Prepare desktop** operation for the selected build.
3. Reuse the behavior and safety checks in [worker provisioning](<../scripts/provision-workers.ps1>) and the [worker guide](<workers.md>), extracting only what the application actually needs.
4. Pass validated arguments without shell-string interpolation. Restrict management to labelled ASHAD-owned containers and approved local build roots.
5. Mount the build read-only and verify the mounted identity after startup; a registered ID or friendly worker name is not proof.
6. Preserve reusable account/Steam volumes. Never remount, replace, or repurpose a desktop with active or unresolved ownership or human setup.
7. If Docker, the supported worker image, or the required artifact is missing, show an actionable prerequisite. Do not silently install system software or download a different game.
8. Record operation outcome and enough identity to reconcile a timeout/retry. Do not create another container just because the first response was lost.

Initial setup supports the current integration. Any second game's source extraction, observation contract, actions, save assumptions, and runtime must be reviewed and tested separately.

## 8. One-active-instance invariant

### 8.1 Slot definition

The global slot is occupied by:

- An accepted queued/start request that is committed to execution.
- A run in Starting, Running, Paused, Investigating, or Stopping.
- A run under human or external control.
- An ended run with unresolved desktop/account/endpoint ownership or unconfirmed teardown.

A profile being viewed, an unsent goal draft, and retained history do not occupy the slot. Tuning remains distinct from gameplay admission, subject to the existing account/model and cancellation safeguards.

### 8.2 Enforcement

- Put the authoritative check in the shared controller launch/admission path, under the same lock/transaction boundary that commits the run and its ownership decision.
- Perform idempotency reconciliation before treating an exact retry as a competing launch.
- Two simultaneous cross-project starts must produce one accepted run and one informative conflict, not two queued runs waiting to execute.
- Preserve current per-worker/account leases in addition to the global rule. A global cap is not a replacement for resource ownership.
- Pausing, switching sidebar selection, closing a viewer, or losing connectivity cannot free the slot.
- Ended status does not free the slot if teardown remains unconfirmed; reconciliation must establish the result.
- Prevent new starts while shutdown or a destructive preparation/migration operation is draining the runtime.
- The initial Settings UI must not offer a 1–32 concurrency control. Desktop-era API callers cannot bypass the cap by posting a higher legacy value.

### 8.3 Legacy concurrency

Do not auto-kill older concurrent runs or delete queued history during upgrade. Normal migration requires an idle/reconciled system. If legacy active work is discovered, show a conflict/recovery path and prevent new starts until the operator resolves it. Preserve readable records and the ability to stop/reconcile each existing owner.

## 9. Activity, world state, and usage

### 9.1 Agent activity timeline

Project real events into a small set of readable card types:

| Card | Evidence source | Example presentation |
| --- | --- | --- |
| Observing | Recorded structured-context update with timestamp/revision. | “Received observation 42: inventory and nearby targets updated.” |
| Planning | Planner/request start, persisted goal hierarchy, recorded planner explanation. | “Planning prerequisites for the current goal.” |
| Selecting | Provider request boundary and current phase. | “Selecting an available action.” |
| Acting | Validated selected target/action and execution status. | “Move toward the observed workbench — dispatched.” |
| Checking outcome | Subsequent observation/action outcome or local goal verification. | “Checking whether the requested interaction changed the observed state.” |
| Waiting | Recorded loading, action wait, synchronization, or other known wait. | “Waiting for the current action to finish.” |
| Blocked/error | Recorded provider, action, readiness, or goal blocker. | “No supported action is available; operator review needed.” |
| Human/lifecycle | Pause, takeover, handback, stop, recovery, reconciliation. | “Manual control active; bot input is paused.” |

The examples are presentation patterns, not text to fabricate in the absence of matching evidence.

Rules:

- Attribute cards using run/player/agent identity where recorded. For old unattributed events, say **Instance activity** rather than guessing which agent produced them.
- Correlate request/action/observation IDs where available. Add missing attribution at the source when needed, not by parsing prose.
- Distinguish **requested**, **dispatched**, **accepted**, **completed**, **rejected**, **cancelled**, and **unknown**. Input delivery is not proof of gameplay success.
- Show recorded explanations and concise deterministic summaries. No extra model calls for UI narration and no promise of streaming private internal reasoning.
- Keep raw records and evidence available in expandable details.
- Use stable event IDs and sequence cursors; bounded pages/load-older controls prevent an entire long-run ledger from being rendered repeatedly. Bound ordinary server-side cursor reads as well as the rendered page; do not load the whole remaining event history merely to return its first page.
- Preserve scroll position. Follow new events only when the user is already at the live edge; otherwise show a new-activity indicator.
- Retain the existing polling transport initially. Add push transport only for a demonstrated requirement that polling cannot meet.

### 9.2 World-state inspector

Default to the selected agent's **latest retained structured observation**, not privileged monitor-only telemetry.

Suggested groups, rendered only when supported by the observation:

- Connection/readiness and observation identity.
- Player state and position.
- Inventory/hotbar.
- Visible or nearby entities and supported interactions.
- Objectives/quests and observed progress.
- UI/modal state and active action.
- Raw structured JSON for detailed inspection.

Always expose:

- Observation timestamp, age, revision/ID, and whether it is stale.
- Coverage/partial status; `complete` is observation coverage, not whole-game knowledge or goal completion.
- Explicit absent/unknown states. Do not disappear the entire inspector when context has not arrived.
- A distinction between **latest observed state** and **state used for a selected decision** when those differ.
- A clear label for any separate monitor-only diagnostics; they must not be described as information available to the model.

Use retained context for routine rendering. Do not trigger game-side observation calls or model work merely by rerendering a panel. State freshness and camera freshness are separate indicators.

### 9.3 Recent activity and goals

- **Agent activity** explains the observation/plan/action progression.
- **Recent activity** is a compact operational list: start, pause, error, takeover, stop, and important goal changes. It links into the same underlying evidence, not a second competing event store.
- Keep **Currently chasing**, goal ancestry, the checklist, blocked reasons, and completion evidence accessible.
- Preserve goal recalculation/cancellation semantics: archived prior plan, inputs held, explicit Resume afterward, no model-declared completion of the free-text root.

### 9.4 Price and usage

- Show per-instance tokens, request counts, and API-equivalent estimate in a compact inspector summary.
- Label subscription requests as subscription usage. Equivalent API prices are comparisons, not billed subscription charges or spending limits.
- Preserve historical metered records with their original billing meaning.
- Unknown, missing, and partial usage remain explicit; no missing value becomes `$0`.
- Group by stable run/model IDs. Project totals, if displayed, must sum the correctly associated runs rather than match names.
- Tuning currently produces unassigned usage. Keep setup usage separate; do not claim project/agent tuning totals until new records have explicit attribution. Do not invent historical attribution.
- Fix the known classification edge case: completed `planning_total` usage should not be labelled partial merely because [the current scope allowlist](<../ashad/static/app.js#L135-L141>) lists only selection and tuning.
- Do not double-count reasoning/cache subsets when displaying token totals.

## 10. Frontend refactor boundaries

Reuse [the current renderer](<../ashad/static/app.js>), [styles](<../ashad/static/style.css>), and [HTML entry](<../ashad/static/index.html>) rather than introducing a new frontend stack.

Refactor by responsibility:

1. Shell/project navigation.
2. Project overview and agent setup.
3. New-instance composer.
4. Instance workspace and lifecycle controls.
5. Activity projection and cards.
6. World-state and usage inspector.

These are responsibilities, not a requirement to create six files or a component framework. Extract small modules only when the touched code becomes clearer and independently testable.

Preserve or add:

- Existing escaping of untrusted game, source, goal, and provider text.
- Stable live-feed URLs and ownership epochs; polling must not restart the camera.
- Old-camera cleanup when changing instances or closing the view.
- Existing render-epoch/request guards so a late response cannot overwrite another project/instance.
- Native links for navigation, buttons for actions, labelled **+** controls, keyboard-accessible disclosures, and meaningful active states.
- Form values, focus, disclosure state, and scroll during background refresh.
- Field-local errors and focus on the first invalid field, not only a generic toast.
- Dirty-draft protection on navigation/window close. V1 need not persist an unsent draft indefinitely, but must not discard it silently.
- Accessible status announcements without announcing every telemetry refresh.
- Bounded event/history rendering and a responsive, usable narrow-window layout.

Possible new application files are limited to a small Electron main/preload entry, desktop package/build configuration, and a cohesive project/migration helper if justified. Do not pre-create unused adapters, registries, or framework layers.

## 11. Implementation milestones

Every milestone should be reviewable and testable independently. The checkboxes below track implementation, not agreement with the product direction. Leave them unchecked until their exit criteria have evidence.

### P0 — Baseline and executable packaging investigation

**Purpose:** Establish a known starting point and discover packaging blockers before redesign work depends on them.

- [ ] P0.1 Record a clean baseline of the existing Python/Node checks and any pre-existing failures; do not relabel historical test counts as a current pass.
- [ ] P0.2 Capture the current dashboard in supported states using test data: no setup, saved agent, active/paused/ended instance, missing feed, and provider error.
- [ ] P0.3 Validate how a packaged Python runtime locates the app, static assets, keyring backend, Codex SDK resources, and independent watchdog entry points.
- [ ] P0.4 Choose one Electron packaging approach and a reliable owned-backend readiness/shutdown mechanism. Record concrete versions and tradeoffs in the decision log.
- [ ] P0.5 Identify the current data/worker registration/build paths and the required migration backup. Do not modify or copy live runtime data during discovery.

**Exit criteria:** Baseline evidence is recorded; startup, package-resource, and graceful-shutdown approaches have a small runnable proof. No inference or gameplay is needed for this phase.

### P1 — Thin Electron shell over the existing application

**Depends on:** P0.

- [ ] P1.1 Add the smallest Electron main/preload/package configuration that launches the current app from an owned loopback controller.
- [ ] P1.2 Implement single-instance focus, authenticated readiness, startup failures, and strict navigation/IPC boundaries.
- [ ] P1.3 Add native supported-build selection and safe external login/viewer handling without changing the gameplay workflow.
- [ ] P1.4 Implement admission-draining quit behavior, provider cancellation, confirmed Stop and quit, and failure/recovery messaging.
- [ ] P1.5 Produce an early packaged Windows build that opens the existing dashboard outside the checkout and without a global Python installation.

**Likely touchpoints:** [Controller entry](<../ashad/__main__.py>), [app lifecycle/boundary](<../ashad/app.py#L82-L176>), [process locks](<../ashad/process_lock.py>), [Codex runtime](<../ashad/codex_backend.py>), plus new minimal desktop entries/configuration.

**Exit criteria:** Existing functionality works inside Electron; second launch does not start another controller; renderer reload does not launch/resume a game; unsafe quit is prevented; credentials remain outside renderer persistence.

### P2 — Project records, migration, and single-instance admission

**Depends on:** P0; integrate into P1 without duplicating data stores.

- [ ] P2.1 Add project records and controller-validated build/agent/run membership.
- [ ] P2.2 Extend bootstrap/create/tune/instance contracts additively and preserve old run routes/contracts where safe.
- [ ] P2.3 Implement an idempotent membership migration with a preview, backup, explicit import grouping, and unassigned-history fallback.
- [ ] P2.4 Enforce one global occupied slot in the shared launch/admission boundary, including exact retries, competing requests, paused work, and unresolved teardown.
- [ ] P2.5 Remove the adjustable concurrency affordance for the initial product; reject API bypass and handle legacy active records without destructive cleanup.

**Likely touchpoints:** [Store](<../ashad/store.py>), [API](<../ashad/app.py>), [agent profiles](<../ashad/agent_profiles.py>), [instance facade](<../ashad/instances.py>), [coordinator](<../ashad/coordinator.py>), [contracts](<../ashad/models.py>).

**Exit criteria:** Multiple game projects and multiple profiles persist across restart; cross-project mismatches fail before side effects; two concurrent starts produce one run; existing frozen records/evidence are preserved.

### P3 — Project navigation and complete setup flow

**Depends on:** P1 and P2.

- [ ] P3.1 Replace global session navigation with projects and nested run-thread links. Preserve Settings, usage, legacy links, and access to unassigned history.
- [ ] P3.2 Implement project overview, empty/incomplete states, agent configuration selection, and the Projects **+** setup journey.
- [ ] P3.3 Integrate build selection/validation and compatible-desktop inspection/preparation; never equate manifest registration with readiness.
- [ ] P3.4 Add project-scoped tuning, instruction review, honest progress/error/retry states, and names for multiple configurations.
- [ ] P3.5 Add the project-scoped goal composer with supported settings, stable request IDs, draft protection, and active-instance conflict navigation.

**Likely touchpoints:** [Renderer](<../ashad/static/app.js>), [styles](<../ashad/static/style.css>), [API build/setup handlers](<../ashad/app.py>), [worker provisioning](<../scripts/provision-workers.ps1>), [worker client](<../ashad/worker_client.py>).

**Exit criteria:** A user can create a project, prepare/select a compatible desktop, tune and review an agent, and explicitly start one instance. Cancelled/failed setup never starts gameplay or corrupts previous registrations/profiles. Leaving review retains the saved profile; retrying a completed tuning request returns that profile without repeating inference, while an explicit retune creates a new profile.

### P4 — Instance workspace, activity, and inspector

**Depends on:** P2 and P3.

- [ ] P4.1 Recompose the current live view into the dominant computer area, compact controls, activity stream, and collapsible inspector.
- [ ] P4.2 Add deterministic activity-card projection from real events, stable identity/correlation, bounded history, and live-edge scroll behavior.
- [ ] P4.3 Render structured world-state groups with stale/partial/unknown states and raw evidence fallback; distinguish camera freshness and decision-time context.
- [ ] P4.4 Integrate cost/token summaries, recent activity, current goal/checklist, and existing recovery/recalculation controls. Correct planning-usage classification.
- [ ] P4.5 Verify polling stability, late-response isolation, long content, empty states, focus, keyboard use, reduced motion, and small-window behavior.

**Likely touchpoints:** [Instance UI](<../ashad/static/app.js#L219-L343>), [structured runtime](<../ashad/structured.py>), [AI diagnostics](<../ashad/ai.py>), [run/event/feed APIs](<../ashad/app.py#L208-L290>), [dashboard tests](<../tests/test_dashboard_state.js>).

**Exit criteria:** Users can understand observed state, current intent, actual action outcomes, blockers, and usage without opening raw logs; raw evidence remains accessible; no feature invents state or changes control semantics.

### P5 — Packaged end-to-end acceptance and handoff

**Depends on:** P1–P4.

- [ ] P5.1 Run the extended automated suite and record exact outcomes, including intentionally changed legacy concurrency expectations.
- [ ] P5.2 Exercise data import and repeat migration on backed-up fixtures, including absent builds, orphaned history, prior concurrency settings, and interrupted migration.
- [ ] P5.3 Install the desktop build under a non-admin Windows account/path with spaces; verify startup, login handoff, bundled runtime resources, diagnostics, and safe uninstall/data retention.
- [ ] P5.4 With explicit authorization and a supplied compatible game/account, perform a bounded live setup → tune → start → pause → takeover → handback → stop → reopen-history test.
- [ ] P5.5 Exercise controlled controller/viewer failures, stale camera/context, unavailable Docker, failed teardown, and repeated launch/quit requests. Verify no duplicate or unattended run.
- [ ] P5.6 Update current user/runtime documentation only for delivered behavior; record limitations and release evidence in this plan's log.

**Documentation touchpoints:** [README](<../README.md>), [dashboard guide](<ui.md>), [architecture](<architecture.md>), [worker guide](<workers.md>), [structured testing](<structured-testing.md>), and current API documentation as appropriate.

**Exit criteria:** The [release definition of done](#15-release-definition-of-done) is met. Mocked tests alone cannot satisfy packaged-app, account, desktop-rendering, or gameplay acceptance.

### Milestone ordering and review discipline

- Implement P1 as a thin shell first; it should remain useful even before the visual refactor.
- P2 can be developed/tested independently against the existing controller, but must merge before P3 depends on project APIs.
- Split each milestone by its listed substeps. Avoid a single rewrite that simultaneously changes storage, lifecycle, renderer, and game control.
- Keep current operational recovery available throughout. Do not remove old UI paths before the replacement covers their safety functions.
- Do not add a migration framework, event streaming system, or generic adapter layer merely to satisfy a milestone label.

## 12. Verification matrix

Use the existing Python tests and Node dashboard harness where they cover the behavior. Add one focused check for each new non-trivial rule; add Electron-specific automation only for desktop behaviors the current harness cannot exercise.

| ID | Behavior | Required evidence |
| --- | --- | --- |
| T01 | Project/profile/build relationships | API tests for valid relationships, cross-project rejection, missing records, and duplicate display names. |
| T02 | Migration repeatability | Fixture test: migration twice produces the same mapping without duplicate projects, rewritten snapshots, or changed ledger hashes. |
| T03 | Legacy readability | Old/no-project/no-current-build records remain visible, and existing run URLs still open. |
| T04 | Concurrent start race | Two simultaneous starts in different projects yield one accepted run and a useful conflict; no second queued execution. |
| T05 | Idempotency | Same request key/payload returns the same run; changed payload with that key is rejected; a lost response does not start another run. A completed tuning retry returns the saved profile without additional inference; cancelling review retains that profile, and explicit retune uses a new ID. |
| T06 | Slot ownership | Pause, takeover, investigation, stopping, and ended/unconfirmed teardown retain the slot. Confirmed reconciliation releases it. |
| T07 | Legacy cap bypass | A higher saved/postable concurrency value cannot enable concurrent execution in the initial release. |
| T08 | Setup is not launch | Project creation, build import, profile tuning, instruction review, and sidebar navigation issue no gameplay launch. |
| T09 | Desktop preparation | Wrong build, missing Docker, active lease/setup, unlabelled container, lost response, and retry are handled without destructive replacement. |
| T10 | Frozen inputs | Retuning or changing current project metadata cannot alter an existing run's model, build, manual, objective, or approved plan. |
| T11 | Late UI responses | Switching project/instance during a request cannot overwrite the selected view or navigate the user to an unrelated run. |
| T12 | Live feed stability | Polling preserves the feed URL/epoch; switching runs stops the old camera; disconnected/stale feed is not represented as fresh. |
| T13 | Activity truthfulness | Pending/dispatched/completed/unknown outcomes stay distinct; absent attribution/explanation is not invented; event text is escaped. |
| T14 | World-state truthfulness | Missing/stale/partial observations render explicitly; privileged monitor data is not labelled agent knowledge; historical decision context is identified. |
| T15 | Usage correctness | Missing counts remain unknown; complete planning totals are not falsely partial; subscription estimate is not a billed charge; identities prevent cross-run totals. |
| T16 | Draft and accessibility behavior | Form contents, focus, disclosures, and scroll survive polling; navigation warns about unsaved input; keyboard flows and narrow windows remain usable. |
| T17 | Electron ownership | Second app launch focuses one window; conflicting backend identity/port/data lock does not get adopted or killed. |
| T18 | Desktop security | IPC/path inputs validated; untrusted links/popups/viewers denied or isolated; no renderer Node access, credential persistence, or secret-bearing URLs. |
| T19 | Safe shutdown | Cancel leaves the run untouched; Stop and quit waits for confirmed teardown; failures keep recovery available; a start cannot race the drain. |
| T20 | Crash/recovery | Renderer failure does not duplicate/resume gameplay; backend loss marks data stale; recovery keeps input held and honors watchdog/deadline ownership. |
| T21 | Packaged runtime | Installed build outside the checkout launches Python, keyring, Codex resources, static assets, and watchdog without global development tools. |
| T22 | Live bounded flow | Authorized supported-game exercise records real tuning/start/control/stop outcomes separately from mocks and source review. |

Baseline checks remain available through [the test script](<../scripts/test.ps1>):

```powershell
.\scripts\test.ps1
```

Do not present syntax checks, screenshots, or mocked model/worker results as proof of successful live account access, safe real takeover, automatic goal completion, or support for a new game.

## 13. Migration, rollout, and rollback

### 13.1 Preflight and backup

1. Require current games, provider operations, and desktop preparation to be stopped/reconciled before normal migration.
2. Ensure no existing controller/watchdog can concurrently write the source data being moved. Preserve any independent safety process until its owned work is resolved.
3. Identify controller data, managed Codex home, external manifests/builds, worker registrations/secrets, and Docker volumes. They are not all contained in one SQLite file.
4. Take a consistent backup. Use a cold copy after shutdown or SQLite's backup mechanism with the surrounding evidence accounted for; never copy only a live WAL database file and call that a complete backup.
5. Record backup location, source/destination roots, versions, and ownership mappings without exporting secrets into logs or source control.

### 13.2 Import and verification

1. Ask before importing existing development data into the desktop's user-data location. Leave original data untouched.
2. Apply schema/membership changes transactionally and record the migration version. Retrying a partial operation must be safe.
3. Preview the game/build grouping and preserve an unassigned fallback for ambiguous history.
4. Validate run/profile counts, IDs, frozen snapshots, ledger integrity, worker/account references, and external build paths.
5. Check OS-keyring access and managed Codex authentication; offer normal sign-in if the moved home cannot reuse its previous authenticated state. Never copy plaintext tokens as a workaround.
6. Verify the desktop reads the intended data root before enabling launches.
7. Keep old higher concurrency configuration as historical/migration information if useful, but make v1 admission fixed at one.

### 13.3 Rollout

- Use migrated copies/test fixtures first, then an authorized live workspace.
- Keep the prior package and verified backup available until the new package passes the bounded flow.
- Record source revision, package version, migration version, supported build identity, and test evidence for each acceptance run.
- Do not let old and new controllers operate different copies of metadata against the same live worker/account pool at once.

### 13.4 Rollback

- Stop/reconcile the new app's work before switching versions or data roots.
- Do not downgrade against a store whose compatibility is unknown.
- Preserve both the pre-upgrade backup and any new post-upgrade records. Do not overwrite newer evidence as part of a silent rollback.
- Restore a verified compatible backup only with an explicit decision about subsequent data; keep unsupported new records archived/readable rather than discarding them.
- Worker volumes, game saves, and Steam profiles are not reset merely because the desktop package is rolled back.

## 14. Risks and decisions to validate

The product scope is agreed. The following implementation details need evidence or a recorded choice; they are not reasons to silently expand scope.

| Item | Default direction | Validation / decision gate |
| --- | --- | --- |
| Electron version and packager | One maintained stable Electron release and one standard packaging tool; no custom updater. | P0/P1 packaged-runtime proof and reproducible dependency lock. |
| Bundled Python strategy | Private runtime with pinned current dependencies and required native/Codex resources. | Keyring, SDK subprocesses, watchdog, resource paths, and non-admin install must work before relying on it. |
| Backend readiness/auth/shutdown transport | Owned loopback controller with per-launch authentication and a graceful drain handshake. | Verify JSON, camera, external sign-in, same-origin boundary, port conflicts, and Windows shutdown behavior in P1. |
| Existing-data location and Codex home | Explicit import to user-data with a consistent backup; original retained. | P2/P5 migration fixtures plus keyring/reauthentication check. |
| Game identity in legacy history | Stable build/run references and explicit import grouping. | Ambiguous records remain unassigned; never infer from labels alone. |
| Desktop provisioning integration | Reuse current labelled-container/read-only-mount behavior; no generic deployment framework. | Confirm ownership and account-volume safety before enabling preparation in the wizard. |
| Human desktop viewer | Isolated noVNC viewer/context or approved external browser; no app preload bridge. | Preserve explicit handback and close/reconnect behavior with the real worker. |
| Broader game/runtime support | Existing compatible integration only in this release. | A second game or native Windows runtime is a separate approved plan with observation/action and execution acceptance. |
| Visual density and panel placement | Computer first, activity below, collapsible right inspector, project threads on left. | Review the working Electron UI at normal and narrow window sizes against the supplied Codex reference. |
| Signed installer/release distribution | Local packaged Windows acceptance first; no claim of signed/public distribution. | Decide signing/distribution requirements before distributing beyond local testing. |

## 15. Release definition of done

- [ ] ASHADBOT opens as a packaged Electron application, not a browser tab requiring a manual controller command.
- [ ] Projects represent games; each has multiple selectable agent configurations and nested saved instance threads.
- [ ] Projects **+** provides the complete, honest setup flow, including build/desktop readiness.
- [ ] A new instance accepts a goal and supported settings, freezes its inputs, and starts only on explicit confirmation.
- [ ] One globally occupied instance slot is enforced server-side, including pauses, takeover, and failed teardown.
- [ ] The central workspace displays the bot's computer with truthful connection/freshness states.
- [ ] Agent activity exposes actual observations, planning/action progression, recorded explanations, and outcomes without invented thinking.
- [ ] The inspector provides observed world state, recent activity, goals/evidence, and correctly labelled cost/token information.
- [ ] Existing pause/resume, takeover/handback, goal recalculation, stop, watchdog, stale-action, credential, and evidence safeguards remain intact.
- [ ] Ended instances remain readable without claiming that opening history resumes their world.
- [ ] Existing data migrates repeatably with a backup and without loss or rewriting of frozen execution evidence.
- [ ] Quit and crash behavior do not create duplicate or silently unattended runs.
- [ ] Automated, visual/keyboard, packaged-runtime, and authorized bounded live acceptance are recorded separately and accurately.
- [ ] Current documentation reflects delivered behavior, and remaining game/runtime limitations are explicit.

## 16. Execution and decision log

Use this section as the durable handoff. For each completed slice, record what changed, the exact checks performed, outcomes, and remaining limitations. A checked task without verification evidence is not complete.

### Initial planning record

- Source review confirmed substantial reusable functionality; no application rewrite is planned.
- The user agreed to the Electron/project/thread direction and requested this detailed reference plan.
- This planning change creates documentation only. It does not implement the milestones, migrate data, install dependencies, run inference, or launch a game.
- No new live acceptance claim is made by this document.

### Milestone register

| Milestone | Status | Change / verification reference | Remaining limitations |
| --- | --- | --- | --- |
| P0 — Baseline and packaging investigation | Not started | — | Baseline and executable packaging proof required. |
| P1 — Electron shell | Not started | — | Desktop lifecycle/security/packaging not implemented. |
| P2 — Projects and admission | Not started | — | Project records/migration and fixed global admission not implemented. |
| P3 — Navigation and setup | Not started | — | Project sidebar and integrated setup not implemented. |
| P4 — Instance workspace and inspector | Not started | — | Codex-style composition and richer telemetry presentation not implemented. |
| P5 — Packaged acceptance and handoff | Not started | — | No packaged or live acceptance under this plan. |

### Decision entry template

```text
Decision ID / affected milestone:
Problem and evidence:
Chosen approach:
Alternatives intentionally deferred:
Compatibility / data / security impact:
Verification performed and outcome:
Remaining limitation or follow-up:
```

Update this plan when a scope or ownership decision changes. Do not silently reinterpret “instance,” “ready,” “thinking,” “cost,” or “world state” in a later implementation.
