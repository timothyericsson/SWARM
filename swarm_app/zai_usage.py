"""Read Z.ai Coding Plan quota using the credentials already saved by Hermes.

Only the read-only monitor endpoint is called; no inference or credential
rotation takes place. Run ``fetch_zai_usage`` in a worker thread.

Endpoint and authorization follow Z.ai's official usage plugin:
https://github.com/zai-org/zai-coding-plugins/blob/main/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs
The live API additionally returns CREDIT_LIMIT for current Coding Plans.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .usage import UsageSnapshot, UsageUnavailable, UsageWindow, _close_helper


QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
KEY_NAMES = ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY")
MAX_FILE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
POLL_SECONDS = 0.1


# A separate process bounds DNS resolution and slow response bodies as well as
# ordinary socket I/O. The key travels over stdin, never command arguments.
# This uses only stdlib, so it also works from the installed application.
_HTTP_HELPER = r'''
import json
import sys
import urllib.error
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

try:
    request_data = json.loads(sys.stdin.buffer.read(16384))
    request = urllib.request.Request(
        "https://api.z.ai/api/monitor/usage/quota/limit",
        headers={"Authorization": request_data["key"],
                 "Accept": "application/json", "Accept-Language": "en-US,en"},
        method="GET",
    )
    with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=request_data["timeout"]) as response:
        body = response.read(262145)
        if len(body) > 262144:
            sys.exit(4)
        sys.stdout.buffer.write(body)
except urllib.error.HTTPError as error:
    sys.exit(2 if error.code in (401, 403) else 3)
except Exception:
    sys.exit(3)
'''


def _read_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise UsageUnavailable("Hermes credential file is too large.")
        return data.decode("utf-8-sig")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError, ValueError) as exc:
        raise UsageUnavailable("Could not read the Hermes Z.ai credentials.") from exc


def _read_env(path: Path) -> dict[str, str]:
    """Parse only the credential variables, without executing or expanding text.

    Quoting matches the small .env subset written by Hermes config.py.
    """
    values = {}
    for line in _read_file(path).splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in (*KEY_NAMES, "GLM_BASE_URL"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = re.sub(r'\\(["\\])', r'\1', value[1:-1])
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[name] = value
    return values


def _read_auth(path: Path) -> dict:
    raw = _read_file(path)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise UsageUnavailable("Could not read the Hermes Z.ai credentials.") from exc
    if not isinstance(value, dict):
        raise UsageUnavailable("Could not read the Hermes Z.ai credentials.")
    return value


def _validate_key(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise UsageUnavailable("The Hermes Z.ai API key is invalid.")
    return value


def _validate_endpoint(value) -> None:
    if not value:
        return
    if not isinstance(value, str):
        raise UsageUnavailable("Hermes is not configured for the Z.ai service at api.z.ai.")
    try:
        endpoint = urlsplit(value)
        valid = (
            endpoint.scheme == "https" and endpoint.hostname == "api.z.ai"
            and endpoint.port in (None, 443)
            and endpoint.username is None and endpoint.password is None
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise UsageUnavailable("Hermes is not configured for the Z.ai service at api.z.ai.")


def _load_api_key() -> str:
    hermes_home = Path(os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
    saved = _read_env(hermes_home / ".env")
    # Match Hermes: saved values win over stale shell exports; provider aliases
    # are considered in Hermes' GLM_API_KEY, ZAI_API_KEY, Z_AI_API_KEY order.
    key = None
    for name in KEY_NAMES:
        key = _validate_key(saved.get(name) or os.environ.get(name))
        if key:
            break
    base_url = saved.get("GLM_BASE_URL") or os.environ.get("GLM_BASE_URL")
    if key and base_url:
        _validate_endpoint(base_url)
        return key
    auth = _read_auth(hermes_home / "auth.json")
    pool = auth.get("credential_pool")
    entries = pool.get("zai", []) if isinstance(pool, dict) else []
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    entries.sort(key=lambda entry: entry.get("priority") if type(entry.get("priority")) is int else 0)
    if not key:
        for entry in entries:
            key = _validate_key(entry.get("access_token") or entry.get("runtime_api_key"))
            if key:
                base_url = base_url or entry.get("base_url")
                break
    if not key:
        raise UsageUnavailable("No Z.ai API key found. Configure Z.ai in Hermes first.")
    if not base_url:
        providers = auth.get("providers")
        provider = providers.get("zai") if isinstance(providers, dict) else None
        detected = provider.get("detected_endpoint") if isinstance(provider, dict) else None
        if isinstance(detected, dict) and detected.get("key_hash") == hashlib.sha256(key.encode()).hexdigest()[:16]:
            base_url = detected.get("base_url")
    _validate_endpoint(base_url)
    return key


def _parse_snapshot(response) -> UsageSnapshot:
    if not isinstance(response, dict):
        raise UsageUnavailable("Z.ai returned an invalid usage response.")
    if response.get("success") is False or response.get("code") not in (None, 0, 200, "0", "200"):
        raise UsageUnavailable("Z.ai could not provide usage. Check your Hermes Z.ai API key and plan.")
    data = response.get("data", response)
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        raise UsageUnavailable("Z.ai returned an invalid usage response.")
    windows = []
    for limit in limits:
        # TIME_LIMIT is MCP tools/search, not the shared model quota.
        if not isinstance(limit, dict) or limit.get("type") not in ("CREDIT_LIMIT", "TOKENS_LIMIT"):
            continue
        used = limit.get("percentage")
        if type(used) not in (int, float):
            continue
        try:
            used = float(used)
        except (ValueError, OverflowError):
            continue
        if not math.isfinite(used):
            continue
        unit, number = limit.get("unit"), limit.get("number")
        multiplier = {3: 60, 6: 10080}.get(unit) if type(unit) is int else None
        duration = number * multiplier if type(number) is int and 0 < number <= 10000 and multiplier else None
        # The official legacy plugin describes an unqualified TOKENS_LIMIT as
        # the 5-hour quota. Never guess a duration for a new/unknown unit.
        if unit is None and number is None and limit["type"] == "TOKENS_LIMIT":
            duration = 300
        reset = limit.get("nextResetTime")
        reset = reset // 1000 if type(reset) is int and 0 <= reset <= 253402300799999 else None
        windows.append(UsageWindow(min(100.0, max(0.0, 100.0 - used)), duration, reset))
    windows.sort(key=lambda window: window.duration_minutes if window.duration_minutes is not None else math.inf)
    return UsageSnapshot(windows[0] if windows else None, windows[1] if len(windows) > 1 else None)


def fetch_zai_usage(timeout: float = 15, cancel: threading.Event | None = None,
                    *, api_key: str | None = None) -> UsageSnapshot:
    """Read shared GLM quota; an explicit key bypasses Hermes credentials."""
    if cancel is not None and cancel.is_set():
        raise UsageUnavailable("Usage refresh cancelled.")
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise UsageUnavailable("Usage refresh needs a positive timeout.")
    deadline = time.monotonic() + min(timeout, 60)
    key = _load_api_key() if api_key is None else _validate_key(api_key)
    if not key:
        raise UsageUnavailable("No Z.ai API key supplied.")
    request = json.dumps({"key": key, "timeout": min(timeout, 60)}).encode("utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _HTTP_HELPER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise UsageUnavailable("Could not start the Z.ai usage helper.") from exc
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise UsageUnavailable("Usage refresh cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageUnavailable("Usage refresh timed out.")
            try:
                output, _ = process.communicate(request, timeout=min(remaining, POLL_SECONDS))
                break
            except subprocess.TimeoutExpired:
                request = None
        if process.returncode == 2:
            raise UsageUnavailable("Z.ai rejected the API key. Check your Z.ai credentials.")
        if process.returncode == 4 or len(output) > MAX_RESPONSE_BYTES:
            raise UsageUnavailable("Z.ai returned an oversized usage response.")
        if process.returncode != 0:
            raise UsageUnavailable("Could not reach Z.ai to read usage.")
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise UsageUnavailable("Z.ai returned an invalid usage response.") from exc
        return _parse_snapshot(response)
    except OSError as exc:
        raise UsageUnavailable("Could not read Z.ai usage.") from exc
    finally:
        try:
            _close_helper(process)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UsageUnavailable("Could not close the Z.ai usage helper.") from exc
