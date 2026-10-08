# Dashboard API contract v1

The implementation is `ashad/app.py`; FastAPI `/openapi.json` lists every route. Plan/run schemas live in `ashad/models.py`.

## Boundary

- Every request (all methods, `/api`, `/mcp`, `/static`, `/`) must carry exactly one `Host` header naming `127.0.0.1`, `localhost` or `[::1]` (any port); anything else is `403`. This blocks DNS rebinding even when `Origin` matches `Host`. Desktop mode additionally requires its exact `127.0.0.1:<port>` authority and session key (`ashad/desktop.py`).
- `POST`/`PUT`/`PATCH`/`DELETE` require `X-ASHAD-Request: 1`, and any `Origin` must equal `Host` (`403` otherwise).
- While a workspace import is interrupted or the controller is draining, every mutation is `409`.
- Errors use `detail`. Invalid input is `422`; instance-slot conflicts are `409` with `code: "active_instance_conflict"` and `owning_instances`; provider errors are `409` (`configuration_required`) or `502` with `status`.
- No credentials in browser storage. Times are UTC Unix seconds; IDs are strings. Responses carry `Cache-Control: no-store` and a same-origin CSP.

## Bootstrap and usage

`GET /api/bootstrap` (polled) returns `{settings, ai, model_options:[{id,label}], builds:[], workers:[], accounts:[], agents:[], runs:[], usage_count, leases:[], projects:[], instance_slot:{available,owners,limit:1,blocked_reason}, workspace_import:{interrupted,completed,readiness_verified:false}}`. `settings` adds `saved_concurrent_sessions`, `concurrent_sessions:1`, `max_active_instances:1` and `hardware:{cpu_percent,memory_percent,memory_available,disk_free,gpu:null}`. `ai` = `{configured,status,model,api:"codex",auth_method:"chatgpt",billing:"subscription",message,plan_type?,available_models?}`; only `status=ready` confirms model availability.

`GET /api/usage` returns `{usage:[all rows]}` for the Usage page only; bootstrap carries just `usage_count`. Usage rows record `api,auth_method,billing,estimated_cost_usd,model,purpose,status,run_id?` and optional token usage/thread/turn IDs. Subscription usage has no dollar price (null means not applicable); historical metered rows keep their recorded cost.

`GET /api/health` returns `{status,model,api,local_only:true}`.

## Settings and ChatGPT sign-in

- `POST /api/settings {concurrent_sessions?:1, storage_reserve_bytes?:int>=0, duration_seconds?:finite>0|null, spend_usd?:finite>0|null}`. Booleans, NaN and Infinity are `422`; nothing is persisted on rejection.
- `GET /api/settings/codex/status` returns the `ai` object; it checks account/model availability without inference.
- `POST /api/settings/codex/login {}` returns `{auth_url,login_id}`; `/login/cancel {}` and `/logout {}` return status. Login/logout are `422` while tuning, while any run is Queued/Starting/Running/Stopping, while a goal recalculation is planning (even on a Paused run), or while agent inference tasks are pending. API-key routes do not exist.

## Projects, builds and agents

- `POST /api/projects {name, request_id}` creates (idempotent per `request_id`). `GET /api/projects/{id}` returns the project plus its `builds`, `agents`, `runs`.
- `GET /api/projects/{id}/readiness?build_id=&agent_id=` returns `{project_id,build_id,provider,build_validation,environments:[{id,name,ready,reason,checks,steam}],slot,steam,ready}` from fresh probes.
- `POST /api/projects/{id}/environments/prepare {build_id,request_id,confirmed:true}` prepares one desktop (images/volumes/credentials) under the process lock and drain; never starts a game.
- `POST /api/projects/migration/preview {groups}` returns `{version,projects,assignments,unassigned,blockers,preview_token,...}`. `POST /api/projects/migration/apply {groups,preview_token,backup_dir(absolute),confirmed:true}` applies it once (replay returns the same result).
- `POST /api/workspace/import {source_root,backup_dir,external_inventory?,confirmed,source_idle_confirmed,external_backup_confirmed}` (absolute paths; owned process lock required) imports history. Imported sign-in is never trusted; `409` when refused.
- `POST /api/builds {manifest_path(absolute), project_id, name?, id?}` imports a verified manifest. Membership comes only from the request (a manifest's own `project_id` is ignored); unreadable manifests are `422`. `GET /api/builds/{id}/catalog` returns the pinned catalog.
- `POST /api/agents/tune {build_id, model?, project_id?, name?, request_id}` tunes one agent at a time; a completed `request_id` replays without inference. Cancelling via desktop quit returns `409`. `GET /api/agents/tuning/{request_id}` returns `{status:"completed"|"in_progress"|"unknown", request_id, agent?, progress?}`. While in progress, `progress` is `{stage:"reading_source"|"source_notes"|"instructions", batch, batch_count, started_at, model_calls, rejected_replies, last_call_at}` (null between tunes); `batch_count` is 0 when the source fits one call. Model calls come from that tuning's usage rows; a rejected reply is one that failed validation and was repaired or retried.

## Instances (one global slot)

- `POST /api/instances {agent_id, project_id?, primary_goal, transport:"enet"|"steam", request_id}` returns the run, or `409` with the owning instance.
- `GET /api/instances/requests/{request_id}` recovers a lost reply: `{status:"accepted",run}` once launched, otherwise `"in_progress"`, `"prepared"` (with the original `request`) or `"unknown"`.
- `GET /api/runs/{id}` returns the run plus `external_control`, `event_states`, `events` (newest 200), `events_page:{next,before,total,has_more,older_count,newer_count,recent_100_older_count}`, `pause` when Paused/Investigating, and that run's `incidents` and `usage`.
- `GET /api/runs/{id}/events?after=&limit<=1000&search=&before=&tail=` returns `{events,next,before,total,has_more,older_count,newer_count}`; use `after` for newer pages or `before`/`tail` for older ones, not both.
- `POST /api/runs/{id}/control {command:"pause"|"resume"|"stop"|"terminate"|"extend"|"manual_event"|"goal", command_id, reason?, seconds?, event_id?, player_id?, goal?}`. The run ID comes only from the URL; `takeover`/`takeover_finish` are rejected here. A Resume reason becomes the recovery instruction until the next Resume.
- `POST /api/runs/{id}/recalculate-goals {command_id}` pauses, replans from fresh state and requires an explicit Resume.
- `POST /api/runs/{id}/flag {description, expected?, players?}`; `POST /api/runs/{id}/reconcile {}` retries teardown and releases reservations only after confirmation.
- `GET /api/runs/{id}/report` `{markdown}`; `/evidence` `{files,gaps,status}`; `POST /evidence/collect {}`; `GET /files/{path}` serves only files inside an existing run's evidence directory (`404` otherwise).
- `GET /api/runs/{id}/players/{player}/frame` JPEG or `503`; `/live?epoch=` multipart MJPEG for the current binding (`409` for an earlier epoch); `/context` structured observation.
- `POST /api/runs/{id}/players/{player}/mcp-session {control?:bool}` returns `{token,expires_in_seconds:60,control,path:"/mcp/"}`; `/mcp` requires `Authorization: Bearer <token>` (`401` otherwise).

## Human takeover

- `POST /api/runs/{run_id}/players/{player_id}/takeover {}` pauses every bot, cancels pending inference and opens the player's desktop viewer. Returns `{desktop_url,password,run}`; the desktop-only password is returned only here and never stored with run/evidence data.
- `POST /api/runs/{run_id}/takeover/finish {reason?:"",resume?:true}` closes access and resumes by default; the worker must confirm closure before any bot resumes. `run.human_control` describes active takeover without a password; closing a tab never resumes bots.

## Catalog: workers and Steam accounts

- `POST /api/workers {id?, name?, url, token?, desktop_url?, host_address?, enabled?:bool}`. `token` is write-only (OS credential store). A token is required for a new worker or when the URL's scheme/host/port changes; a stored token is never sent to a different endpoint. Updating a leased worker or one referenced by a Queued/active run is `422`.
- `POST /api/workers/{id}/probe {}`, `/steam/start {}`, `/steam/finish {}` (both require an unleased worker), `/steam/status {}`, `/steam/verify {account_id,build_id}`. Verification launches the game, so it requires the free global instance slot (`409` otherwise) and an unleased worker.
- `POST /api/accounts {id?, name, worker_id, steam_id(digits), app_id:int (not 480)}`; readiness is only set by worker verification.
- `DELETE /api/{builds|workers|accounts}/{id}` is `422` while leased or referenced by a Queued/active run; files are preserved.

Retired routes (`/api/plan`, `/validate`, `/scenarios`, `/launch`, `/fixtures`, `/compare`, `/runs/{id}/rerun`, `/runs/{id}/review`, `/runs/{id}/fixtures`, `/incidents/{id}/review`, `/findings/{id}/review`, `/api/settings/key`) return `404`/`405`. See [current setup and recovery](../README.md).
