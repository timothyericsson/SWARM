"""Deterministic usage refresh races, using queued workers and main-loop callbacks."""

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from swarm_app.agent_usage import AgentUsageManager, uses_default_usage_account
from swarm_app.credentials import Credential
from swarm_app.linked_agents import StartupAgent
from swarm_app.provider_usage import ProviderUsageSnapshot
from swarm_app.usage import UsageUnavailable


class DefaultUsageAccountTests(unittest.TestCase):
    def uses_default(self, command, harness="codex"):
        return uses_default_usage_account(StartupAgent("agent", "Agent", command), harness)

    def test_normal_commands_and_explicit_builtin_openai_keep_default_account(self):
        for command in (
            "codex --yolo", "/opt/renamed-codex --yolo", "codex -c model_provider=openai",
            "codex -c 'model_provider = \"openai\"'", "codex --config=model_provider=openai",
            "codex -cmodel_provider=openai", "codex -c 'tui.terminal_title=[\"spinner\",\"status\"]'",
            "codex -c model_provider=custom -c model_provider=openai",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.uses_default(command))
        self.assertTrue(self.uses_default("hermes chat --provider deepseek --model deepseek-flash --yolo", "hermes"))

    def test_local_remote_or_custom_codex_provider_does_not_mirror_chatgpt(self):
        for command in (
            "codex --oss", "codex --oss=true", "codex --local-provider ollama",
            "codex --local-provider=lmstudio", "codex --remote https://server.example",
            "codex --remote=https://server.example", "codex -c model_provider=custom",
            "codex -c 'model_provider = \"openrouter\"'", "codex --config model_provider=custom",
            "codex --config=model_provider=custom", "codex -cmodel_provider=custom",
            "codex -c model_provider=openai -c model_provider=custom",
            "codex -c model_providers.openai.base_url=https://other.example",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.uses_default(command))

    def test_named_profiles_cannot_use_default_codex_or_hermes_credentials(self):
        for harness in ("codex", "hermes"):
            for option in ("--profile work", "--profile=work", "-p work", "-pwork"):
                with self.subTest(harness=harness, option=option):
                    self.assertFalse(self.uses_default(harness + " " + option, harness))

    def test_prompt_after_separator_is_not_interpreted_as_account_flags(self):
        for command in ("codex -- --oss", "codex -- -pwork", "codex -- -c model_provider=custom"):
            with self.subTest(command=command):
                self.assertTrue(self.uses_default(command))
        self.assertTrue(self.uses_default("hermes chat -- --profile other", "hermes"))
        self.assertFalse(self.uses_default("codex -pwork -- --yolo"))

    def test_malformed_or_unknown_commands_do_not_guess_an_account(self):
        for command in ("", "codex 'unterminated", "codex -c", "codex --config", "codex -c model_provider"):
            with self.subTest(command=command):
                self.assertFalse(self.uses_default(command))
        self.assertFalse(self.uses_default("other-agent", "custom"))


class AgentUsageManagerTests(unittest.TestCase):
    def setUp(self):
        self.workers = []
        self.callbacks = []
        self.changed = Mock()
        self.manager = AgentUsageManager(self.changed)
        self.addCleanup(self.manager.close)

        def thread(*, target, daemon):
            self.assertFalse(daemon)
            return Mock(start=lambda: self.workers.append(target))

        def idle_add(callback, *args):
            self.callbacks.append((callback, args))
            return len(self.callbacks)

        self.thread_patch = patch("swarm_app.agent_usage.threading.Thread", side_effect=thread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.idle_patch = patch("swarm_app.agent_usage.GLib.idle_add", side_effect=idle_add)
        self.idle_patch.start()
        self.addCleanup(self.idle_patch.stop)
        self.fetch_patch = patch("swarm_app.agent_usage.fetch_provider_usage")
        self.fetch = self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        self.agent = StartupAgent("research", "Research", "hermes chat --provider openrouter")
        self.credential = Credential("openrouter", "private-first-key", "OPENROUTER_API_KEY")
        self.snapshot = ProviderUsageSnapshot(75, "75% of this key budget remains.")
        self.fetch.return_value = self.snapshot

    def configure(self, credential=None, *, provider="openrouter", agent=None):
        self.manager.configure([(agent or self.agent, provider, credential or self.credential)])
        return self.manager.states[(agent or self.agent).id]

    def run_worker(self, index=0):
        self.workers.pop(index)()

    def deliver(self, index=0):
        callback, args = self.callbacks.pop(index)
        callback(*args)

    def complete(self):
        self.run_worker()
        self.deliver()

    def test_refresh_publishes_only_on_main_loop_and_pending_reads_do_not_overlap(self):
        state = self.configure()
        self.assertTrue(state.pending)
        self.assertIsNone(state.snapshot)
        self.manager.refresh(self.agent.id)
        self.manager.refresh()
        self.assertEqual(len(self.workers), 1)
        self.run_worker()
        self.fetch.assert_called_once_with("openrouter", "private-first-key", cancel=state.cancel)
        self.assertTrue(state.pending)
        self.assertIsNone(state.snapshot)
        self.deliver()
        self.assertFalse(state.pending)
        self.assertIsNone(state.cancel)
        self.assertIs(state.snapshot, self.snapshot)

    def test_queued_old_key_result_cannot_replace_rotated_key_usage(self):
        old = self.configure()
        old_cancel = old.cancel
        self.run_worker()
        replacement = replace(self.credential, secret="private-second-key")
        current = self.configure(replacement)
        self.assertIsNot(current, old)
        self.assertTrue(old_cancel.is_set())
        self.assertIsNone(current.snapshot)
        self.fetch.return_value = ProviderUsageSnapshot(18, "New account budget.")
        self.run_worker()
        self.deliver(1)  # New account finishes before an already queued old result.
        before = self.changed.call_count
        self.deliver(0)
        self.assertEqual(current.snapshot.percent_remaining, 18)
        self.assertEqual(self.changed.call_count, before)
        self.assertNotIn("private-second-key", repr(current))

    def test_rotating_before_worker_runs_passes_cancelled_token_and_rejects_result(self):
        old = self.configure()
        token = old.cancel
        current = self.configure(replace(self.credential, secret="private-second-key"))
        self.assertTrue(token.is_set())
        self.run_worker()
        self.fetch.assert_called_once_with("openrouter", "private-first-key", cancel=token)
        self.deliver()
        self.assertTrue(current.pending)
        self.assertIsNone(current.snapshot)
        self.complete()
        self.assertIs(current.snapshot, self.snapshot)

    def test_switching_provider_rejects_old_result_even_when_key_text_is_unchanged(self):
        old = self.configure()
        token = old.cancel
        self.run_worker()
        credential = replace(self.credential, provider="deepseek", variable="DEEPSEEK_API_KEY")
        current = self.configure(credential, provider="deepseek")
        self.assertIsNot(current, old)
        self.assertEqual(current.fingerprint, old.fingerprint)
        self.assertTrue(token.is_set())
        self.deliver()
        self.assertIsNone(current.snapshot)
        self.complete()
        self.assertEqual(self.fetch.call_args.args, ("deepseek", "private-first-key"))

    def test_removing_row_or_closing_cancels_worker_and_ignores_queued_result(self):
        for close in (False, True):
            with self.subTest(close=close):
                old = self.configure()
                token = old.cancel
                self.run_worker()
                if close:
                    self.manager.close()
                else:
                    self.manager.configure([])
                before = self.changed.call_count
                self.assertTrue(token.is_set())
                self.deliver()
                self.assertEqual(self.manager.states, {})
                self.assertEqual(self.changed.call_count, before)
                self.manager.refresh(self.agent.id)
                self.assertEqual(self.workers, [])

    def test_renaming_same_account_preserves_cached_snapshot_and_active_worker(self):
        state = self.configure()
        token = state.cancel
        renamed = replace(self.agent, name="Code review", command="hermes chat --provider openrouter --model new")
        self.configure(agent=renamed)
        self.assertIs(self.manager.states[self.agent.id], state)
        self.assertIs(state.agent, renamed)
        self.assertFalse(token.is_set())
        self.assertEqual(len(self.workers), 1)
        self.complete()
        self.configure(agent=self.agent)
        self.assertIs(state.snapshot, self.snapshot)
        self.assertEqual(self.workers, [])

    def test_same_key_rows_show_shared_percentage_without_dividing_or_summing(self):
        other = replace(self.agent, id="review", name="Review")
        self.manager.configure([(self.agent, "openrouter", self.credential),
                                (other, "openrouter", self.credential)])
        first, second = self.manager.states.values()
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertIsNot(first, second)
        self.complete()
        self.complete()
        self.assertEqual([state.snapshot.percent_remaining for state in self.manager.states.values()], [75, 75])
        self.assertTrue(all(call.args == ("openrouter", "private-first-key") for call in self.fetch.call_args_list))

    def test_distinct_keys_and_rotation_do_not_clear_another_account(self):
        other = replace(self.agent, id="review", name="Review")
        second_key = replace(self.credential, secret="private-second-key")
        specs = [(self.agent, "openrouter", self.credential), (other, "openrouter", second_key)]
        self.manager.configure(specs)
        self.fetch.side_effect = lambda _provider, key, **_: ProviderUsageSnapshot(
            75 if key == "private-first-key" else 20, "Account balance.")
        self.complete()
        self.complete()
        other_state = self.manager.states[other.id]
        self.manager.configure([(self.agent, "openrouter", replace(self.credential, secret="rotated-key")), specs[1]])
        self.assertIs(self.manager.states[other.id], other_state)
        self.assertEqual(other_state.snapshot.percent_remaining, 20)
        self.assertIsNone(self.manager.states[self.agent.id].snapshot)
        self.assertEqual(len(self.workers), 1)

    def test_implicit_credential_rotation_cancels_only_fallback_provider_reads(self):
        fallback = self.agent
        explicit = replace(self.agent, id="explicit", name="Saved account")
        unrelated = replace(self.agent, id="glm", name="GLM")
        saved_key = Credential("deepseek", "private-deepseek-key", "DEEPSEEK_API_KEY")
        self.manager.configure([(fallback, "deepseek", None), (explicit, "deepseek", saved_key),
                                (unrelated, "zai", None)])
        old_state = self.manager.states[fallback.id]
        old_token = old_state.cancel
        explicit_token = self.manager.states[explicit.id].cancel
        unrelated_token = self.manager.states[unrelated.id].cancel
        self.run_worker()  # Old fallback account response waits in the GTK queue.
        self.manager.invalidate("deepseek", implicit_only=True)
        self.assertTrue(old_token.is_set())
        self.assertFalse(explicit_token.is_set())
        self.assertFalse(unrelated_token.is_set())
        self.assertIsNone(old_state.snapshot)
        self.assertIsNot(old_state.cancel, old_token)
        self.deliver()
        self.assertIsNone(old_state.snapshot)
        self.assertTrue(old_state.pending)
        self.run_worker(2)  # Newly queued fallback fetch uses current Hermes credentials.
        self.fetch.assert_called_with("deepseek", None, cancel=old_state.cancel)
        self.deliver()
        self.assertIs(old_state.snapshot, self.snapshot)
        self.assertEqual(len(self.workers), 2)

    def test_removing_saved_key_clears_old_account_before_using_fallback(self):
        old = self.configure(provider="deepseek", credential=Credential(
            "deepseek", "private-deepseek-key", "DEEPSEEK_API_KEY"))
        self.complete()
        self.manager.configure([(self.agent, "deepseek", None)])
        current = self.manager.states[self.agent.id]
        self.assertIsNot(current, old)
        self.assertIsNone(current.snapshot)
        self.assertIsNone(current.fingerprint)
        self.complete()
        self.assertEqual(self.fetch.call_args.args, ("deepseek", None))

    def test_failed_refresh_removes_stale_percent_and_unexpected_errors_stay_private(self):
        state = self.configure()
        self.complete()
        for error in (UsageUnavailable("Provider rejected the key."), RuntimeError("private-first-key")):
            with self.subTest(error=type(error).__name__):
                self.fetch.side_effect = error
                self.manager.refresh(self.agent.id)
                self.complete()
                self.assertIsNone(state.snapshot)
                self.assertFalse(state.pending)
                self.assertNotIn("private-first-key", state.detail)
        self.assertIn("Click to try again", state.detail)

    def test_codex_mirror_and_unsupported_providers_do_not_create_workers(self):
        other = replace(self.agent, id="codex-copy")
        self.manager.configure([(self.agent, "anthropic", None), (other, "codex", None)])
        unsupported = self.manager.states[self.agent.id]
        self.assertIsNone(unsupported.snapshot.percent_remaining)
        self.assertEqual(unsupported.snapshot.label, "N/A")
        self.assertIn("SWARM", unsupported.snapshot.detail)
        self.assertFalse(self.manager.states[other.id].pending)
        self.manager.refresh()
        self.assertEqual(self.workers, [])
        self.fetch.assert_not_called()

    def test_mismatched_binding_stays_actionable_during_periodic_refresh_then_recovers(self):
        original = self.configure()
        old_token = original.cancel
        self.run_worker()
        error = "This agent's provider changed."
        invalid = [(self.agent, None, None)]
        self.manager.configure(invalid, errors={self.agent.id: error})
        state = self.manager.states[self.agent.id]
        self.assertTrue(old_token.is_set())
        self.assertIsNone(state.snapshot.percent_remaining)
        self.assertEqual(state.snapshot.label, "Update key")
        self.assertIn(error, state.snapshot.detail)
        self.deliver()  # A response for the previous binding must not replace the error.
        self.manager.refresh()
        self.manager.configure(invalid, errors={self.agent.id: error})
        self.assertIs(self.manager.states[self.agent.id], state)
        self.assertEqual(state.snapshot.label, "Update key")
        self.assertFalse(state.pending)
        self.assertEqual(self.workers, [])
        recovered = self.configure()
        self.assertIsNot(recovered, state)
        self.assertIsNone(recovered.snapshot)
        self.complete()
        self.assertIs(recovered.snapshot, self.snapshot)


if __name__ == "__main__":
    unittest.main()
