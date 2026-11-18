"""Read-only discovery for the new-agent form."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PureWindowsPath

from .paths import local_path

MODEL_TIMEOUT_SECONDS = 15
MAX_DIRECTORY_ENTRIES = 2000


def _agent_dir() -> Path:
    return local_path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent"))


def configured_default_model() -> str | None:
    """Return Pi's user-level provider/model default without exposing other settings."""
    try:
        settings = json.loads((_agent_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(settings, dict):
        return None
    provider, model = settings.get("defaultProvider"), settings.get("defaultModel")
    if not isinstance(model, str) or not model.strip():
        return None
    if isinstance(provider, str) and provider.strip():
        return f"{provider.strip()}/{model.strip()}"
    return model.strip()


def discover_models(pi: str) -> dict:
    """Ask the configured Pi executable for the chat models it can currently use."""
    default = configured_default_model()
    try:
        result = subprocess.run(
            [pi, "--list-models"],
            text=True,
            capture_output=True,
            timeout=MODEL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"models": [], "default": default, "error": f"Cannot list Pi models: {exc}"}
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "pi --list-models failed"
        return {"models": [], "default": default, "error": detail[:2000]}

    models = []
    seen = set()
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 6 or columns[:2] == ["provider", "model"]:
            continue
        provider, model = columns[:2]
        model_id = f"{provider}/{model}"
        if model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "provider": provider,
                "model": model,
                "context": columns[2] if len(columns) > 2 else "",
                "thinking": columns[4] if len(columns) > 4 else "",
                "images": columns[5] if len(columns) > 5 else "",
            }
        )
    models.sort(key=lambda item: (item["provider"].casefold(), item["model"].casefold()))
    return {"models": models, "default": default, "error": None}


def _is_git_worktree(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def browse_folders(value: str | None = None) -> dict:
    """List one server-local directory level for the dashboard folder picker."""
    path = local_path(value) if value else Path.home().resolve()
    if not path.is_dir():
        raise ValueError("folder browser path must be an existing directory")
    try:
        children = sorted(
            (child.resolve() for child in path.iterdir() if child.is_dir()),
            key=lambda child: child.name.casefold(),
        )
    except OSError as exc:
        raise ValueError(f"cannot read folder: {exc}") from exc
    truncated = len(children) > MAX_DIRECTORY_ENTRIES
    children = children[:MAX_DIRECTORY_ENTRIES]
    parent = path.parent if path.parent != path else None
    return {
        "path": str(path),
        "parent": str(parent) if parent else None,
        "directories": [{"name": child.name, "path": str(child)} for child in children],
        "git": _is_git_worktree(path),
        "truncated": truncated,
    }


def canonical_worktree_parent(value: str | Path) -> Path | None:
    """Validate and canonicalize an optional server-local worktree parent."""
    raw = str(value)
    if not raw.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError("worktree_parent cannot contain control characters")
    expanded = os.path.expanduser(raw)
    if not (Path(expanded).is_absolute() or PureWindowsPath(expanded).is_absolute()):
        raise ValueError("worktree_parent must be an absolute path")
    parent = local_path(expanded)
    if not parent.is_absolute():
        raise ValueError("worktree_parent must map to an absolute local path")
    return parent


def sibling_worktree_path(
    source: str | Path, branch: str, worktree_parent: str | Path | None = None
) -> Path:
    """Return a safe automatic destination, optionally below a custom parent."""
    if any(ord(character) < 32 or ord(character) == 127 for character in branch):
        raise ValueError("branch cannot contain control characters")
    safe_branch = branch.replace("/", "_").replace("\\", "_")
    if not safe_branch:
        raise ValueError("branch is required when creating a worktree")
    if safe_branch in {".", ".."}:
        raise ValueError("branch does not produce a safe worktree folder name")

    parent = canonical_worktree_parent(worktree_parent or "")
    if parent is None:
        source_path = local_path(source)
        return source_path.with_name(f"{source_path.name}_{safe_branch}")

    destination = parent / safe_branch
    if destination.parent != parent or destination == parent:
        raise ValueError("worktree destination escapes its parent")
    return destination
