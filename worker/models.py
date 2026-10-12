from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, ConfigDict, FiniteFloat


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: str | float | int) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value) or value in (".", ".."):
        raise ValueError("Identifier must contain only letters, digits, underscore, dot and hyphen")
    return value


class Binding(BaseModel):
    run_id: str
    player_id: str
    epoch: str
    identity: str
    transport: Literal["enet", "steam"] = "enet"
    app_id: int | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    _ids = field_validator("run_id", "player_id", "epoch")(safe_id)


class BoundCommand(BaseModel):
    run_id: str
    player_id: str
    epoch: str
    command_id: str | None = None


class Heartbeat(BoundCommand):
    investigation_deadline: str | float | None = None
    run_deadline: str | float | None = None

    @field_validator("investigation_deadline", "run_deadline")
    @classmethod
    def _parseable_deadline(cls, value):
        if value is not None:
            timestamp(value)
        return value


class Launch(BoundCommand):
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)


class Stop(BoundCommand):
    terminate: bool = False
    reason: str = "requested"


class Pin(BoundCommand):
    incident_id: str
    before_seconds: int = Field(default=30, ge=0, le=600)
    after_seconds: int = Field(default=30, ge=0, le=600)

    _id = field_validator("incident_id")(safe_id)


class GameRequest(BoundCommand):
    operation: Literal["observe", "read_changes", "inspect_entity", "list_actions", "perform_action", "get_action_status", "cancel_action"]
    arguments: dict[str, Any] = Field(default_factory=dict)


class GameParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=128)
    button: Literal["left", "right"] | None = None
    duration_ms: int = Field(default=160, ge=0, le=2000)
    position: tuple[FiniteFloat, FiniteFloat] | None = None
    world_position: tuple[FiniteFloat, FiniteFloat] | None = None
    destination_id: str | None = Field(default=None, max_length=120)
    direction: int = Field(default=1, ge=-1, le=1)


class GameAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    observation_id: str = Field(max_length=200)
    expires_at: FiniteFloat
    verb: Literal["move_to", "interact", "input", "ui_click", "ui_drag", "ui_text", "ui_scroll", "fish_follow"]
    target_id: str = Field(default="", max_length=120)
    target_version: int = Field(default=-1, ge=-1)
    parameters: GameParameters = Field(default_factory=GameParameters)

    _id = field_validator("action_id")(safe_id)


class FixtureImport(BoundCommand):
    manifest: dict[str, Any]
    archive_base64: str
    identity_mapping: dict[str, str] = Field(default_factory=dict)


class SteamVerify(BaseModel):
    app_id: int
    steam_id: str
    command: list[str] = Field(default_factory=lambda: ["/game/runtime/godotsteam.451.template.x86_64", "--main-pack", "/game/game.pck", "--rendering-method", "gl_compatibility", "--resolution", "1280x720"])
