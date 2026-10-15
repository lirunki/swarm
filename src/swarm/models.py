"""Portable file protocol; process liveness is deliberately not a goal status."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(UTC)


class Status(StrEnum):
    running = "running"
    waiting = "waiting"
    dormant = "dormant"
    idle = "idle"
    done = "done"


class Activity(BaseModel):
    title: str
    status: str = "pending"

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in {"pending", "running", "done"}:
            raise ValueError("activity status must be pending, running, or done")
        return value


class ImportSource(BaseModel):
    """Provenance of an offline conversion; never a command or runtime backend."""

    backend: str
    session_id: str
    session_file: str
    sha256: str
    original_folder: str
    imported_at: datetime = Field(default_factory=now)
    warnings: list[str] = Field(default_factory=list)


class Agent(BaseModel):
    model_config = ConfigDict(extra="allow")  # future adapters may add metadata
    version: int = 1
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=120)
    backend: str = "pi"
    goal: str = Field(min_length=1)
    status: Status = Status.idle
    activity: str = ""
    activities: list[Activity] = Field(default_factory=list)
    folder: str
    worktree: str = ""
    repository: str = ""
    branch: str = ""
    model: str | None = None
    pid: int | None = Field(default=None, gt=0)
    session_id: str | None = None
    session_file: str | None = None
    import_source: ImportSource | None = None
    managed: bool = False  # merely observing a file must NEVER execute a process
    auto_resume: bool = True
    wake_at: datetime | None = None
    wait_reason: str = ""
    updated_at: datetime = Field(default_factory=now)
    created_at: datetime = Field(default_factory=now)
    last_error: str | None = None
    notes: str = ""  # Markdown body: checkpoint and context for recovery

    @field_validator("wake_at", "updated_at", "created_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must include a timezone (e.g. Z or +00:00)")
        return value


class CreateAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1)
    folder: str = Field(min_length=1)
    repository: str = ""
    branch: str = ""
    create_worktree: bool = False
    model: str | None = None

    @model_validator(mode="after")
    def worktree_requirements(self) -> CreateAgent:
        if self.create_worktree and (not self.repository or not self.branch):
            raise ValueError("creating a worktree requires a local repository and new branch")
        return self


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    goal: str | None = Field(default=None, min_length=1)
    status: Status | None = None
    activity: str | None = None
    activities: list[Activity] | None = None
    wait_reason: str | None = None
    wake_at: datetime | None = None
    auto_resume: bool | None = None
    notes: str | None = None

    @field_validator("wake_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        return Agent.timezone_required(value)


class WakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(default="", max_length=32000)
