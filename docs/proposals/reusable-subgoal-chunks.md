# Proposal: Reusable sub-goal plans ("chunks")

> **Status: Implemented on feat/ashad-subgoal-chunks**
>
> Drafted: 2026-10-16 · Builds on: PR waspflannel/agentic-game-testing#79 (`fix/ashad-planner-goal-check`)

---

## 1. Summary

Today ASHAD saves a plan only for the **whole goal** it reached, and reuses it only for the whole goal at the start of a run. This proposal also saves **sub-goals** (for example "get 5 wheat", with its till, plant, water and harvest stages) as reusable plans, called **chunks**. A later run that meets a matching sub-goal inside a different goal (for example "finish the first quest") reuses the chunk instead of calling Opus.

It also covers a smaller related fix: a saved plan is turned down when only the goal's number changes ("Plant 3 wheat" after "Plant 5 wheat"). See section 8.

## 2. How it works today

- Opus plans the goal into a tree: the root, which has its own check (for example `crops_planted(plant_id=wheat_seed) >= 5`), and a flat row of ordered stages. A built-in "enter the world" stage always comes first.
- When the root's check passes, the plan is saved as a **plan pattern**: the root's check plus the stages that finished, with blanks for the count and the item (`Plant {count} {plant_id}`).
- At the start of the next run, Jev picks a saved pattern that fits the goal, or answers none. A picked pattern is used without an Opus call.
- Inside a bigger goal, only the **skills** are reused (till, plant and water each replay from the cache). The planning for that piece still costs an Opus call.

## 3. The problem

1. **Sub-goal planning is never cached.** With "finish the tutorial" as the goal, a "plant 5 seeds" piece is planned by Opus every run, even if the same piece was finished before inside another goal.
2. **Many plans are never saved at all.** A pattern can hold only one value per blank. A "have 5 wheat" plan names `item_id=wheat` for the goal and `item_id=wheat_seed` for its "get seeds" stage. These are two values for one blank, so `skills.save_plan` returns None and nothing is logged. The next run plans from scratch.

## 4. Proposed changes

### 4.1 Fix the blank limit first

Let a pattern keep related values in one blank: store `wheat_seed` as `{item_id}_seed` when `item_id=wheat`. `skills.bind` already does this for stage steps. When a save is still refused, log a `plan_save_skipped` event with the reason, so it no longer fails silently.

Without this fix, a chunk like "get 5 wheat" could never be saved.

### 4.2 Give plans real middle levels

Change the planner prompt so Opus groups stages under sub-goals, and each sub-goal gets its own check:

```
Finish the first quest          (root, its own check)
├─ Enter the world              (built in)
├─ Get 5 wheat                  item_count(item_id=wheat) >= 5
│  ├─ Take seeds from the rocket
│  ├─ Till 5 tiles
│  ├─ Plant 5 seeds
│  ├─ Water them
│  └─ Harvest 5 wheat
└─ Deliver 5 wheat to the ship  container_contents(...) >= 5
```

The root keeps its own check, and stages still complete in order.

### 4.3 Save each finished sub-goal as a chunk

When a sub-goal that has stages under it passes its own check, save it the same way the root is saved now: its check plus its finished stages in order, with blanks for the count and the item. The root save stays as it is.

### 4.4 Look up chunks whenever a sub-goal needs planning

Today the cache is asked only when the root is first planned. Instead, whenever any goal without children needs planning:

1. Find saved patterns whose goal check has the same shape as this goal's check (for example `item_count(item_id) >=` matches "get N of any item").
2. Jev picks one or answers none (`jev_plan_pick`, as for root plans).
3. Fill the pattern's stages in under this goal instead of calling Opus.

The existing safety net applies to chunks unchanged:
- A stage that breaks under a cached chunk counts a fail against that chunk.
- When the operator says "not done", it counts against the patterns behind the claim.
- A chunk stays in use while its fails don't outnumber its wins.

## 5. Risks

- **A chunk may assume a situation.** For example, it may expect the seeds to be in the rocket. If a stage doesn't fit, it breaks after 6 tries, Opus re-plans it, and the chunk gets a fail. A bad chunk costs one break before it is dropped.
- **Opus has to produce good middle levels.** Chunks are only as reusable as the sub-goal checks Opus picks. A sub-goal with an `unknown` check can't be saved.
- **Matching is by check shape.** "Get 5 wheat" and "get 5 stone" have the same shape, so Jev decides whether a farming chunk fits. Its "none" answer is the guard.

## 6. Files

| File | Change |
|---|---|
| `ashad/skills.py` | Blank fix (`save_plan`, `pattern_stage`, `instantiate`); match patterns by goal-check shape |
| `ashad/prompts.py` | Planner groups stages under sub-goals with their own checks |
| `ashad/structured.py` | Save finished sub-goals as chunks; ask the chunk cache for any goal that needs planning; `plan_save_skipped` event |
| `ashad/subgoals.py` | Small changes to apply a chunk under a non-root goal |
| `ashad/ai.py` | `jev_plan_pick` takes the sub-goal's text as the goal |
| `README.md` | Describe chunk reuse |

Rules from `CLAUDE.md`: no automated tests are added; `.\scripts\check.ps1` must pass before merging.

## 7. How to confirm it in the game

1. **Run "Have 5 wheat in your inventory".** A `plan_saved` event should appear, which proves the blank fix.
2. **Run "Finish the first quest" (or any goal that needs wheat).** The "get 5 wheat" sub-goal should be filled from the cache (`plan_routed` with `route: plan_cache` for that sub-goal), with no Opus call for it.
3. **Run "Have 3 carrots".** The same chunk should be picked with the item and count filled in. If carrots need different steps, one stage breaks and is re-planned, and the chunk gets a fail.

## 8. Related optimization: reuse when the count changes

### Finding (Jantra, PR #79)

"Plant 3 wheat" half passed. It correctly stopped at 3 wheat, but it turned down the saved "Plant 5 wheat" plan and planned again with Opus (18 s). So reuse doesn't carry over when the number changes, even though the plan is saved with a `{count}` blank for exactly this case.

### Likely cause (to confirm)

Jev chooses a saved plan from a short text about each one. Since PR #79 that text starts with the goal the plan won, as written: "Won the goal: Plant 5 wheat. plan: till() → plant(<seed>): Till {count} tiles; Plant {count} {plant_id}". The new goal is "Plant 3 wheat". The 5 against the 3, plus the bare `{count}` blanks, can lead Jev to answer none or to be unsure.

To confirm, check the run's `plan_pick` event:
- **Reason `none` or `low_confidence`:** this is the cause, and the fix below applies.
- **Reason `missing_arg:<name>`, or a `plan_pattern_rejected` event after the pick:** the cause is something else.

### Proposed fix

Show Jev the plan already filled in with the new goal's count:

1. In `skills.save_plan`, save the won goal's text with its count as a blank ("Plant {count} wheat"), the same way stage text is saved.
2. In `ai.jev_plan_pick`, read the new goal's count (`skills.count_of`) and fill `{count}` into each plan's text before asking. Jev then sees "Won the goal: Plant 3 wheat. … Till 3 tiles; Plant 3 {plant_id}", which matches the goal.

Plans saved before this change keep their raw text. The first win after the change rewrites it.

This is a small change of about two places in the code, and it is independent of the chunk work above, so it can ship on its own.

### How to confirm

After a "Plant 5 wheat" run has saved its plan, run "Plant 3 wheat". `plan_pick` should give `reason: null` and `plan_routed` should show `route: plan_cache`. The run should stop at 3 planted, with no Opus planning call.
