"""Hermetic Z.ai quota, saved-credential, and bounded helper checks."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from swarm_app.usage import UsageSnapshot, UsageUnavailable, UsageWindow
from swarm_app.zai_usage import (
    _HTTP_HELPER, _load_api_key, _parse_snapshot, fetch_zai_usage,
)


def limit(used=20, unit=3, number=5, kind="CREDIT_LIMIT", reset=1900000000999):
    return {"type": kind, "percentage": used, "unit": unit,
            "number": number, "nextResetTime": reset}


def response(*limits):
    return {"success": True, "code": 200, "data": {"limits": list(limits)}}


class ZaiParsingTests(unittest.TestCase):
    def test_live_credit_schema_percentage_and_millisecond_reset(self):
        snapshot = _parse_snapshot(response(limit(46, 6, 1), limit()))
        self.assertEqual(snapshot, UsageSnapshot(
            UsageWindow(80, 300, 1900000000), UsageWindow(54, 10080, 1900000000)))

    def test_legacy_token_quota_excludes_tool_usage(self):
        snapshot = _parse_snapshot(response(
            limit(100, 5, 1, "TIME_LIMIT"), limit(35, kind="TOKENS_LIMIT")))
        self.assertEqual(snapshot.primary.remaining, 65)
        self.assertIsNone(snapshot.secondary)
        self.assertEqual(_parse_snapshot(response({"type": "TOKENS_LIMIT", "percentage": 5})).primary.duration_minutes, 300)

    def test_missing_model_quota_is_unknown(self):
        self.assertEqual(_parse_snapshot(response()), UsageSnapshot(None, None))
        self.assertEqual(_parse_snapshot(response(limit(kind="TIME_LIMIT"))), UsageSnapshot(None, None))
        self.assertEqual(_parse_snapshot(response(None, 5, {})), UsageSnapshot(None, None))

    def test_bad_percentages_are_unknown_and_numeric_values_clamp(self):
        for invalid in (None, True, "20", float("nan"), float("inf"), 10 ** 1000, {}):
            with self.subTest(value=invalid):
                self.assertIsNone(_parse_snapshot(response(limit(invalid))).primary)
        self.assertEqual(_parse_snapshot(response(limit(-2))).primary.remaining, 100)
        self.assertEqual(_parse_snapshot(response(limit(102))).primary.remaining, 0)
        self.assertEqual(_parse_snapshot(response(limit(32.5))).primary.remaining, 67.5)

    def test_bad_optional_metadata_does_not_hide_percentage(self):
        for invalid in (None, True, "5", [], -1, 10 ** 1000):
            with self.subTest(value=invalid):
                window = _parse_snapshot(response(limit(number=invalid, reset=invalid))).primary
                self.assertEqual(window.remaining, 80)
                self.assertIsNone(window.duration_minutes)
                self.assertIsNone(window.resets_at)
        window = _parse_snapshot(response(limit(unit=99))).primary
        self.assertIsNone(window.duration_minutes)

    def test_error_envelope_never_looks_like_success_or_leaks_remote_text(self):
        for value in (None, [], {}, {"data": []}, {"success": False, "msg": "secret"},
                      {"code": 401, "msg": "secret", "data": {"limits": [limit()]}}):
            with self.subTest(value=value), self.assertRaises(UsageUnavailable) as error:
                _parse_snapshot(value)
            self.assertNotIn("secret", str(error.exception))


class ZaiCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_reads_hermes_home_saved_key_with_export_and_quotes(self):
        (self.home / ".env").write_text("# GLM_API_KEY=ignored\nexport GLM_API_KEY='saved-key'\nGLM_BASE_URL=https://api.z.ai/api/coding/paas/v4\n")
        self.assertEqual(_load_api_key(), "saved-key")

    def test_saved_value_precedes_shell_but_alias_order_matches_hermes(self):
        (self.home / ".env").write_text('GLM_API_KEY="new-key"\nZAI_API_KEY=alias-key\n')
        with patch.dict(os.environ, {"GLM_API_KEY": "old-key"}):
            self.assertEqual(_load_api_key(), "new-key")
        (self.home / ".env").write_text("ZAI_API_KEY=saved-alias\n")
        with patch.dict(os.environ, {"GLM_API_KEY": "primary-key"}):
            self.assertEqual(_load_api_key(), "primary-key")

    def test_each_hermes_key_alias_is_supported(self):
        for alias in ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"):
            with self.subTest(alias=alias), patch.dict(os.environ, {alias: "key"}):
                self.assertEqual(_load_api_key(), "key")

    def test_credentials_are_not_expanded_or_executed(self):
        (self.home / ".env").write_text('GLM_API_KEY="$(never-run)`never-run`${SECRET}"\n')
        self.assertEqual(_load_api_key(), "$(never-run)`never-run`${SECRET}")

    def test_pool_selects_priority_and_does_not_rewrite_credentials(self):
        auth = {"credential_pool": {"zai": [
            {"priority": 3, "access_token": "second"},
            {"priority": 0, "source": "env:GLM_API_KEY"},
            {"priority": 1, "access_token": "first", "base_url": "https://api.z.ai/api/coding/paas/v4"},
        ]}}
        path = self.home / "auth.json"
        content = json.dumps(auth)
        path.write_text(content)
        self.assertEqual(_load_api_key(), "first")
        self.assertEqual(path.read_text(), content)

    def test_unrelated_auth_and_missing_keys_stay_unavailable(self):
        (self.home / "auth.json").write_text(json.dumps({"credential_pool": {"openai-codex": [{"access_token": "unrelated-secret"}]}}))
        with self.assertRaisesRegex(UsageUnavailable, "No Z.ai API key"):
            _load_api_key()

    def test_pool_runtime_api_key_fallback(self):
        (self.home / "auth.json").write_text(json.dumps({"credential_pool": {"zai": [{"runtime_api_key": "pool-key"}]}}))
        self.assertEqual(_load_api_key(), "pool-key")

    def test_foreign_endpoints_and_header_injection_are_rejected(self):
        for base in ("https://api.z.ai.evil.example/", "http://api.z.ai/", "https://open.bigmodel.cn/", "https://user:pass@api.z.ai/", "https://api.z.ai:444/"):
            with self.subTest(base=base), patch.dict(os.environ, {"GLM_API_KEY": "key", "GLM_BASE_URL": base}), self.assertRaises(UsageUnavailable):
                _load_api_key()
        with patch.dict(os.environ, {"GLM_API_KEY": "key\r\nHeader: value"}), self.assertRaises(UsageUnavailable):
            _load_api_key()

    def test_corrupt_and_oversized_files_have_sanitized_errors(self):
        for content in ('{"secret":bad', "[]", "x" * (1024 * 1024 + 1)):
            (self.home / "auth.json").write_text(content)
            with self.assertRaises(UsageUnavailable) as error:
                _load_api_key()
            self.assertNotIn(content[:10], str(error.exception))


class ZaiHelperTests(unittest.TestCase):
    def setUp(self):
        self.key_patch = patch("swarm_app.zai_usage._load_api_key", return_value="private-test-key")
        self.key_patch.start()
        self.processes = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            self.assertNotIn("private-test-key", repr(args))
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.popen_patch = patch("swarm_app.zai_usage.subprocess.Popen", side_effect=popen)
        self.popen_patch.start()

    def tearDown(self):
        self.popen_patch.stop()
        self.key_patch.stop()
        for process in self.processes:
            self.assertIsNotNone(process.poll(), "helper process must be reaped")

    def helper(self, code, **kwargs):
        with patch("swarm_app.zai_usage._HTTP_HELPER", code):
            return fetch_zai_usage(**kwargs)

    def test_credential_is_only_passed_on_stdin_and_result_parsed(self):
        code = "import json,sys; data=json.load(sys.stdin); assert data['key']=='private-'+'test-key'; print(" + repr(json.dumps(response(limit()))) + ")"
        self.assertEqual(self.helper(code).primary.remaining, 80)

    def test_timeout_and_cancellation_reap_helper(self):
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

    def test_cancelled_before_start_and_invalid_timeouts_do_not_spawn(self):
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(UsageUnavailable, "cancelled"):
            fetch_zai_usage(cancel=event)
        for invalid in (0, -1, True, "15", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(timeout=invalid), self.assertRaises(UsageUnavailable):
                fetch_zai_usage(timeout=invalid)
        self.assertEqual(self.processes, [])

    def test_transport_failures_and_remote_bodies_are_sanitized(self):
        for code in ("import sys; sys.exit(2)", "import sys; sys.exit(3)", "import sys; sys.exit(4)", "print('private-response')", "print('x'*262145)"):
            with self.subTest(code=code), self.assertRaises(UsageUnavailable) as error:
                self.helper(code)
            self.assertNotIn("private-response", str(error.exception))

    def test_real_http_helper_uses_get_fixed_endpoint_and_rejects_redirects(self):
        # Execute the actual helper with stdlib network access replaced inside
        # that process. This covers its request construction, not a live account.
        fixture = '''
import io,json,sys,urllib.request
class Response(io.BytesIO):
    pass
class Opener:
    def open(self, request, timeout):
        assert request.full_url == "https://api.z.ai/api/monitor/usage/quota/limit"
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "private-" + "test-key"
        assert request.data is None
        return Response(BODY)
def opener(handler):
    assert handler.redirect_request(None,None,302,"",{},"https://elsewhere.example") is None
    return Opener()
urllib.request.build_opener = opener
'''.replace("BODY", repr(json.dumps(response(limit())).encode()))
        self.assertEqual(self.helper(fixture + _HTTP_HELPER).primary.remaining, 80)


if __name__ == "__main__":
    unittest.main()
