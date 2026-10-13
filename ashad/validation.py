from __future__ import annotations
from pathlib import Path
import shutil
from pydantic import ValidationError
from .models import Plan, PROVIDERS, provider_of
from .repository import Repository, catalog_ids, sha256_file


def validate_plan(raw, store, check_credentials=False):
    errors, warnings = [], []
    try:
        plan = Plan.model_validate(raw)
    except ValidationError as exc:
        return {"valid": False, "errors": [".".join(str(x) for x in e["loc"]) + ": " + e["msg"] for e in exc.errors()], "warnings": []}
    build = store.get("builds", plan.build_id)
    if not build:
        errors.append("Select and import an available pinned game build.")
    else:
        if build.get("structured_version") != plan.structured_version:
            errors.append("Structured play requires a build with the matching game interface version.")
        try:
            repo = Repository(build)
            catalog = repo.catalog(build)
            ids = {g["id"] for g in catalog.get("goals", []) if g.get("available_in_build", True)}
            if ids:
                targets = set()
                if plan.success_criteria.predicate != "manual":
                    targets |= {p.goal_id for p in plan.players}
                    if plan.success_criteria.predicate != "item_quantity":
                        targets.add(plan.success_criteria.target)
                    targets |= set(plan.success_criteria.side_goals)
                    targets |= {str(e.value) for e in plan.events if e.trigger == "milestone"}
                for target in targets:
                    if target not in ids:
                        errors.append(f"Unknown mechanic or objective in selected build: {target}")
                if plan.success_criteria.predicate == "item_quantity" and plan.success_criteria.target not in catalog_ids(catalog.get("items", [])):
                    errors.append("Item completion predicate must reference an actual item in the selected build.")
            else:
                errors.append("Build has no source catalog; goals cannot be resolved.")
            for item in build.get("artifacts", []):
                p = Path(item["path"])
                if not p.is_file() or (item.get("sha256") and sha256_file(p) != item["sha256"]):
                    errors.append(f"Build artifact missing or changed: {p.name}")
            if not build.get("command"):
                errors.append("Build has no worker launch command.")
            if not build.get("source_revision"):
                errors.append("Build source revision is not pinned.")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"Build unavailable: {exc}")
    for player in plan.players:
        worker = store.get("workers", player.worker_id) if player.worker_id else None
        if not worker or not worker.get("enabled", True):
            errors.append(f"{player.name}: select an enabled independent desktop worker.")
        elif worker.get("last_probe", {}).get("independent_desktop") is False:
            errors.append(f"{player.name}: the worker does not provide an independent desktop.")
        if plan.transport.mode == "steam":
            account = store.get("accounts", player.account_id) if player.account_id else None
            if not account:
                errors.append(f"{player.name}: select a persistent Steam account.")
            elif account.get("worker_id") != player.worker_id:
                errors.append(f"{player.name}: Steam account belongs to another desktop.")
            elif account.get("readiness") != "ready":
                errors.append(f"{player.name}: Steam sign-in and licensed App ID readiness must be verified.")
            elif str(account.get("app_id")) != str(plan.transport.app_id):
                errors.append(f"{player.name}: Steam account App ID differs from plan.")
            if account and not account.get("steam_id"):
                errors.append(f"{player.name}: actual Steam identity is unknown.")
    if plan.transport.mode == "steam" and not plan.transport.app_id:
        errors.append("Enter the actual Agronauts Steam App ID; no fallback is allowed.")
    elif plan.transport.mode == "steam" and build and build.get("steam_app_id") and plan.transport.app_id != build["steam_app_id"]:
        errors.append("Selected App ID differs from the verified build App ID.")
    host = next(p for p in plan.players if p.role == "host")
    host_worker = store.get("workers", host.worker_id, {})
    if plan.transport.mode == "enet" and not (plan.transport.host_address or host_worker.get("host_address")):
        errors.append("The host worker requires a reachable private host address.")
    if plan.transport.mode == "enet" and len(plan.players) > 1 and plan.transport.host_address in {"localhost", "127.0.0.1", "::1"}:
        errors.append("Independent workers cannot join using the controller's localhost.")
    if plan.seed is not None and build and not build.get("supports_seed", False):
        errors.append("Selected build does not expose a supported seed control.")
    if plan.world_setup.mode != "fresh":
        fixture = store.get("fixtures", plan.world_setup.fixture_id)
        if not fixture:
            errors.append("Saved-start package is missing.")
        else:
            if fixture.get("build_id") != plan.build_id:
                errors.append("Saved-start package build/version is not compatible with selected build.")
            if build and fixture.get("build_revision") != build.get("source_revision") and fixture.get("build_revision") not in build.get("compatible_save_revisions", []):
                errors.append("Saved-start source revision has no verified compatibility with this build.")
            if build and fixture.get("save_version") != build.get("save_version"):
                errors.append("Saved-start save version differs from the selected game runtime.")
            if not Path(fixture.get("path", "")).is_dir():
                errors.append("Saved-start source package is inaccessible.")
            for p in plan.players:
                mapping = plan.world_setup.identity_mapping.get(p.id)
                records = fixture.get("players", {})
                if not mapping or mapping not in records:
                    errors.append(f"{p.name}: an explicit saved-character identity mapping is required.")
                elif plan.world_setup.character_policy == "restore":
                    expected = (store.get("accounts", p.account_id, {}).get("steam_id") if plan.transport.mode == "steam" else p.identity)
                    source = records[mapping].get("identity")
                    if expected and str(source) != str(expected) and not fixture.get("validated_mappings", {}).get(f"{source}:{expected}"):
                        errors.append(f"{p.name}: changed identity needs a validated conversion package.")
                if mapping in records and plan.world_setup.character_policy == "fresh" and p.identity and p.identity == records[mapping].get("identity"):
                    errors.append(f"{p.name}: fresh-character policy requires a new local identity; leave identity unset or choose a distinct identity.")
            if fixture.get("transport") != plan.transport.mode and not fixture.get("validated_transport_conversion"):
                errors.append("Cross-transport restore requires an explicitly validated conversion package.")
    if plan.success_criteria.predicate == "item_quantity" and plan.success_criteria.quantity is None:
        errors.append("Item-quantity success requires a positive quantity.")
    resolved_identities = []
    for player in plan.players:
        identity = player.identity
        if plan.transport.mode == "steam":
            identity = store.get("accounts", player.account_id, {}).get("steam_id") if player.account_id else None
        elif plan.world_setup.mode != "fresh" and plan.world_setup.character_policy == "restore":
            fixture = store.get("fixtures", plan.world_setup.fixture_id, {})
            mapping = plan.world_setup.identity_mapping.get(player.id)
            identity = identity or fixture.get("players", {}).get(mapping, {}).get("identity")
        if identity:
            from integration.fixtures import character_key
            try:
                resolved_identities.append(character_key(str(identity), plan.transport.mode))
            except ValueError as exc:
                errors.append(str(exc))
    if len(resolved_identities) != len(set(resolved_identities)):
        errors.append("Resolved characters share a persistent identity or local character hash; choose distinct mappings.")
    if plan.success_criteria.predicate == "item_quantity":
        warnings.append("Quantity measures items currently held in the authoritative host's Inventory and Hotbar; NPC stock, cumulative gathering and consumed items are excluded.")
    if plan.success_criteria.predicate == "observed_goal":
        errors.append("A visual claim alone is not a launchable completion predicate. Select a source-resolved quest, biome, or item quantity.")
    if check_credentials:
        provider = provider_of(plan.model)
        pricing = PROVIDERS[provider]["pricing"]
        status = store.get("settings", PROVIDERS[provider]["status_key"], {})
        available = status.get("available_models", [status.get("model")] if status.get("status") == "ready" else [])
        if not (status.get("api") == pricing["api"] and status.get("auth_method") == pricing["auth_method"] and status.get("configured")
                and status.get("status") in {"ready", "model_unavailable"} and plan.model in available):
            message = status.get("message", "Refresh sign-in status to verify subscription access.")
            if provider == "claude":
                errors.append(f"Connect Claude (paste a token from claude setup-token) and verify access to {plan.model} before Launch. " + message)
            elif provider == "grok":
                errors.append(f"Sign in to Grok Build (run the command in Settings › Account yourself) and verify access to {plan.model} before Launch. " + message)
            else:
                errors.append(f"Connect Codex with ChatGPT and verify access to {plan.model} before Launch. " + message)
    if plan.limits.duration_seconds is None:
        warnings.append("Run duration ceiling is unset.")
    if plan.limits.spend_usd is not None:
        warnings.append(f"The saved API dollar ceiling does not apply to subscription-backed {PROVIDERS[provider_of(plan.model)]['label']}. Subscription usage limits apply.")
    free = shutil.disk_usage(store.root).free
    reserve = store.get("settings", "main", {}).get("storage_reserve_bytes", 1024**3)
    if free < reserve + min(plan.limits.storage_bytes, 256 * 1024**2):
        errors.append("Insufficient storage headroom for durable evidence.")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "plan": plan.model_dump()}
