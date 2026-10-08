"""Credential detection, private persistence, and launch isolation without API calls."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swarm_app.credentials import CredentialError, CredentialStore, parse_key, provider_for_agent
from swarm_app.linked_agents import StartupAgent


class DetectionTests(unittest.TestCase):
    def test_distinctive_prefixes_and_generic_ambiguity(self):
        for key, provider in (("sk-proj-fixture", "openai"), ("sk-ant-api03-fixture", "anthropic"),
                              ("sk-or-v1-fixture", "openrouter"), ("gsk_fixture", "groq"),
                              ("xai-fixture", "xai"), ("AIzaFixture", "gemini")):
            self.assertEqual(parse_key(key).provider, provider)
        for key in ("sk-generic-fixture", "opaque-fixture", "123.456"):
            self.assertIsNone(parse_key(key).provider)

    def test_assignments_identify_provider_without_exposing_secret(self):
        value = "fixture-only-key"
        parsed = parse_key("export DEEPSEEK_API_KEY='" + value + "' # copied from settings")
        self.assertEqual((parsed.provider, parsed.variable, parsed.secret),
                         ("deepseek", "DEEPSEEK_API_KEY", value))
        self.assertNotIn(value, repr(parsed))
        custom = parse_key("ACME_API_KEY=fixture")
        self.assertEqual(custom.variable, "ACME_API_KEY")
        self.assertIsNone(custom.provider)

    def test_invalid_or_conflicting_input_has_safe_errors(self):
        for value in ("", "secret with spaces", "KEY='unterminated-secret", "KEY=one\nOTHER=two",
                      "DEEPSEEK_API_KEY=sk-ant-api03-private-marker"):
            with self.assertRaises(CredentialError) as caught:
                parse_key(value)
            self.assertNotIn("private-marker", str(caught.exception))
            self.assertNotIn("unterminated-secret", str(caught.exception))

    def test_startup_provider_inference_prioritizes_explicit_provider(self):
        cases = (("codex --yolo", "openai"), ("/usr/bin/claude", "anthropic"),
                 ("gemini -m gemini-pro", "gemini"),
                 ("hermes chat --provider=openrouter --model deepseek-flash", "openrouter"),
                 ("hermes chat --provider openai-api", "openai"),
                 ("hermes chat --model glm-5.3-flash", "zai"),
                 ("hermes chat --provider unknown --model glm-5.3-flash", None),
                 ("hermes", None), ("my-harness --model gpt-5", None))
        for command, provider in cases:
            with self.subTest(command=command):
                self.assertEqual(provider_for_agent(StartupAgent("a", "Agent", command)), provider)

    def test_codex_explicit_routes_do_not_take_an_openai_provider_default(self):
        for options in ("--oss", "--local-provider ollama", "--profile work", "-p work",
                        "--remote ws://localhost:7777", '-c model_provider="custom"',
                        '--config=model_provider="openrouter"'):
            self.assertIsNone(provider_for_agent(StartupAgent("a", "Agent", "codex " + options)))
        self.assertEqual(provider_for_agent(StartupAgent("a", "Agent", 'codex -c model_provider="openai"')), "openai")
        self.assertEqual(provider_for_agent(StartupAgent("a", "Agent", "codex -- --oss")), "openai")


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "swarm" / "credentials.json"
        self.store = CredentialStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_atomic_private_roundtrip_agent_override_and_delete(self):
        agent = StartupAgent("deepseek", "DeepSeek", "hermes chat --provider deepseek")
        self.store.set("deepseek", "default-fixture")
        record = self.store.set("deepseek", "agent-fixture", agent_id=agent.id)
        self.assertNotIn("agent-fixture", repr(record))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        loaded = CredentialStore(self.path)
        self.assertEqual(loaded.credential_for_agent(agent).secret, "agent-fixture")
        loaded.delete("deepseek", agent_id=agent.id)
        self.assertEqual(loaded.credential_for_agent(agent).secret, "default-fixture")
        loaded.delete("deepseek")
        self.assertIsNone(loaded.credential_for_agent(agent))

    def test_failed_save_keeps_disk_and_memory_and_hides_oserror(self):
        self.store.set("deepseek", "original-fixture")
        original = self.path.read_bytes()
        with patch("swarm_app.credentials.os.replace", side_effect=OSError("private-marker")):
            with self.assertRaises(CredentialError) as caught:
                self.store.set("deepseek", "replacement-fixture")
        self.assertNotIn("private-marker", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.get("deepseek").secret, "original-fixture")
        self.assertEqual(list(self.path.parent.glob(".credentials-*")), [])

    def test_malformed_store_error_never_contains_file_contents(self):
        self.path.parent.mkdir()
        self.path.write_text('{"secret":"private-marker"')
        store = CredentialStore(self.path)
        self.assertTrue(store.load_error)
        self.assertNotIn("private-marker", store.load_error)
        self.assertEqual(store.records, ())

    def test_changed_provider_cannot_receive_previous_agent_key(self):
        agent = StartupAgent("same", "Agent", "hermes --provider deepseek")
        self.store.set("deepseek", "fixture", agent_id=agent.id)
        changed = replace(agent, command="hermes --provider openrouter")
        with self.assertRaises(CredentialError):
            self.store.environment_for_agent(changed)
        with self.assertRaises(CredentialError):
            self.store.command_for_agent(changed, ["hermes", "--provider", "openrouter"])

    def test_unknown_agents_require_explicit_binding(self):
        agent = StartupAgent("custom-agent", "Custom", "my-harness")
        self.store.set("openai", "sk-proj-fixture")
        self.assertEqual(self.store.environment_for_agent(agent), {})
        self.store.set("custom", "opaque-fixture", agent_id=agent.id, variable="ACME_CREDENTIAL")
        self.assertEqual(self.store.environment_for_agent(agent), {"ACME_CREDENTIAL": "opaque-fixture"})

    def test_custom_variable_cannot_override_process_loader(self):
        for variable in ("PATH", "LD_PRELOAD", "CODEX_HOME", "INVALID=VAR"):
            with self.assertRaises(CredentialError):
                self.store.set("custom", "fixture", variable=variable)

    def test_codex_launch_uses_env_provider_and_preserves_saved_oauth(self):
        agent = StartupAgent("codex", "Codex", "codex --yolo")
        self.store.set("openai", "sk-proj-fixture-secret")
        command = self.store.command_for_agent(agent, ["codex", "--yolo"])
        self.assertIn("--no-daemon", command)
        self.assertIn('model_provider="swarm_openai"', command)
        self.assertTrue(any('env_key="OPENAI_API_KEY"' in part for part in command))
        self.assertTrue(any("requires_openai_auth=false" in part for part in command))
        self.assertNotIn("sk-proj-fixture-secret", repr(command))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "inherited-fixture"}):
            self.assertEqual(self.store.environment_for_agent(agent), {"OPENAI_API_KEY": "sk-proj-fixture-secret"})
            self.assertEqual(os.environ["OPENAI_API_KEY"], "inherited-fixture")

    def test_codex_api_key_refuses_remote_server_and_wrong_provider(self):
        agent = StartupAgent("codex", "Codex", "codex --yolo")
        self.store.set("openai", "fixture")
        with self.assertRaises(CredentialError):
            self.store.command_for_agent(agent, ["codex", "--remote", "ws://localhost:1234"])
        self.store.set("custom", "fixture", agent_id=agent.id, variable="ACME_KEY")
        with self.assertRaises(CredentialError):
            self.store.command_for_agent(agent, ["codex"])

    def test_hermes_profile_preserves_resources_and_isolates_stale_keys(self):
        agent = StartupAgent("glm", "GLM", "hermes chat --provider zai")
        profile = Path(self.temp.name) / "hermes"
        profile.mkdir()
        (profile / "config.yaml").write_text("model: fixture\n")
        (profile / "sessions").mkdir()
        (profile / ".env").write_text("GLM_API_KEY=old-fixture\nOTHER_KEY=keep-fixture\n")
        (profile / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": {
            "zai": [{"access_token": "stale-pool-fixture"}], "openrouter": [{"access_token": "other-fixture"}]}}))
        original = (profile / ".env").read_bytes()
        original_auth = (profile / "auth.json").read_bytes()
        self.store.set("zai", "new-fixture", agent_id=agent.id)
        with patch.dict(os.environ, {"HERMES_HOME": str(profile), "GLM_API_KEY": "old-shell"}):
            environment = self.store.environment_for_agent(agent)
            self.assertEqual(os.environ["GLM_API_KEY"], "old-shell")
        overlay = Path(environment["HERMES_HOME"])
        self.assertEqual((overlay / "config.yaml").read_text(), "model: fixture\n")
        self.assertEqual((overlay / "sessions").resolve(), profile / "sessions")
        self.assertEqual((overlay / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((overlay / "auth.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o700)
        self.assertEqual((profile / ".env").read_bytes(), original)
        self.assertEqual((profile / "auth.json").read_bytes(), original_auth)
        saved_auth = json.loads((overlay / "auth.json").read_text())
        self.assertNotIn("zai", saved_auth["credential_pool"])
        self.assertIn("openrouter", saved_auth["credential_pool"])
        for name in ("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"):
            self.assertEqual(environment[name], "new-fixture")
            self.assertIn(name + "='new-fixture'", (overlay / ".env").read_text())
        self.store.close()
        self.assertFalse(overlay.exists())

    def test_hermes_old_env_endpoint_is_replaced_by_official_provider_endpoint(self):
        agent = StartupAgent("deepseek", "Agent", "hermes --provider deepseek")
        profile = Path(self.temp.name) / "hermes"
        profile.mkdir()
        original = "DEEPSEEK_BASE_URL=https://old-proxy.invalid/v1\n"
        (profile / ".env").write_text(original)
        self.store.set("deepseek", "fixture")
        with patch.dict(os.environ, {"HERMES_HOME": str(profile), "HERMES_MANAGED_DIR": str(profile / "absent")}):
            environment = self.store.environment_for_agent(agent)
        self.assertEqual(environment["DEEPSEEK_BASE_URL"], "https://api.deepseek.com/v1")
        overlay = Path(environment["HERMES_HOME"])
        self.assertTrue((overlay / ".env").read_text().rstrip().endswith("DEEPSEEK_BASE_URL='https://api.deepseek.com/v1'"))
        self.assertEqual((profile / ".env").read_text(), original)

    def test_hermes_foreign_config_endpoint_or_embedded_key_is_rejected_safely(self):
        agent = StartupAgent("deepseek", "Agent", "hermes --provider deepseek")
        profile = Path(self.temp.name) / "hermes"
        profile.mkdir()
        self.store.set("deepseek", "fixture")
        for config in ({"model": {"provider": "deepseek", "base_url": "https://foreign.invalid/v1"}},
                       {"model": {"base_url": "https://api.deepseek.com.attacker.invalid/v1"}},
                       {"model": {"base_url": "https://api.deepseek.com@foreign.invalid/v1"}},
                       {"model": {"api_key": "private-marker"}}):
            (profile / "config.yaml").write_text(json.dumps(config))
            with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
                with self.assertRaises(CredentialError) as caught:
                    self.store.environment_for_agent(agent)
            self.assertNotIn("private-marker", str(caught.exception))
            self.assertEqual(list(self.path.parent.glob(".hermes-launch-*")), [])

    def test_hermes_managed_key_cannot_silently_override_saved_key(self):
        agent = StartupAgent("deepseek", "Agent", "hermes --provider deepseek")
        profile = Path(self.temp.name) / "hermes"
        managed = Path(self.temp.name) / "managed"
        profile.mkdir()
        managed.mkdir()
        (managed / ".env").write_text("DEEPSEEK_API_KEY=private-marker\n")
        self.store.set("deepseek", "fixture")
        with patch.dict(os.environ, {"HERMES_HOME": str(profile), "HERMES_MANAGED_DIR": str(managed)}):
            with self.assertRaises(CredentialError) as caught:
                self.store.environment_for_agent(agent)
        self.assertIn("Machine-managed", str(caught.exception))
        self.assertNotIn("private-marker", str(caught.exception))

    def test_hermes_new_history_survives_private_profile_cleanup(self):
        agent = StartupAgent("deepseek", "Agent", "hermes --provider deepseek")
        profile = Path(self.temp.name) / "missing-hermes"
        self.store.set("deepseek", "fixture")
        with patch.dict(os.environ, {"HERMES_HOME": str(profile), "HERMES_MANAGED_DIR": str(profile / "absent")}):
            overlay = Path(self.store.environment_for_agent(agent)["HERMES_HOME"])
            history = (overlay / ".hermes_history").resolve()
            (overlay / ".hermes_history").write_text("fixture command history")
            self.store.close()
            second = Path(self.store.environment_for_agent(agent)["HERMES_HOME"])
        self.assertEqual(history.read_text(), "fixture command history")
        self.assertEqual((second / ".hermes_history").resolve(), history)
        self.assertFalse(profile.exists())

    def test_resolved_executable_alias_keeps_original_auth_configuration(self):
        agent = StartupAgent("codex", "Codex", "codex --yolo")
        self.store.set("openai", "fixture")
        command = self.store.command_for_agent(agent, ["/opt/company/codex-wrapper", "--yolo"])
        self.assertIn('model_provider="swarm_openai"', command)

    def test_codex_provider_default_respects_local_route_but_row_binding_switches_it(self):
        agent = StartupAgent("codex", "Codex", "codex --oss --local-provider ollama")
        argv = ["codex", "--oss", "--local-provider", "ollama", "--", "--oss"]
        self.store.set("openai", "default-fixture")
        self.assertEqual(self.store.command_for_agent(agent, argv), argv)
        self.assertEqual(self.store.environment_for_agent(agent), {})
        self.store.set("openai", "bound-fixture", agent_id=agent.id)
        command = self.store.command_for_agent(agent, argv)
        self.assertNotIn("--oss", command[:command.index("--")])
        self.assertNotIn("--local-provider", command)
        self.assertEqual(command[-2:], ["--", "--oss"])
        self.assertEqual(self.store.environment_for_agent(agent)["OPENAI_API_KEY"], "bound-fixture")

    def test_anthropic_api_key_does_not_reenable_cached_oauth_tokens(self):
        agent = StartupAgent("claude", "Claude", "claude")
        self.store.set("anthropic", "sk-ant-api03-fixture")
        environment = self.store.environment_for_agent(agent)
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "sk-ant-api03-fixture")
        self.assertEqual(environment["ANTHROPIC_TOKEN"], "")
        self.assertEqual(environment["CLAUDE_CODE_OAUTH_TOKEN"], "")

    def test_hermes_provider_is_explicit_and_oauth_route_switches_to_api(self):
        agent = StartupAgent("hermes", "OpenAI", "hermes chat --provider openai-codex")
        self.store.set("openai", "fixture")
        command = self.store.command_for_agent(agent, ["hermes", "chat", "--provider=openai-codex", "--yolo"])
        self.assertEqual(command, ["hermes", "chat", "--yolo", "--provider", "openai-api"])

    def test_launch_options_leave_prompt_suffix_after_separator_unchanged(self):
        self.store.set("openai", "fixture")
        for executable in ("codex", "hermes"):
            agent = StartupAgent("a", "Agent", executable + " --model gpt-5 -- --provider example")
            suffix = ["--", "--provider", "example"]
            command = self.store.command_for_agent(agent, [executable, "--model", "gpt-5"] + suffix)
            self.assertEqual(command[command.index("--"):], suffix)


if __name__ == "__main__":
    unittest.main()
