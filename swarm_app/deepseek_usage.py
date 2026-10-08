"""DeepSeek prepaid balance and percentage of the highest observed credit.

DeepSeek does not expose a subscription quota or original deposit total. The
percentage uses a persisted high-water balance per credential and currency;
it is not a daily/weekly quota. Only GET /user/balance is called.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .usage import UsageUnavailable, _close_helper


BALANCE_URL = "https://api.deepseek.com/user/balance"
MAX_BYTES = 256 * 1024
_HISTORY_LOCK = threading.Lock()

# Bound DNS, socket reads and response size. Credentials travel via stdin.
_HTTP_HELPER = r'''
import json, sys, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
try:
    data = json.loads(sys.stdin.buffer.read(16384))
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
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


@dataclass(frozen=True)
class CreditBalance:
    currency: str
    total: Decimal
    baseline: Decimal

    @property
    def remaining(self):
        if self.baseline <= 0:
            return 0
        return min(100, max(0, self.total / self.baseline * 100))


@dataclass(frozen=True)
class DeepSeekSnapshot:
    balances: tuple[CreditBalance, ...]
    available: bool

    @property
    def primary(self):
        return self.balances[0]


def _read_file(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized file")
        return raw.decode("utf-8-sig")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, UnicodeError) as exc:
        raise UsageUnavailable("Could not read DeepSeek credentials or balance history.") from exc


def _load_api_key():
    home = Path(os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
    saved = {}
    for line in _read_file(home / ".env").splitlines():
        line = line.strip().removeprefix("export ")
        name, separator, value = line.partition("=")
        if separator and name.strip() in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = re.sub(r'\\(["\\])', r'\1', value[1:-1])
            elif len(value) >= 2 and value[0] == value[-1] == "'":
                value = value[1:-1]
            saved[name.strip()] = value
    key = saved.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    endpoint = saved.get("DEEPSEEK_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL")
    if not key:
        try:
            auth = json.loads(_read_file(home / "auth.json") or "{}")
            entries = auth.get("credential_pool", {}).get("deepseek", [])
            entries = sorted((entry for entry in entries if isinstance(entry, dict)),
                             key=lambda entry: entry.get("priority") if type(entry.get("priority")) is int else 0)
            for entry in entries:
                key = entry.get("access_token") or entry.get("runtime_api_key")
                if key:
                    endpoint = endpoint or entry.get("base_url")
                    break
        except (ValueError, TypeError, AttributeError, RecursionError) as exc:
            raise UsageUnavailable("Could not read the Hermes DeepSeek credentials.") from exc
    if not isinstance(key, str) or not key.strip():
        raise UsageUnavailable("No DeepSeek API key found. Configure DEEPSEEK_API_KEY in Hermes first.")
    key = key.strip()
    if len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise UsageUnavailable("The Hermes DeepSeek API key is invalid.")
    if endpoint:
        try:
            url = urlsplit(endpoint)
            valid = (url.scheme == "https" and url.hostname == "api.deepseek.com"
                     and url.port in (None, 443) and url.username is None and url.password is None)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise UsageUnavailable("Balance tracking requires the official DeepSeek API at api.deepseek.com.")
    return key


def _amount(value):
    if type(value) not in (str, int, float):
        raise ValueError("invalid balance")
    amount = Decimal(str(value))
    if not amount.is_finite() or abs(amount) > Decimal("1e15"):
        raise ValueError("invalid balance")
    return amount


def _parse_balances(response):
    try:
        if not isinstance(response, dict) or type(response.get("is_available")) is not bool:
            raise ValueError("invalid availability")
        balances = {}
        for entry in response["balance_infos"]:
            currency = entry["currency"]
            if currency not in ("USD", "CNY") or currency in balances:
                raise ValueError("invalid currency")
            balances[currency] = _amount(entry["total_balance"])
        if not balances:
            raise ValueError("missing balances")
        return balances, response["is_available"]
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise UsageUnavailable("DeepSeek returned an invalid balance response.") from exc


def _track_balances(balances, available, key, path=None):
    if path is None:
        state = Path(os.environ.get("XDG_STATE_HOME", ""))
        if not state.is_absolute():
            state = Path.home() / ".local" / "state"
        path = state / "swarm" / "deepseek-balance.json"
    path = Path(path)
    identity = hashlib.sha256(key.encode()).hexdigest()
    try:
        history = json.loads(_read_file(path) or "{}")
        if not isinstance(history, dict):
            raise ValueError("invalid history")
        account = history.setdefault(identity, {})
        result = []
        for currency in sorted(balances, key=lambda value: value != "USD"):
            total = balances[currency]
            baseline = max(Decimal(0), total, _amount(account.get(currency, "0")))
            account[currency] = str(baseline)
            result.append(CreditBalance(currency, total, baseline))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".deepseek-balance-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(history, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return DeepSeekSnapshot(tuple(result), available)
    except (OSError, ValueError, TypeError, AttributeError, InvalidOperation, RecursionError) as exc:
        raise UsageUnavailable("Could not save or read DeepSeek balance history.") from exc


def fetch_deepseek_usage(timeout=15, cancel: threading.Event | None = None, *, api_key=None):
    """Read a balance; an explicit key bypasses Hermes credential resolution."""
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise UsageUnavailable("Balance refresh needs a positive timeout.")
    timeout = min(timeout, 60)
    deadline = time.monotonic() + timeout
    if cancel is not None and cancel.is_set():
        raise UsageUnavailable("Balance refresh cancelled.")
    if api_key is None:
        key = _load_api_key()
    else:
        if (not isinstance(api_key, str) or not api_key.strip()
                or len(api_key.strip()) > 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key.strip())):
            raise UsageUnavailable("The DeepSeek API key is invalid.")
        key = api_key.strip()
    request = json.dumps({"key": key, "timeout": timeout}).encode()
    try:
        process = subprocess.Popen([sys.executable, "-c", _HTTP_HELPER], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    except (OSError, ValueError) as exc:
        raise UsageUnavailable("Could not start the DeepSeek balance helper.") from exc
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise UsageUnavailable("Balance refresh cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageUnavailable("Balance refresh timed out.")
            try:
                output, _ = process.communicate(request, timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                request = None
        if process.returncode == 2:
            raise UsageUnavailable("DeepSeek rejected the API key. Check your DeepSeek credentials.")
        if process.returncode == 4 or len(output) > MAX_BYTES:
            raise UsageUnavailable("DeepSeek returned an oversized balance response.")
        if process.returncode != 0:
            raise UsageUnavailable("Could not reach DeepSeek to read the balance.")
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise UsageUnavailable("DeepSeek returned an invalid balance response.") from exc
        balances, available = _parse_balances(response)
        if cancel is not None and cancel.is_set():
            raise UsageUnavailable("Balance refresh cancelled.")
        # Several saved accounts can refresh together; preserve every account's
        # previous high-water balance across the shared atomic history update.
        with _HISTORY_LOCK:
            return _track_balances(balances, available, key)
    except OSError as exc:
        raise UsageUnavailable("Could not read the DeepSeek balance.") from exc
    finally:
        try:
            _close_helper(process)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UsageUnavailable("Could not close the DeepSeek balance helper.") from exc
