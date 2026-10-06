"""Shared value types for per-harness usage snapshots.

``usage.py`` (Codex app-server) and ``hermes_usage.py`` (Hermes CLI) each
import these dataclasses; neither tracker imports the other.
"""

from __future__ import annotations

from dataclasses import dataclass


class UsageUnavailable(Exception):
    """A sanitized reason why current usage could not be read."""


@dataclass(frozen=True)
class UsageWindow:
    remaining: float
    duration_minutes: int | None
    resets_at: int | None
    label: str | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    # The shortest known window comes first. A single weekly window is primary.
    primary: UsageWindow | None
    secondary: UsageWindow | None
    reset_credits_remaining: int | None = None
