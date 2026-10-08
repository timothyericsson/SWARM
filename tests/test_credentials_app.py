"""Credential and per-agent usage integration without real accounts or network."""

from dataclasses import replace
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swarm_app.app import Gtk, SwarmApplication
from swarm_app.credentials import CredentialError, CredentialStore
from swarm_app.credentials_dialog import CredentialsDialog
from swarm_app.deepseek_usage import CreditBalance, DeepSeekSnapshot
from swarm_app.linked_agents import StartupAgent, default_startup_agents
from swarm_app.provider_usage import ProviderUsageSnapshot
from swarm_app.session import TerminalSession
from swarm_app.usage import UsageSnapshot, UsageWindow
from gtk_test_support import shutdown_application
from test_session import GTK_AVAILABLE, pump_until


KEY = "sk-test-placeholder-12345678901234567890"
OPENAI_KEY = "sk-proj-test-placeholder-12345678901234567890"
ROUTER_KEY = "sk-or-v1-test-placeholder-12345678901234567890"


@unittest.skipUnless(GTK_AVAILABLE, "A display or xvfb-run is required")
class CredentialApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings_path = self.root / "linked-agents.json"
        self.credentials_path = self.root / "credentials.json"
        managed = self.root / "hermes-managed"
        managed.mkdir()
        environment = dict(os.environ)
        environment.pop("SWARM_DEEPSEEK_KEY_FILE", None)
        environment["HERMES_HOME"] = str(self.root / "hermes")
        environment["HERMES_MANAGED_DIR"] = str(managed)
        environment["XDG_CONFIG_HOME"] = str(self.root / "config")
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.fetch_codex = self.enterContext(patch("swarm_app.app.fetch_usage", return_value=UsageSnapshot(None, None)))
        self.fetch_zai = self.enterContext(patch("swarm_app.app.fetch_zai_usage", return_value=UsageSnapshot(
            UsageWindow(74, 300, None), None)))
        self.fetch_deepseek = self.enterContext(patch("swarm_app.app.fetch_deepseek_usage", return_value=DeepSeekSnapshot(
            (CreditBalance("USD", Decimal("5"), Decimal("10")),), True)))
        self.fetch_provider = self.enterContext(patch("swarm_app.agent_usage.fetch_provider_usage", return_value=
            ProviderUsageSnapshot(62.8, "Fake provider budget: 62.8% left.")))
        self.enterContext(patch.object(SwarmApplication, "refresh_auth"))
        self.legacy_sync = self.enterContext(patch("swarm_app.app.DeepSeekKeySync"))
        self.app = SwarmApplication(str(self.root), linked_agents_path=self.settings_path,
                                    credentials_path=self.credentials_path)
        self.app.set_application_id("io.swarm.Terminal.CredentialsAppTests")
        self.assertTrue(self.app.register(None))
        self.app.hold()
        self.window = self.app.new_window(start_terminal=False)
        self.windows = [self.window]

    def tearDown(self):
        self.settle()
        for window in self.windows:
            window.destroy()
        pump_until(lambda: not self.app.get_windows())
        self.app.release()
        shutdown_application(self.app)

    def settle(self):
        pump_until(lambda: not self.app.zai_usage_pending and not self.app.deepseek_usage_pending
                   and not any(state.pending for state in self.app.agent_usage.states.values()))

    def test_save_and_remove_keys_persist_with_agent_scope_and_keep_other_keys(self):
        self.assertTrue(self.app.save_api_key("deepseek", "DEEPSEEK_API_KEY=" + KEY))
        self.assertTrue(self.app.save_api_key("zai", KEY + "-agent", agent_id="hermes"))
        self.settle()
        restored = CredentialStore(self.credentials_path)
        self.assertEqual(restored.get("deepseek").secret, KEY)
        self.assertEqual(restored.get("zai", "hermes").secret, KEY + "-agent")
        self.assertEqual(self.credentials_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.app.delete_api_key("zai", agent_id="hermes"))
        self.assertEqual([(record.provider, record.agent_id) for record in self.app.credentials.records],
                         [("deepseek", None)])
        self.assertIsNone(CredentialStore(self.credentials_path).get("zai", "hermes"))
        self.assertTrue(self.app.delete_api_key("deepseek"))
        self.assertEqual(CredentialStore(self.credentials_path).records, ())
        self.assertIsNone(self.app.credential_error)

    def test_invalid_provider_target_and_assignment_cannot_change_saved_keys(self):
        self.assertTrue(self.app.save_api_key("deepseek", KEY))
        previous = self.credentials_path.read_bytes()
        for provider, key, target, variable in (
                ("unknown", KEY, None, None),
                ("deepseek", OPENAI_KEY, None, None),
                ("deepseek", KEY, "missing-agent", None),
                ("deepseek", KEY, "codex", None),
                ("openai", "DEEPSEEK_API_KEY=" + KEY, None, None),
                ("custom", KEY, None, "HELPER_API_KEY"),
                ("custom", KEY, "hermes", "PATH")):
            with self.subTest(provider=provider, target=target, variable=variable):
                self.assertFalse(self.app.save_api_key(provider, key, agent_id=target, variable=variable))
                self.assertEqual(self.credentials_path.read_bytes(), previous)
                self.assertTrue(self.app.credential_error)
                self.assertNotIn(KEY, self.app.credential_error)
                self.assertNotIn(OPENAI_KEY, self.app.credential_error)

    def test_failed_save_and_delete_leave_previous_credentials_available(self):
        self.assertTrue(self.app.save_api_key("deepseek", KEY))
        previous = self.credentials_path.read_bytes()
        with patch.object(self.app.credentials, "set", side_effect=CredentialError("Could not save API keys.")):
            self.assertFalse(self.app.save_api_key("deepseek", KEY + "-replacement"))
        self.assertEqual(self.app.credential_error, "Could not save API keys.")
        with patch.object(self.app.credentials, "delete", side_effect=CredentialError("Could not save API keys.")):
            self.assertFalse(self.app.delete_api_key("deepseek"))
        self.assertEqual(self.credentials_path.read_bytes(), previous)
        self.assertEqual(self.app.credentials.get("deepseek").secret, KEY)

    def test_open_swarm_forwards_saved_keys_as_environment_and_selects_api_auth(self):
        defaults = default_startup_agents()
        custom = StartupAgent("helper", "Local helper", "/fake/helper --model local")
        self.assertTrue(self.app.save_startup_swarm([defaults[0], defaults[2], custom], False))
        self.assertTrue(self.app.save_api_key("openai", OPENAI_KEY, agent_id="codex"))
        self.assertTrue(self.app.save_api_key("deepseek", KEY, agent_id="deepseek"))
        self.assertTrue(self.app.save_api_key("custom", KEY + "-custom", agent_id="helper",
                                               variable="HELPER_API_KEY"))
        with patch("swarm_app.app.shutil.which", side_effect=lambda command: {
                "codex": "/fake/codex", "hermes": "/fake/hermes", "/fake/helper": "/fake/helper"}.get(command)), \
                patch.object(TerminalSession, "start", autospec=True) as start:
            sessions = self.window.open_swarm()
        self.assertEqual(len(sessions), 3)
        calls = {call.args[0].agent_profile: call for call in start.call_args_list}
        codex = calls["codex"]
        self.assertIn('model_provider="swarm_openai"', codex.args[1])
        self.assertEqual(codex.kwargs["environment"]["OPENAI_API_KEY"], OPENAI_KEY)
        deepseek = calls["deepseek"]
        self.assertEqual(deepseek.kwargs["environment"]["DEEPSEEK_API_KEY"], KEY)
        self.assertEqual(deepseek.args[1][-2:], ["--provider", "deepseek"])
        overlay = Path(deepseek.kwargs["environment"]["HERMES_HOME"]) / ".env"
        self.assertIn("DEEPSEEK_API_KEY='" + KEY + "'", overlay.read_text())
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)
        self.assertEqual(calls["helper"].kwargs["environment"], {"HELPER_API_KEY": KEY + "-custom"})
        for call in start.call_args_list:
            self.assertNotIn(KEY, " ".join(call.args[1]))
            self.assertNotIn(OPENAI_KEY, " ".join(call.args[1]))

    def test_custom_variable_can_target_hermes_but_cannot_change_codex_provider(self):
        self.assertTrue(self.app.save_api_key("custom", KEY, agent_id="hermes", variable="CUSTOM_API_KEY"))
        record = self.app.credentials.credential_for_agent(self.app.linked_agents.agents[1])
        self.assertEqual(record.provider, "custom")
        self.assertEqual(record.variable, "CUSTOM_API_KEY")
        self.settle()
        self.assertFalse(self.window.zai_usage_button.get_visible())
        self.assertIn("N/A", self.window.agent_usage_badges["hermes"].get_label())
        self.assertFalse(any(call.kwargs.get("api_key") == KEY for call in self.fetch_zai.call_args_list))
        self.assertFalse(self.app.save_api_key("custom", KEY, agent_id="codex", variable="CUSTOM_API_KEY"))
        self.assertIn("OpenAI", self.app.credential_error)
        self.assertEqual([(saved.provider, saved.agent_id) for saved in self.app.credentials.records],
                         [("custom", "hermes")])

    def test_key_rotation_applies_on_restart_without_relaunching_running_tabs(self):
        agent = StartupAgent("helper", "Local helper", "/fake/helper")
        self.assertTrue(self.app.save_startup_swarm([agent], False))
        self.assertTrue(self.app.save_api_key("custom", KEY, agent_id="helper", variable="HELPER_API_KEY"))
        with patch("swarm_app.app.shutil.which", return_value="/fake/helper"), \
                patch.object(TerminalSession, "start", autospec=True) as start:
            original = self.window.open_swarm()[0]
            original.state = "running"
            self.assertTrue(self.app.save_api_key("custom", KEY + "-rotated", agent_id="helper",
                                                   variable="HELPER_API_KEY"))
            self.assertEqual(start.call_count, 1)
            self.assertEqual(start.call_args.kwargs["environment"]["HELPER_API_KEY"], KEY)
            original.state = "exited"
            self.window.restart_current()
            self.assertEqual(start.call_count, 2)
            self.assertEqual(start.call_args.kwargs["environment"]["HELPER_API_KEY"], KEY + "-rotated")

    def test_codex_symlink_resolution_keeps_api_auth_selection(self):
        target = self.root / "codex.js"
        target.write_text("#!/usr/bin/node\n")
        target.chmod(0o755)
        launcher = self.root / "codex"
        launcher.symlink_to(target)
        self.assertTrue(self.app.save_startup_swarm([default_startup_agents()[0]], False))
        self.assertTrue(self.app.save_api_key("openai", OPENAI_KEY))
        with patch.object(self.app, "codex", str(launcher)), \
                patch.object(TerminalSession, "start", autospec=True) as start:
            self.window.open_swarm()
        self.assertEqual(start.call_args.args[1][0], str(target))
        self.assertIn('model_provider="swarm_openai"', start.call_args.args[1])
        self.assertEqual(start.call_args.kwargs["environment"]["OPENAI_API_KEY"], OPENAI_KEY)

    def test_changed_provider_blocks_old_key_launch_and_requests_key_update_in_badge(self):
        agent = StartupAgent("extra", "Extra model", "hermes chat --provider deepseek")
        self.assertTrue(self.app.save_startup_swarm([agent], False))
        self.assertTrue(self.app.save_api_key("deepseek", KEY, agent_id="extra"))
        self.settle()
        self.fetch_provider.reset_mock()
        self.assertTrue(self.app.save_startup_swarm([replace(agent, command="hermes chat --provider openrouter")], False))
        self.settle()
        badge = self.window.agent_usage_badges["extra"]
        self.assertIn("Update key", badge.get_label())
        self.assertFalse(badge.refresh_button.get_sensitive())
        self.fetch_provider.assert_not_called()
        with patch.object(self.window, "resolve_harness", return_value="/fake/hermes"), \
                patch.object(self.window, "message") as message, \
                patch.object(TerminalSession, "start") as start:
            self.assertEqual(self.window.open_swarm(), [])
        start.assert_not_called()
        message.assert_called_once()
        self.assertNotIn(KEY, str(message.call_args))

    def test_actions_add_key_opens_reusable_dialog_and_saved_keys_sync_other_windows(self):
        self.window.add_key_item.activate()
        first = self.window.credentials_dialog
        self.assertIsInstance(first, CredentialsDialog)
        self.window.add_key_item.activate()
        self.assertIs(self.window.credentials_dialog, first)
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        other.open_credentials()
        second = other.credentials_dialog
        second.key_entry.set_text(KEY + "-draft")
        self.assertTrue(self.app.save_api_key("deepseek", KEY))
        self.assertEqual(len(first.saved_rows), 1)
        self.assertEqual(len(second.saved_rows), 1)
        self.assertEqual(second.key_entry.get_text(), KEY + "-draft")
        self.assertNotIn(KEY, first.saved_rows[0].label.get_text())
        first.response(Gtk.ResponseType.CLOSE)
        self.assertIsNone(self.window.credentials_dialog)

    def test_fourth_agent_gets_live_badge_and_disabling_or_removing_it_updates_windows(self):
        defaults = default_startup_agents()
        fourth = StartupAgent("router", "Fourth model", "hermes chat --provider openrouter --model example/model")
        self.assertTrue(self.app.save_api_key("openrouter", ROUTER_KEY))
        self.assertTrue(self.app.save_startup_swarm([*defaults, fourth], False))
        self.settle()
        self.assertEqual(set(self.window.agent_usage_badges), {"router"})
        badge = self.window.agent_usage_badges["router"]
        self.assertIn("62%", badge.get_label())
        self.assertTrue(badge.refresh_button.get_sensitive())
        self.assertTrue(self.window.extra_usage_scroll.get_visible())
        self.assertTrue(any(call.args[:2] == ("openrouter", ROUTER_KEY)
                            for call in self.fetch_provider.call_args_list))
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        self.assertEqual(set(other.agent_usage_badges), {"router"})
        self.assertTrue(self.app.save_startup_swarm([*defaults, replace(fourth, enabled=False)], False))
        self.assertEqual(self.window.agent_usage_badges, {})
        self.assertEqual(other.agent_usage_badges, {})
        self.assertFalse(self.window.extra_usage_scroll.get_visible())
        self.assertTrue(self.app.save_startup_swarm([*defaults, fourth], False))
        self.assertIn("router", self.window.agent_usage_badges)
        self.assertTrue(self.app.save_startup_swarm(defaults, False))
        self.assertNotIn("router", self.app.agent_usage.states)
        self.assertEqual(self.window.agent_usage_badges, {})

    def test_unsupported_provider_has_unavailable_badge_without_network_probe(self):
        agent = StartupAgent("claude", "Claude helper", "claude --dangerously-skip-permissions")
        self.assertTrue(self.app.save_startup_swarm([agent], False))
        self.assertTrue(self.app.save_api_key("anthropic", KEY, agent_id="claude"))
        self.settle()
        badge = self.window.agent_usage_badges["claude"]
        self.assertIn("N/A", badge.get_label())
        self.assertIn("billing dashboard", badge.details.get_text())
        self.assertFalse(badge.refresh_button.get_sensitive())
        self.fetch_provider.assert_not_called()
        self.assertFalse(self.window.usage_button.get_visible())
        self.assertFalse(self.window.zai_usage_button.get_visible())
        self.assertFalse(self.window.deepseek_usage_button.get_visible())

    def test_local_custom_and_alternate_profiles_do_not_reuse_default_usage_accounts(self):
        defaults = default_startup_agents()
        agents = [replace(defaults[0], command="codex --yolo --oss"),
                  replace(defaults[1], command=defaults[1].command + " --profile alternate"),
                  StartupAgent("other-codex", "Custom Codex", "codex --yolo -c model_provider=example")]
        self.assertTrue(self.app.save_startup_swarm(agents, False))
        self.settle()
        self.assertEqual(self.app.legacy_usage_agents(), {})
        self.assertFalse(self.window.usage_button.get_visible())
        self.assertFalse(self.window.zai_usage_button.get_visible())
        self.assertEqual(set(self.app.agent_usage.states), {agent.id for agent in agents})
        for agent in agents:
            with self.subTest(agent=agent.id):
                self.assertIsNone(self.app.agent_usage.states[agent.id].provider)
                badge = self.window.agent_usage_badges[agent.id]
                self.assertIn("N/A", badge.get_label())
                self.assertFalse(badge.refresh_button.get_sensitive())
        self.fetch_provider.assert_not_called()

    def test_saved_openai_key_replaces_subscription_badge_with_api_usage_status(self):
        self.assertTrue(self.window.usage_button.get_visible())
        self.assertTrue(self.app.save_api_key("openai", OPENAI_KEY))
        self.settle()
        self.assertFalse(self.window.usage_button.get_visible())
        self.assertIn("codex", self.window.agent_usage_badges)
        self.assertIn("N/A", self.window.agent_usage_badges["codex"].get_label())
        self.assertTrue(self.app.delete_api_key("openai"))
        self.assertTrue(self.window.usage_button.get_visible())
        self.assertNotIn("codex", self.window.agent_usage_badges)

    def test_legacy_usage_fetches_receive_selected_saved_keys(self):
        self.assertTrue(self.app.save_api_key("zai", KEY + "-zai", agent_id="hermes"))
        self.assertTrue(self.app.save_api_key("deepseek", KEY + "-deepseek", agent_id="deepseek"))
        self.settle()
        self.assertEqual(self.fetch_zai.call_args.kwargs["api_key"], KEY + "-zai")
        self.assertEqual(self.fetch_deepseek.call_args.kwargs["api_key"], KEY + "-deepseek")
        self.assertEqual(self.window.zai_usage_button.get_label(), "74%")
        self.assertEqual(self.window.deepseek_usage_button.get_label(), "50%")

    def test_default_startup_never_opens_legacy_deepseek_text_file(self):
        self.assertIsNone(self.app.deepseek_key_sync)
        self.assertEqual(self.app.deepseek_key_poll, 0)
        self.legacy_sync.assert_not_called()
        self.app._sync_deepseek_key()
        self.legacy_sync.assert_not_called()
        self.assertFalse(self.credentials_path.exists())


if __name__ == "__main__":
    unittest.main()
