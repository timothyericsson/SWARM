"""Completion signals from managed and default manual Codex titles."""

import unittest

from swarm_app.activity import title_finished


class CompletionTitleTests(unittest.TestCase):
    def test_manual_spinner_disappearance_means_finished_after_observed_work(self):
        for busy, ready in (
            ("⠋ Task | project", "Task | project"),
            ("● ⠹ Task | project", "● Task | project"),
            ("⠼ Old task | project", "Updated task | project"),
        ):
            with self.subTest(busy=busy, ready=ready):
                self.assertTrue(title_finished(ready, busy))

    def test_manual_title_without_observed_spinner_never_means_finished(self):
        for busy in (None, "", "project", "Ready", "Working", "[ ! ] Action Required"):
            with self.subTest(busy=busy):
                self.assertFalse(title_finished("Task | project", busy))

    def test_manual_unknown_busy_and_action_required_titles_never_mean_finished(self):
        for title in (
            None, "", "  ", "●", "●  ", "⠋ Task | project", "● ⠋ Task | project",
            "[ ! ] Action Required | project", "[ . ] Action Required | project",
            "● [ ! ] Action Required | project", "Starting", "Working", "Thinking", "Waiting",
        ):
            with self.subTest(title=title):
                self.assertFalse(title_finished(title, "⠋ Task | project"))

    def test_spinner_only_title_clearing_is_unavailable(self):
        for title in (None, "", "●"):
            with self.subTest(title=title):
                self.assertFalse(title_finished(title, "⠋"))

    def test_managed_completion_still_requires_affirmative_ready(self):
        for title in ("Ready", "● Ready", "⠹ Ready"):
            with self.subTest(title=title):
                self.assertTrue(title_finished(title, "Working", managed=True))
        for title in (None, "", "project", "Starting", "Working", "[ ! ] Action Required"):
            with self.subTest(title=title):
                self.assertFalse(title_finished(title, "Working", managed=True))


if __name__ == "__main__":
    unittest.main()
