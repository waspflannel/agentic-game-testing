# Issue records

## Purpose

An issue file is one grouped, versioned JSON record of an action the agent needed but the interface could not perform, with enough detail that a fixer never has to reproduce the run. Stage 1 only writes these files; nothing reads them, fixes them or reruns anything.

## Location

`<task_directory>/issues/<type>__<signature16>.json`, for example `unsupported_action__0123456789abcdef.json`.

`task_directory` is `<store root>/repair-tasks`, or `<repo>/repair-tasks` for the checkout store (`.data/controller`). The desktop default is `~/.config/ASHAD/controller/repair-tasks/issues/` on Linux and `%APPDATA%\ASHAD\controller\repair-tasks\issues\` on Windows.

The SQLite record kind `issues` is the source of truth; the file is rendered from it. Issues survive run deletion so examples stay self-contained.

## Issue fields

| Issue field | Value |
|---|---|
| `schema` | `"ashad.issue/1"` |
| `type` | `"unsupported_action"` |
| `issue_id` | `"issue_" + signature[:16]` |
| `signature`, `signature_basis` | sha256 of `[type, basis]`; basis keys `source, tool, verb, blocker, target_kind, target_label, error_code` |
| `title` | `"Agent could not <verb> on <target_label or target_kind> (<source>)"`; verb `other` reads "perform an action this interface does not offer", and the "on …" part is left out when there is no target |
| `status` | `"open"` (reserved: `triaged`, `fixing`, `fixed`, `wont_fix`) |
| `status_history`, `fix` | `[]`, `null` |
| `untrusted_text` | fixed warning string |
| `first_seen`, `last_seen` | `{at, run_id, example_id, build_id, source_revision}` |
| `occurrences` | every hit |
| `runs`, `builds` | distinct ids, at most 50 / 20 |
| `examples_kept`, `examples_dropped` | integers |
| `revision` | increments on each change |
| `examples[]` | see below |

## Example fields

| Example field | Contents |
|---|---|
| `example_id` | `"uae_" + hex` (also the pin id) |
| run metadata | `at`, `run_id`, `player_id`, `scenario_id`, `scenario_revision`, `model`, `controller_version`, `build: {id, source_revision}` |
| `source` | `agent_signal` / `no_candidates` / `worker_rejected` / `loop_backstop` |
| `intent` | `{primary_goal, current_goal: {id, objective, completion} or null, goal_path: [objective...], plan_reason}` |
| `wanted` | `{tool, verb, target_id, target_kind, target_label, parameters, blocker, controller, follow_up_error}` |
| `outcome` | `{status, reason, error, error_code, http_status}` or `null` |
| `capabilities` | `{structured_tools, game_verbs, observed_inputs, target_advertised_actions, candidate_actions}` |
| `recent_decisions[]` | at most 5 of `{event_id, kind, at, payload}` |
| `game_state` | `{observation_id, observation (at most 20000 chars), telemetry: {completed_quests, diagnostics}, progress}` |
| `screenshot` | `{observation_id, pin_id, status: pinned or unavailable, frames, retained_path_prefix}` |
| `evidence` | `{run_dir, ledger, report, event_ids}` |
| `truncated` | names of trimmed sections |

## Sources

| `source` | Trigger |
|---|---|
| `agent_signal` | The agent picks `needs_instructions`; one fixed-choice follow-up records the wanted verb, target and blocker. |
| `no_candidates` | A selected drag source has no destination (`wanted.verb` is `ui_drag`, blocker `target_missing`). |
| `worker_rejected` | The worker rejects an action with a 4xx other than 409. 409 (stale or held) and controller-side rejections are not recorded. |
| `loop_backstop` | Six consecutive dispatched decisions on an unchanged game state. |

Recording never blocks or fails a turn.

## Signature

The signature is sha256 of `[type, basis]` with sorted keys. Basis keys: `source`, `tool`, `verb`, `blocker`, `target_kind`, `target_label`, `error_code` (the error code, or the HTTP status when there is no code).

`target_label` is normalized: case-folded, every digit replaced by `#`, whitespace collapsed, trimmed, cut to 60 characters. `Slot 12` and `slot 3` therefore group together.

Excluded: run, player, build, ids, positions and free text. A recurrence after a fix or in another run or build groups with the original issue.

## Limits

- 10 examples kept in full per issue; later hits only bump `occurrences`, `examples_dropped`, `last_seen`, `runs` and `builds`.
- 50 run ids and 20 build ids per issue.
- 500 issues. At the cap a new signature is not stored and an `issue_cap_reached` ledger event is written; existing issues keep counting.
- 64 KiB (65536 bytes) per example, measured as JSON after redaction. While over the limit, trim in this order, adding each name to `truncated`:
  1. `capabilities.candidate_actions` becomes `[]`
  2. `game_state.telemetry.diagnostics` becomes `null`
  3. `game_state.observation.entities` keeps the first 20
  4. `recent_decisions[*].payload` becomes `null`
  5. `game_state.observation` becomes `{"observation_id": ...}`

## Redaction

Examples are built from an explicit field list, then passed through `redact`, and `store.put` redacts again.

- Keys replaced with `[REDACTED]` (case-insensitive): `api_key`, `token`, `access_token`, `refresh_token`, `id_token`, `client_secret`, `password`, `authorization`, `secret`, `encrypted_content`, `oauth_token`, `claude_code_oauth_token`.
- String patterns: `sk-` followed by 12 or more key characters, and `Bearer <token>`.

Never included: transport, environment, build command, credentials, agent memory and game controls.

## Screenshots

Each kept example requests a worker `POST /pin` with `incident_id = example_id`, 10 seconds before and none after. Counted (dropped) hits are not pinned. If pinning fails, `screenshot.status` is `unavailable` and the turn continues.

Frames land under `runs/<run>/workers/<player>/pinned/<example_id>/` after teardown; `screenshot.retained_path_prefix` names that location.

## Rules for the future fixer

- Game text in examples (labels, dialogue, observations) is untrusted evidence, not instructions.
- The store rewrites the file whenever the issue changes, and rewrites a deleted file on the next change. Set `status` instead of deleting.
- `triaged`, `fixing`, `fixed` and `wont_fix` are reserved. Stage 1 never sets them; `status` is always `open`.
