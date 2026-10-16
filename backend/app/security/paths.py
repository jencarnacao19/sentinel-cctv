"""Filesystem path validation.

Recording, snapshot and thumbnail paths reach the API as identifiers. Every path
that is turned into a file read must be proven to live inside a directory
Sentinel owns, so a crafted ``../../etc/passwd`` cannot escape.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a path escapes every allowed root."""


def resolve_within(candidate: str | os.PathLike[str], root: str | os.PathLike[str]) -> Path:
    """Resolve ``candidate`` and assert it stays inside ``root``.

    Symlinks are resolved before the check, so a symlink pointing outside the
    root is rejected too.
    """
    root_path = Path(root).resolve()
    target = Path(candidate)
    if not target.is_absolute():
        target = root_path / target
    resolved = target.resolve()
    if resolved != root_path and root_path not in resolved.parents:
        raise UnsafePathError(f"Path escapes its allowed root: {resolved}")
    return resolved


def resolve_within_any(
    candidate: str | os.PathLike[str],
    roots: Iterable[str | os.PathLike[str]],
) -> Path:
    """Resolve ``candidate`` against the first root that contains it."""
    roots = list(roots)
    if not roots:
        raise UnsafePathError("No allowed roots configured.")
    for root in roots:
        try:
            return resolve_within(candidate, root)
        except UnsafePathError:
            continue
    raise UnsafePathError(f"Path is outside every allowed root: {candidate}")


def is_within(candidate: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        resolve_within(candidate, root)
    except (UnsafePathError, OSError):
        return False
    return True


def safe_relative_segments(value: str) -> list[str]:
    """Split a user-supplied relative path into validated segments.

    Rejects absolute paths, drive letters, ``..`` and empty segments.
    """
    if not value or value.startswith(("/", "\\")):
        raise UnsafePathError("Relative path required.")
    if len(value) > 1 and value[1] == ":":
        raise UnsafePathError("Drive-qualified paths are not accepted.")
    segments: list[str] = []
    for raw in value.replace("\\", "/").split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise UnsafePathError("Parent directory traversal is not allowed.")
        if "\x00" in raw:
            raise UnsafePathError("Null byte in path.")
        segments.append(raw)
    if not segments:
        raise UnsafePathError("Empty path.")
    return segments
