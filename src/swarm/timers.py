"""Durable one-shot wake delivery for waiting/dormant agents.

The Markdown record is the outbox. A stable event is written before touching tmux,
and acknowledged only after handoff succeeds. Uncertain delivery is at-least-once.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from .adapters import resume_prompt
from .meta_compact import busy
from .models import Agent, Status, WakeDelivery, now

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 300


def scheduled(agent: Agent) -> bool:
    event = agent.wake_delivery
    return bool(
        agent.wake_at
        and (
            agent.status in {Status.waiting, Status.dormant}
            or (event and event.state == "pending" and event.scheduled_for == agent.wake_at)
        )
    )


def eligible(agent: Agent) -> bool:
    return bool(
        agent.managed
        and agent.auto_resume
        and agent.status != Status.done
        and not busy(agent)
        and not getattr(agent, "restart_job_id", None)
        and agent.spawn_state in {None, "ready"}
        and not getattr(agent, "stop_pending", False)
        and not getattr(agent, "orchestration_stop", None)
    )


class ScheduledWakes:
    def __init__(self, controller: Controller):
        self.controller, self.store = controller, controller.store

    @staticmethod
    def matches(agent: Agent, event: WakeDelivery) -> bool:
        current = agent.wake_delivery
        return bool(
            current
            and current.id == event.id
            and current.attempts == event.attempts
            and current.state == "pending"
            and agent.wake_at == event.scheduled_for
        )

    def attempt(self, observed: Agent) -> bool:
        """Attempt one eligible due delivery. Failures stay queued with persisted backoff."""
        clock = now()
        if not scheduled(observed) or not eligible(observed) or observed.wake_at > clock:
            return False
        if (
            observed.wake_delivery
            and observed.wake_delivery.state == "pending"
            and observed.wake_delivery.scheduled_for == observed.wake_at
            and observed.wake_delivery.retry_at
            and observed.wake_delivery.retry_at > clock
        ):
            return False

        def claim(current: Agent):
            if (
                current.wake_at != observed.wake_at
                or not scheduled(current)
                or not eligible(current)
                or current.wake_at > clock
            ):
                return None
            event = current.wake_delivery
            if event and event.state == "pending" and event.scheduled_for == current.wake_at:
                if event.retry_at and event.retry_at > clock:
                    return None
            else:
                event = WakeDelivery(id=uuid4(), scheduled_for=current.wake_at)
            attempts = event.attempts + 1
            delay = min(RETRY_MAX_SECONDS, RETRY_INITIAL_SECONDS * 2 ** min(attempts - 1, 6))
            event = event.model_copy(
                update={
                    "state": "pending",
                    "attempts": attempts,
                    "last_attempt_at": clock,
                    "retry_at": clock + timedelta(seconds=delay),
                }
            )
            return {"wake_delivery": event.model_dump(mode="json")}

        try:
            claimed = self.store.transition(observed.id, claim)
        except (OSError, ValueError) as exc:
            log.warning("Cannot persist scheduled wake for %s: %s", observed.id, exc)
            return False  # Never deliver before the intent is durably recorded.
        if claimed is None:
            return False
        event = claimed.wake_delivery
        assert event is not None
        try:
            # Agent reports share the file lock, not this controller's in-process lock.
            # Recheck cancellation/replacement immediately before the external side effect.
            current = self.store.get(claimed.id)
            if not self.matches(current, event) or not eligible(current):
                return False
            prompt = (
                f"Scheduled wake event {event.id}, requested for {event.scheduled_for.isoformat()}.\n"
                f"Wait reason: {current.wait_reason or '(see record)'}\n"
                "Before working, read your record and verify wake_delivery.id matches this event. "
                "If it does not, the timer was cancelled or replaced; ignore this notification. "
                "This event may be redelivered after a controller interruption. If this event ID "
                "was already handled, or your current record says the goal is done, do not repeat "
                "its work. Verify current state before external "
                "side effects. Schedule a new wake explicitly if monitoring should continue.\n\n"
                + resume_prompt(current)
            )
            if self.controller.processes.alive(current):
                self.controller.processes.send_prompt(current, prompt)
            elif not self.controller._launch(current, prompt):
                raise RuntimeError(self.store.get(current.id).last_error or "Agent launch failed")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            error = f"Scheduled wake {event.id} delivery failed: {exc}"[:4096]
            assert event.retry_at is not None and event.last_attempt_at is not None
            delay = event.retry_at - event.last_attempt_at
            failed = event.model_copy(
                update={"last_error": error, "retry_at": max(event.retry_at, now() + delay)}
            )
            try:
                self.store.transition(
                    claimed.id,
                    lambda current: (
                        {"wake_delivery": failed.model_dump(mode="json"), "last_error": error}
                        if self.matches(current, event)
                        else None
                    ),
                )
            except (OSError, ValueError):
                log.exception(
                    "Could not record wake failure for %s; pending intent remains", claimed.id
                )
            log.warning("%s", error)
            return True

        delivered = event.model_copy(
            update={
                "state": "delivered",
                "retry_at": None,
                "delivered_at": now(),
                "last_error": None,
            }
        )

        def acknowledge(latest: Agent):
            if not self.matches(latest, event):
                return None  # A report cancelled/replaced this timer while delivery was underway.
            fields = {"wake_at": None, "wake_delivery": delivered.model_dump(mode="json")}
            # Do not overwrite a newer agent report/checkpoint produced by the resumed turn.
            if (latest.status, latest.wait_reason, latest.activity, latest.notes) == (
                current.status,
                current.wait_reason,
                current.activity,
                current.notes,
            ):
                if latest.auto_resume:
                    fields.update(status=Status.running, wait_reason="")
            if getattr(latest, "child_wait", None) == getattr(current, "child_wait", None):
                fields["child_wait"] = None
            if latest.last_error == current.last_error or latest.last_error is None:
                fields["last_error"] = None
            return fields

        try:
            self.store.transition(claimed.id, acknowledge)
        except (OSError, ValueError):
            # Handoff may already have happened. Retry the same event ID, never silently drop it.
            log.exception("Scheduled wake %s sent but acknowledgement was not saved", event.id)
        return True
