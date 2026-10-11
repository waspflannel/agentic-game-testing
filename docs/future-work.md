# Deferred work

ASHAD currently supports tuning a reusable agent profile and starting a fresh, one-bot host instance with a primary goal over ENet or Steam. This reduction keeps the next question testable: can the bot use its controls, understand the current state and make progress without getting trapped in ordinary UI?

Tuning creates instructions from pinned source/control evidence. It does not train weights or establish that the bot can complete the game. Each run freezes its profile and build; its model defaults to the profile's and can be changed within the same provider. Usage, capacity, infrastructure settings, live view, pause, stop, takeover and operational evidence remain in scope.

## Before restoring broader features

Establish a reproducible single-bot baseline on a pinned build. Demonstrate menu and character setup, cutscene/dialogue advancement, closing UI, resumed movement and at least one bounded gameplay objective. Record failed-action recovery and real takeover/handback behavior. Distinguish delivered input from confirmed effects.

Inspect failures against the actual profile, observation, available actions and execution outcome. Add missing control semantics or observation fields before adding another planning layer. Keep checks small and tied to a demonstrated failure; a long prompt cannot repair a missing executable action or an unobserved modal state.

Live acceptance must be recorded separately from mocked provider/worker checks and headless Godot fixtures. No autonomous DEMO or Steam gameplay acceptance is implied by the simplified workflow.

## Deferred capabilities and restoration gates

| Capability | Why it is deferred; what to restore | Prerequisites and test gate |
|---|---|---|
| Individual player goals and team assignments | One host needs one primary goal. Restore separate goals and coordination when multiple agents have a demonstrated purpose. | Establish one-bot progress first; define shared versus private observations, conflict handling and goal ownership. Verify no private-state leakage or contradictory assignment overwrites. |
| Multiplayer setup and scheduling | Player grids, join triggers, dependencies, disconnect/rejoin and host rotation multiply lifecycle states before basic play is reliable. Restore explicit participation and timing when testing those game behaviors. | Independent desktops/accounts, authoritative readiness and known world identity. Exercise late joins, failed sync, capacity reservations, pause/resume, overdue events and reconnects without inventing host migration. |
| Saved starts, continuation and save conversion | Fresh setup avoids ambiguity about inherited world progress and character state. Restore packages, mappings and continuation for targeted tests. | Versioned immutable archives, integrity checks, compatible build/save revisions and explicit identity mappings. Verify fresh versus restored characters, source-package isolation, failed staging and transport changes. |
| Seed selection and repeatability controls | A seed field suggests stronger reproducibility than a live game necessarily provides. Restore it when comparing known world-generation conditions is useful. | Define exactly what the seed controls. Confirm the game receives it and record build/setup/runtime differences; do not claim deterministic network or model replay. |
| Advanced scenario builder and editable JSON | The old contract exposes many choices irrelevant to starting one bot. Restore only fields needed by a concrete experiment, with a reviewable configuration. | Stable schema/version migration, validation before launch, immutable run inputs and duplicate-launch protection. Old records must remain readable; omitted or invalid fields must not silently change behavior. |
| Custom success predicates and side goals | Free-text goals do not provide trustworthy automatic completion. Restore explicit predicates when the game exposes sufficient evidence. | Define scope, quantities, timing and authority. Test false positives, stale observations, incomplete propagation and failure cases. Current inventory is not cumulative gathered quantity; dialogue is not a committed milestone. |
| Balancing analysis | Agent/controller delay and motor assistance can be mistaken for game difficulty or player effort. Restore resource/time/drop observations when execution is reliable enough to interpret them. | Separate game time, waiting, decision latency and failed controls; retain sample counts and build/profile identity. Validate calculations against known events before recommendations. Never automatically edit game balance. |
| Run comparisons and reruns | Different instructions, setup and execution failures make comparisons easy to misread. Restore comparison views and rerun configuration after single-run evidence is dependable. | Pin profile, model, build and setup; disclose differences and incomplete usage. Verify unique run identities, historical readability and calculations. Recorded inputs are evidence, not deterministic replay. |
| AI reviews and incident workflows | Additional model interpretation does not solve action reliability. Restore review queues, classifications and merges when retained evidence makes them actionable. | Cite concrete events, preserve raw evidence and mark model judgments provisional. Test missing evidence, duplicates and reviewer failures; deterministic operational reporting must survive model failure. |

## Existing records and retired endpoints

Do not delete historical scenarios, runs, saved packages, reports, evidence, usage or account/worker state as part of the simplification. Keep them readable and preserve operational recovery for existing runs. Retired feature endpoints are unavailable; compatibility does not authorize creating or editing old advanced scenarios through an alternate route.

Reintroducing a feature requires an intentional UI/API decision and its applicable checks, not merely re-enabling a legacy button or route. Historical architecture, API and validation documents describe earlier behavior and are reference material. The [README](../README.md), [dashboard guide](ui.md) and [structured testing](structured-testing.md) define the current supported workflow.

## Order of work

1. Verify profile tuning and fresh single-bot startup with the actual account, build and desktop.
2. Diagnose and fix observed control/state gaps, especially cutscenes and UI ownership; prove recovery with one focused check and bounded live acceptance.
3. Verify a meaningful gameplay objective and its evidence before attempting full DEMO completion.
4. Restore a deferred capability only when a real test requires it, carrying forward the prerequisites above.

Keep credentials, Steam sign-in and entitlement outside model instructions. Preserve input ownership, stale-action rejection, failure reporting, teardown confirmation and storage/account protections throughout; these are current operational requirements, not deferred enhancements.
