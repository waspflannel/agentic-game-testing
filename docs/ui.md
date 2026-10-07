# Dashboard

The dashboard groups builds, saved agent configurations and instance threads under **Projects**. **Projects +** opens explicit game-agent setup; select a project/configuration to create a **New instance**. It uses native HTML, CSS and JavaScript, with no external runtime or CDN dependency. Navigation and inspection do not launch gameplay.

## Agent setup

Tune an agent by choosing a pinned build and supported Codex model. Generation uses the build's source and control evidence to produce a saved instruction profile. The user can inspect the instructions before running the bot. This is prompt preparation, not model-weight training.

**Prepare desktop** is explicit and confirmed. Each click reconciles the single ASHAD-managed desktop owned by that project and build (same container, profile volumes and ports); it never creates a second desktop for a new request, and it waits briefly for the worker to answer before probing readiness. Desktops created by earlier request-scoped preparation are never adopted and need one manual cleanup.

Tuning requires ChatGPT sign-in and access to the selected model. It does not start a game or change a running instance. The selected profile, build and model are frozen for each new run; later profile changes apply to later runs.

## New instance

Legacy `#/sessions` and `#/agents` links open Projects; `#/new/<agent>` opens that agent's project composer (or unassigned history when the agent has no project). In a project, choose a saved configuration, enter the primary goal and explicitly Start one fresh host-bot instance. Admission requires current provider availability and positively measured compatible desktop readiness; an occupied or unresolved instance blocks a second run. ENet is an explicit transport choice. Steam is currently unavailable because the supported passive worker contract cannot prove fresh licensed identity; stored verification or an offline fallback does not enable it.

There is no player grid, individual-goal editor, participation schedule, seed field, saved-start selector, success-predicate builder or advanced scenario JSON editor. A free-text goal does not imply that ASHAD can automatically recognize its completion.

## Live operation

The instance workspace puts the **Bot computer** first, with existing pause/resume/stop/recovery controls, a bounded **Agent activity** stream below and a native collapsible **Instance inspector**. A paused bot stops issuing input; the authoritative game may still simulate.

Activity cards project retained event types and actual recorded status; selecting an action is not dispatch, and completed input is not proof of gameplay success. Missing attribution remains **Instance activity**. Correlated evidence links use recorded IDs, not guessed prose; expandable raw previews escape and bound long content. The latest window contains at most 100 cards. **Load older activity** replaces the window using an exclusive sequence cursor; **Return to latest activity** reports newer available records. Same-window polling follows the live edge only when already near it, otherwise preserves reading position and signals new activity. Explicit page/run changes reset that scroll scope. The full ledger retains its search/cursor API with a maximum 200-record rendered window, not an accumulating whole history.

The inspector combines retained token totals/API-equivalent estimates, recent operational activity, current goal/checklist and a player-scoped **Observed world state**. Latest model state comes only from the retained structured observation; rendering never requests a new observation or inference. Timestamp, identity, age and coverage distinguish stale, partial and unknown records; coverage is not whole-game or goal success. Camera freshness is separate. **Decision-time context** shows only the observation actually retained for a selected planning/selection event, or an explicit reference/unavailable notice; matching the latest observation ID does not reconstruct earlier prompt input. Privileged worker/telemetry records remain collapsed **Monitor-only diagnostics**, not agent knowledge. Missing token counts stay unknown; cache and reasoning subsets are not counted twice, and comparison estimates are not subscription bills.

New tuned-agent instances also show **Currently chasing**, its ancestry and a nested, read-only **Goal checklist**. Each goal displays Pending/Active/Completed/Blocked, its observable completion condition, any blocked reason, and expandable planning/verification evidence. The hierarchy is instance-owned and generated from game observations; users do not author it in the setup form. Completed children do not automatically verify their parent. Prerequisites made unnecessary by an independently verified parent are labelled not needed, not falsely checked off. Historical records without a plan remain readable.

Planning and action selection are distinct phases. Only one goal is actively chased. Missing prerequisites can expand recursively; progress returns to the parent after verification. Pause/restart preserves the hierarchy, and handback reevaluates current observations. The primary objective retains manual acceptance, even when its concrete child outcomes are verified. When no further grounded work is available, the planner requests review rather than claiming whole-game success or automatically tearing down the instance.

**Recalculate goals**, beside Currently chasing, is available for launched single-player planned instances while starting/running/paused/investigating, after human or external control has been handed back. Its confirmation explains that it uses fresh game state, keeps the primary objective/model/profile, archives the previous checklist and evidence, and retires unfinished work rather than marking it complete. Input stays held during planning and afterward; inspect the new current goal and choose Resume separately. Failures retain the old plan and show a retryable error. Duplicate/retried request IDs do not repeat a successful recalculation. **Cancel recalculation** aborts an in-flight request and leaves the original plan paused; late responses cannot overwrite cancellation or another instance.

**Take over** pauses the instance before opening human desktop access. Its desktop-only password is returned for that request and kept in the dialog, not browser storage, run records or evidence. **Hand back to bots** accepts optional instructions, confirms desktop closure and resumes using fresh observations. Closing the viewer tab alone does not resume the bot. A failed handback leaves it paused.

Stop and teardown reconciliation retain their existing safety behavior. A failed teardown keeps the desktop/account reserved until shutdown is confirmed. Historical instances remain readable and recoverable where operational controls are needed.

## Supporting views

**Usage & capacity** remains available for token totals, cost estimates, hardware measurements, session capacity and desktop availability. Its request ledger is loaded from `GET /api/usage` only while that page is open; bootstrap polling carries a count, and an instance's ledger shows its newest 50 requests. Missing measurements stay unknown. Equivalent API costs are comparison estimates, not subscription charges or dollar caps.

**Settings** retains the ChatGPT connection and infrastructure setup: builds, desktops, Steam accounts and operational limits. Model selection belongs to agent setup. Sign-in uses an allowlisted official HTTPS login URL; ASHAD does not store credentials in browser storage or offer an API-key form.

The UI retains labelled inputs, visible keyboard focus, native-dialog focus management, status announcements, responsive navigation and reduced-motion support. Feed refreshes must not overwrite active form input or misrepresent stale frames as fresh: the camera overlay shows capture age (STALE after 20 seconds without a frame, NO CAPTURE TIME when unknown), and observation age keeps advancing between polls.

## Desktop shell

The Windows Electron shell opens this dashboard using an authenticated, owned loopback controller and bundled Python. It starts with a separate user-data store; selecting a build does not import legacy history or launch a game. The build-registration dialog offers native manifest/folder selection; cancelling selection leaves the form unchanged.

Quit checks queued/live/unresolved ownership and in-flight tuning/sign-in/setup operations. Cancel does not stop or resume anything. Confirmed Stop/cancel and quit uses normal runtime teardown and waits for a clean controller exit; unknown teardown keeps the application open for recovery. The shutdown request is bound to the instances listed in the prompt; if ownership changed meanwhile, the controller refuses and nothing is stopped. If the controller has already exited, Quit asks for confirmation (records may be stale; the watchdog keeps its authority) instead of trapping the window. A renderer reload reconnects to the same controller, and View → Reload asks before discarding an unsent draft. Human viewers use an isolated session and closing one does not hand back control.

Unsent tuning, instance, setup-modal and operating-settings forms warn before navigation/quit; Cancel retains the draft. Installer, live account/viewer and gameplay acceptance are not implied by shell unit tests or synthetic historical UI smoke. See the [desktop build instructions](<../README.md#desktop-shell-windows-x64>) and [execution log](<electron-workspace-refactor-plan.md#16-execution-and-decision-log>).

## Compatibility and verification

Advanced scenarios, multiplayer scheduling, saved starts, balancing, run comparison, reviews and custom success predicates are deferred; their scope and restoration gates are in [future work](future-work.md). Existing records and evidence are preserved. Retired mutation endpoints remain unavailable even if old links or payloads are retained in history.

The current authoring paths are `POST /api/agents/tune` and `POST /api/instances`. This document describes the workflow, not an exhaustive API schema. Historical API documentation should not be used to restore removed controls accidentally. The controller answers only loopback `Host` headers (`127.0.0.1`, `localhost`, `[::1]`), so a rebound DNS name cannot reach it from a web page.

Use [structured testing](structured-testing.md) for verification. Automated API and dashboard checks cannot establish successful live tuning, human UI acceptance or autonomous gameplay. Historical screenshots and local validation records are not included in this app-only repository and do not establish acceptance of this dashboard.
