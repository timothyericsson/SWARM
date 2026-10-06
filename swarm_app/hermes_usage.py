"""Read Hermes model usage through the ``hermes usage`` CLI.

Whatever provider/model Hermes is configured with is what gets reported; this
module never stores credentials and never starts a session. Call
fetch_hermes_usage from a worker thread; it performs bounded blocking I/O.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time

from .usage_types import UsageSnapshot, UsageUnavailable, UsageWindow


MAX_OUTPUT_BYTES = 256 * 1024
re_hours = re.compile(r"(\d+)\s*-?\s*hour")


def _optional_number(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _parse_timestamp(value) -> int | None:
    """Accept epoch seconds or an ISO-8601 string; return epoch seconds."""
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799:
        return int(value)
    if isinstance(value, str) and value:
        try:
            import datetime
            parsed = datetime.datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return None
    return None


def _parse_window(window) -> tuple[UsageWindow, int] | None:
    """Return (window, sort minutes) with unknown duration sorting last."""
    if not isinstance(window, dict):
        return None
    used = _optional_number(window.get("used_percent"))
    if used is None:
        return None
    remaining = min(100.0, max(0.0, 100.0 - used))
    resets_at = _parse_timestamp(window.get("resets_at"))
    label = window.get("label")
    label = label.strip() if isinstance(label, str) and label.strip() else None
    # Window labels across providers look like "5-hour window", "weekly",
    # "7-day", "monthly". Map the common words to minutes for ordering.
    minutes = None
    if label:
        lowered = label.lower()
        if "week" in lowered or "7-day" in lowered:
            minutes = 10080
        elif "month" in lowered:
            minutes = 43200
        elif "day" in lowered:
            minutes = 1440
        elif "hour" in lowered:
            match = re_hours.match(lowered)
            minutes = 60 * (int(match.group(1)) if match else 5)
    return UsageWindow(remaining, minutes, resets_at, label), (minutes if minutes is not None else 1 << 30)


def parse_snapshot(document) -> UsageSnapshot:
    """Parse one ``hermes usage --json`` document; raise UsageUnavailable."""
    if not isinstance(document, dict):
        raise UsageUnavailable("Hermes returned an invalid usage response.")
    reason = document.get("unavailable_reason")
    if isinstance(reason, str) and reason.strip():
        raise UsageUnavailable("Hermes could not read usage for its configured model.")
    windows = document.get("windows")
    if not isinstance(windows, list):
        raise UsageUnavailable("Hermes returned an invalid usage response.")
    parsed = []
    for window in windows:
        entry = _parse_window(window)
        if entry is not None:
            parsed.append(entry)
    if not parsed:
        raise UsageUnavailable("Hermes did not report a usage limit.")
    parsed.sort(key=lambda entry: entry[1])
    primary, secondary = parsed[0][0], (parsed[1][0] if len(parsed) > 1 else None)
    return UsageSnapshot(primary, secondary)


def fetch_hermes_usage(hermes: str, timeout: float = 20, cancel=None) -> UsageSnapshot:
    """Read the configured Hermes model's account limits without a session.

    hermes is one executable name or path, never a shell command. Windows the
    provider does not report are unknown, not zero.
    """
    if cancel is not None and cancel.is_set():
        raise UsageUnavailable("Usage refresh cancelled.")
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise UsageUnavailable("Usage refresh needs a positive timeout.")
    if not isinstance(hermes, str) or not hermes or "\0" in hermes:
        raise UsageUnavailable("Hermes CLI was not found.")
    deadline = time.monotonic() + min(timeout, 60)
    try:
        process = subprocess.Popen(
            [hermes, "usage", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise UsageUnavailable("Could not start Hermes to read usage.") from exc
    try:
        output, _stderr = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
        if cancel is not None and cancel.is_set():
            raise UsageUnavailable("Usage refresh cancelled.")
        if len(output) > MAX_OUTPUT_BYTES:
            raise UsageUnavailable("Hermes returned too much usage data.")
        if process.returncode != 0:
            # Exit 1 covers: no credential, provider has no usage endpoint,
            # or the fetch failed. Never surface provider error text.
            raise UsageUnavailable("Hermes could not read usage for its configured model.")
        try:
            document = json.loads(output.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UsageUnavailable("Hermes returned an invalid usage response.") from exc
        return parse_snapshot(document)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=5)
        raise UsageUnavailable("Usage refresh timed out.") from exc
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
