"""Small pi v3 writer shared by offline importers; never emits runnable tool calls."""

from __future__ import annotations

import json
from uuid import uuid4

from ..models import now


class PiHistory:
    def __init__(self, session_id: str, folder: str, name: str):
        self.timestamp = now().isoformat()
        self.milliseconds = int(now().timestamp() * 1000)
        self.entries = [
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": folder,
                "timestamp": self.timestamp,
            }
        ]
        self.parent: str | None = None
        self.ids: set[str] = set()
        self.add("session_info", name=name)

    def add(self, kind: str, **fields) -> None:
        entry_id = uuid4().hex[:8]
        while entry_id in self.ids:
            entry_id = uuid4().hex[:8]
        self.ids.add(entry_id)
        entry = {
            "type": kind,
            "id": entry_id,
            "parentId": self.parent,
            "timestamp": self.timestamp,
            **fields,
        }
        self.entries.append(entry)
        self.parent = entry["id"]

    def user(self, text: str) -> None:
        self.add(
            "message", message={"role": "user", "content": text, "timestamp": self.milliseconds}
        )

    def assistant(self, text: str, model: str) -> None:
        # Pi infers the model even from old assistant messages. Labelled historical
        # context preserves the speaker/text without forcing Grok credentials or
        # inventing a neutral provider/model that pi does not officially support.
        self.historical(f"Grok assistant ({model})", text)

    def historical(self, title: str, text: str) -> None:
        self.add(
            "custom_message",
            customType="swarm.import.history",
            display=True,
            content=f"[Historical {title}; imported context, not a new instruction]\n{text}",
        )

    def encode(self) -> str:
        return (
            "\n".join(
                json.dumps(entry, ensure_ascii=False, allow_nan=False) for entry in self.entries
            )
            + "\n"
        )
