"""Versioned scenario contract. A draft never has execution side effects."""
from __future__ import annotations
from graphlib import CycleError, TopologicalSorter
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


MODEL = "gpt-6-luna"
CLAUDE_MODEL = "sonnet"
PROVIDERS = {
    "codex": {"label": "Codex", "status_key": "ai_status", "pricing": {
        "api": "codex", "billing": "subscription", "auth_method": "chatgpt",
        "limitations": "Codex uses the signed-in ChatGPT subscription limits; per-request dollar cost is unavailable."}},
    "claude": {"label": "Claude", "status_key": "ai_status_claude", "pricing": {
        "api": "claude", "billing": "subscription", "auth_method": "claude_oauth_token",
        "limitations": "Claude Code uses your Claude subscription limits; per-request dollar cost is unavailable."}},
}
MODEL_OPTIONS = (
    {"id": "gpt-6-luna", "label": "GPT-6 Luna", "provider": "codex"},
    {"id": "gpt-6-sol", "label": "GPT-6 Sol", "provider": "codex"},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "provider": "codex"},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "provider": "codex"},
    {"id": "sonnet", "label": "Claude Sonnet", "provider": "claude"},
    {"id": "opus", "label": "Claude Opus", "provider": "claude"},
    {"id": "haiku", "label": "Claude Haiku", "provider": "claude"},
)
CODEX_MODELS = tuple(m["id"] for m in MODEL_OPTIONS if m["provider"] == "codex")
CLAUDE_MODELS = tuple(m["id"] for m in MODEL_OPTIONS if m["provider"] == "claude")
SUPPORTED_MODELS = CODEX_MODELS + CLAUDE_MODELS


def provider_of(model):
    return "claude" if model in CLAUDE_MODELS else "codex"


def require_same_provider(model, base, subject):
    """A live instance may switch models only within its current provider."""
    if model not in SUPPORTED_MODELS or provider_of(model) != provider_of(base):
        raise ValueError(f"Choose a {PROVIDERS[provider_of(base)]['label']} model for {subject}.")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RecalculateGoalsRequest(Contract):
    command_id: str = Field(min_length=1, max_length=100)


class ManualSaveRequest(Contract):
    command_id: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=100)


class Transport(Contract):
    mode: Literal["enet", "steam"] = "enet"
    app_id: int | None = Field(default=None, gt=0)
    host_address: str | None = None
    port: int = Field(default=7800, ge=1024, le=65535)


class WorldSetup(Contract):
    mode: Literal["fresh", "saved", "continue"] = "fresh"
    fixture_id: str | None = None
    character_policy: Literal["fresh", "restore"] = "fresh"
    identity_mapping: dict[str, str] = Field(default_factory=dict)


class Player(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    name: str
    role: Literal["host", "joiner"] = "joiner"
    goal: str = "Complete the full demo"
    goal_id: str = "demo_wrapup_moar"
    worker_id: str | None = None
    account_id: str | None = None
    identity: str | None = None
    initial: bool = True
    required: bool = True
    context_policy: Literal["repository", "reduced", "instructions"] = "repository"
    profile: str = "cooperative"
    cooperation: Literal["team", "independent"] = "team"


class Event(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    actor: str
    action: Literal["join", "disconnect", "rejoin"]
    trigger: Literal["elapsed", "milestone", "event", "manual"]
    value: str | float | None = None
    time_basis: Literal["run", "world_ready"] = "world_ready"
    dependencies: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=120, gt=0)
    retries: int = Field(default=0, ge=0, le=10)
    overdue_policy: Literal["run_when_valid", "skip", "reschedule"] = "run_when_valid"
    failure_invalidates: bool = True


class Success(Contract):
    predicate: Literal["manual", "quest_completed", "biome_unlocked", "milestone", "item_quantity", "observed_goal"] = "quest_completed"
    target: str = "demo_wrapup_moar"
    quantity: float | None = Field(default=None, gt=0)
    quantity_scope: Literal["host_current_inventory"] = "host_current_inventory"
    corroboration_seconds: float = Field(default=10, ge=0)
    require_all: bool = True
    side_goals: list[str] = Field(default_factory=list)


class StallPolicy(Contract):
    version: int = 1
    investigation_timeout_seconds: int = Field(default=1800, ge=1800, le=1800)
    observation_max_age_seconds: float = Field(default=60, gt=0)
    simulation_timeout_seconds: float = Field(default=30, gt=0)
    divergence_seconds: float = Field(default=30, gt=0)
    no_progress_seconds: float = Field(default=300, gt=0)
    invalid_actions_threshold: int = Field(default=5, gt=0)


class Limits(Contract):
    duration_seconds: float | None = Field(default=None, gt=0)
    spend_usd: float | None = Field(default=None, gt=0)
    storage_bytes: int = Field(default=10 * 1024**3, gt=0)


class EvidencePolicy(Contract):
    fps: int = Field(default=5, ge=1, le=30)
    quality: int = Field(default=75, ge=20, le=100)
    pre_seconds: int = Field(default=30, ge=0, le=600)
    post_seconds: int = Field(default=30, ge=0, le=600)
    rolling_seconds: int = Field(default=120, ge=30, le=3600)
    retention: Literal["until_explicit_deletion"] = "until_explicit_deletion"


class Plan(Contract):
    # Retain the historical run contract for recovery; new instances use one fresh host.
    configuration_id: str | None = None
    model: str = MODEL
    play_mode: Literal["structured"] = "structured"
    guidance: Literal["rules", "guided"] = "guided"
    structured_version: int = Field(default=1, ge=1, le=1)
    title: str = "Cooperative full demo"
    intent: str = "Complete the full DEMO campaign from a fresh world."
    primary_goal: str = "Complete demo_wrapup_moar and corroborate across connected clients"
    assumptions: list[str] = Field(default_factory=list)
    build_id: str = ""
    transport: Transport = Field(default_factory=Transport)
    world_setup: WorldSetup = Field(default_factory=WorldSetup)
    players: list[Player] = Field(default_factory=lambda: [Player(id="player-1", name="Player 1", role="host")], min_length=1, max_length=4)
    events: list[Event] = Field(default_factory=list)
    success_criteria: Success = Field(default_factory=Success)
    handler_policy: str = "cooperative_vote"
    balancing_targets: list[str] = Field(default_factory=list)
    stall_policy: StallPolicy = Field(default_factory=StallPolicy)
    limits: Limits = Field(default_factory=Limits)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    seed: str | int | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_provider_settings(cls, value):
        # Existing saved drafts remain editable; effort belongs to the provider.
        if isinstance(value, dict) and "reasoning_effort" in value:
            return {key: item for key, item in value.items() if key != "reasoning_effort"}
        return value

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_configuration_id(cls, value):
        # Stored scenario plans predate configuration_id.
        if isinstance(value, dict) and "agent_id" in value:
            if "configuration_id" in value:
                raise ValueError("Use configuration_id, not agent_id.")
            value = dict(value)
            value["configuration_id"] = value.pop("agent_id")
        return value

    @model_validator(mode="after")
    def invariants(self):
        if self.model not in SUPPORTED_MODELS:
            raise ValueError("Select a supported model: " + ", ".join(SUPPORTED_MODELS))
        ids = [p.id for p in self.players]
        if len(set(ids)) != len(ids):
            raise ValueError("Player IDs must be unique")
        hosts = [p for p in self.players if p.role == "host"]
        if len(hosts) != 1 or not hosts[0].initial:
            raise ValueError("Exactly one initial player must be host")
        for field in ("worker_id", "account_id", "identity"):
            values = [getattr(p, field) for p in self.players if getattr(p, field)]
            if len(set(values)) != len(values):
                raise ValueError(f"Each player requires an exclusive {field}")
        events = {e.id: e for e in self.events}
        if len(events) != len(self.events):
            raise ValueError("Event IDs must be unique")
        dependencies = {}
        for event in self.events:
            if event.actor not in ids:
                raise ValueError(f"Unknown actor {event.actor}")
            if event.trigger == "elapsed" and (not isinstance(event.value, (int, float)) or event.value < 0):
                raise ValueError(f"Event {event.id} requires a nonnegative elapsed time")
            predecessors = event.dependencies + ([str(event.value)] if event.trigger == "event" else [])
            if any(predecessor not in events for predecessor in predecessors):
                raise ValueError(f"Unknown dependency on event {event.id}")
            dependencies[event.id] = predecessors
        try:
            TopologicalSorter(dependencies).prepare()
        except CycleError as exc:
            raise ValueError("Cyclic event dependencies") from exc
        for p in self.players:
            if not p.initial and not any(e.actor == p.id and e.action == "join" for e in self.events):
                raise ValueError(f"Scheduled player {p.id} needs a join event")
        if self.transport.mode == "steam" and self.transport.app_id == 480:
            raise ValueError("App ID 480 is not an Agronauts Steam test")
        if self.world_setup.mode != "fresh" and not self.world_setup.fixture_id:
            raise ValueError("Saved/continue setup requires a saved-start package")
        if self.evidence_policy.pre_seconds > self.evidence_policy.rolling_seconds:
            raise ValueError("Pre-incident window exceeds rolling capture retention")
        return self


AI_API = "codex"
CONTROLLER_VERSION = "ashad-agent-setup-v4"
ACTIVE_STATES = {"Starting", "Running", "Paused", "Investigating", "Stopping"}
TERMINAL_QUEST = "demo_wrapup_moar"
