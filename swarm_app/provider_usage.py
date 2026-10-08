"""Read usage for explicitly selected provider credentials, without inference.

OpenRouter's ordinary-key endpoint reports a key spending cap, not the user's
account balance. Its account credits endpoint requires a management key, so we
do not request it. See:
https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key
https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits

Unsupported providers return an unavailable percentage without making a request.
Call fetch_provider_usage in a worker thread, never on the GTK event loop.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import subprocess
import sys
import threading
import time

from .deepseek_usage import fetch_deepseek_usage
from .usage import UsageUnavailable, _close_helper
from .zai_usage import fetch_zai_usage


SUPPORTED_PROVIDERS = frozenset({"zai", "deepseek", "openrouter"})
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
MAX_RESPONSE_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    percent_remaining: float | None
    detail: str
    label: str = ""


# Run network I/O in a cancellable child: this also bounds DNS resolution and
# slow response bodies. Only the fixed official host receives the key, via an
# Authorization header; the parent passes it through stdin, never argv.
_HTTP_HELPER = r'''
import json, sys, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
try:
    data = json.loads(sys.stdin.buffer.read(16384))
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": "Bearer " + data["key"], "Accept": "application/json"},
        method="GET")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=data["timeout"]) as response:
        body = response.read(262145)
        if len(body) > 262144:
            sys.exit(4)
        sys.stdout.buffer.write(body)
except urllib.error.HTTPError as error:
    sys.exit(2 if error.code in (401, 403) else 3)
except Exception:
    sys.exit(3)
'''


def _validate_key(api_key):
    if not isinstance(api_key, str):
        raise UsageUnavailable("A valid provider API key is required.")
    key = api_key.strip()
    if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise UsageUnavailable("A valid provider API key is required.")
    return key


def _number(value):
    if type(value) not in (int, float):
        raise ValueError("invalid amount")
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1e15:
        raise ValueError("invalid amount")
    return number


def _parse_openrouter(response):
    try:
        data = response["data"]
        if not isinstance(data, dict) or "limit" not in data or "limit_remaining" not in data:
            raise ValueError("missing key limits")
        limit = _number(data["limit"]) if data["limit"] is not None else None
        remaining = _number(data["limit_remaining"]) if data["limit_remaining"] is not None else None
        usage = _number(data["usage"]) if data.get("usage") is not None else None
        if (limit is not None and limit < 0) or (usage is not None and usage < 0):
            raise ValueError("invalid amount")
        rows = []
        if limit is None:
            rows.append("This OpenRouter key has no spending cap, so a remaining percentage is unavailable.")
            percent = None
        elif remaining is None:
            rows.append(f"Key spending cap: USD {limit:.2f}. OpenRouter did not report the remaining amount.")
            percent = None
        else:
            percent = min(100.0, max(0.0, 100.0 * remaining / limit)) if limit > 0 else 0.0
            rows.append(f"Key budget: USD {remaining:.2f} left of USD {limit:.2f}.")
        reset = data.get("limit_reset")
        if isinstance(reset, str) and reset in {"daily", "weekly", "monthly"}:
            rows.append(f"The key spending cap resets {reset}.")
        if usage is not None:
            rows.append(f"Total usage charged to this key: USD {usage:.2f}.")
        rows.append("This is the key spending cap, not the account credit balance or model rate limit.")
        # Never expose the remote label: OpenRouter includes a key prefix there.
        return ProviderUsageSnapshot(percent, "\n".join(rows), "N/A" if percent is None else "")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise UsageUnavailable("OpenRouter returned an invalid usage response.") from exc


def _fetch_openrouter(api_key, timeout, cancel):
    deadline = time.monotonic() + timeout
    request = json.dumps({"key": api_key, "timeout": timeout}).encode("utf-8")
    try:
        process = subprocess.Popen([sys.executable, "-c", _HTTP_HELPER], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    except (OSError, ValueError) as exc:
        raise UsageUnavailable("Could not start the OpenRouter usage helper.") from exc
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise UsageUnavailable("Usage refresh cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageUnavailable("Usage refresh timed out.")
            try:
                output, _ = process.communicate(request, timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                request = None
        if process.returncode == 2:
            raise UsageUnavailable("OpenRouter rejected the API key. Check your saved credentials.")
        if process.returncode == 4 or len(output) > MAX_RESPONSE_BYTES:
            raise UsageUnavailable("OpenRouter returned an oversized usage response.")
        if process.returncode != 0:
            raise UsageUnavailable("Could not reach OpenRouter to read usage.")
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise UsageUnavailable("OpenRouter returned an invalid usage response.") from exc
        if cancel is not None and cancel.is_set():
            raise UsageUnavailable("Usage refresh cancelled.")
        return _parse_openrouter(response)
    except OSError as exc:
        raise UsageUnavailable("Could not read OpenRouter usage.") from exc
    finally:
        try:
            _close_helper(process)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UsageUnavailable("Could not close the OpenRouter usage helper.") from exc


def _zai_snapshot(snapshot):
    if snapshot.primary is None:
        return ProviderUsageSnapshot(None, "Z.ai did not report a Coding Plan model quota.", "N/A")
    rows = ["Shared Z.ai Coding Plan model quota:"]
    for window in (snapshot.primary, snapshot.secondary):
        if window is None:
            continue
        minutes = window.duration_minutes
        if minutes is None:
            period = "Quota window"
        elif minutes % 60 == 0:
            period = f"{minutes // 60}-hour window"
        else:
            period = f"{minutes}-minute window"
        row = f"{period}: {math.floor(window.remaining)}% left"
        if window.resets_at is not None:
            reset = datetime.fromtimestamp(window.resets_at, timezone.utc)
            row += f"; resets {reset:%Y-%m-%d %H:%M UTC}"
        rows.append(row)
    return ProviderUsageSnapshot(float(snapshot.primary.remaining), "\n".join(rows))


def _deepseek_snapshot(snapshot):
    rows = [f"{balance.currency} {balance.total:.2f} remaining "
            f"({math.floor(balance.remaining)}% of the highest observed balance, "
            f"{balance.currency} {balance.baseline:.2f})." for balance in snapshot.balances]
    rows.append("DeepSeek reports prepaid credit, not a daily or weekly quota. "
                "The percentage uses the highest balance SWARM has observed for this key.")
    if not snapshot.available:
        rows.append("DeepSeek reports insufficient credit for API calls.")
    return ProviderUsageSnapshot(float(snapshot.primary.remaining), "\n".join(rows))


def fetch_provider_usage(provider, api_key=None, *, timeout=15,
                         cancel: threading.Event | None = None) -> ProviderUsageSnapshot:
    """Return usage for a provider explicitly chosen by the user; never probe keys."""
    try:
        if cancel is not None and cancel.is_set():
            raise UsageUnavailable("Usage refresh cancelled.")
        if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
            return ProviderUsageSnapshot(
                None, "Automatic usage tracking is not available for this provider in SWARM. "
                "Check its billing or usage dashboard for the current limits.", "N/A")
        if provider == "openrouter" and api_key is None:
            return ProviderUsageSnapshot(None, "Save an OpenRouter API key to track its spending cap.", "Add key")
        # Existing Hermes sign-in remains usable when no SWARM key is saved.
        key = None if api_key is None else _validate_key(api_key)
        try:
            valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise UsageUnavailable("Usage refresh needs a positive timeout.")
        timeout = min(timeout, 60)
        if provider == "zai":
            return _zai_snapshot(fetch_zai_usage(timeout=timeout, cancel=cancel, api_key=key))
        if provider == "deepseek":
            return _deepseek_snapshot(fetch_deepseek_usage(timeout=timeout, cancel=cancel, api_key=key))
        return _fetch_openrouter(key, timeout, cancel)
    except UsageUnavailable:
        raise
    except Exception:
        # Unexpected library failures can embed a key or remote response in
        # their text. Only our intentional, sanitized errors may reach the UI.
        raise UsageUnavailable("Could not read provider usage. Try refreshing again.") from None
