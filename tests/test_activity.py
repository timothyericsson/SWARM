"""Activity signals from agent titles and Hermes's live composer."""

import unittest

from swarm_app.activity import hermes_prompt_activity, hermes_title_activity, title_finished


class HermesTitleActivityTests(unittest.TestCase):
    def test_busy_marker_with_or_without_session_title(self):
        for title in ("⏳", "⏳ Session", "⏳ Session · model · project", "⏳\ufe0f Session"):
            with self.subTest(title=title):
                self.assertIs(hermes_title_activity(title), True)

    def test_idle_and_waiting_markers_are_not_running(self):
        for marker in ("✓", "⚠", "⚠\ufe0f"):
            for suffix in ("", " Session", " Session · model · project"):
                title = marker + suffix
                with self.subTest(title=title):
                    self.assertIs(hermes_title_activity(title), False)

    def test_arbitrary_titles_and_embedded_markers_are_unknown(self):
        for title in (
            None, "", "  ", "Hermes", "Working", "Ready", "Hermes 2",
            "⠋ Working", "● ⠹ Task | project", "Review ⏳ task", "⏳Task",
            "✓Task", "⚠Task",
        ):
            with self.subTest(title=title):
                self.assertIsNone(hermes_title_activity(title))


class HermesPromptActivityTests(unittest.TestCase):
    columns = 80
    rule = "─" * columns

    def activity(self, *lines):
        return hermes_prompt_activity(list(lines), columns=self.columns)

    def test_live_busy_prompt_survives_typing_and_wrapped_input(self):
        for prompt in (
            "⚕ ❯ ",
            "⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel",
            "⚕ ❯ draft for the next turn",
            "⚕ > draft for the next turn",
            "⚕",
            "⚕ draft for the next turn",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(self.activity("previous output", self.rule, prompt), True)
        self.assertIs(self.activity(self.rule, "⚕ ❯ first draft line", "second draft line"), True)

    def test_waiting_and_idle_prompts_are_not_running(self):
        for prompt in (
            "❯ ", "❯ next message", "coder ❯ next message", "❯ quoted ⚕ ❯ output",
            "⚠ ❯ ", "🔐 ❯ ", "🔑 ❯ ", "✎ ❯ answer", "? ❯ ",
            "⚠", "🔐", "🔑", "✎", "?",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(self.activity(self.rule, prompt), False)

    def test_marker_in_multiline_idle_draft_does_not_mean_running(self):
        self.assertIs(self.activity(self.rule, "❯ explain this transcript", "⚕ ❯ quoted output"), False)

    def test_image_badges_do_not_hide_the_live_prompt(self):
        for badge in (
            "[📎 Image #1]",
            "[📎 Image #1] [📎 Image #2]",
            "[📎 screenshot.png]",
            "[📎 2 images attached]",
        ):
            with self.subTest(badge=badge):
                self.assertIs(self.activity(self.rule, badge, "⚕ ❯ draft"), True)
                self.assertIs(self.activity(self.rule, badge, "❯ draft"), False)

    def test_only_prompt_after_nearest_input_rule_is_considered(self):
        self.assertIs(self.activity(self.rule, "⚕ ❯ old turn", self.rule, "❯ current draft"), False)
        self.assertIsNone(self.activity(self.rule, "⚕ ❯ old turn", self.rule, "unrecognized chrome"))

    def test_unframed_output_and_short_rules_are_unknown(self):
        for lines in (
            [], ["⚕ ❯ "], ["tool output contains ⚕"],
            ["─" * 12, "⚕ ❯ "], ["-" * self.columns, "⚕ ❯ "],
            [self.rule, "tool output", "⚕ ❯ printed example"],
            [self.rule, ""], [self.rule, "Hermes is loading"],
        ):
            with self.subTest(lines=lines):
                self.assertIsNone(hermes_prompt_activity(lines, columns=self.columns))


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
