"""Durable, shared dashboard grouping with family-aware assignments."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import unicodedata
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from .models import Agent
from .store import Store, atomic_write

MAX_GROUPS = 100
MAX_ASSIGNMENTS = 10_000
MAX_RECEIPTS = 256
MAX_FILE_BYTES = 1024 * 1024
RESERVED_NAMES = {"defaultgroup", "default group", "ungrouped"}


class GroupingError(ValueError):
    """A safe-to-display grouping request error."""


class GroupingConflict(RuntimeError):
    """The caller's revision or request identity is stale."""


class GroupingCorrupt(RuntimeError):
    """The shared grouping file cannot safely be used or replaced."""


class Groups:
    def __init__(self, store: Store):
        self.store = store
        self.path = store.home / "groups.json"
        self.lock_path = store.locks_dir / "groups.lock"
        self._last_good: dict[str, Any] | None = None

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": 2,
            "revision": 0,
            "groups": [],
            "assignments": {},
            "receipts": [],
            "settings": {"server_managed": False, "initialized": False},
            "grouped_branches": [],
        }

    @staticmethod
    def projection(state: dict[str, Any]) -> dict[str, Any]:
        """Keep the original groups API projection byte-for-byte compatible in shape."""
        return {
            "revision": state["revision"],
            "groups": [dict(group) for group in state["groups"]],
            "assignments": dict(state["assignments"]),
        }

    @staticmethod
    def settings_projection(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "revision": state["revision"],
            "server_managed": state["settings"]["server_managed"],
            "initialized": state["settings"]["initialized"],
            "grouped_branches": list(state["grouped_branches"]),
        }

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor: int | None = None
        locked = False
        try:
            self.store.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW,
                0o600,
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("invalid lock")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            yield
        except OSError as exc:
            raise GroupingCorrupt("Groups could not be read or saved") from exc
        finally:
            if descriptor is not None:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _uuid(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise GroupingError(f"{label} must be a UUID")
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise GroupingError(f"{label} must be a UUID") from exc

    @classmethod
    def _validate_state(cls, value: Any) -> dict[str, Any]:
        try:
            if not isinstance(value, dict) or type(value.get("version")) is not int:
                raise ValueError
            version = value["version"]
            common = {"version", "revision", "groups", "assignments", "receipts"}
            if version == 1:
                if set(value) != common:
                    raise ValueError
                # A legacy owner already used server-side groups. Migration is
                # deliberately in-memory until the next successful mutation.
                settings = {"server_managed": True, "initialized": False}
                grouped_branches: list[str] = []
            elif version == 2:
                if set(value) != common | {"settings", "grouped_branches"}:
                    raise ValueError
                raw_settings = value["settings"]
                if (
                    not isinstance(raw_settings, dict)
                    or set(raw_settings) != {"server_managed", "initialized"}
                    or type(raw_settings["server_managed"]) is not bool
                    or type(raw_settings["initialized"]) is not bool
                ):
                    raise ValueError
                settings = dict(raw_settings)
                if (
                    not isinstance(value["grouped_branches"], list)
                    or len(value["grouped_branches"]) > MAX_ASSIGNMENTS
                ):
                    raise ValueError
                grouped_branches = [
                    cls._uuid(item, "grouped branch id") for item in value["grouped_branches"]
                ]
                if len(set(grouped_branches)) != len(grouped_branches):
                    raise ValueError
            else:
                raise ValueError
            if not cls._revision(value["revision"]):
                raise ValueError
            groups, ids = cls._validate_groups(value["groups"])
            if (
                not isinstance(value["assignments"], dict)
                or len(value["assignments"]) > MAX_ASSIGNMENTS
            ):
                raise ValueError
            assignments = {
                cls._uuid(agent_id, "agent id"): cls._uuid(group_id, "group id")
                for agent_id, group_id in value["assignments"].items()
            }
            if any(group_id not in ids for group_id in assignments.values()):
                raise ValueError
            receipts = value["receipts"]
            if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
                raise ValueError
            clean_receipts = []
            receipt_ids: set[str] = set()
            for receipt in receipts:
                operation = receipt.get("operation") if isinstance(receipt, dict) else None
                if (
                    not isinstance(receipt, dict)
                    or set(receipt) != {"request_id", "payload_hash", "operation"}
                    or not isinstance(receipt["payload_hash"], str)
                    or len(receipt["payload_hash"]) != 64
                    or any(char not in "0123456789abcdef" for char in receipt["payload_hash"])
                    or not isinstance(operation, dict)
                    or set(operation) != {"action", "group_id"}
                    or operation["action"]
                    not in {"create", "assign", "rename", "delete", "configure", "branches"}
                ):
                    raise ValueError
                operation_group = operation["group_id"]
                if operation_group is not None:
                    operation_group = cls._uuid(operation_group, "group id")
                if operation["action"] in {"configure", "branches"} and operation_group is not None:
                    raise ValueError
                request_id = cls._uuid(receipt["request_id"], "request id")
                if request_id in receipt_ids:
                    raise ValueError
                receipt_ids.add(request_id)
                clean_receipts.append(
                    {
                        "request_id": request_id,
                        "payload_hash": receipt["payload_hash"],
                        "operation": {"action": operation["action"], "group_id": operation_group},
                    }
                )
            return {
                "version": 2,
                "revision": value["revision"],
                "groups": groups,
                "assignments": assignments,
                "receipts": clean_receipts,
                "settings": settings,
                "grouped_branches": grouped_branches,
            }
        except (GroupingError, KeyError, TypeError, ValueError) as exc:
            raise GroupingCorrupt("Saved groups could not be read") from exc

    @classmethod
    def _validate_groups(cls, value: Any) -> tuple[list[dict[str, str]], set[str]]:
        if not isinstance(value, list) or len(value) > MAX_GROUPS:
            raise GroupingError(f"At most {MAX_GROUPS} groups are allowed")
        groups: list[dict[str, str]] = []
        ids: set[str] = set()
        names: set[str] = set()
        for group in value:
            if not isinstance(group, dict) or set(group) != {"id", "name"}:
                raise GroupingError("seed groups must contain only id and name")
            group_id = cls._uuid(group["id"], "group id")
            name = cls._name(group["name"])
            if group_id in ids or name.casefold() in names:
                raise GroupingError("Group ids and names must be unique")
            ids.add(group_id)
            names.add(name.casefold())
            groups.append({"id": group_id, "name": name})
        return groups, ids

    def _read(self) -> dict[str, Any]:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            state = self.empty()
            self._last_good = copy.deepcopy(state)
            return state
        except OSError as exc:
            raise GroupingCorrupt("Saved groups could not be read") from exc
        try:
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValueError
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self.path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_FILE_BYTES:
                    raise ValueError
                chunks: list[bytes] = []
                remaining = MAX_FILE_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                if len(content) > MAX_FILE_BYTES or os.read(descriptor, 1):
                    raise ValueError
            finally:
                os.close(descriptor)
            state = self._validate_state(json.loads(content.decode("utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, GroupingCorrupt) as exc:
            raise GroupingCorrupt("Saved groups could not be read") from exc
        self._last_good = copy.deepcopy(state)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        if len(state["assignments"]) > MAX_ASSIGNMENTS:
            raise GroupingError(
                f"At most {MAX_ASSIGNMENTS} agent assignments are allowed"
            )
        content = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(content.encode()) > MAX_FILE_BYTES:
            raise GroupingError("Saved groups are too large")
        try:
            atomic_write(self.path, content)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise GroupingCorrupt("Groups could not be saved") from exc
        self._last_good = copy.deepcopy(state)

    @staticmethod
    def _revision(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str):
            raise GroupingError(
                "Group name must be 1-80 characters without controls and not reserved"
            )
        name = value.strip()
        if (
            not 1 <= len(name) <= 80
            or any(unicodedata.category(char).startswith("C") for char in name)
            or name.casefold() in RESERVED_NAMES
        ):
            raise GroupingError(
                "Group name must be 1-80 characters without controls and not reserved"
            )
        return name

    @staticmethod
    def _families(agents: list[Agent]) -> tuple[dict[str, set[str]], dict[str, int]]:
        ids = {str(agent.id) for agent in agents}
        adjacent = {agent_id: set() for agent_id in ids}
        children = {agent_id: set() for agent_id in ids}
        parents: dict[str, str] = {}
        for agent in agents:
            agent_id = str(agent.id)
            parent = str(agent.parent_id) if agent.parent_id else None
            if parent in ids:
                adjacent[agent_id].add(parent)
                adjacent[parent].add(agent_id)
                children[parent].add(agent_id)
                parents[agent_id] = parent
        family: dict[str, set[str]] = {}
        rank: dict[str, int] = {}
        unseen = set(ids)
        while unseen:
            first = min(unseen)
            component: set[str] = set()
            todo = [first]
            while todo:
                current = todo.pop()
                if current in component:
                    continue
                component.add(current)
                todo.extend(sorted(adjacent[current] - component, reverse=True))
            unseen -= component
            roots = sorted(node for node in component if parents.get(node) not in component)
            root = roots[0] if roots else min(component)  # deterministic cycle cut
            queue = deque([(root, 0)])
            seen: set[str] = set()
            while queue:
                node, depth = queue.popleft()
                if node in seen:
                    continue
                seen.add(node)
                rank[node] = depth
                queue.extend((child, depth + 1) for child in sorted(children[node] - seen))
            # Defensive fallback for malformed multi-cycle graphs; UUID order is stable.
            for node in sorted(component - seen):
                rank[node] = len(component)
            for node in component:
                family[node] = component
        return family, rank

    @classmethod
    def _reconcile(cls, state: dict[str, Any], agents: list[Agent]) -> bool:
        ids = {str(agent.id) for agent in agents}
        current = {key: value for key, value in state["assignments"].items() if key in ids}
        families, rank = cls._families(agents)
        settled: dict[str, str] = {}
        visited: set[str] = set()
        for agent_id in sorted(ids):
            component = families[agent_id]
            if agent_id in visited:
                continue
            visited |= component
            candidates = [node for node in component if node in current]
            if candidates:
                winner = min(candidates, key=lambda node: (rank[node], node))
                settled.update({node: current[winner] for node in component})
        branches = [item for item in state["grouped_branches"] if item in ids]
        changed = settled != state["assignments"] or branches != state["grouped_branches"]
        state["assignments"] = settled
        state["grouped_branches"] = branches
        return changed

    def get(self) -> dict[str, Any]:
        with self._lock():
            return self.projection(self._read())

    def settings(self) -> dict[str, Any]:
        with self._lock():
            return self.settings_projection(self._read())

    def refresh_with_settings(
        self, agents: list[Agent], *, records_valid: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        try:
            with self._lock():
                state = self._read()
                if records_valid and self._reconcile(state, agents):
                    state["revision"] += 1
                    self._write(state)
                return self.projection(state), self.settings_projection(state), None
        except GroupingError as exc:
            fallback = self._last_good or self.empty()
            return self.projection(fallback), self.settings_projection(fallback), str(exc)
        except GroupingCorrupt:
            fallback = self._last_good or self.empty()
            return (
                self.projection(fallback),
                self.settings_projection(fallback),
                "Saved groups could not be read",
            )

    def refresh(
        self, agents: list[Agent], *, records_valid: bool = True
    ) -> tuple[dict[str, Any], str | None]:
        groups, _settings, error = self.refresh_with_settings(
            agents, records_valid=records_valid
        )
        return groups, error

    @staticmethod
    def _hash(body: dict[str, Any]) -> str:
        try:
            encoded = json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise GroupingError("Grouping request must contain valid JSON values") from exc
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _seed_state(
        cls, seed: Any, agents: list[Agent]
    ) -> tuple[list[dict[str, str]], dict[str, str], list[str]]:
        if not isinstance(seed, dict) or set(seed) != {
            "groups",
            "assignments",
            "grouped_branches",
        }:
            raise GroupingError(
                "seed must contain groups, assignments, and grouped_branches"
            )
        groups, group_ids = cls._validate_groups(seed["groups"])
        raw_assignments = seed["assignments"]
        if not isinstance(raw_assignments, dict) or len(raw_assignments) > MAX_ASSIGNMENTS:
            raise GroupingError("seed assignments must be an object within the assignment limit")
        assignments = {
            cls._uuid(agent_id, "agent id"): cls._uuid(group_id, "group id")
            for agent_id, group_id in raw_assignments.items()
        }
        known = {str(agent.id) for agent in agents}
        if any(agent_id not in known for agent_id in assignments):
            raise GroupingError("seed assignments must reference agents from this Swarm")
        if any(group_id not in group_ids for group_id in assignments.values()):
            raise GroupingError("seed assignments must reference seed groups")

        families, _rank = cls._families(agents)
        normalized: dict[str, str] = {}
        visited: set[str] = set()
        for agent_id in sorted(known):
            family = families[agent_id]
            if agent_id in visited:
                continue
            visited |= family
            selected = {assignments[item] for item in family if item in assignments}
            if len(selected) > 1:
                raise GroupingError("A seeded agent family cannot be assigned to multiple groups")
            if selected:
                group_id = next(iter(selected))
                normalized.update({item: group_id for item in family})
        branches = cls._branch_ids(seed["grouped_branches"], known)
        return groups, normalized, branches

    @classmethod
    def _branch_ids(cls, value: Any, known: set[str]) -> list[str]:
        if not isinstance(value, list) or len(value) > MAX_ASSIGNMENTS:
            raise GroupingError("grouped_branches must be a list within the assignment limit")
        branches = [cls._uuid(item, "grouped branch id") for item in value]
        if len(set(branches)) != len(branches) or any(item not in known for item in branches):
            raise GroupingError(
                "grouped_branches must contain distinct agents from this Swarm"
            )
        return branches

    def apply_settings(
        self, body: Any, agents: list[Agent], *, records_valid: bool = True
    ) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise GroupingError("Request body must be an object")
        action = body.get("action")
        if action == "configure":
            required, optional = {"action", "revision", "request_id", "server_managed"}, {"seed"}
        elif action == "branches":
            required, optional = {
                "action",
                "revision",
                "request_id",
                "grouped_branches",
            }, set()
        else:
            raise GroupingError("Unknown grouping settings action")
        if not required <= set(body) or set(body) - required - optional:
            raise GroupingError(f"Invalid fields for {action}")
        if not self._revision(body.get("revision")):
            raise GroupingError("revision must be a nonnegative integer")
        if action == "configure" and type(body.get("server_managed")) is not bool:
            raise GroupingError("server_managed must be a boolean")
        request_id = self._uuid(body.get("request_id"), "request_id")
        payload_hash = self._hash(body)
        with self._lock():
            state = self._read()
            for receipt in state["receipts"]:
                if receipt["request_id"] == request_id:
                    if receipt["payload_hash"] != payload_hash:
                        raise GroupingConflict(
                            "request_id was already used for a different operation"
                        )
                    return self.settings_projection(state)
            if not records_valid:
                raise GroupingConflict(
                    "Agent records could not be read; grouping settings were not changed"
                )
            if body["revision"] != state["revision"]:
                raise GroupingConflict("Group revision changed; refresh and retry")

            known = {str(agent.id) for agent in agents}
            operation = {"action": action, "group_id": None}
            if action == "configure":
                enabled = body["server_managed"]
                seed_present = "seed" in body
                if seed_present and (not enabled or state["settings"]["initialized"]):
                    raise GroupingConflict(
                        "seed is accepted only on the first explicit server-managed enable"
                    )
                if seed_present:
                    groups, assignments, branches = self._seed_state(body["seed"], agents)
                    state["groups"] = groups
                    state["assignments"] = assignments
                    state["grouped_branches"] = branches
                state["settings"]["server_managed"] = enabled
                if enabled:
                    state["settings"]["initialized"] = True
            else:
                if not state["settings"]["server_managed"]:
                    raise GroupingConflict(
                        "grouped branches can be changed only in server-managed mode"
                    )
                state["grouped_branches"] = self._branch_ids(
                    body["grouped_branches"], known
                )

            state["revision"] += 1
            state["receipts"] = (
                state["receipts"]
                + [{"request_id": request_id, "payload_hash": payload_hash, "operation": operation}]
            )[-MAX_RECEIPTS:]
            self._write(state)
            return self.settings_projection(state)

    def apply(
        self, body: Any, agents: list[Agent], *, records_valid: bool = True
    ) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise GroupingError("Request body must be an object")
        action = body.get("action")
        fields = {
            "create": ({"action", "revision", "request_id", "name"}, {"agent_ids"}),
            "assign": ({"action", "revision", "request_id", "group_id", "agent_ids"}, set()),
            "rename": ({"action", "revision", "request_id", "group_id", "name"}, set()),
            "delete": ({"action", "revision", "request_id", "group_id"}, set()),
        }
        if not isinstance(action, str) or action not in fields:
            raise GroupingError("Unknown grouping action")
        required, optional = fields[action]
        if not required <= set(body) or set(body) - required - optional:
            raise GroupingError(f"Invalid fields for {action}")
        if not self._revision(body.get("revision")):
            raise GroupingError("revision must be a nonnegative integer")
        request_id = self._uuid(body.get("request_id"), "request_id")
        payload_hash = self._hash(body)
        with self._lock():
            state = self._read()
            for receipt in state["receipts"]:
                if receipt["request_id"] == request_id:
                    if receipt["payload_hash"] != payload_hash:
                        raise GroupingConflict(
                            "request_id was already used for a different operation"
                        )
                    return self.projection(state) | {"operation": receipt["operation"]}
            if not records_valid:
                raise GroupingConflict("Agent records could not be read; groups were not changed")
            if not state["settings"]["server_managed"]:
                raise GroupingConflict(
                    "Group changes require server-managed grouping; enable server-managed grouping"
                )
            if body["revision"] != state["revision"]:
                raise GroupingConflict("Group revision changed; refresh and retry")

            self._reconcile(state, agents)
            known_agents = {str(agent.id) for agent in agents}
            raw_agent_ids = body.get("agent_ids", [])
            if not isinstance(raw_agent_ids, list):
                raise GroupingError("agent_ids must be a list")
            agent_ids = [self._uuid(value, "agent id") for value in raw_agent_ids]
            if len(set(agent_ids)) != len(agent_ids) or any(
                x not in known_agents for x in agent_ids
            ):
                raise GroupingError("agent_ids must contain distinct agents from this Swarm")
            group_id = body.get("group_id")
            if group_id is not None:
                group_id = self._uuid(group_id, "group_id")
            groups = {group["id"]: group for group in state["groups"]}
            operation: dict[str, Any] = {"action": action, "group_id": group_id}

            if action == "create":
                if len(groups) >= MAX_GROUPS:
                    raise GroupingError("At most 100 groups are allowed")
                name = self._name(body["name"])
                if any(group["name"].casefold() == name.casefold() for group in groups.values()):
                    raise GroupingError("Group names must be unique")
                group_id = str(uuid4())
                state["groups"].append({"id": group_id, "name": name})
                operation["group_id"] = group_id
            elif action == "assign":
                if group_id is not None and group_id not in groups:
                    raise GroupingError("assign requires an existing group_id or null")
            elif action == "rename":
                if group_id not in groups:
                    raise GroupingError("rename requires an existing group_id and name")
                name = self._name(body["name"])
                if any(
                    existing_id != group_id and group["name"].casefold() == name.casefold()
                    for existing_id, group in groups.items()
                ):
                    raise GroupingError("Group names must be unique")
                groups[group_id]["name"] = name
            elif action == "delete":
                if group_id not in groups:
                    raise GroupingError("delete requires an existing group_id")
                state["groups"] = [group for group in state["groups"] if group["id"] != group_id]
                state["assignments"] = {
                    key: value for key, value in state["assignments"].items() if value != group_id
                }

            if action in {"create", "assign"}:
                families, _ = self._families(agents)
                expanded = (
                    set().union(*(families[agent_id] for agent_id in agent_ids))
                    if agent_ids
                    else set()
                )
                for agent_id in expanded:
                    if group_id is None:
                        state["assignments"].pop(agent_id, None)
                    else:
                        state["assignments"][agent_id] = group_id
            state["revision"] += 1
            state["receipts"] = (
                state["receipts"]
                + [{"request_id": request_id, "payload_hash": payload_hash, "operation": operation}]
            )[-MAX_RECEIPTS:]
            self._write(state)
            return self.projection(state) | {"operation": operation}
