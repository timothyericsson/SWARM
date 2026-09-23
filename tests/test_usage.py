"""Usage parsing and stdio integration checks, using only a local fake server."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from swarm_app import __version__
from swarm_app.usage import UsageSnapshot, UsageUnavailable, UsageWindow, _parse_snapshot, fetch_usage


FIXTURE = Path(__file__).parent / "fixtures" / "fake_usage_server.py"


def window(used=25, duration=300, reset=1900000000):
    return {"usedPercent": used, "windowDurationMins": duration, "resetsAt": reset}


class UsageParsingTests(unittest.TestCase):
    def parse(self, primary=None, secondary=None):
        return _parse_snapshot({"rateLimits": {"primary": primary, "secondary": secondary}})

    def test_core_map_precedes_legacy_and_other_buckets(self):
        snapshot = _parse_snapshot({
            "rateLimits": {"primary": window(99)},
            "rateLimitsByLimitId": {
                "unrelated": {"primary": window(100)},
                "codex": {"primary": window(12.5)},
            },
        })
        self.assertEqual(snapshot.primary.remaining, 87.5)

    def test_map_without_codex_never_falls_back_to_other_or_legacy_limits(self):
        for buckets in ({}, {"other": {"primary": window(90)}}, {"codex": None}, [], "bad"):
            with self.subTest(buckets=buckets):
                self.assertEqual(_parse_snapshot({"rateLimits": {"primary": window(99)}, "rateLimitsByLimitId": buckets}), UsageSnapshot(None, None))

    def test_missing_or_null_map_supports_legacy_codex(self):
        for extra in ({}, {"rateLimitsByLimitId": None}):
            snapshot = _parse_snapshot({"rateLimits": {"limitId": "codex", "primary": window(10)}, **extra})
            self.assertEqual(snapshot.primary.remaining, 90)
        self.assertEqual(_parse_snapshot({"rateLimits": {"limitId": "other", "primary": window()}}), UsageSnapshot(None, None))

    def test_missing_and_null_windows_remain_unknown(self):
        self.assertEqual(self.parse(), UsageSnapshot(None, None))
        self.assertEqual(_parse_snapshot({}), UsageSnapshot(None, None))
        self.assertEqual(self.parse({"usedPercent": None}), UsageSnapshot(None, None))

    def test_weekly_only_is_valid_in_either_source_slot(self):
        weekly = UsageWindow(42, 10080, 1900000000)
        self.assertEqual(self.parse(window(58, 10080)), UsageSnapshot(weekly, None))
        self.assertEqual(self.parse(None, window(58, 10080)), UsageSnapshot(weekly, None))

    def test_shorter_known_window_first_and_unknown_duration_preserves_primary(self):
        snapshot = self.parse(window(25, 10080), window(60, 300))
        self.assertEqual(snapshot.primary.duration_minutes, 300)
        self.assertEqual(snapshot.secondary.duration_minutes, 10080)
        snapshot = self.parse(window(10, None), window(20, 300))
        self.assertEqual(snapshot.primary.remaining, 90)
        self.assertIsNone(snapshot.primary.duration_minutes)

    def test_percentages_clamp_and_invalid_values_do_not_become_zero(self):
        self.assertEqual(self.parse(window(-20)).primary.remaining, 100)
        self.assertEqual(self.parse(window(120)).primary.remaining, 0)
        for invalid in (None, True, False, "5", float("nan"), float("inf"), float("-inf"), {}, 10**1000):
            with self.subTest(invalid=invalid):
                self.assertIsNone(self.parse(window(invalid)).primary)

    def test_malformed_optional_metadata_is_unknown(self):
        for invalid in (None, True, "300", 1.5, -1, 10**100):
            with self.subTest(invalid=invalid):
                parsed = self.parse(window(40, invalid, invalid)).primary
                self.assertEqual(parsed.remaining, 60)
                self.assertIsNone(parsed.duration_minutes)
                self.assertIsNone(parsed.resets_at)

    def test_invalid_result_rejected(self):
        for result in (None, [], "invalid", 0):
            with self.subTest(result=result), self.assertRaises(UsageUnavailable):
                _parse_snapshot(result)


class UsageConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.executable = self.directory / "fake codex"
        shutil.copyfile(FIXTURE, self.executable)
        self.executable.chmod(0o755)
        self.environment = patch.dict(os.environ, {"SWARM_TEST_USAGE_DIRECTORY": str(self.directory), "SWARM_TEST_USAGE_MODE": "normal"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def assert_helper_reaped(self):
        pid = int((self.directory / "pid").read_text())
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def requests(self):
        return [json.loads(line) for line in (self.directory / "requests").read_text().splitlines()]

    def test_exact_handshake_and_matching_responses_ignore_notifications(self):
        snapshot = fetch_usage(str(self.executable), timeout=2)
        self.assertEqual(snapshot.primary, UsageWindow(74.5, 300, 1900000000))
        self.assertEqual(snapshot.secondary, UsageWindow(30, 10080, 1900600000))
        self.assertEqual(json.loads((self.directory / "argv").read_text()), ["app-server"])
        self.assertEqual(self.requests(), [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "swarm", "title": "SWARM", "version": __version__}}},
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/rateLimits/read"},
        ])
        self.assert_helper_reaped()

    def test_partial_json_lines_are_assembled(self):
        with patch.dict(os.environ, {"SWARM_TEST_USAGE_MODE": "partial_lines"}):
            self.assertEqual(fetch_usage(str(self.executable), timeout=2).primary.remaining, 74.5)
        self.assert_helper_reaped()

    def test_timeout_at_each_stage_is_bounded_and_kills_stubborn_helper(self):
        for mode in ("initialize_timeout", "usage_timeout"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"SWARM_TEST_USAGE_MODE": mode}):
                start = time.monotonic()
                with self.assertRaisesRegex(UsageUnavailable, "timed out"):
                    fetch_usage(str(self.executable), timeout=0.15)
                self.assertLess(time.monotonic() - start, 2)
                self.assert_helper_reaped()

    def test_closed_pipe_and_errors_are_sanitized_and_reaped(self):
        for mode in ("close", "close_after_initialize", "initialize_error", "usage_error", "invalid_json"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"SWARM_TEST_USAGE_MODE": mode}):
                with self.assertRaises(UsageUnavailable) as caught:
                    fetch_usage(str(self.executable), timeout=2)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertNotIn("private invalid output", str(caught.exception))
                self.assert_helper_reaped()

    def test_line_and_total_output_are_bounded(self):
        for mode in ("oversized_line", "output_flood"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"SWARM_TEST_USAGE_MODE": mode}):
                with self.assertRaisesRegex(UsageUnavailable, "oversized|too much"):
                    fetch_usage(str(self.executable), timeout=2)
                self.assert_helper_reaped()

    def test_cancellation_during_read_reaps_only_owned_helper(self):
        cancel = threading.Event()
        sentinel = subprocess.Popen(["/bin/sleep", "10"])
        timer = threading.Timer(0.15, cancel.set)
        try:
            timer.start()
            with patch.dict(os.environ, {"SWARM_TEST_USAGE_MODE": "usage_timeout"}):
                with self.assertRaisesRegex(UsageUnavailable, "cancelled"):
                    fetch_usage(str(self.executable), timeout=3, cancel=cancel)
            self.assert_helper_reaped()
            self.assertIsNone(sentinel.poll())
        finally:
            timer.cancel()
            timer.join()
            sentinel.terminate()
            sentinel.wait(timeout=2)

    def test_cancelled_request_never_starts_a_helper(self):
        cancel = threading.Event()
        cancel.set()
        with patch("swarm_app.usage.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(UsageUnavailable, "cancelled"):
                fetch_usage(str(self.executable), cancel=cancel)
            spawn.assert_not_called()

    def test_missing_executable_has_a_safe_message(self):
        with self.assertRaises(UsageUnavailable) as caught:
            fetch_usage("/missing/private-path/codex", timeout=1)
        self.assertNotIn("private-path", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
