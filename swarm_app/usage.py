"""Read account limits through a short-lived Codex app-server connection.

This module reads no credentials itself and creates no Codex threads or turns.
Call fetch_usage from a worker thread; it performs bounded blocking I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import select
import selectors
import subprocess
import threading
import time

from . import __version__


MAX_LINE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
POLL_SECONDS = 0.1


class UsageUnavailable(Exception):
    """A sanitized reason why current usage could not be read."""


@dataclass(frozen=True)
class UsageWindow:
    remaining: float
    duration_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True)
class UsageSnapshot:
    # The shortest known window comes first. A single weekly window is primary.
    primary: UsageWindow | None
    secondary: UsageWindow | None
    reset_credits_remaining: int | None = None


def _optional_integer(value, minimum: int, maximum: int) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None


def _parse_window(value) -> UsageWindow | None:
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent")
    if type(used) not in (int, float):
        return None
    try:
        used = float(used)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(used):
        return None
    return UsageWindow(
        remaining=min(100.0, max(0.0, 100.0 - used)),
        duration_minutes=_optional_integer(value.get("windowDurationMins"), 1, 2**31 - 1),
        resets_at=_optional_integer(value.get("resetsAt"), 0, 253402300799),
    )


def _parse_snapshot(result) -> UsageSnapshot:
    if not isinstance(result, dict):
        raise UsageUnavailable("Codex returned an invalid usage response.")
    reset_credits = result.get("rateLimitResetCredits")
    reset_credits_remaining = (
        _optional_integer(reset_credits.get("availableCount"), 0, 2**63 - 1)
        if isinstance(reset_credits, dict) else None
    )
    buckets = result.get("rateLimitsByLimitId")
    if buckets is not None:
        # A present map is authoritative, even if it only contains other limits.
        bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    else:
        bucket = result.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        return UsageSnapshot(None, None, reset_credits_remaining)
    primary = _parse_window(bucket.get("primary"))
    secondary = _parse_window(bucket.get("secondary"))
    if primary is None:
        primary, secondary = secondary, None
    elif (
        secondary is not None
        and primary.duration_minutes is not None
        and secondary.duration_minutes is not None
        and secondary.duration_minutes < primary.duration_minutes
    ):
        primary, secondary = secondary, primary
    return UsageSnapshot(primary, secondary, reset_credits_remaining)


class _Connection:
    def __init__(self, process, deadline: float, cancel: threading.Event | None):
        self.process = process
        self.deadline = deadline
        self.cancel = cancel
        self.buffer = bytearray()
        self.received_bytes = 0
        self.selector = selectors.DefaultSelector()
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        self.selector.register(process.stdout, selectors.EVENT_READ)

    def _remaining(self) -> float:
        if self.cancel is not None and self.cancel.is_set():
            raise UsageUnavailable("Usage refresh cancelled.")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise UsageUnavailable("Usage refresh timed out.")
        return min(remaining, POLL_SECONDS)

    def send(self, message: dict) -> None:
        data = memoryview((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        descriptor = self.process.stdin.fileno()
        while data:
            interval = self._remaining()
            try:
                count = os.write(descriptor, data)
            except BlockingIOError:
                select.select([], [descriptor], [], interval)
                continue
            except OSError as exc:
                raise UsageUnavailable("Codex closed the usage connection.") from exc
            if count <= 0:
                raise UsageUnavailable("Codex closed the usage connection.")
            data = data[count:]

    def result(self, request_id: int):
        while True:
            interval = self._remaining()
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE_BYTES:
                    raise UsageUnavailable("Codex returned an oversized usage response.")
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                    raise UsageUnavailable("Codex returned an invalid usage response.") from exc
                if not isinstance(message, dict):
                    raise UsageUnavailable("Codex returned an invalid usage response.")
                # Notifications and server-initiated requests cannot satisfy a
                # response, even if their IDs happen to match ours.
                if "method" in message or type(message.get("id")) is not int or message["id"] != request_id:
                    continue
                if message.get("error") is not None:
                    # Never expose server error text: it can include account data.
                    raise UsageUnavailable("Codex could not provide usage. Check your ChatGPT sign-in.")
                if "result" not in message:
                    raise UsageUnavailable("Codex returned an invalid usage response.")
                return message["result"]
            if len(self.buffer) > MAX_LINE_BYTES:
                raise UsageUnavailable("Codex returned an oversized usage response.")
            if not self.selector.select(interval):
                continue
            try:
                data = os.read(self.process.stdout.fileno(), 64 * 1024)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise UsageUnavailable("Codex closed the usage connection.") from exc
            if not data:
                raise UsageUnavailable("Codex closed the usage connection.")
            self.received_bytes += len(data)
            if self.received_bytes > MAX_OUTPUT_BYTES:
                raise UsageUnavailable("Codex returned too much usage response data.")
            self.buffer.extend(data)


def _close_helper(process: subprocess.Popen) -> None:
    """Close and reap only our child, without signaling shared daemons or agents."""
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.wait(timeout=0.15)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
    finally:
        if process.stdout is not None:
            process.stdout.close()


def fetch_usage(codex: str, timeout: float = 15, cancel: threading.Event | None = None) -> UsageSnapshot:
    """Read current account limits without starting a conversation or model turn.

    codex is one executable path/name, never a shell command. Empty/missing
    windows are unknown; they are not represented as zero percent remaining.
    """
    if cancel is not None and cancel.is_set():
        raise UsageUnavailable("Usage refresh cancelled.")
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise UsageUnavailable("Usage refresh needs a positive timeout.")
    if not isinstance(codex, str) or not codex or "\0" in codex:
        raise UsageUnavailable("Codex CLI was not found.")
    deadline = time.monotonic() + min(timeout, 60)
    try:
        process = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise UsageUnavailable("Could not start Codex to read usage.") from exc
    connection = None
    try:
        connection = _Connection(process, deadline, cancel)
        connection.send({
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "swarm", "title": "SWARM", "version": __version__}},
        })
        if not isinstance(connection.result(1), dict):
            raise UsageUnavailable("Codex could not initialize the usage connection.")
        connection.send({"method": "initialized", "params": {}})
        connection.send({"id": 2, "method": "account/rateLimits/read"})
        return _parse_snapshot(connection.result(2))
    except OSError as exc:
        raise UsageUnavailable("Could not read usage from Codex.") from exc
    finally:
        if connection is not None:
            connection.selector.close()
        try:
            _close_helper(process)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UsageUnavailable("Could not close the Codex usage helper.") from exc
