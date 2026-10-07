# Independent player desktops

ASHAD uses one persistent Docker Linux container per player environment. Each container has its own Xvfb display, Openbox window manager, game process, X11 keyboard and mouse state, screen capture, run save directories, and Steam home directory. Four containers therefore provide four simultaneously controllable desktops. They share a private Docker network so an ENet host is reachable by its `ashad-worker-N` name. A run reserves its planned peak player count even when some players join later.

The coordinator runs locally on Windows. Workers expose the versioned `ashad-worker/1` HTTP contract behind a per-worker bearer token. The same contract supports future private remote workers; this implementation provisions only local containers. Local HTTP and noVNC ports bind to `127.0.0.1`. Remote placement requires a trusted private transport or TLS proxy and equivalent secret handling; do not expose these ports publicly.

## Provision and update

Docker Desktop must be running in Linux container mode. Supply a compatible pinned Agronauts artifact separately; game source, builds and Godot AI-layer edits are not part of this app repository. Then run from `C:\ASHADBOT` (use `-BuildPath` to select your external artifact directory):

```powershell
.\scripts\provision-workers.ps1 -Count 4
```

The script creates only `ashad-worker-1` through `ashad-worker-4`, the `ashad-private` network, and corresponding ASHAD named volumes. Existing unrelated containers are untouched. It refuses to take over any matching container without the `ashad.managed=true` label. Repeating the command preserves existing containers, run evidence, saves, and Steam authentication. The supplied `-BuildPath` directory (default `builds\agronauts-decisions`) is mounted read-only at `/game`; provisioning does not download or build the game.

Each default desktop uses 1280×720 Xvfb, Mesa software OpenGL, a three-CPU ceiling, 4 GiB memory ceiling, and 1 GiB shared memory. These are configured ceilings, not a claim that four full gameplay clients meet a particular performance target. Initial four-menu measurement was approximately 33–38 game FPS. Gameplay and concurrent world loads require separate measurement. Hardware GPU rendering is not configured by this portable local profile. The worker reports measured cgroup CPU/memory and game RSS when available; GPU memory remains unknown.

After changing worker code, rebuild and replace *idle* managed containers without deleting persistent volumes:

```powershell
.\scripts\provision-workers.ps1 -Count 4 -Recreate
```

Desktop setup in the app (**Prepare desktop**) runs this script with Windows PowerShell 5.1 for one desktop per project and build. Each new request for the same project/build reconciles that desktop rather than creating another; completed request IDs replay read-only. Preparation ignores inherited `DOCKER_*` variables, kills the whole process tree on timeout, and waits up to 30 seconds for the worker to answer before its passive readiness probe. Desktops from earlier request-scoped preparation are never adopted.

`-Recreate` checks worker state and refuses active games, leases, and human setup sessions. `-SkipBuild` reuses the already built image. The script writes `.data\workers.local.json` with connection details and paths to credentials. Worker tokens and separate VNC passwords are generated under `.data\worker-secrets`; ACLs restrict that directory to the current Windows account and SYSTEM. The backend imports service tokens and desktop-only passwords into its credential store; desktop passwords use `desktop:<worker_id>`. Never paste these files into a scenario, agent context, evidence, or source control.

Default endpoints:

| Environment | Worker API | Human setup desktop | Private ENet address |
|---|---|---|---|
| worker-1 | `http://127.0.0.1:8811` | `http://127.0.0.1:6081/vnc.html` | `ashad-worker-1` |
| worker-2 | `http://127.0.0.1:8812` | `http://127.0.0.1:6082/vnc.html` | `ashad-worker-2` |
| worker-3 | `http://127.0.0.1:8813` | `http://127.0.0.1:6083/vnc.html` | `ashad-worker-3` |
| worker-4 | `http://127.0.0.1:8814` | `http://127.0.0.1:6084/vnc.html` | `ashad-worker-4` |

## Steam account onboarding

An account belongs to one persistent desktop. Create ordinary Steam test accounts and grant each selected account access to the real Agronauts App ID. The implementation does not create accounts, purchase licenses, or store account passwords centrally.

1. Select an idle worker in the dashboard account setup flow and start Steam setup. The worker opens the official Steam client from the Debian Steam installer, starts password-protected noVNC, and disables all screenshot/video capture and bot input.
2. Open that worker's human setup desktop. Read its local VNC password from the `vnc_password_file` referenced by `.data\workers.local.json`; that password only controls the desktop connection. Sign in inside the official Steam UI and complete Steam Guard. Passwords and Guard codes go only to Steam.
3. Enter the account's Steam ID and selected real game App ID in the account record. Verify uses a short normal game launch at its main menu with capture disabled. Fresh, read-only game telemetry must confirm Steam authentication, the expected account ID, the actual App ID, and entitlement to that app. A missing account, wrong account, mismatched App ID, missing entitlement, or fallback cannot pass. App ID 480 is rejected.
4. Finish setup to close the human desktop connection. The Steam client and its persistent `/home/player` authentication state remain available to later runs. The account pool can reserve the verified identity for one run, including late joins and rejoins.

The authentication probe does not host a world, perform gameplay, call a model, or record the login screen. Its private temporary data is outside evidence exports. Steam may need to download its current official client on first use. Account sign-in, Steam Guard, game entitlement, and live Steam multiplayer verification remain human/credential dependent.

The authenticated game main menu is capturable before a multiplayer peer exists. Run readiness separately requires the actual Steam peer, connection/synchronization, expected identities and world/lobby. `requested_transport=steam` alone never establishes successful Steam multiplayer. If Steam authentication later becomes unavailable, capture and unsafe actions stop and the coordinator reports setup/infrastructure state.

## Ownership, input, and deadlines

For manual gameplay recovery, **Take over** on a player's live view pauses all bots in that instance before starting the selected worker's password-protected noVNC desktop. Copy the desktop-only password from the dialog and open the viewer. This is separate from Steam onboarding: the existing game stays open, and normal observation/evidence capture may continue. Only one player per instance can be taken over at a time.

Worker `POST /desktop/start {epoch}` and `POST /desktop/finish {epoch}` use the existing authenticated binding and explicitly acknowledge `human_control:true` or `false`. **Hand back to bots** closes viewer access before resuming fresh bot input; optional instructions describe the user's intervention. A failed or ambiguous close leaves bots paused. The user can close desktop access and leave the instance paused. Closing the browser tab alone never starts autopilot.

Human takeover cancels an existing investigation deadline and enters a manual pause; it does not reset the run's duration or storage limits. Keep the controller running during manual recovery. The credential appears only in the explicit dashboard takeover response, not in desktop URLs or persisted run/ledger records.

`POST /bind` reserves a `(run_id, player_id, epoch)` tuple and identity. A different run cannot replace it until teardown is confirmed. All bot commands use `POST /game` and the authenticated loopback game bridge. Actions reference a current observation and target version, have a bounded expiry, and reject stale ownership or paused input. The game retains action IDs for deduplication; ambiguous delivery must be reconciled with `get_action_status` before another action.

The game executor applies normal gameplay input and UI actions. Pause holds the game executor and invalidates prior observations; resume requires fresh game context. The old desktop `/action` API and screenshot-based inference are removed. Desktop capture is retained for human viewing and evidence. Steam login and graceful window closure still use the worker desktop.

If a worker watchdog tick itself fails, a running instance is paused with `watchdog_tick_failed`; like other infrastructure faults, it must be stopped and reconciled rather than resumed. The coordinator sends heartbeats at least every five seconds. The default worker watchdog pauses and releases inputs after 15 seconds without a heartbeat. A binding can configure this between 5 and 120 seconds. Source-observation age is configurable from 1 to 60 seconds to account for model latency; old input never executes merely because inference was slow.

Investigation and optional run deadlines are stored in `worker-state.json`. A separate watchdog process enforces those persisted deadlines even if the HTTP event loop stalls. It verifies process ownership using the run environment before terminating the process group. It also releases expired held controls independently of the HTTP service. A worker restart releases keys and reconciles/terminates its prior game rather than silently authorizing the old controller.

Stop attempts an ordinary window-close request and waits at most five seconds, then uses bounded process termination. Terminate skips graceful exit. The result distinguishes teardown confirmation from save status: save completion is reported unverified unless independent game evidence establishes it. Disconnect uses the same teardown but retains the binding, identity, and save tree for a normal rejoin launch.

## Saved starts and evidence

Run data is namespaced `/data/runs/<run_id>/game`; the game keeps its in-game world IDs inside that run root. Fresh launches receive isolated XDG settings paths and an explicit persistent local UUID or Steam ID. `/home/player` holds reusable Steam state and is never included in fixture/evidence export.

Fixture export requires a stopped game and a manifest containing world ID, build/save version, starting milestones, and stable player identity mapping. Import only writes an empty bound run directory and rejects traversal, symlinks, oversized archives, changed accounts, and transport changes. A cross-transport or changed-account package must first be explicitly converted and validated for that game/save version; merely copying a world does not qualify.

The worker appends action, observation, and lifecycle records to UTF-8 JSONL during execution. Video uses five-second MPEG-TS segments in a bounded rolling window (default 120 seconds at 8 FPS). Incident pins copy available pre-event frames/segments and collect subsequent segments until the requested post-event window closes. Pinned material is retained independently from the rolling buffer. Early termination records a shortened post-event window; unavailable video is labeled missing rather than synthesized. Capture and game logs are available through the authenticated evidence endpoint. Private Steam data and arbitrary paths are excluded.

The coordinator is responsible for the whole-run ledger/report, session-wide investigation state, and retained evidence index. The worker refuses capture when less than 100 MiB disk space remains, reports capture failure, and releases unsafe input. Per-run storage admission and configured limits belong to the coordinator.

## Verification

```powershell
.\scripts\test.ps1
```

Godot adapter checks require the separately maintained game checkout and its test runner; neither is shipped in this app repository.

Code checks cover the authenticated game bridge, ownership and stale observations, bounded actions, controller loss, pause/resume, persisted deadlines, Steam identity/App ID gating, fixture and evidence isolation. Human desktop capture remains available. The removed screenshot executor and its legacy live smoke scripts are no longer supported; earlier receipts under `.artifacts` describe historical checks only. Gameplay instance testing is user-owned.
