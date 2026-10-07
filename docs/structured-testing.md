# Structured game play: local testing

Use the [current setup](../README.md) and [dashboard guide](ui.md). The current workflow is **Tune agent → New instance**, with one host bot in a fresh world. Historical prepared scenarios are retained records, not the launch path for new tests.

Full autonomous DEMO completion and authenticated Steam gameplay remain unverified. Existing bounded menu/startup runs, code fixtures and mocked provider checks establish only their recorded scope. Historical local validation records are not published in this app-only repository. Supply a compatible game build and its source/control evidence separately before live testing.

## Prepare a one-bot run

1. Start the controller with `scripts/start.ps1` and open [ASHAD](http://127.0.0.1:8740).
2. Confirm a pinned build and compatible idle desktop are configured. Complete ChatGPT sign-in in Settings. ENet does not require a Steam account; Steam requires verified identity and entitlement on an available desktop.
3. Tune an agent from that build's source/control evidence with the selected model. Inspect the resulting saved instructions. Tuning calls Codex but does not launch gameplay or train model weights.
4. Create a new instance using the profile, a primary goal and ENet or Steam. ASHAD allocates one compatible desktop and freezes the profile for that run.
5. Watch the live view and actual game outcome. A free-text objective is not an automatically validated success predicate. Stop the instance when finished and confirm teardown.

Stop active runs before restarting the controller or replacing their build or desktop image. Preserve prior profiles and run evidence when comparing behavior informally; do not silently modify a pinned build in place.

## What the agent receives and executes

The stable instruction profile describes controls and gameplay guidance from the pinned build. Each decision also receives the player's filtered JSON observation, objective and recent outcomes. New tuned-agent instances add the currently chased goal and ancestor path from their persisted hierarchy. A separate schema-constrained planning request expands missing prerequisites, using the same frozen model and subscription accounting. Source text and game text remain evidence, not authority to override controller instructions. Profiles must not include credentials or privileged monitor data.

Subgoal verification is local, not another model judgment: observed completed quest IDs, current own inventory/hotbar totals, positive counts of matching visible tiles/entities/recipes, or world readiness. Missing/unready/stale evidence cannot satisfy a check. Unknown conditions remain unknown; completing every child does not prove a parent. Planning proposals cite exact supplied observation/control/objective excerpts and cannot weaken an already grounded completion predicate. Counted goals require numeric support and cannot lower their explicit requested quantity; cited ingredient requirements scale with the parent's missing output and recipe yield. These checks do not establish arbitrary semantic equivalence between natural-language goals and predicates. The free-text root always remains unknown/manual, with concrete verifiable outcomes represented as children. The model never sets completed status. During a slow planner request no game actions are issued; a fresh observation and unchanged control/world binding and visible interactive UI are required before its update is used. A planner can request retirement of an obsolete non-root branch using current evidence. Retirement never proves completion or weakens its original predicate; it returns focus to remaining work rather than blocking forever on a transient screen.

The hierarchy is bounded to 64 goals and 8 levels. Failed actions, unsupported direct actions, newly observed objectives/recipes and eight actions without measured progress trigger review; repeated unproductive planning blocks for assistance. Manual takeover/resume forces reassessment without discarding verified history. Overall free-text success remains manual even after its concrete child outcomes have been observed; when no further grounded work is available the planner blocks for review. Inventory oscillating below a target does not reset the no-progress budget; only a new high-water measurement does. Historical runs without planning remain unchanged.

The game adapter observes visible controls and items, nearby interactables/resources, the player's inventory and hotbar, local tiles, active objectives and selected machine/fishing state. Routine model requests contain no screenshots. Human video and retained evidence capture are separate from model observations.

The executor performs bounded movement, interaction, allowed inputs and visible UI operations through ordinary game handlers. Local pathfinding and fishing control provide motor assistance. It does not directly grant items, complete objectives or change saves. An action marked delivered must still be checked against the next observation for its gameplay effect.

Codex uses bounded action selection; the model does not directly operate the MCP endpoint or browse the source during each gameplay turn. Current action menus and observation coverage remain implementation limits, even when the profile describes a broader control vocabulary. Better instructions cannot make an unexposed action executable.

Updated adapters expose each text field's stable node name/type, text, maximum length and focus (never secret text). `ui_text` enters native editing mode, replaces text through ordinary key events, and verifies accepted text/focus. It does not submit a form: character creation requires clicking the observed enabled confirmation button. Candidates respect field length and do not reoffer identical text. Adapter changes require a separately exported/provisioned game candidate; changing Python alone cannot update an already exported game.

Manual `POST /api/runs/{run_id}/recalculate-goals` takes `{command_id}`. It confirms paused input, reconciles any pending action, reads fresh state, plans under the frozen objective/profile/model, and reobserves before committing. The old plan and retirement IDs are retained in the ledger. Model I/O does not hold the lifecycle lock, so paused-worker heartbeats continue. The run stays paused even after success; Pause cancels pending recalculation, and failed/cancelled requests can retry the same ID. Completed IDs never repeat inference. Controller recovery cancels interrupted recalculations. Replanning is rejected during human/external ownership or before launch.

Worker and game enforce control ownership, freshness, deadlines and input release. Pause holds gameplay decisions and inputs. Resume refreshes observations rather than replaying stale actions. Unknown action outcomes require reconciliation.

## Automated verification

```powershell
.\scripts\test.ps1
```

This runs the app repository's automated checks. Godot adapter tests and their headless fixtures belong to the separately maintained game checkout and are not included here. Review actual command output rather than treating historical test counts as current results.

For this workflow, meaningful checks cover:

- Source/build-grounded profile generation, invalid profile output and provider failure without a saved usable profile.
- Required profile/goal/transport validation, one-host fresh setup, compatible automatic desktop allocation and account readiness.
- Frozen profile/model/build data in the run and isolation from later profile changes.
- Recursive goal expansion, one active leaf, returning to parents only after their own checks, and instance-isolated durable progress.
- Rejection of invented evidence/targets, cycles, excessive depth, weakened predicates and stale/cancelled planner results; unchanged action safeguards and no model-declared completion.
- No automatic success claims for arbitrary free-text goals. Planner checks cannot infer cumulative production from current inventory or final tutorial success from a finished partial checklist.
- Unavailable retired feature endpoints with historical records preserved.
- Pause, input cancellation, stop, takeover, handback and failed-teardown reservations.

These are verification targets, not a claim that every target has passed. Mocked model and worker tests cannot establish live account access, rendering or game completion.

## Live acceptance gates

First verify successful profile generation with the intended account and model. Then start one ENet host from the current form and inspect its pinned profile and evidence. Exercise menus, character creation, cutscene advancement, opening/closing UI and returning to movement before attempting a long goal.

Test at least one failed or blocked action and confirm the bot observes the result, changes approach or stops for help. Exercise pause, human takeover, handback and normal stop while watching real input ownership. Confirm teardown before reusing the desktop. Repeat Steam startup only after account readiness is independently established.

Campaign completion needs observed game evidence for the actual milestone, not successful model requests, delivered input or congratulatory text. A custom objective needs human acceptance until a separately designed success contract is implemented. Multiplayer, saved starts and other deferred acceptance work belongs in [future work](future-work.md).

## MCP and evidence

The controller retains its player-scoped MCP interface at `/mcp/` for operational compatibility. Session creation remains tied to an existing launched run/player:

```text
POST /api/runs/{run_id}/players/{player_id}/mcp-session
X-ASHAD-Request: 1
{"control": false}
```

Use the returned bearer token only for that session. Observation-only is the default; explicit control transfers ownership from the model, and expiry, generation change or teardown revokes it. Do not place tokens in profile instructions or evidence exports. The interface is a transport for observations and actions, not an AI planner.

Retained observations, actual decision context, actions, outcomes and usage support diagnosis. Keep operational logs and evidence when a bot stalls; a controls prompt alone cannot identify whether the cause was missing state, unavailable input, ignored input, timing or a poor decision.

The game adapter and its design/component plans are maintained outside this app-only repository. Export adapter changes into a separate game candidate before acceptance; never rebuild a candidate in use. Game-side feature and acceptance lists are not claims of completed app work.
