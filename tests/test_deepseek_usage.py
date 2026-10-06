"""Credit tracking, credentials, bounded read-only transport, and persistence."""

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from swarm_app.deepseek_usage import (
    _HTTP_HELPER, _load_api_key, _parse_balances, _track_balances, fetch_deepseek_usage,
)
from swarm_app.usage import UsageUnavailable


def response(total="9.96", available=True):
    return {"is_available": available, "balance_infos": [{"currency": "USD", "total_balance": total}]}


class DeepSeekBalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / "state" / "deepseek.json"

    def track(self, total, key="test-key"):
        balances, available = _parse_balances(response(total))
        return _track_balances(balances, available, key, self.path)

    def test_spend_restart_topup_and_account_switch_keep_separate_baselines(self):
        self.assertEqual(self.track("10").primary.remaining, 100)
        self.assertEqual(self.track("7.45").primary.remaining, Decimal("74.50"))
        self.assertEqual(self.track("12").primary.remaining, 100)
        self.assertEqual(self.track("6").primary.remaining, 50)
        self.assertEqual(self.track("4", "second-key").primary.remaining, 100)
        self.assertEqual(self.track("3").primary.remaining, 25)
        history = self.path.read_text()
        self.assertNotIn("test-key", history)
        self.assertNotIn("second-key", history)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_zero_and_negative_credit_show_zero_and_currency_totals_stay_separate(self):
        self.assertEqual(self.track("0").primary.remaining, 0)
        self.assertEqual(self.track("-0.01").primary.remaining, 0)
        snapshot = _track_balances({"CNY": Decimal("100"), "USD": Decimal("10")}, True, "key", self.path)
        self.assertEqual(snapshot.primary.currency, "USD")
        snapshot = _track_balances({"CNY": Decimal("20"), "USD": Decimal("5")}, False, "key", self.path)
        self.assertEqual([balance.remaining for balance in snapshot.balances], [50, 20])
        self.assertFalse(snapshot.available)

    def test_malformed_remote_data_cannot_show_a_valid_percentage(self):
        for value in (None, {}, [], {"is_available": "true", "balance_infos": []},
                      response("NaN"), response("Infinity"), response("1e1000"), response(True),
                      {"is_available": True, "balance_infos": [None]}):
            with self.subTest(value=value), self.assertRaises(UsageUnavailable):
                _parse_balances(value)

    def test_corrupt_or_failed_history_save_does_not_silently_reset_percentage(self):
        self.path.parent.mkdir()
        self.path.write_text("invalid")
        with self.assertRaises(UsageUnavailable):
            self.track("5")
        self.path.unlink()
        self.track("10")
        before = self.path.read_text()
        with patch("swarm_app.deepseek_usage.os.replace", side_effect=OSError("denied")), \
                self.assertRaises(UsageUnavailable):
            self.track("5")
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_saved_hermes_credentials_win_over_shell_without_execution(self):
        env = {"HERMES_HOME": str(self.home), "DEEPSEEK_API_KEY": "stale-key"}
        (self.home / ".env").write_text("export DEEPSEEK_API_KEY='saved-key'\n")
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_load_api_key(), "saved-key")
        (self.home / ".env").write_text('DEEPSEEK_API_KEY="$(never-run)`never-run`"\n')
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_load_api_key(), "$(never-run)`never-run`")

    def test_missing_invalid_credentials_and_foreign_endpoints_stay_unavailable(self):
        for extra in ({}, {"DEEPSEEK_API_KEY": "secret\nHeader: evil"},
                      {"DEEPSEEK_API_KEY": "key", "DEEPSEEK_BASE_URL": "http://api.deepseek.com"},
                      {"DEEPSEEK_API_KEY": "key", "DEEPSEEK_BASE_URL": "https://evil.example"}):
            with self.subTest(extra=extra), patch.dict(os.environ, {"HERMES_HOME": str(self.home), **extra}, clear=True), \
                    self.assertRaises(UsageUnavailable):
                _load_api_key()
        (self.home / "auth.json").write_text(json.dumps({"credential_pool": {"deepseek": [
            {"access_token": "second", "priority": 2}, {"runtime_api_key": "first", "priority": 1}]}}))
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=True):
            self.assertEqual(_load_api_key(), "first")


class DeepSeekTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.key = patch("swarm_app.deepseek_usage._load_api_key", return_value="private-test-key")
        self.key.start()
        self.addCleanup(self.key.stop)
        self.processes = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            self.assertNotIn("private-test-key", repr(args))
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.popen = patch("swarm_app.deepseek_usage.subprocess.Popen", side_effect=popen)
        self.popen.start()
        self.addCleanup(self.popen.stop)

    def tearDown(self):
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def helper(self, code, **kwargs):
        with patch("swarm_app.deepseek_usage._HTTP_HELPER", code):
            return fetch_deepseek_usage(**kwargs)

    def test_actual_http_request_reads_balance_with_bearer_auth_and_rejects_redirects(self):
        fixture = '''
import io,json,sys,urllib.request
class Opener:
    def open(self, request, timeout):
        assert request.full_url == "https://api.deepseek.com/user/balance"
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer private-" + "test-key"
        assert request.data is None
        return io.BytesIO(BODY)
def opener(handler):
    assert handler.redirect_request(None,None,302,"",{},"https://elsewhere.example") is None
    return Opener()
urllib.request.build_opener = opener
'''.replace("BODY", repr(json.dumps(response()).encode()))
        snapshot = self.helper(fixture + _HTTP_HELPER)
        self.assertEqual(snapshot.primary.total, Decimal("9.96"))
        self.assertEqual(snapshot.primary.remaining, 100)

    def test_timeout_cancel_and_invalid_timeouts_leave_no_helper_running(self):
        code = "import sys,time; sys.stdin.read(); time.sleep(30)"
        start = time.monotonic()
        with self.assertRaisesRegex(UsageUnavailable, "timed out"):
            self.helper(code, timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        try:
            with self.assertRaisesRegex(UsageUnavailable, "cancelled"):
                self.helper(code, cancel=event)
        finally:
            timer.join()
        for invalid in (0, -1, True, "15", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value=invalid), self.assertRaises(UsageUnavailable):
                fetch_deepseek_usage(timeout=invalid)

    def test_auth_transport_and_body_errors_are_sanitized(self):
        for code in ("import sys; sys.exit(2)", "import sys; sys.exit(3)", "import sys; sys.exit(4)",
                     "print('private-response')", "print('x'*262145)"):
            with self.subTest(code=code), self.assertRaises(UsageUnavailable) as error:
                self.helper(code)
            self.assertNotIn("private-response", str(error.exception))


if __name__ == "__main__":
    unittest.main()
