"""Durable, operator-confirmed restarts of a frozen set of owned live processes.

A restart is not Stop+Wake: goal/status/timers/child waits remain untouched. A
native prepare/commit handshake asks Pi to exit only when idle. Exit is observed
before promptless relaunch. Keyboard /quit and kill require separate confirmations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .adapters import session_header
from .meta_compact import busy
from .models import Agent, now
from .paths import local_path
from .store import atomic_write

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)
CONFIRM_SECONDS = 30
MAX_JOB_BYTES = 2 * 1024 * 1024
TERMINAL = {"completed", "cancelled", "failed"}


def active(agent: Agent) -> bool:
    return bool(getattr(agent, "restart_job_id", None))


def require_not_restarting(agent: Agent):
    if active(agent):
        raise RuntimeError("Agent restart is in progress; wait, cancel it, or use Stop")


class RestartTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    token: str = Field(pattern=r"^[0-9a-f]{64}$")


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    targets: list[RestartTarget] = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=300, ge=15, le=86400, strict=True)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class RestartAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_ids: list[UUID] = Field(min_length=1, max_length=128)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class Restarts:
    def __init__(self, controller: Controller):
        self.controller, self.store = controller, controller.store
        self.root = self.store.home / "restarts"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = {}
        self.bad_jobs = {}
        self.launch_budget = 2

    def path(self, id) -> Path:
        path = self.root / f"{UUID(str(id))}.json"
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("Restart storage must not be a symbolic link")
        return path

    def read(self, id) -> dict:
        path = self.path(id)
        info = path.stat()
        signature = (info.st_ino, info.st_size, info.st_mtime_ns)
        old = self.cache.get(str(id))
        if old and old[0] == signature:
            return json.loads(json.dumps(old[1]))
        if info.st_size > MAX_JOB_BYTES:
            raise ValueError("Restart job is oversized")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version", 1) != 1
            or value.get("id") != str(id)
            or not isinstance(value.get("targets"), list)
            or not 1 <= len(value["targets"]) <= 128
        ):
            raise ValueError("Invalid restart job")
        for item in value["targets"]:
            if (
                not isinstance(item, dict)
                or item.get("state")
                not in TERMINAL
                | {"waiting_for_idle", "blocked", "stopping", "launching", "confirming"}
                or not isinstance(item.get("binding"), dict)
                or not isinstance(item.get("stage"), str)
            ):
                raise ValueError("Invalid restart target")
            UUID(str(item.get("id")))
            binding = item["binding"]
            identity = binding.get("identity")
            if (
                not isinstance(identity, dict)
                or type(identity.get("pid")) is not int
                or identity["pid"] <= 0
            ):
                raise ValueError("Invalid captured restart process")
            source = binding.get("source")
            if (
                not isinstance(binding.get("identity"), dict)
                or not isinstance(source, dict)
                or any(
                    not isinstance(source.get(key), str)
                    for key in ("session_id", "session_file", "folder", "backend")
                )
            ):
                raise ValueError("Invalid restart source binding")
        if not isinstance(value.get("created_at"), str):
            raise ValueError("Invalid restart job timestamp")
        datetime.fromisoformat(value["created_at"])
        self.cache[str(id)] = (signature, value)
        return json.loads(json.dumps(value))

    @staticmethod
    def aggregate(job):
        values = [item["state"] for item in job["targets"]]
        if all(state == "cancelled" for state in values):
            return "cancelled"
        if all(state in TERMINAL for state in values):
            return "attention" if "failed" in values else "completed"
        if all(state in TERMINAL | {"blocked"} for state in values):
            return "attention"
        return "running"

    def save(self, job):
        job["status"] = self.aggregate(job)
        # Do not churn disk/timestamps for unchanged idle polls.
        compare = {k: v for k, v in job.items() if k != "updated_at"}
        try:
            previous = {k: v for k, v in self.read(job["id"]).items() if k != "updated_at"}
        except FileNotFoundError:
            previous = None
        if compare != previous:
            job["updated_at"] = now().isoformat()
            content = json.dumps(job, ensure_ascii=False) + "\n"
            if len(content.encode("utf-8")) > MAX_JOB_BYTES:
                raise ValueError("Restart job exceeds 2 MiB; reduce the target batch")
            atomic_write(self.path(job["id"]), content)
            self.cache.pop(job["id"], None)

    def identity(self, agent):
        if hasattr(self.controller.processes, "identity"):
            return self.controller.processes.identity(agent)
        if self.controller.processes.alive(agent):
            return {
                "pid": self.controller.processes.pid(agent),
                "epoch": getattr(agent, "control_epoch", None),
            }
        return None

    def source(self, agent):
        if not agent.session_id or not agent.session_file:
            raise ValueError("Saved session is not yet available")
        path = local_path(agent.session_file)
        if not path.is_file() or session_header(str(path)) != agent.session_id:
            raise ValueError("Saved history is missing or its ID does not match; restart refused")
        return {
            "session_id": agent.session_id,
            "session_file": str(path),
            "folder": str(local_path(agent.folder)),
            "backend": agent.backend,
        }

    def native(self, agent):
        try:
            if not self.controller.messages.heartbeat(agent):
                return False
            heartbeat = self.controller.messages.read_json(
                self.controller.messages.control(agent.id) / "heartbeat.json", 4096
            )
            return bool(heartbeat.get("capabilities", {}).get("graceful_shutdown"))
        except (OSError, ValueError, TypeError):
            return False

    def candidate(self, agent):
        if not agent.managed:
            return None, "Observed agent; not owned by this controller"
        if agent.backend != "pi":
            return None, "Backend has no restart adapter"
        if active(agent):
            return None, "Already included in an unfinished restart"
        if agent.spawn_state not in {None, "ready"}:
            return None, "Workspace setup is incomplete"
        if busy(agent):
            return None, "Compaction is running"
        if getattr(agent, "stop_pending", False) or getattr(agent, "orchestration_stop", None):
            return None, "Stop is pending"
        identity = self.identity(agent)
        if not identity:
            return None, "No live process"
        try:
            source = self.source(agent)
        except (OSError, ValueError) as exc:
            return None, str(exc)
        binding = {
            "id": str(agent.id),
            "identity": identity,
            "source": source,
            "epoch": getattr(agent, "control_epoch", None),
        }
        token = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        return {
            "id": str(agent.id),
            "name": agent.name,
            "status": str(agent.status),
            "token": token,
            "transport": "native" if self.native(agent) else "legacy",
            "binding": binding,
        }, None

    def preview(self):
        with self.controller.lock:
            self.recover()  # Rebind already-approved intent before offering another job.
            targets, skipped = [], []
            for agent in self.store.scan():
                try:
                    target, reason = self.candidate(agent)
                except (OSError, ValueError, RuntimeError) as exc:
                    target, reason = None, str(exc)
                if target:
                    targets.append({k: v for k, v in target.items() if k != "binding"})
                else:
                    skipped.append({"id": str(agent.id), "name": agent.name, "reason": reason})
            return {"targets": targets, "skipped": skipped, "home": str(self.store.home)}

    def start(self, request: RestartRequest):
        with self.controller.lock:
            self.recover()
            id = str(request.request_id)
            spec = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
            if self.path(id).exists():
                job = self.read(id)
                if job["spec"] != spec:
                    raise RuntimeError("Restart ID already belongs to a different request")
                return {"job": self.describe(job), "reused": True}
            if len({target.id for target in request.targets}) != len(request.targets):
                raise ValueError("Duplicate restart targets")
            targets = []
            for desired in request.targets:
                agent = self.store.get(desired.id)
                value, reason = self.candidate(agent)
                if not value or value["token"] != desired.token:
                    raise RuntimeError(
                        f"Fleet changed for {agent.name}; refresh preview and confirm again. {reason or 'Process changed'}"
                    )
                native = value["transport"] == "native"
                targets.append(
                    {
                        "id": str(agent.id),
                        "name": agent.name,
                        "state": "waiting_for_idle" if native else "blocked",
                        "stage": "Waiting for Pi to become idle"
                        if native
                        else "Native shutdown unavailable; choose an explicit fallback",
                        "error": None
                        if native
                        else "This process does not have the shutdown-capable mailbox extension",
                        "binding": value["binding"],
                        "transport": value["transport"],
                        "bound": False,
                        "deadline": (
                            now() + timedelta(seconds=request.timeout_seconds)
                        ).isoformat(),
                        "shutdown_committed": False,
                        "legacy": False,
                    }
                )
            job = {
                "version": 1,
                "id": id,
                "spec": spec,
                "status": "running",
                "created_at": now().isoformat(),
                "targets": targets,
                "timeout_seconds": request.timeout_seconds,
            }
            self.save(job)  # Durable intent precedes agent leases and all process side effects.
            self.bind(job)
            self.collect()
            try:
                self.controller.refresh()
            except (OSError, ValueError, RuntimeError):
                log.exception("Restart job saved; deferred dashboard refresh")
            return {"job": self.describe(job), "reused": False}

    def marker(self, agent, item, job_id):
        return {
            "restart_job_id": job_id,
            "restart": {
                "job_id": job_id,
                "state": item["state"],
                "stage": item["stage"],
                "error": item.get("error"),
            },
        }

    def matches_source(self, agent, item):
        return self.source(agent) == item["binding"]["source"]

    def bind(self, job):
        for item in job["targets"]:
            if item.get("bound") or item["state"] in TERMINAL:
                continue
            try:
                agent = self.store.get(item["id"])
                identity = self.identity(agent)
                if (
                    not agent.managed
                    or not self.matches_source(agent, item)
                    or (identity is not None and identity != item["binding"]["identity"])
                    or (active(agent) and str(agent.restart_job_id) != job["id"])
                ):
                    raise RuntimeError("Agent/process changed before restart intent could bind")
                self.store.update(agent.id, self.marker(agent, item, job["id"]))
                item["bound"] = True
            except (OSError, ValueError, RuntimeError) as exc:
                item.update(state="failed", stage="Could not reserve agent", error=str(exc)[:4096])
        self.save(job)

    def jobs(self):
        if self.root.is_symlink():
            raise ValueError("Restart storage must not be a symbolic link")
        jobs = []
        for path in self.root.glob("*.json"):
            try:
                jobs.append(self.read(str(UUID(path.stem))))
                self.bad_jobs.pop(path.name, None)
            except (OSError, ValueError):
                try:
                    signature = (path.stat().st_size, path.stat().st_mtime_ns)
                except OSError:
                    signature = None
                if path.name not in self.bad_jobs or self.bad_jobs[path.name] != signature:
                    log.warning("Unreadable restart job %s", path.name)
                    self.bad_jobs[path.name] = signature
                    if len(self.bad_jobs) > 1024:
                        self.bad_jobs.clear()
        return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    def collect(self):
        agents = self.store.scan()
        if self.store.errors:
            return  # Unknown leases must not lose their recovery record.
        leased = {str(getattr(agent, "restart_job_id", "")) for agent in agents}
        finished = [
            job
            for job in self.jobs()
            if job["id"] not in leased
            and all(target["state"] in TERMINAL for target in job["targets"])
        ]
        for job in finished[100:]:
            try:
                self.path(job["id"]).unlink(missing_ok=True)
                self.cache.pop(job["id"], None)
            except OSError:
                log.warning("Could not clean old restart job %s", job["id"])

    def recover(self):
        with self.controller.lock:
            self.launch_budget = 2
            for job in self.jobs():
                if any(
                    not target.get("bound") and target["state"] not in TERMINAL
                    for target in job["targets"]
                ):
                    self.bind(job)

    def describe(self, job):
        targets = []
        for item in job["targets"]:
            precommit = not item.get("shutdown_committed") and item["state"] in {
                "waiting_for_idle",
                "blocked",
            }
            targets.append(
                {k: item.get(k) for k in ("id", "name", "state", "stage", "error")}
                | {
                    "can_cancel": precommit,
                    "can_force": item["state"] in {"waiting_for_idle", "blocked", "stopping"}
                    and item.get("bound", False),
                    "can_legacy": precommit and item.get("transport") == "legacy",
                }
            )
        return {k: job.get(k) for k in ("id", "status", "created_at", "updated_at")} | {
            "targets": targets,
            "counts": {
                state: sum(i["state"] == state for i in targets)
                for state in {i["state"] for i in targets}
            },
        }

    def list(self):
        with self.controller.lock:
            return {"jobs": [self.describe(job) for job in self.jobs()[:20]]}

    def get(self, id):
        with self.controller.lock:
            return {"job": self.describe(self.read(str(id)))}

    def control_file(self, agent):
        path = self.controller.messages.control(agent.id) / "lifecycle.json"
        if path.is_symlink():
            raise ValueError("Lifecycle request must not be a symlink")
        return path

    def request_exit(self, agent, item, job, phase):
        if not getattr(agent, "control_epoch", None):
            return
        path = self.control_file(agent)
        payload = {
            "id": job["id"],
            "agent_id": str(agent.id),
            "epoch": item["binding"]["epoch"],
            "session_id": item["binding"]["source"]["session_id"],
            "action": "shutdown",
            "phase": phase,
            "requested_at": job["created_at"],
        }
        text = json.dumps(payload) + "\n"
        if not path.exists() or path.read_text() != text:
            atomic_write(path, text)

    def remove_request(self, agent, job_id):
        path = self.control_file(agent)
        if path.exists():
            value = self.controller.messages.read_json(path, 8192)
            if value.get("id") == job_id:
                path.unlink(missing_ok=True)

    def ack(self, agent, item, job):
        path = self.controller.messages.control(agent.id) / "lifecycle-ack.json"
        try:
            value = self.controller.messages.read_json(path, 8192)
        except (OSError, ValueError):
            return None
        if (
            value.get("id") == job["id"]
            and value.get("epoch") == item["binding"]["epoch"]
            and value.get("agent_id") == str(agent.id)
            and value.get("session_id") == item["binding"]["source"]["session_id"]
        ):
            return value
        return None

    def finish(self, job, item, agent, state, stage, error=None, pause_recovery=False):
        pause_recovery = pause_recovery or item.get("pause_recovery", False)
        item.update(state=state, stage=stage, error=error, pause_recovery=pause_recovery)
        self.save(job)
        try:
            self.remove_request(agent, job["id"])
        except (OSError, ValueError):
            log.warning("Could not clean completed lifecycle request")
        try:
            self.store.transition(
                agent.id,
                lambda current: (
                    {
                        "restart_job_id": None,
                        "restart": {
                            "job_id": job["id"],
                            "state": state,
                            "stage": stage,
                            "error": error,
                        },
                        **({"auto_resume": False, "last_error": error} if pause_recovery else {}),
                    }
                    if str(getattr(current, "restart_job_id", None)) == job["id"]
                    else None
                ),
            )
        except (OSError, ValueError):
            # The terminal outcome is already durable. Never reopen it as a blocked
            # launch and repeat effects merely because record acknowledgement failed.
            log.warning("Deferred terminal restart acknowledgement for %s", agent.id)

    def guard(self, agent):
        try:
            return self.tick_agent(agent)
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as exc:
            # Keep an orphan lease paused, but never starve unrelated agents/timers.
            try:
                self.store.update(
                    agent.id,
                    {
                        "restart": {
                            "job_id": str(getattr(agent, "restart_job_id", "")),
                            "state": "blocked",
                            "stage": "Restart metadata needs repair",
                            "error": f"{type(exc).__name__}: restart state could not be reconciled. Stop remains available.",
                        }
                    },
                )
            except (OSError, ValueError):
                log.warning("Could not publish orphan restart state for %s", agent.id)
            return True

    def tick_agent(self, agent):
        id = str(getattr(agent, "restart_job_id", ""))
        if not id:
            return False
        job = self.read(id)
        item = next((t for t in job["targets"] if t["id"] == str(agent.id)), None)
        if item is None:
            raise ValueError("Restart target does not match its agent lease")
        if item["state"] in TERMINAL:
            self.finish(job, item, agent, item["state"], item["stage"], item.get("error"))
            return True
        try:
            if item.get("cancel_requested") and not item.get("shutdown_committed"):
                self.remove_request(agent, job["id"])
                if self.control_file(agent).exists():
                    raise RuntimeError(
                        "Cancel cleanup remains pending; no shutdown will be committed"
                    )
                self.finish(job, item, agent, "cancelled", "Cancelled before shutdown commit")
                return True
            current = self.identity(agent)
            if not self.matches_source(agent, item):
                self.finish(
                    job,
                    item,
                    agent,
                    "failed",
                    "Session identity changed; restart refused",
                    "Original history/path/workspace changed; no new process was targeted",
                )
                return True
            if (
                item["state"] == "launching"
                and current is None
                and getattr(agent, "control_epoch", None) == item["binding"]["epoch"]
            ):
                # Real launch writes a fresh bridge epoch before any process is created.
                # This intent was persisted but provably never crossed that boundary.
                item["state"] = "stopping"
            if item["state"] in {"launching", "confirming"}:
                if current is not None:
                    if current == item["binding"]["identity"]:
                        item.update(
                            state="blocked",
                            stage="Original process is still alive",
                            error="No replacement was launched",
                        )
                    elif getattr(agent, "control_epoch", None) != item["binding"]["epoch"]:
                        if self.native(agent):
                            self.finish(
                                job,
                                item,
                                agent,
                                "completed",
                                "Restarted — ready, no prompt submitted",
                            )
                            return True
                        since = datetime.fromisoformat(item["launch_started_at"])
                        if (now() - since).total_seconds() > CONFIRM_SECONDS:
                            self.finish(
                                job,
                                item,
                                agent,
                                "failed",
                                "New process started but mailbox is unavailable",
                                "Inspect Pi startup output. No further restart was attempted.",
                            )
                            return True
                        item.update(
                            state="confirming",
                            stage="Waiting for the replacement Pi mailbox",
                            error=None,
                        )
                    else:
                        self.finish(
                            job,
                            item,
                            agent,
                            "failed",
                            "Process changed independently; not targeted again",
                            "Could not identify the replacement launch",
                        )
                        return True
                else:
                    self.finish(
                        job,
                        item,
                        agent,
                        "failed",
                        "Restart launch did not remain alive",
                        "Inspect terminal output before Wake. Automatic recovery paused to avoid replay.",
                        True,
                    )
                    return True
            elif current is None:
                if hasattr(
                    self.controller.processes, "exited"
                ) and not self.controller.processes.exited(item["binding"]["identity"]):
                    item.update(
                        state="blocked",
                        stage="Original process exit is not confirmed",
                        error="Its tmux pane is unavailable but the captured process may still be alive; no relaunch occurred",
                    )
                    self.save(job)
                    self.store.update(agent.id, self.marker(agent, item, job["id"]))
                    return True
                if self.launch_budget <= 0:
                    return True
                self.launch_budget -= 1
                item.update(
                    state="launching",
                    stage="Opening the same session without a prompt",
                    error=None,
                    launch_started_at=now().isoformat(),
                )
                self.save(job)
                # No Stop/Wake calls: those would clear schedules, status and child waits.
                if not self.controller._launch(
                    agent, None, restart_id=job["id"], restart_identity=item["binding"]["identity"]
                ):
                    error = self.store.get(agent.id).last_error or "Pi launch failed"
                    self.finish(
                        job,
                        item,
                        self.store.get(agent.id),
                        "failed",
                        "Pi launch failed; explicit Wake required",
                        error,
                        True,
                    )
                    return True
            elif (
                current != item["binding"]["identity"]
                or getattr(agent, "control_epoch", None) != item["binding"]["epoch"]
            ):
                self.finish(
                    job,
                    item,
                    agent,
                    "failed",
                    "Process changed independently; not restarted",
                    "The original process was replaced outside this job",
                )
                return True
            elif item.get("shutdown_committed"):
                if not item.get("legacy"):
                    self.request_exit(agent, item, job, "commit")
                item.update(state="stopping", stage="Waiting for the original process to exit")
                receipt = self.ack(agent, item, job) if not item.get("legacy") else None
                if receipt and receipt.get("state") == "failed":
                    item.update(
                        state="blocked",
                        stage="Pi shutdown failed — no force was applied",
                        error=str(receipt.get("error") or "Inspect the original terminal")[:4096],
                    )
                elif now() > datetime.fromisoformat(item["deadline"]):
                    item.update(
                        state="blocked",
                        stage="Still waiting for exit — no automatic kill",
                        error="Choose Force restart only if interruption is acceptable",
                    )
            elif self.native(agent):
                item["transport"] = "native"
                receipt = self.ack(agent, item, job)
                if receipt and receipt.get("state") == "ready":
                    item.update(
                        state="stopping",
                        stage="Requesting graceful exit",
                        error=None,
                        shutdown_committed=True,
                    )
                    self.save(job)  # Commit cancellation boundary before exposing shutdown.
                    self.request_exit(agent, item, job, "commit")
                else:
                    self.request_exit(agent, item, job, "prepare")
                    item.update(
                        state="waiting_for_idle", stage="Waiting for Pi to become idle", error=None
                    )
                    if receipt and receipt.get("state") == "failed":
                        item.update(
                            state="blocked",
                            stage="Pi could not prepare shutdown",
                            error=receipt.get("error"),
                        )
                    elif now() > datetime.fromisoformat(item["deadline"]):
                        item.update(
                            state="blocked",
                            stage="Still waiting for a safe opportunity",
                            error="The current run/draft queue is still active; nothing was killed",
                        )
            else:
                item.update(
                    state="blocked",
                    stage="Native shutdown unavailable",
                    error="Use separately confirmed Legacy /quit or Force restart, or enable the mailbox manually",
                )
            self.save(job)
            self.store.transition(
                agent.id,
                lambda current: (
                    self.marker(current, item, job["id"])
                    if str(getattr(current, "restart_job_id", None)) == job["id"]
                    else None
                ),
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            item.update(state="blocked", stage="Restart needs attention", error=str(exc)[:4096])
            self.save(job)
            try:
                self.store.transition(
                    agent.id,
                    lambda current: (
                        self.marker(current, item, job["id"])
                        if str(getattr(current, "restart_job_id", None)) == job["id"]
                        else None
                    ),
                )
            except (OSError, ValueError):
                log.exception("Could not publish restart attention state")
        return True

    def action(self, id, request: RestartAction, action: str):
        with self.controller.lock:
            job = self.read(str(id))
            targets = []
            public = {i["id"]: i for i in self.describe(job)["targets"]}
            for agent_id in request.agent_ids:
                item = next((i for i in job["targets"] if i["id"] == str(agent_id)), None)
                if item is None or not public[item["id"]].get("can_" + action, False):
                    raise RuntimeError(
                        "Target action is no longer available; refresh the restart job"
                    )
                agent = self.store.get(agent_id)
                if str(getattr(agent, "restart_job_id", None)) != job["id"]:
                    raise RuntimeError("Agent is no longer reserved by this restart")
                targets.append((agent, item))
            for agent, item in targets:
                if action == "cancel":
                    item["cancel_requested"] = True
                    self.save(job)  # A failed cleanup must never later commit shutdown instead.
                    try:
                        self.remove_request(agent, job["id"])
                        if self.control_file(agent).exists():
                            raise RuntimeError("A lifecycle request still exists")
                    except (OSError, ValueError, RuntimeError) as exc:
                        item.update(
                            state="blocked",
                            stage="Cancel cleanup needs attention",
                            error="Prepare request could not be removed; agent remains reserved. Retry Cancel after fixing storage.",
                        )
                        self.save(job)
                        self.store.update(agent.id, self.marker(agent, item, job["id"]))
                        raise RuntimeError(item["error"]) from exc
                    self.finish(job, item, agent, "cancelled", "Cancelled before shutdown commit")
                    continue
                current = self.identity(agent)
                if current is not None and current != item["binding"]["identity"]:
                    raise RuntimeError("Process changed; refresh before targeting a replacement")
                if not self.matches_source(agent, item):
                    raise RuntimeError("Session history changed; action refused")
                if action == "legacy":
                    if self.native(agent):
                        raise RuntimeError(
                            "Native shutdown is available; use the graceful handshake"
                        )
                    if current is None:
                        raise RuntimeError("Original process already exited; no /quit was sent")
                    item.update(
                        state="stopping",
                        stage="Legacy /quit requested; monitoring exit",
                        legacy=True,
                        shutdown_committed=True,
                        error=None,
                    )
                    self.save(job)  # Never retype /quit after an uncertain controller crash.
                    try:
                        self.remove_request(agent, job["id"])
                        if hasattr(self.controller.processes, "legacy_quit"):
                            self.controller.processes.legacy_quit(
                                agent, item["binding"]["identity"]
                            )
                        else:
                            self.controller.processes.send_prompt(agent, "/quit")
                    except (OSError, ValueError, RuntimeError) as exc:
                        item["error"] = (
                            f"Legacy quit outcome is unknown: {exc}. Inspect the terminal before forcing."
                        )
                elif action == "force":
                    item.update(
                        state="stopping",
                        stage="Force restart authorized; monitoring exit",
                        shutdown_committed=True,
                        legacy=True,
                        error=None,
                        forced=True,
                    )
                    self.save(job)
                    if hasattr(self.controller.processes, "stop_identity"):
                        self.controller.processes.stop_identity(agent, item["binding"]["identity"])
                    elif current is not None:
                        self.controller.processes.stop(agent)
                    if self.identity(agent) is not None:
                        item.update(
                            state="blocked",
                            stage="Original process did not stop",
                            error="No replacement was launched",
                        )
                self.save(job)
                self.store.update(agent.id, self.marker(agent, item, job["id"]))
            self.controller.refresh()
            return {"job": self.describe(self.read(job["id"]))}

    def operator_stop(self, agent):
        if not active(agent):
            return
        try:
            job = self.read(str(agent.restart_job_id))
        except (OSError, ValueError, KeyError, TypeError):
            try:
                self.remove_request(agent, str(agent.restart_job_id))
            except (OSError, ValueError):
                pass
            self.store.update(
                agent.id,
                {
                    "restart_job_id": None,
                    "restart": {
                        "job_id": str(agent.restart_job_id),
                        "state": "cancelled",
                        "stage": "Orphaned restart cancelled by Stop",
                        "error": None,
                    },
                },
            )
            return
        item = next((i for i in job["targets"] if i["id"] == str(agent.id)), None)
        if item:
            self.finish(
                job, item, agent, "cancelled", "Cancelled by explicit Stop; no automatic relaunch"
            )
