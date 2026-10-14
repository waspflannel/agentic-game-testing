# Proposal: AI-Assisted Regression Triage for Game CI

> **Status: Proposal — not approved for implementation.**
>
> Author: Jaden Varghese · Drafted: 2026-10-14 · Applies to: AGTF Bot (internally ASHAD), `waspflannel/agentic-game-testing`
>
> This document describes a possible future feature. Nothing here changes the current supported workflow in the [README](../../README.md), and it does not reopen any capability in [Deferred work](../future-work.md) on its own. Each phase below has to pass that document's restoration gates before work starts.

---

## 1. Summary

AGTF already plays a game the way a player would. It runs an LLM agent inside an isolated Docker desktop. The agent works from filtered JSON game state, follows a goal hierarchy grounded in evidence, and records grouped issue records (`crash`, `game_bug`, `unsupported_action`, `capability_gap`, `planner_rejected`) that include pinned screenshots. This proposal adds a **regression triage layer** that points that capability at a game's CI pipeline. It has three components:

1. **Change-aware test selection.** Choose and rank the regression scenarios to run for a given game change.
2. **Visual and gameplay anomaly detection.** Compare a candidate build's frames and telemetry with a known-good baseline build. The goal is to catch bugs that goal completion checks miss.
3. **Triage agent.** Group related failures, separate flaky failures from real regressions, and draft bug reports for a human to approve. The report includes a screenshot, logs and the likely culprit commit.

The feature is about **AGTF testing games**, starting with Agronauts (Godot, co-op farming). It is **not** about adding automated tests to the AGTF repository. ASHAD keeps its current policy: no automated code tests, with manual testing by Jaden and the existing `scripts/check.*` pre-merge check. This proposal adds no test suite to the AGTF repo. All gating is **advisory**: a human approves every filed bug and every merge block.

## 2. Motivation

- **Pass/fail checks miss whole classes of game bugs.** A child goal like "harvest 5 wheat" can finish while the crop textures render magenta, a HUD panel overlaps the inventory, the player clips through the floor for two seconds, or FPS drops by half. Today these appear only if the agent happens to pick `report_game_bug` (`visual_glitch` / `broken_ui`) or a crash detector fires.
- **Agent runs are expensive.** ASHAD allows exactly **one occupied instance globally**, and every decision costs tokens, which the Usage & capacity ledger already tracks. Running every scenario on every change does not scale. Selection has to decide which runs are worth the time and money.
- **Raw failures are noisy.** LLM-driven play is not deterministic. The same build can pass one run and give up on a goal in the next because of model variance, timing or stall-detection thresholds. Without triage, developers either ignore failures or chase flakes.
- **The evidence already exists.** Issue records already carry signatures, `first_seen`/`last_seen` with `build_id` and `source_revision`, recent decisions, the observation, and a pinned 10-second frame window. Triage mostly needs to *connect* that evidence across builds, not invent new evidence.

## 3. Goals and non-goals

### Goals

- G1. For a game change (commit range or PR), produce a **ranked, explained scenario list** that fits a time and cost budget.
- G2. Detect **visual anomalies** (missing or fallback textures, broken or overlapping UI, rendering corruption) and **gameplay and performance anomalies** (falling through the world, stuck or teleporting entities, FPS and frame-time regressions) by comparing against a baseline build.
- G3. **Cluster** failures across runs and builds. Label each cluster `likely_regression`, `likely_flaky`, `environment` or `needs_human`, with cited evidence.
- G4. **Draft bug reports**: title, repro context (configuration, goal path, build pins), screenshot, log tail, ledger event IDs and a ranked list of suspect commits. A human **approves** each report before it is filed or a merge is blocked.
- G5. Measure everything against a **planted-regression benchmark** (Section 6) before any team relies on it.

### Non-goals

- N1. **No automated tests for the AGTF/ASHAD codebase**, and no unit or integration test suite added to the AGTF repo.
- N2. **No autonomous gating.** The system never blocks a merge, files a ticket or reverts a commit without human approval.
- N3. **No deterministic-replay claims.** In line with Deferred work, recorded inputs are evidence, not replay. Baselines are statistical, not frame-exact.
- N4. **No automatic code fixes** to the game or AGTF. Suggested remediations stay text inside the bug draft.
- N5. **No Steam or multiplayer expansion** as part of this work. ENet single-bot instances are enough for the MVP.
- N6. **No game source in the AGTF repo.** Agronauts source, assets and builds stay separately supplied, as they are today.

## 4. Terminology (aligned with ASHAD)

| Term | Meaning in this proposal |
|---|---|
| **Project configuration** | Existing ASHAD concept: a pinned build plus a frozen controls file. |
| **Regression scenario** | A named, versioned pairing of a project configuration and a primary goal, plus the child-goal completion checks expected for it, such as "plant and harvest one crop" or "craft a hoe". Scenarios in the old advanced-builder sense remain retired. This is a deliberately minimal reintroduction under the "Advanced scenario builder" and "Run comparisons and reruns" gates. |
| **Instance / run** | Existing: one fresh-world bot execution with its own `run_id`, ledger and evidence. |
| **Baseline build** | A build a human has marked known-good for a scenario. Its runs form the reference distribution. |
| **Candidate build** | The build under test (a PR head or nightly). |
| **Finding** | An anomaly-detector or check result for a candidate run. Findings are provisional and never treated as fact. |
| **Cluster** | A group of findings and issue records judged to share one root cause. |
| **Bug draft** | A human-reviewable report produced by the triage agent. Nothing is filed until approved. |

## 5. Architecture

### 5.1 Components

```
 Game repo CI (Agronauts)                                AGTF / ASHAD (desktop, local)
 ───────────────────────                                ───────────────────────────────────────────
 push / PR ──► build artifact ──► [Build registration] ─► registered manifest (exact pins)
                    │                                              │
                    └─ diff (files, commits) ──►  ① Test Selector ─┤ ranked scenario plan (budgeted)
                                                                   ▼
                                                    Scheduler (respects 1 occupied instance)
                                                                   │  fresh instance per scenario
                                                                   ▼
                       ┌──────────────── existing ASHAD run loop ─────────────────┐
                       │ planner → goal hierarchy → action loop → MCP tools       │
                       │ ledger events, issue records, pinned screenshots         │
                       │ + NEW: sampled frame capture & perf telemetry stream     │
                       └──────────────────────────────────────────────────────────┘
                                                                   │ run evidence
                                                                   ▼
                                     ② Anomaly Detector  (candidate vs baseline distribution)
                                                                   │ findings
                                                                   ▼
                                     ③ Triage Agent  (cluster · flaky/real · suspect commits)
                                                                   │ bug drafts + advisory verdict
                                                                   ▼
                                     Review queue in ASHAD UI ──► human approves ──► file bug / post
                                                                                    advisory CI status
```

**① Test Selector** (controller service, Python)
- *MVP: rule-based mapping.* A versioned `selection-map` per project maps game source paths and globs to scenarios. For example, `scenes/ui/inventory/**` maps to `inventory-and-crafting`, `systems/farming/**` maps to `plant-harvest`, and shaders or materials map to every scenario that takes a visual baseline. The map can be bootstrapped from the existing Get game controls notes, which already link source to controls, and then edited by hand. Unmapped changes fall back to a small **smoke set** that always runs.
- *Later: learned ranker.* Features include changed paths, directory embeddings, commit-message terms, historical co-failure (which scenarios produced regression clusters when similar files changed), scenario cost (median tokens and minutes from the usage ledger) and time since the scenario last ran. The output is the probability that a scenario reveals a regression, ranked by value per cost within the budget. The rule map stays as a floor: mapped scenarios are never dropped.
- *Output:* a plan with an explanation per scenario ("selected: `systems/farming/crop.gd` changed; this scenario failed on 3 of the last 4 farming changes").

**Scheduler.** It queues fresh instances on the candidate build and on the baseline when baseline samples are stale. It respects the one-occupied-instance rule and all existing readiness, teardown and reconciliation safeguards. A failed teardown blocks the queue exactly as it does now. Each scenario runs **N times** (default 3) so variance can be measured, and N is a budget knob.

**Recording extension (prerequisite).** Today frames are retained only as **pins**: 10 seconds before each kept issue example, under `runs/<run>/workers/<player>/pinned/<example_id>/`. The live camera stream runs only while Bot view is open. Anomaly detection needs:
- **Sampled frame capture** from the worker for the whole run (for example 1–2 fps keyframes plus a short burst on scene changes), written to run evidence. This is separate from the operator camera and never fed to the model.
- **Performance telemetry**: frame time / FPS, plus memory if cheap. It would come from a small Godot AI-layer hook or the worker, recorded in `game_state.telemetry.diagnostics` or a new ledger stream. Because Godot AI-layer edits live outside this repo, this is a cross-repo dependency.
- **Alignment keys**: each sample is tagged with `observation_id`, the current goal ID, scene or screen identity from the observation, and player position, so candidate and baseline can be compared *at equivalent game moments* rather than at equal wall-clock times.

**② Anomaly Detector** (offline worker, runs after the run ends; never in the decision loop)
- **Visual.** Frames are matched to baseline frames using the alignment keys, giving the same scene, goal and UI state at nearby positions. Checks:
  - perceptual and structural diff (SSIM / LPIPS-style) on matched UI regions;
  - a **missing-texture detector** for Godot fallback patterns (solid magenta/black, checkerboard) and unusual flat-colour regions on meshes;
  - **UI layout checks**: elements expected from the observation's UI tree but not visible, overlapping or off-screen panels, text overflow;
  - an optional vision-language model pass on flagged frames only, which writes a short description that becomes evidence.
- **Gameplay.** Telemetry rules on the JSON state stream: vertical position below world bounds or sudden large z-drops ("fell through floor"), velocity or position discontinuities (teleports), entities stuck in an animation or state past a threshold, and quest or inventory state reverting. These complement the existing `game_bug` follow-up categories (`position_change`, `reverted`, `visual_corruption`).
- **Performance.** A per-scenario-segment frame-time distribution is compared with the baseline using a non-parametric test and a minimum effect size, such as p95 frame time more than 20% worse in the same scene. Hardware measurements from Usage & capacity are recorded so that host contention is not mistaken for a regression.
- **Output:** findings, each with type, severity, confidence, frame and telemetry references, and the matched baseline reference. Findings feed the existing issue pipeline as `game_bug` examples with a new `source: anomaly_detector`, so signature grouping, the 10-example cap, redaction and reopen-on-recurrence apply unchanged.

**③ Triage Agent** (LLM-backed, advisory)
- **Clustering.** Start with the existing issue `signature` (deterministic). Then merge across signatures using embeddings of the title, symptom and target, plus a shared suspect commit and co-occurrence in the same runs. Every merge cites the issue IDs and event IDs it relied on.
- **Flaky vs real.** Compare how often a cluster appears on the candidate with how often it appears on the baseline across N runs each, along with its history: did it reproduce on unrelated builds? Did it disappear on rerun? Is the cause `environment` (`worker_lost`, `desktop_lost`, `capture_unhealthy`, host suspended) or `planner_gap`, which points at the agent rather than the game? Labels: `likely_regression` (present on candidate, absent or rare on baseline, reproduced at least 2 of N times), `likely_flaky`, `environment`, `agent_limitation`, `needs_human`.
- **Suspect commits.** Builds already carry `source_revision`. Within the commit range from baseline to candidate, suspects are ranked by overlap between changed files and the cluster's selection-map scenarios and target labels, by stack or log mentions, and by diff relevance read by the model. *Stretch:* automatic **build bisection**, queuing runs on intermediate builds when CI artifacts exist (budgeted, human-triggered).
- **Bug draft.** Title, the cluster label with confidence, scenario and goal path, build pins, baseline versus candidate screenshots side by side, the log tail, ledger event IDs, ranked suspect commits with reasons, and the agent's provisional explanation, clearly marked as model judgment, as Deferred work requires for AI reviews.
- **Human approval.** Drafts appear in a review queue in the ASHAD UI. The human can approve and file (to a configured tracker, initially GitHub Issues on the game repo), edit, merge into another cluster, mark as flaky or dismiss. The CI-facing signal is an **advisory status** (`neutral`, with a summary link). It becomes "request block" only after a human approves a `likely_regression` cluster. Deterministic reporting (raw issue counts and crash records) still works if the model fails.

### 5.2 How it plugs into existing AGTF pieces

| Existing piece | Role in this feature |
|---|---|
| **Hierarchical planner / goal hierarchy** | Regression scenarios reuse primary goals and child-goal completion checks. Goal IDs and goal paths serve as alignment keys between baseline and candidate runs. `capability_gap` results with cause `planner_gap` are routed to `agent_limitation`, not game regressions. |
| **Local MCP server (FastMCP)** | It exposes read-only *triage tools* (list findings, get cluster, get baseline frame, get run evidence) under the existing permissioned-session model, so the triage agent and external clients see the same evidence. No new action tools. |
| **Issue records** (`ashad.issue/2`) | The system of record for findings. New `source` values are proposed: `anomaly_detector` and `triage`. A possible `ashad.issue/3` would add `cluster_id` and `triage_label`. Migration follows the existing one-time-at-startup pattern. |
| **Pinned screenshots and run evidence** | Reused as-is for the moment of failure. Sampled capture adds baseline-comparable context. |
| **Stall detection / replanning** | Stall and give-up events count as *signals*, not verdicts. A new stall that does not occur on the baseline in the same goal is a strong regression signal. |
| **Human take-over / hand-back** | Runs with human takeover are marked and **excluded** from automatic regression verdicts by default, because human input breaks comparability. |
| **Token / cost ledger** | Provides scenario cost for the selector's budget and records triage-agent and VLM spend separately. |

### 5.3 Data flow (one PR)

1. Game CI produces a build and posts `{build artifact, commit range, changed files}` to a local ASHAD intake endpoint, or a human imports it. The build goes through normal registration with exact pins.
2. The Test Selector produces a budgeted plan. A human can approve or edit it (MVP), or it is auto-accepted within budget (later phases).
3. The Scheduler runs N candidate instances per scenario and refreshes baseline samples if they are older than the baseline-staleness policy.
4. After each run, the Anomaly Detector writes findings, and those findings become issue examples.
5. After the plan finishes, the Triage Agent clusters, labels and drafts reports, then posts an advisory CI summary.
6. A human reviews, approves, files or requests a block. Decisions are logged and fed back as training labels for the selector and the flaky classifier.

## 6. Evaluation plan

### 6.1 Planted-regression benchmark

A set of **Agronauts build variants**, kept outside the AGTF repo next to the game builds. Each variant has one known, deliberately planted defect plus a ground-truth record (category, the scenario expected to reveal it, culprit commit). Target: around 30 plants at MVP and 60 or more later, spread across:

| Category | Example plants |
|---|---|
| Visual / texture | Remove a crop albedo texture; break a material so it uses the fallback. |
| UI | Shift an inventory panel off-screen; truncate the crafting button label; hide the quest tracker. |
| Physics / gameplay | Disable a floor collider in one field tile; double the gravity scale; make tools fail to reduce soil state. |
| Progression / logic | A recipe that consumes inputs without producing output; a quest flag that never sets. |
| Performance | An O(n²) loop in the crop-growth tick; an unbounded particle emitter (target: p95 frame time +30%). |
| Control (negative) | Builds with **no** planted defect, only harmless refactors, used to measure false positives. |

Each plant sits on a synthetic commit range with 3–10 decoy commits, so culprit ranking can be scored.

### 6.2 Metrics

| Metric | Definition | MVP target | Stretch target |
|---|---|---|---|
| **Planted regressions caught pre-merge** | Share of plants that produce an approved `likely_regression` cluster on the candidate build within budget | ≥ 60% | ≥ 85% |
| …by category | Same, per category (visual, UI, physics, logic, perf) | reported | ≥ 70% each |
| **Triage precision** | Of clusters labelled `likely_regression`, the share that match a real plant | ≥ 0.7 | ≥ 0.9 |
| **Triage recall** | Of real plants that produced findings, the share labelled `likely_regression` (not flaky or dismissed) | ≥ 0.7 | ≥ 0.9 |
| **False-positive rate** | `likely_regression` clusters per control build (and per 100 runs) | ≤ 0.3 per build | ≤ 0.1 per build |
| **Culprit accuracy** | Share of caught plants where the true commit is ranked first (and in the top 3) | top-3 ≥ 60% | top-1 ≥ 60% |
| **CI minutes saved by selection** | 1 − (selected-plan minutes ÷ full-suite minutes), measured **with** detection rate held within 5 points of the full suite | ≥ 40% | ≥ 70% |
| **Cost** | Tokens and $-equivalent per PR (from the usage ledger), split into play, detection and triage | reported | budget-bounded |
| **Human effort** | Median reviewer minutes per bug draft; share of drafts approved without edits | reported | ≥ 50% unedited |

Scores come from repeated runs (N ≥ 3 per scenario per build) and are reported with confidence intervals, because play is not deterministic. Detector and triage results are recorded **separately** from mocked or offline evaluations, matching the README's rule that live acceptance is recorded separately.

## 7. Phased milestones

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0: Prerequisites** | Prove the Deferred-work baseline: reliable single-bot progress on a pinned build for 3–5 bounded scenarios. Add sampled frame capture, frame-time telemetry and alignment keys. Minimal versioned regression-scenario definitions. Define the planted-regression benchmark format. | Each scenario completes its child goals on the baseline in ≥ 80% of runs. Capture overhead is < 5% frame time. |
| **P1: MVP** | Rule-based selection map plus smoke set. Scheduler with N=3. Deterministic detectors: missing texture, fall-through and teleport, FPS regression. Signature-based clustering. Rule-based flaky vs real (baseline-vs-candidate frequency). Template bug drafts with screenshot, log and commit range. Human review queue. Manual CI import. Advisory status only. | Benchmark: ≥ 60% caught, FP ≤ 0.3 per control build, selection saves ≥ 40% of minutes. |
| **P2: Triage agent** | LLM clustering across signatures, explanations with citations, suspect-commit ranking, UI layout checks, VLM pass on flagged frames, a CI webhook intake and an advisory PR comment. | Triage precision and recall ≥ 0.8; culprit top-3 ≥ 60%. |
| **P3: Learned selection** | Ranker trained on the history of human-approved labels and co-failures. Cost-aware budgeting. Baseline auto-refresh policy. | ≥ 70% minutes saved with detection within 5 points of the full suite. |
| **Stretch** | Human-triggered build bisection; perceptual-diff models fine-tuned on Agronauts art; adaptive regression coverage (the selector proposes *new* scenarios for areas with no coverage, for human approval); multi-bot scenarios once the multiplayer gate passes; tracker integrations beyond GitHub Issues. | Measured on the extended benchmark. |

## 8. Risks and open questions

**Risks**

- **Agent variance looks like regressions.** LLM play differs from run to run, so a "new stall" might be the model and not the build. *Mitigation:* N-run distributions, baseline-relative frequency, the `planner_gap` → `agent_limitation` routing, and pinned model, controls file and build per comparison.
- **Throughput.** With one occupied instance globally, a 10-scenario × N=3 plan could take hours. Selection is essential, not optional. Parallel desktops would need the multiplayer and capacity gates from Deferred work.
- **Frame alignment.** Without deterministic replay, matched frames will differ in camera, time of day and crop growth stage. Visual diffing may be noisy. *Mitigation:* compare matched UI regions and specific detectors first, and use whole-frame diffs only as a weak signal.
- **Cross-repo dependency.** Telemetry hooks need Godot AI-layer changes outside this repo, and the benchmark needs game-build variants.
- **Cost.** VLM passes and triage reasoning add tokens on top of gameplay. Budgets are enforced through the existing ledger.
- **Over-trust.** Model-written explanations may read as authoritative. *Mitigation:* every claim cites event or frame IDs, labels are provisional, there is no autonomous block, and human decisions are auditable.
- **Privacy and redaction.** Bug drafts leave the machine when filed. They must pass the existing `redact` rules and never include credentials, Steam profiles, controls files or agent memory.

**Open questions**

1. Where should CI intake live? A local endpoint on the desktop controller, which is private and per-launch keyed today, or a separate headless runner? The current controller rejects any client without the Electron-injected key. That is a security property to keep, so intake may need its own explicitly approved channel.
2. Who marks a baseline known-good, and how long does it stay valid as content changes legitimately (new art, intentional UI redesign)? Do we need an "accept as new baseline" action per finding?
3. Should anomaly findings be `game_bug` examples with a new `source`, or a new issue type (for example `anomaly`)? The trade-off is reusing signatures and caps versus keeping agent reports separate from detector reports.
4. What minimum telemetry can Agronauts expose cheaply (frame time, physics body states, renderer warnings in the log)?
5. What N and what budget per PR are acceptable to Jaden in tokens and wall-clock time?
6. Which tracker comes first, and does filing need its own permissioned session scope?
7. How do we keep the learned selector from overfitting to the planted benchmark instead of real regressions?

## 9. Industry alignment

Two recent Riot Games postings describe nearly this exact problem space, which is evidence that studios are investing in it:

- **Principal Software Engineer, Test Automation – League of Legends** (Riot Games, Los Angeles; Greenhouse job #7118616, first published 2025-10-06). [Posting](https://boards-api.greenhouse.io/v1/boards/riotgames/jobs/7118616). Its desired qualifications include having *"leveraged machine learning, AI, and/or functional testing techniques to enhance automated testing workflows, enabling smarter test case prioritization, anomaly detection, and adaptive regression coverage across complex game systems"* and *"2+ years experience utilizing computer vision in support of automation tests."* It also notes that Python drives their test automation and validation systems.
  - Maps to: **① Test Selector** (test prioritization), **② Anomaly Detector** (anomaly detection, computer vision) and the stretch **adaptive regression coverage** item.
- **Principal Software Engineer – ML Platform Engineer, AI Efficiency team** (Riot Games, Singapore; Greenhouse job #8017748, first published 2026-06-19). [Posting](https://boards-api.greenhouse.io/v1/boards/riotgames/jobs/8017748). It describes operationalizing *"agent-assisted code review, automated bug triage and remediation, AI-driven performance and security analysis, browser-based UI validation."* It also covers pipelines that combine CI/CD with agentic workflows for *"bug detection, regression analysis,"* and defining *"guardrails, approval workflows, observability"* and evaluation frameworks covering *"quality lift, false positive rates, latency, cost."*
  - Maps to: **③ Triage Agent** (automated bug triage, regression analysis), **performance anomaly detection**, the **human-approval / advisory gating** model, and the **evaluation plan** (false-positive rate, cost, throughput).

Both postings describe AI that *augments* human judgment with auditable guardrails. That matches this proposal's advisory-only gating and evidence-citation requirements.

## 10. Decision requested

None yet. This document is for discussion. Approval would mean, at minimum, committing to **P0** (baseline reliability plus recording prerequisites) and agreeing on the planted-regression benchmark format. Later phases would be approved individually against their exit criteria.
