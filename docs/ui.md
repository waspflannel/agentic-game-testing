# Dashboard

The dashboard centers on two actions: **Tune agent**, then **New instance**. It uses native HTML, CSS and JavaScript, with no external runtime or CDN dependency. Live game views, readable status and recovery controls take priority over configuration breadth.

## Agent setup

Tune an agent by choosing a pinned build and supported Codex model. Generation uses the build's source and control evidence to produce a saved instruction profile. The user can inspect the instructions before running the bot. This is prompt preparation, not model-weight training.

Tuning requires ChatGPT sign-in and access to the selected model. It does not start a game or change a running instance. The selected profile, build and model are frozen for each new run; later profile changes apply to later runs.

## New instance

Choose an agent, enter the primary goal, and select ENet or Steam. ASHAD starts one host bot with fresh world and character setup and assigns a compatible available desktop automatically. Steam requires a verified account with access to the pinned game's App ID.

There is no player grid, individual-goal editor, participation schedule, seed field, saved-start selector, success-predicate builder or advanced scenario JSON editor. A free-text goal does not imply that ASHAD can automatically recognize its completion.

## Live operation

Each launched instance exposes its live view, status, usage and controls to pause, resume, stop and recover. Observation freshness, pending actions and errors remain visible. A paused bot stops issuing input; the authoritative game may still simulate.

New tuned-agent instances also show **Currently chasing**, its ancestry and a nested, read-only **Goal checklist**. Each goal displays Pending/Active/Completed/Blocked, its observable completion condition, any blocked reason, and expandable planning/verification evidence. The hierarchy is instance-owned and generated from game observations; users do not author it in the setup form. Completed children do not automatically verify their parent. Prerequisites made unnecessary by an independently verified parent are labelled not needed, not falsely checked off. Historical records without a plan remain readable.

Planning and action selection are distinct phases. Only one goal is actively chased. Missing prerequisites can expand recursively; progress returns to the parent after verification. Pause/restart preserves the hierarchy, and handback reevaluates current observations. The primary objective retains manual acceptance, even when its concrete child outcomes are verified. When no further grounded work is available, the planner requests review rather than claiming whole-game success or automatically tearing down the instance.

**Recalculate goals**, beside Currently chasing, is available for launched single-player planned instances while starting/running/paused/investigating, after human or external control has been handed back. Its confirmation explains that it uses fresh game state, keeps the primary objective/model/profile, archives the previous checklist and evidence, and retires unfinished work rather than marking it complete. Input stays held during planning and afterward; inspect the new current goal and choose Resume separately. Failures retain the old plan and show a retryable error. Duplicate/retried request IDs do not repeat a successful recalculation. **Cancel recalculation** aborts an in-flight request and leaves the original plan paused; late responses cannot overwrite cancellation or another instance.

**Take over** pauses the instance before opening human desktop access. Its desktop-only password is returned for that request and kept in the dialog, not browser storage, run records or evidence. **Hand back to bots** accepts optional instructions, confirms desktop closure and resumes using fresh observations. Closing the viewer tab alone does not resume the bot. A failed handback leaves it paused.

Stop and teardown reconciliation retain their existing safety behavior. A failed teardown keeps the desktop/account reserved until shutdown is confirmed. Historical instances remain readable and recoverable where operational controls are needed.

## Supporting views

**Usage & capacity** remains available for token totals, cost estimates, hardware measurements, session capacity and desktop availability. Missing measurements stay unknown. Equivalent API costs are comparison estimates, not subscription charges or dollar caps.

**Settings** retains the ChatGPT connection and infrastructure setup: builds, desktops, Steam accounts and operational limits. Model selection belongs to agent setup. Sign-in uses an allowlisted official HTTPS login URL; ASHAD does not store credentials in browser storage or offer an API-key form.

The UI retains labelled inputs, visible keyboard focus, native-dialog focus management, status announcements, responsive navigation and reduced-motion support. Feed refreshes must not overwrite active form input or misrepresent stale frames as fresh.

## Desktop shell

The Windows Electron shell opens this dashboard using an authenticated, owned loopback controller and bundled Python. It starts with a separate user-data store; selecting a build does not import legacy history or launch a game. The build-registration dialog offers native manifest/folder selection; cancelling selection leaves the form unchanged.

Quit checks queued/live/unresolved ownership and in-flight tuning/sign-in/setup operations. Cancel does not stop or resume anything. Confirmed Stop/cancel and quit uses normal runtime teardown and waits for a clean controller exit; unknown teardown keeps the application open for recovery. A renderer reload reconnects to the same controller. Human viewers use an isolated session and closing one does not hand back control.

Unsent tuning, instance and operating-settings forms warn before navigation/quit. Integrated setup-modal draft handling belongs to the remaining workspace refactor. Installer, live account/viewer and gameplay acceptance are not implied by shell unit tests. See the [desktop build instructions](<../README.md#desktop-shell-windows-x64>) and [execution log](<electron-workspace-refactor-plan.md#16-execution-and-decision-log>).

## Compatibility and verification

Advanced scenarios, multiplayer scheduling, saved starts, balancing, run comparison, reviews and custom success predicates are deferred; their scope and restoration gates are in [future work](future-work.md). Existing records and evidence are preserved. Retired mutation endpoints remain unavailable even if old links or payloads are retained in history.

The current authoring paths are `POST /api/agents/tune` and `POST /api/instances`. This document describes the workflow, not an exhaustive API schema. Historical API documentation should not be used to restore removed controls accidentally.

Use [structured testing](structured-testing.md) for verification. Automated API and dashboard checks cannot establish successful live tuning, human UI acceptance or autonomous gameplay. Historical screenshots and local validation records are not included in this app-only repository and do not establish acceptance of this dashboard.
