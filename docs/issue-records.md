# Issue records

## Purpose

An issue file is one grouped, versioned JSON record of a problem the agent hit, with enough detail that a developer never has to reproduce the run. There are five types:

- `unsupported_action`: an action the agent needed but the interface could not perform.
- `crash`: the game or its environment died or hung.
- `game_bug`: the game contradicted itself, rendered wrongly or did not respond after an offered action.
- `planner_rejected`: the planner's proposed plan failed ASHAD's validators; logged on every attempt.
- `capability_gap`: a subgoal the agent gave up on: the planner could not plan it, it was stuck, or the planner reported it blocked; titled “MCP or game needs updating”.

The instance page reads them: each instance lists its own hits from its ledger. These types are only recorded; a planner rejection no longer pauses the run: after the corrective retry the subgoal is given up and a `capability_gap` is recorded. Each issue belongs to one project; a project's issues are listed by `GET /api/projects/{id}/issues`.

## Location

`<task_directory>/issues/<project_id>/<type>__<signature16>.json`; issues from instances without a project go to `issues/unassigned/`. For example `unsupported_action__0123456789abcdef.json`, `crash__0123456789abcdef.json` or `game_bug__0123456789abcdef.json` or `planner_rejected__0123456789abcdef.json`.

`task_directory` is `<store root>/repair-tasks`, or `<repo>/repair-tasks` for the checkout store (`.data/controller`). The desktop default is `~/.config/ASHAD/controller/repair-tasks/issues/<project_id>/` on Linux and `%APPDATA%\ASHAD\controller\repair-tasks\issues\<project_id>\` on Windows.

The SQLite record kind `issues` is the source of truth; the file is rendered from it. Issues survive run deletion so examples stay self-contained.

The store rewrites a file whenever its issue changes, and rewrites a deleted file on the next change; never delete issue files by hand.

## Issue fields

| Issue field | Value |
|---|---|
| `schema` | `"ashad.issue/2"` (the type-specific example sections are identified by `type`) |
| `project_id` | the instance's project, or `null` |
| `type` | `unsupported_action \| crash \| game_bug \| planner_rejected \| capability_gap` |
| `issue_id` | the record id: `issue_` + 16 hex; legacy records keep their v1 id |
| `signature`, `signature_basis` | sha256 of `[type, basis]`; basis keys per type are listed below |
| `title` | per type, see below |
| `status` | `open \| closed` |
| `status_history` | `[{at, from, to, by, run_id?, example_id?}]`, the last 50 changes; `by` is `user`, `recurrence` or `migration`; `[]` until the first change |
| `fix` | absent on new records; v1 records keep the removed fixer's block as history |
| `untrusted_text` | fixed warning string |
| `first_seen`, `last_seen` | `{at, run_id, example_id, build_id, source_revision}` |
| `occurrences` | every hit |
| `runs`, `builds` | distinct ids, at most 50 / 20 |
| `examples_kept`, `examples_dropped` | integers |
| `revision` | increments on each change |
| `examples[]` | see below |

Records written before save/load was removed had `save_before`; startup removes it.

Basis keys per type:

| Type | `signature_basis` keys |
|---|---|
| `unsupported_action` | `tool, verb, blocker, target_kind, target_label, error_code` |
| `crash` | `kind, detector, exit` |
| `game_bug` | `symptom, expected, observed, action_verb, target_kind, target_label` |
| `planner_rejected` | `reason, focus_objective, mode`; `focus_objective` is the focused goal's objective normalised like `target_label` (digits become `#`, case-folded, at most 60 chars); list indices in `reason`'s location become `#` (`subgoals.2.completion` → `subgoals.#.completion`) |
| `capability_gap` | `kind, cause, focus_objective, missing`; `cause` is `game_bug \| mcp_gap \| planner_gap`: `planner_gap` when the goal's check is `unknown` or the planner reported a plan problem, or when a stuck goal's measured progress or actions changed the observed state (the plan is wrong, not the game); `mcp_gap` when the agent was blocked or an action failed (missing action or observation support); `game_bug` when actions completed and the observed state did not change |

Titles per type:

| Type | `title` |
|---|---|
| `unsupported_action` | `"Agent could not <verb> on <target_label or target_kind>"`; verb `other` reads "perform an action this interface does not offer", and the "on ..." part is left out when there is no target |
| `crash` | `"<kind title> (<exit or detector>)"`, for example `Game process exited (signal:SIGSEGV)` or `Game worker was lost (worker_unreachable)`. Kind titles: `game_exit` "Game process exited", `game_hang` "Game stopped responding", `worker_lost` "Game worker was lost", `desktop_lost` "Game desktop was lost" |
| `game_bug` | `"Game bug (<symptom>): expected <expected>, observed <observed>"`, then `" on <target_label or target_kind>"` when there is a target (a `target_kind` of `none` counts as none), then `" after <action_verb>"` when there is an action |
| `planner_rejected` | `"Planner rejected: <reason> while planning “<focus_objective>”"` |
| `capability_gap` | `"MCP or game needs updating (<game bug, MCP gap or planner gap>): <missing> while working on “<focus_objective>”"` (`game bug`, `MCP gap` or `planner gap` for the matching `cause`) |

## Example fields

Every example has these common fields:

| Example field | Contents |
|---|---|
| `example_id` | prefix + hex (also the pin id): `uae_` for `unsupported_action`, `crash_` for `crash`, `bug_` for `game_bug`, `plan_` for `planner_rejected`, `gap_` for `capability_gap` |
| run metadata | `at`, `run_id`, `player_id`, `scenario_id`, `scenario_revision`, `model`, `controller_version`, `build: {id, source_revision}` |
| `source` | see Sources |
| `intent` | `{primary_goal, current_goal: {id, objective, completion} or null, goal_path: [objective...], plan_reason}` |
| `recent_decisions[]` | at most 5 of `{event_id, kind, at, payload}` |
| `game_state` | `{observation_id, observation (at most 20000 chars), telemetry: {completed_quests, diagnostics}, progress}` |
| `screenshot` | `{observation_id, pin_id, status: pinned or unavailable, frames, retained_path_prefix}` |
| `evidence` | `{run_dir, ledger, report, event_ids}` |
| `truncated` | names of trimmed sections |

On top of the common fields, each type adds its own sections:

| Type | Section | Contents |
|---|---|---|
| `unsupported_action` | `wanted` | `{tool, verb, target_id, target_kind, target_label, parameters, blocker, controller, follow_up_error}` |
| | `outcome` | `{status, reason, error, error_code, http_status}` or `null` |
| | `capabilities` | `{structured_tools, game_verbs, observed_inputs, target_advertised_actions, candidate_actions}` |
| `crash` | `crash` | `{kind, detector, reason (at most 2000 chars), exit_code, signal, log_tail, worker_status: {lifecycle, game_running, infrastructure_error, capture, controller_age_seconds, resources}}`; `exit_code`, `signal` and `log_tail` are `null` when the worker did not report a game exit |
| | `last_action` | `{event_id, at, verb, target_id, status}` of the player's last `structured_action_intent` within the last 120 events (`status` is the matching `action_result` status or `null`), or `null` |
| | `timing` | `{detected_at, launched_at, telemetry_received_at, feed_captured_at, simulation_advanced_at}` |
| `game_bug` | `report` | `{symptom, expected, observed, target_id, target_kind, target_label, target_state, related_action: {action_id, verb, target_id, parameters, status, intent_event_id, result_event_id} or null}` |
| `planner_rejected` | `planner` | `{source, attempt (1 or 2), reason (one line), mode, proposal (the raw model answer, or null when trimmed), goal: {primary_goal, focus: {id, objective, completion}, goal_path: [objective...]}, plan_revision}`; `screenshot.status` is always `unavailable` (no pin) |
| `capability_gap` | `gap` | `{kind, cause, missing, tried, subgoal: {id, objective, completion, goal_path}}` |
| | `capabilities` | `{structured_tools, game_verbs, observed_inputs, target_advertised_actions, candidate_actions}`; `screenshot` is pinned only when `gap.kind` is `stuck` |

A section that does not apply to a type is absent, not `null`.

## Sources

| `source` | Type | Trigger |
|---|---|---|
| `agent_signal` | `unsupported_action` | The agent picks `needs_instructions`; one fixed-choice follow-up records the wanted verb, target and blocker. |
| `no_candidates` | `unsupported_action` | A selected drag source has no destination (`wanted.verb` is `ui_drag`, blocker `target_missing`). |
| `worker_rejected` | `unsupported_action` | The worker rejects an action with a 4xx other than 409. 409 (stale or held) and controller-side rejections are not recorded. |
| `loop_backstop` | `unsupported_action` | Six consecutive dispatched decisions on an unchanged game state. |
| `agent_signal` | `game_bug` | The agent picks `report_game_bug`; one fixed-choice follow-up describes it. |
| `monitor` | `crash` | The coordinator's tick or monitor detects a crash, see Crash detectors. |
| `turn_planning` | `planner_rejected` | A proposal from turn planning fails validation (attempt 1 or 2). |
| `recalculation` | `planner_rejected` | A proposal during Recalculate goals fails validation (attempt 1 or 2); the focus is the new plan's root. |
| `agent` | `capability_gap` | The agent gives up on a subgoal after planning failure, stuck progress, or a blocked report from the planner. |

Recording never blocks or fails a turn, a tick or a monitor pass.

## Crash detectors

Crashes are detected in `Coordinator.tick` and `Coordinator.monitor` and recorded once per `(run, player, generation, kind)`. `resume` bumps the generation, so a new crash after a resume counts again. The worker `/status` carries `game_exit: {exit_code, signal, log_tail} | null`; an older worker sends no field and the crash fields are `null`. `exit` in the basis is `signal:<NAME>`, else `code:<n>`, else `null`.

| `kind` | `detector` | Trigger |
|---|---|---|
| `game_exit` | `worker_status` | The worker status says `game_running` is false and the worker did not restart. |
| `game_hang` | `simulation_stalled` | The freeze check in `monitor`: the simulation stopped advancing past `simulation_timeout_seconds`. |
| `game_hang` | `heartbeat_stalled` | The freeze check in `monitor`: telemetry stopped arriving past `simulation_timeout_seconds`. |
| `worker_lost` | `worker_unreachable` | A worker heartbeat or status read fails with no HTTP status (the worker did not answer). No `/pin` is requested; `screenshot.status` is `unavailable`. |
| `worker_lost` | `worker_restarted` | The worker status `infrastructure_error` starts with `worker_restarted`. |
| `desktop_lost` | `capture_unhealthy` | The worker status `infrastructure_error` is `capture_unhealthy`. |

Not a crash (these stay repair tasks or are not issues at all):

- Controller-side failures: `controller_failure`, `structured_interface_failure`, the telemetry monitoring step, `unknown_execution` and `teardown_failed`.
- Config and integrity failures: build hash mismatch, transport or identity, readiness and sync deadlines, `controller_heartbeat_lost`, `worker state corrupt`.

## Bug rules

The `report_game_bug` choice sits in its own candidate group, directly before `needs_instructions`, which stays last. If no offered action fits, the agent picks `needs_instructions` (`unsupported_action`); a game bug is for a game that contradicts itself after an offered action, renders wrongly or does not respond. `loop_backstop` stays `unsupported_action`.

One fixed-choice follow-up asks five questions, in this order:

1. `symptom`: `wrong_result`, `visual_glitch`, `broken_ui`, `soft_lock`, `not_a_bug`.
2. `expected`: `state_change`, `item_change`, `progress`, `ui_change`, `position_change`, `renders_correctly`, `other`.
3. `observed`: `no_change`, `wrong_change`, `reverted`, `visual_corruption`, `unresponsive`, `other`.
4. `related_action`: one of the player's last 5 action intents, or `none`.
5. `target`: one of the observed entities (at most 60), or `none`.

A report is dropped, with a `game_bug_report_rejected` ledger event and no issue, for one of these reasons:

| `reason` | When |
|---|---|
| `follow_up_failed` | The follow-up call failed or returned an invalid answer. |
| `not_a_bug` | The agent answered `not_a_bug` for `symptom`. |
| `no_completed_action` | `symptom` is `wrong_result` but there is no related action that completed. |

A related action counts as completed when an `action_result` event for its `action_id` has status `completed`, or a `structured_action_outcome` event whose result has that `action_id` and status `completed`. An outcome without an `action_id` never matches.

After a report, the offer is hidden for that player while the game state signature is unchanged. A new game state, a new generation (resume) or the end of the run makes it available again.

## Signature

Issues group by `(project_id, signature)`: the same signature in two projects is two issues. The signature is sha256 of `[type, basis]` with sorted keys. Basis keys by type are in the Issue fields section. For `unsupported_action`, `error_code` is the error code, or the HTTP status when there is no code. `source` is not part of the basis; it stays in each example, so the same missing action from two sources is one issue.

`target_label` is normalized: case-folded, every digit replaced by `#`, whitespace collapsed, trimmed, cut to 60 characters. `Slot 4` and `slot 7` therefore group together; each digit becomes its own `#`, so `slot 12` (`slot ##`) does not group with `slot 3` (`slot #`).

Excluded: run, player, build, ids, positions and free text. The last action of a crash stays in the examples, so different actions do not split one crash. A recurrence after a fix or in another run or build groups with the original issue.

## Limits

- 10 examples kept in full per issue; later hits only bump `occurrences`, `examples_dropped`, `last_seen`, `runs` and `builds`.
- 50 run ids and 20 build ids per issue.
- 500 issues per project, shared across all types. At the cap a new signature is not stored and an `issue_cap_reached` ledger event is written; existing issues keep counting.
- 64 KiB (65536 bytes) per example, measured as JSON after redaction. While over the limit, trim in this order, adding each name to `truncated`. A step whose section is absent from the example is skipped and not named.
  1. `capabilities.candidate_actions` becomes `[]`
  2. `game_state.telemetry.diagnostics` becomes `null`
  3. `game_state.observation.entities` keeps the first 20
  4. `recent_decisions[*].payload` becomes `null`
  5. `wanted.parameters` is trimmed
  6. `planner.proposal` becomes `null`
  7. `crash.log_tail` keeps its last 1024 characters
  8. `game_state.observation` becomes `{"observation_id": ...}`

## Ledger events

Each is written to the run ledger after the record is stored, never before, so the ledger does not claim a failed record. `evidence.event_ids` therefore does not contain the recorded event's id.

| Event | Payload |
|---|---|
| `unsupported_action_recorded`, `crash_recorded`, `game_bug_recorded`, `planner_rejected_recorded`, `capability_gap_recorded` | `{player_id, issue_id, example_id, source, kept, outcome}`; `outcome` is `kept` or `counted`, and nothing is written for a failed record |
| `capability_gap_recorded` (extra) | `goal_id`, the focused subgoal's id |
| `issue_reopened` | `{player_id, issue_id, example_id}` |
| `issue_cap_reached` | `{player_id, issue_type, signature}` |
| `worker_heartbeat_retry` | a worker heartbeat failed and is being retried |
| `host_suspended` | the host was suspended (for example sleep) while the run was active |
| `structured_action_outcome` | `result.effect` is `changed` or `no_effect` when the action completed (observed facts or player position changed or not), absent otherwise |
| `game_bug_report_rejected` | `{player_id, reason}` |

## Per-instance history

An instance's issues are its `<type>_recorded` ledger events grouped by `issue_id`. The newest 500 events are read and 20 hits are shown per issue. `GET /api/runs/{run_id}` returns them as `issues` and `issues_truncated`.

Deleting the instance removes its history, not the issue files.

## Close and reopen

`POST /api/issues/{issue_id}/status` with `{"status": "open" | "closed"}`. Setting the current status is a no-op. Other values return 422, an unknown id 404. The file is republished.

A hit on a closed issue reopens it (`by: "recurrence"`), always keeps the example (the oldest is dropped at 10) and pins a screenshot.

`GET /api/issues/{issue_id}` returns the record plus `file`.

## Run behavior

Logging only; the run is handled as before.

- A crash leaves the run as it was: Investigating, the 30-minute hold, then `investigation_expired`. `infrastructure_failure` invalidates the run; a freeze (`game_hang`) does not.
- A game bug never pauses the run. It does not replan or investigate; it is evidence, not a blocker.
- Per-run counters (loop counts, bug offers, recorded crashes) are cleared when the run finishes.

## Redaction

Examples are built from an explicit field list, then passed through `redact`, and `store.put` redacts again. The crash `log_tail` is redacted on the controller side only.

- Keys replaced with `[REDACTED]` (case-insensitive): `api_key`, `token`, `access_token`, `refresh_token`, `id_token`, `client_secret`, `password`, `authorization`, `secret`, `encrypted_content`, `oauth_token`, `claude_code_oauth_token`.
- String patterns: `sk-` followed by 12 or more key characters, and `Bearer <token>`.

Never included: transport, environment, build command, credentials, agent memory and game controls.

## Screenshots

Each kept example requests a worker `POST /pin` with `incident_id = example_id`, 10 seconds before and none after. Counted (dropped) hits are not pinned, and a `worker_lost` crash is never pinned because the worker is gone. The pin is the only moment-of-failure evidence. `GET /api/issues/{id}` adds `screenshot.url` (the last frame, or `null`) and `screenshot.note`: `retained`, `pending` (instance still running), `deleted` (instance deleted), `missing` (frames not retained) or `unavailable` (never pinned). If pinning fails, `screenshot.status` is `unavailable` and the turn continues.

Frames land under `runs/<run>/workers/<player>/pinned/<example_id>/` after teardown; `screenshot.retained_path_prefix` names that location.

## Migration

Files written before stage 2 for `unsupported_action` have a basis that includes `source`. New hits group under the new basis (without `source`), so one such stage-1 issue may stop growing while a new file starts for the same missing action.

Issues written as `ashad.issue/1` are migrated once at startup to `ashad.issue/2`:

- The project comes from the `first_seen` run, then any `runs[]` run, then any `builds[]` build, else `null`.
- `fixed` becomes `closed`, anything else `open`. A `by: "migration"` history entry is added only when the value changes.
- `fix` and ids are kept.
- Flat `issues/*.json` files named after a record are deleted after republishing; other files stay.

Deleting a project deletes its issue records and folder; deleting an instance keeps issues.
