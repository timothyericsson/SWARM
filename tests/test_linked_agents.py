"""Linked agent preferences stay consistent across loads and save failures."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swarm_app.linked_agents import (HARNESS_NAMES, LinkedAgentsSettings, StartupAgent,
                                    command_harness, default_startup_agents, parse_startup_command)


class LinkedAgentsSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "settings" / "linked-agents.json"

    def write(self, content):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(content, encoding="utf-8")

    def test_first_use_enables_three_without_creating_config(self):
        settings = LinkedAgentsSettings(self.path)
        self.assertEqual(settings.enabled, {"codex": True, "hermes": True, "deepseek": True})
        self.assertIsNone(settings.load_error)
        self.assertFalse(self.path.exists())
        self.assertFalse(settings.dont_show_again)
        self.assertEqual(settings.agents[0].command, "codex --yolo")

    def test_preferences_survive_restart_including_all_off(self):
        settings = LinkedAgentsSettings(self.path)
        settings.set_enabled("hermes", False)
        self.assertEqual(LinkedAgentsSettings(self.path).enabled,
                         {"codex": True, "hermes": False, "deepseek": True})
        settings.set_enabled("codex", False)
        settings.set_enabled("deepseek", False)
        self.assertEqual(LinkedAgentsSettings(self.path).enabled,
                         {"codex": False, "hermes": False, "deepseek": False})
        settings.set_enabled("hermes", True)
        self.assertEqual(LinkedAgentsSettings(self.path).enabled,
                         {"codex": False, "hermes": True, "deepseek": False})

    def test_legacy_switches_migrate_when_commands_are_saved(self):
        self.write('{"codex": false, "hermes": true}')
        settings = LinkedAgentsSettings(self.path)
        self.assertEqual([agent.id for agent in settings.agents], ["codex", "hermes", "deepseek"])
        self.assertFalse(settings.agents[0].enabled)
        self.assertTrue(settings.agents[2].enabled)
        settings.save(settings.agents, True)
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)
        restored = LinkedAgentsSettings(self.path)
        self.assertEqual(restored.agents, settings.agents)
        self.assertTrue(restored.dont_show_again)

    def test_custom_commands_and_order_survive_reload(self):
        settings = LinkedAgentsSettings(self.path)
        agents = [StartupAgent("extra", "Local helper", "'/some folder/tool' --model 'local model'"),
                  replace(settings.agents[0], command="codex --yolo -m custom-model", enabled=False)]
        settings.save(agents, True)
        restored = LinkedAgentsSettings(self.path)
        self.assertEqual(restored.agents, agents)
        self.assertTrue(restored.dont_show_again)
        self.assertEqual(parse_startup_command(agents[0].command),
                         ["/some folder/tool", "--model", "local model"])
        self.assertEqual(command_harness(parse_startup_command(agents[0].command)), "custom")

    def test_invalid_drafts_leave_all_preferences_unchanged(self):
        settings = LinkedAgentsSettings(self.path)
        settings.save(settings.agents, False)
        original = self.path.read_text()
        valid = settings.agents[0]
        for invalid in ([replace(valid, command="")], [replace(valid, command="codex '")],
                        [replace(valid, command="codex\nlogout")], [replace(valid, name="")],
                        [valid, valid], [replace(valid, enabled=1)]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings.save(invalid, True)
            self.assertEqual(settings.agents, default_startup_agents())
            self.assertFalse(settings.dont_show_again)
            self.assertEqual(self.path.read_text(), original)

    def test_malformed_new_schema_preserves_file_and_reports_error(self):
        for data in ({"version": 3, "agents": []}, {"version": 2, "agents": [None]},
                     {"version": 2, "agents": [{"id": "a", "name": "A", "command": ""}]},
                     {"version": 2, "agents": [], "dont_show_again": "yes"}):
            with self.subTest(data=data):
                self.write(json.dumps(data))
                settings = LinkedAgentsSettings(self.path)
                self.assertTrue(settings.load_error)
                self.assertEqual(settings.agents, default_startup_agents())
                self.assertFalse(settings.dont_show_again)
                self.assertEqual(json.loads(self.path.read_text()), data)

    def test_xdg_config_path_and_home_fallback(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.directory)}):
            self.assertEqual(LinkedAgentsSettings().path,
                             self.directory / "swarm" / "linked-agents.json")
        for config in ("", "relative/config"):
            with self.subTest(config=config), \
                    patch.dict(os.environ, {"XDG_CONFIG_HOME": config}), \
                    patch("swarm_app.linked_agents.Path.home", return_value=self.directory):
                self.assertEqual(LinkedAgentsSettings().path,
                                 self.directory / ".config" / "swarm" / "linked-agents.json")

    def test_missing_harness_defaults_on_and_unknown_keys_are_ignored(self):
        self.write('{"hermes": false, "future-agent": false}')
        settings = LinkedAgentsSettings(self.path)
        self.assertEqual(settings.enabled, {"codex": True, "hermes": False, "deepseek": True})
        self.assertIsNone(settings.load_error)

    def test_corrupt_settings_default_on_without_overwriting_file(self):
        for content in ("{", "[]", "null", '{"codex": false, "hermes": "off"}',
                        '{"codex": 0}', '{"hermes": null}'):
            with self.subTest(content=content):
                self.write(content)
                settings = LinkedAgentsSettings(self.path)
                self.assertEqual(settings.enabled, dict.fromkeys(HARNESS_NAMES, True))
                self.assertTrue(settings.load_error)
                self.assertEqual(self.path.read_text(), content)

    def test_unreadable_settings_default_on_with_error(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            settings = LinkedAgentsSettings(self.path)
        self.assertEqual(settings.enabled, dict.fromkeys(HARNESS_NAMES, True))
        self.assertEqual(settings.load_error, "denied")

    def test_successful_save_replaces_corrupt_file_and_clears_load_error(self):
        self.write("invalid")
        settings = LinkedAgentsSettings(self.path)
        settings.set_enabled("hermes", False)
        self.assertIsNone(settings.load_error)
        self.assertEqual(LinkedAgentsSettings(self.path).enabled,
                         {"codex": True, "hermes": False, "deepseek": True})

    def test_invalid_harness_or_boolean_never_writes(self):
        settings = LinkedAgentsSettings(self.path)
        for harness, value in (("other", True), ("codex", 1), ("hermes", "false"),
                               ("codex", None)):
            with self.subTest(harness=harness, value=value):
                with self.assertRaises(ValueError):
                    settings.set_enabled(harness, value)
        self.assertFalse(self.path.exists())
        self.assertEqual(settings.enabled, dict.fromkeys(HARNESS_NAMES, True))

    def test_failed_atomic_replace_preserves_memory_file_and_cleans_temporary(self):
        settings = LinkedAgentsSettings(self.path)
        settings.set_enabled("hermes", False)
        original = self.path.read_text()
        with patch("swarm_app.linked_agents.os.replace", side_effect=OSError("save failed")):
            with self.assertRaisesRegex(OSError, "save failed"):
                settings.set_enabled("codex", False)
        self.assertEqual(settings.enabled, {"codex": True, "hermes": False, "deepseek": True})
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_parent_creation_preserves_memory(self):
        settings = LinkedAgentsSettings(self.path)
        with patch.object(Path, "mkdir", side_effect=OSError("save failed")):
            with self.assertRaisesRegex(OSError, "save failed"):
                settings.set_enabled("codex", False)
        self.assertEqual(settings.enabled, dict.fromkeys(HARNESS_NAMES, True))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
