"""Provider percentages, explicit credentials and bounded transport; no live APIs."""

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from swarm_app.deepseek_usage import CreditBalance, DeepSeekSnapshot, fetch_deepseek_usage
from swarm_app import deepseek_usage
from swarm_app.provider_usage import (
    _HTTP_HELPER, _parse_openrouter, fetch_provider_usage,
)
from swarm_app.usage import UsageSnapshot, UsageUnavailable, UsageWindow
from swarm_app.zai_usage import fetch_zai_usage


def response(limit=100, remaining=75, **extra):
    return {"data": {"limit": limit, "limit_remaining": remaining, "usage": 25, **extra}}


class ProviderSnapshotTests(unittest.TestCase):
    def test_openrouter_cap_is_a_percentage_with_explicit_scope(self):
        snapshot = _parse_openrouter(response(200, 75, limit_reset="monthly", label="private-key-prefix"))
        self.assertEqual(snapshot.percent_remaining, 37.5)
        self.assertIn("USD 75.00 left of USD 200.00", snapshot.detail)
        self.assertIn("resets monthly", snapshot.detail)
        self.assertIn("not the account credit balance", snapshot.detail)
        self.assertNotIn("private-key-prefix", snapshot.detail)

    def test_uncapped_and_missing_remaining_never_show_a_percentage(self):
        for data in (response(None, None), response(100, None)):
            with self.subTest(data=data):
                snapshot = _parse_openrouter(data)
                self.assertIsNone(snapshot.percent_remaining)
                self.assertEqual(snapshot.label, "N/A")
                self.assertIn("USD 25.00", snapshot.detail)

    def test_exhausted_zero_and_overfull_budgets_are_bounded(self):
        for cap, remaining, percent in ((100, -2, 0), (0, 0, 0), (10, 20, 100)):
            with self.subTest(cap=cap, remaining=remaining):
                self.assertEqual(_parse_openrouter(response(cap, remaining)).percent_remaining, percent)

    def test_bad_response_values_cannot_produce_a_valid_percentage(self):
        malformed = [None, [], {}, {"data": {}}, {"data": []}, response(-1, 3)]
        for value in (True, "75", float("nan"), float("inf"), 10 ** 1000):
            malformed.extend((response(value, 5), response(100, value)))
        for data in malformed:
            with self.subTest(data=data), self.assertRaises(UsageUnavailable):
                _parse_openrouter(data)

    def test_unsupported_providers_never_probe_credentials(self):
        with patch("swarm_app.provider_usage.subprocess.Popen") as spawn, \
                patch("swarm_app.provider_usage.fetch_zai_usage") as zai, \
                patch("swarm_app.provider_usage.fetch_deepseek_usage") as deepseek:
            for provider in ("openai", "anthropic", "google", "groq", "custom", "sk-private"):
                snapshot = fetch_provider_usage(provider, "private-api-key")
                self.assertIsNone(snapshot.percent_remaining)
                self.assertIn("not available", snapshot.detail)
                self.assertNotIn("private", snapshot.detail)
            spawn.assert_not_called()
            zai.assert_not_called()
            deepseek.assert_not_called()

    def test_supported_hermes_providers_receive_explicit_or_fallback_credentials(self):
        zai = UsageSnapshot(UsageWindow(82.5, 300, 1780000000), UsageWindow(70, 10080, None))
        deepseek = DeepSeekSnapshot((CreditBalance("USD", Decimal("3"), Decimal("10")),), True)
        with patch("swarm_app.provider_usage.fetch_zai_usage", return_value=zai) as fetch:
            self.assertEqual(fetch_provider_usage("zai", "saved-key").percent_remaining, 82.5)
            fetch.assert_called_once_with(timeout=15, cancel=None, api_key="saved-key")
            fetch.reset_mock()
            self.assertEqual(fetch_provider_usage("zai").percent_remaining, 82.5)
            fetch.assert_called_once_with(timeout=15, cancel=None, api_key=None)
        with patch("swarm_app.provider_usage.fetch_deepseek_usage", return_value=deepseek) as fetch:
            snapshot = fetch_provider_usage("deepseek", "saved-key")
            self.assertEqual(snapshot.percent_remaining, 30)
            self.assertIn("highest balance", snapshot.detail)
            self.assertIn("USD 3.00", snapshot.detail)
            fetch.assert_called_once_with(timeout=15, cancel=None, api_key="saved-key")
            fetch.reset_mock()
            self.assertEqual(fetch_provider_usage("deepseek").percent_remaining, 30)
            fetch.assert_called_once_with(timeout=15, cancel=None, api_key=None)

    def test_openrouter_requires_saved_key_and_unexpected_errors_are_sanitized(self):
        with patch("swarm_app.provider_usage.subprocess.Popen") as spawn:
            snapshot = fetch_provider_usage("openrouter")
            self.assertIsNone(snapshot.percent_remaining)
            self.assertEqual(snapshot.label, "Add key")
            spawn.assert_not_called()
        with patch("swarm_app.provider_usage.fetch_zai_usage", side_effect=RuntimeError("private-api-key")):
            with self.assertRaises(UsageUnavailable) as error:
                fetch_provider_usage("zai", "private-api-key")
            self.assertNotIn("private", str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)


class ExplicitProviderKeyTests(unittest.TestCase):
    def test_parallel_deepseek_accounts_keep_each_persisted_baseline(self):
        body = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "10"}]}
        code = "import sys; sys.stdin.read(); print(" + repr(json.dumps(body)) + ")"
        read_file = deepseek_usage._read_file

        def delayed_read(path):
            raw = read_file(path)
            # Widen the read/write race; an unlocked refresh would overwrite
            # other accounts after each worker reads the same old history.
            time.sleep(.05)
            return raw

        keys = [f"parallel-account-{number}" for number in range(4)]
        with tempfile.TemporaryDirectory() as state, \
                patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                patch("swarm_app.deepseek_usage._HTTP_HELPER", code), \
                patch("swarm_app.deepseek_usage._read_file", side_effect=delayed_read):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda key: fetch_deepseek_usage(api_key=key), keys))
            self.assertEqual([result.primary.baseline for result in results], [Decimal("10")] * 4)
            history = json.loads((Path(state) / "swarm" / "deepseek-balance.json").read_text())
            self.assertEqual(set(history), {hashlib.sha256(key.encode()).hexdigest() for key in keys})

    def test_explicit_keys_bypass_hermes_files_and_are_only_sent_on_stdin(self):
        cases = (
            ("zai_usage", fetch_zai_usage, {"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": 25}]}}),
            ("deepseek_usage", fetch_deepseek_usage,
             {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "10"}]}),
        )
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"XDG_STATE_HOME": state}):
            for module, fetch, body in cases:
                with self.subTest(module=module):
                    code = ("import json,sys; data=json.load(sys.stdin); "
                            "assert data['key']=='explicit-'+'test-key'; "
                            "print(" + repr(json.dumps(body)) + ")")
                    with patch(f"swarm_app.{module}._load_api_key", side_effect=AssertionError("Must bypass Hermes")), \
                            patch(f"swarm_app.{module}._HTTP_HELPER", code), \
                            patch(f"swarm_app.{module}.subprocess.Popen", wraps=subprocess.Popen) as spawn:
                        snapshot = fetch(api_key="explicit-test-key")
                    self.assertIsNotNone(snapshot.primary)
                    self.assertNotIn("explicit-test-key", repr(spawn.call_args))

    def test_invalid_explicit_key_does_not_fall_back_or_spawn(self):
        for module, fetch in (("zai_usage", fetch_zai_usage), ("deepseek_usage", fetch_deepseek_usage)):
            with patch(f"swarm_app.{module}._load_api_key") as load, \
                    patch(f"swarm_app.{module}.subprocess.Popen") as spawn:
                for value in ("", "  ", "secret\nInjected: value", "x" * 4097, 123):
                    with self.subTest(module=module, value=value), self.assertRaises(UsageUnavailable):
                        fetch(api_key=value)
                load.assert_not_called()
                spawn.assert_not_called()


class OpenRouterTransportTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            self.assertNotIn("private-test-key", repr(args))
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.patch = patch("swarm_app.provider_usage.subprocess.Popen", side_effect=popen)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        self.assertTrue(all(process.poll() is not None for process in self.processes), "helpers must be reaped")

    def helper(self, code, **kwargs):
        with patch("swarm_app.provider_usage._HTTP_HELPER", code):
            return fetch_provider_usage("openrouter", "private-test-key", **kwargs)

    def test_actual_helper_uses_fixed_get_bearer_auth_and_no_redirects(self):
        fixture = '''
import io,json,sys,urllib.request
class Opener:
    def open(self, request, timeout):
        assert request.full_url == "https://openrouter.ai/api/v1/key"
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer private-" + "test-key"
        assert request.data is None
        return io.BytesIO(BODY)
def opener(handler):
    assert handler.redirect_request(None,None,302,"",{},"https://elsewhere.example") is None
    return Opener()
urllib.request.build_opener = opener
'''.replace("BODY", repr(json.dumps(response()).encode()))
        self.assertEqual(self.helper(fixture + _HTTP_HELPER).percent_remaining, 75)

    def test_timeouts_and_cancellation_reap_children(self):
        code = "import sys,time; sys.stdin.read(); time.sleep(30)"
        started = time.monotonic()
        with self.assertRaisesRegex(UsageUnavailable, "timed out"):
            self.helper(code, timeout=.1)
        self.assertLess(time.monotonic() - started, 2)
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(UsageUnavailable, "cancelled"):
                self.helper(code, cancel=cancel)
        finally:
            timer.join()

    def test_invalid_input_and_precancellation_do_not_spawn(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(UsageUnavailable, "cancelled"):
            fetch_provider_usage("openrouter", "private-test-key", cancel=cancel)
        for timeout in (0, -1, True, "15", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(timeout=timeout), self.assertRaises(UsageUnavailable):
                fetch_provider_usage("openrouter", "private-test-key", timeout=timeout)
        for key in ("", "bad\nheader", "key with spaces", "x" * 4097, 123):
            with self.subTest(key=key), self.assertRaises(UsageUnavailable):
                fetch_provider_usage("openrouter", key)
        self.assertEqual(self.processes, [])

    def test_transport_failures_and_malformed_remote_text_stay_private(self):
        for code in ("import sys; sys.exit(2)", "import sys; sys.exit(3)", "import sys; sys.exit(4)",
                     "print('private-response')", "print('x'*262145)"):
            with self.subTest(code=code), self.assertRaises(UsageUnavailable) as error:
                self.helper(code)
            self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
