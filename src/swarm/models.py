"""Portable file protocol; process liveness is deliberately not a goal status."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
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


class Product(BaseModel):
    """The single currently advertised result; file bytes live in controller-owned storage."""

    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    kind: Literal["link", "file"]
    label: str = Field(min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=8192)
    filename: str | None = Field(default=None, max_length=255)
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def validate_product(self) -> Product:
        if not self.label.strip() or self.created_at.tzinfo is None:
            raise ValueError("product needs a nonblank label and timezone-aware timestamp")
        if self.kind == "link":
            url = self.url or ""
            if any(ord(c) < 33 or ord(c) in (92, 127) for c in url):
                raise ValueError("product URL must not contain whitespace or control characters")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("product URL must be an HTTP(S) link without credentials")
            _ = parsed.port  # Validate malformed ports too.
            if any(v is not None for v in (self.filename, self.size, self.sha256)):
                raise ValueError("link products cannot contain file metadata")
        elif self.url is not None or not self.filename or self.size is None or not self.sha256:
            raise ValueError("file products require filename, size and checksum, not a URL")
        elif self.filename in {".", ".."} or any(
            ord(c) < 32 or ord(c) in (47, 92, 127) for c in self.filename
        ):
            raise ValueError("product filename must be a plain filename")
        return self


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
    parent_id: UUID | None = None
    spawn_state: str | None = None
    pid: int | None = Field(default=None, gt=0)
    session_id: str | None = None
    session_file: str | None = None
    import_source: ImportSource | None = None
    product: Product | None = None
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
    automatic_worktree_path: bool = False
    model: str | None = None

    @model_validator(mode="after")
    def worktree_requirements(self) -> CreateAgent:
        if not self.create_worktree:
            if self.automatic_worktree_path:
                raise ValueError("automatic_worktree_path requires create_worktree")
            return self
        if not self.branch:
            raise ValueError("creating a worktree requires a new branch")
        if not self.automatic_worktree_path and not self.repository:
            raise ValueError("creating a worktree requires a local repository")
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
    # Omitted preserves automated resume behavior; an explicit empty string means
    # "start/mark running without submitting a prompt".
    message: str | None = Field(default=None, max_length=32000)
