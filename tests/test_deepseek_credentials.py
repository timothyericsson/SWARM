"""Local key rotation preserves Hermes settings and never exposes key material."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swarm_app.deepseek_credentials import DeepSeekKeyError, DeepSeekKeySync, _source_path
from swarm_app.deepseek_usage import _load_api_key


class DeepSeekKeySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "deepseek.txt"
        self.home = self.root / "hermes"
        self.env_file = self.home / ".env"
        self.sync = DeepSeekKeySync(self.source, self.home)
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "DEEPSEEK_API_KEY": "sk-stale-parent"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_startup_rotation_and_unchanged_polls(self):
        self.source.write_text("sk-first-key\n")
        self.assertTrue(self.sync.sync())
        self.assertEqual(_load_api_key(), "sk-first-key")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-first-key")
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        before = self.env_file.stat().st_mtime_ns
        self.assertFalse(self.sync.sync())
        self.assertEqual(self.env_file.stat().st_mtime_ns, before)
        self.source.write_text("sk-fresh-key\n")
        self.assertTrue(self.sync.sync())
        self.assertEqual(_load_api_key(), "sk-fresh-key")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-fresh-key")
        # A later Hermes edit is not overwritten without a source change.
        self.env_file.write_text("DEEPSEEK_API_KEY=sk-hermes-edit\n")
        self.assertFalse(self.sync.sync())
        self.assertEqual(_load_api_key(), "sk-hermes-edit")

    def test_env_settings_crlf_and_duplicate_assignments_are_preserved(self):
        self.home.mkdir()
        self.env_file.write_bytes(b'# settings\r\nZAI_API_KEY=other-secret\r\n'
                                  b'export DEEPSEEK_API_KEY="sk-old"\r\n'
                                  b'DEEPSEEK_API_KEY=sk-duplicate\r\nLAST=value')
        self.source.write_text('export DEEPSEEK_API_KEY="sk-new" # rotated\n')
        self.assertTrue(self.sync.sync())
        self.assertEqual(self.env_file.read_bytes(),
                         b'# settings\r\nZAI_API_KEY=other-secret\r\n'
                         b'DEEPSEEK_API_KEY=sk-new\r\nDEEPSEEK_API_KEY=sk-new\r\nLAST=value')
        self.assertEqual(_load_api_key(), "sk-new")

    def test_key_appends_after_an_unterminated_env_setting_and_last_source_key_wins(self):
        self.home.mkdir()
        self.env_file.write_text("OTHER_SETTING=value")
        self.source.write_text("\ufeff# previous key\nsk-old\n\nDEEPSEEK_API_KEY='sk-new'\n")
        self.assertTrue(self.sync.sync())
        self.assertEqual(self.env_file.read_text(), "OTHER_SETTING=value\nDEEPSEEK_API_KEY=sk-new\n")

    def test_absent_empty_and_invalid_sources_keep_existing_credentials(self):
        self.home.mkdir()
        self.env_file.write_text("DEEPSEEK_API_KEY=sk-existing\n")
        self.assertFalse(self.sync.sync())
        for text in ("", " \n# waiting for a key\n"):
            self.source.write_text(text)
            self.assertFalse(self.sync.sync())
        for text in ("private-invalid-key", "sk-old\nprivate-invalid-key", "sk-",
                     "DEEPSEEK_API_KEY=$(private-command)", "sk-secret\x00",
                     "sk-" + "a" * 4096):
            self.source.write_text(text)
            with self.subTest(text=text[:20]), self.assertRaises(DeepSeekKeyError) as error:
                self.sync.sync()
            self.assertNotIn("private-", str(error.exception))
            self.assertNotIn("sk-secret", str(error.exception))
            self.assertEqual(_load_api_key(), "sk-existing")
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-stale-parent")

    def test_atomic_source_replacement_and_recreation_are_detected(self):
        self.source.write_text("sk-first\n")
        self.sync.sync()
        replacement = self.root / "replacement"
        replacement.write_text("sk-other\n")
        original_stat = self.source.stat()
        os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        os.replace(replacement, self.source)
        self.assertTrue(self.sync.sync())
        self.assertEqual(_load_api_key(), "sk-other")
        self.source.unlink()
        self.assertFalse(self.sync.sync())
        self.source.write_text("sk-third\n")
        self.assertTrue(self.sync.sync())
        self.assertEqual(_load_api_key(), "sk-third")

    def test_failed_write_leaves_old_file_and_environment_then_retries(self):
        self.home.mkdir()
        self.env_file.write_text("OTHER_SETTING=value\nDEEPSEEK_API_KEY=sk-old\n")
        self.source.write_text("sk-new\n")
        with patch("swarm_app.deepseek_credentials.os.replace", side_effect=OSError("private-details")):
            with self.assertRaises(DeepSeekKeyError) as error:
                self.sync.sync()
        self.assertNotIn("private-details", str(error.exception))
        self.assertEqual(_load_api_key(), "sk-old")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-stale-parent")
        self.assertEqual(list(self.home.iterdir()), [self.env_file])
        self.assertTrue(self.sync.sync())
        self.assertEqual(_load_api_key(), "sk-new")

    def test_oversized_or_non_utf8_sources_do_not_change_credentials(self):
        self.home.mkdir()
        self.env_file.write_text("DEEPSEEK_API_KEY=sk-old\n")
        for raw in (b"sk-" + b"a" * (256 * 1024), b"\xff"):
            self.source.write_bytes(raw)
            with self.assertRaises(DeepSeekKeyError):
                self.sync.sync()
            self.assertEqual(_load_api_key(), "sk-old")

    def test_env_symlink_is_kept_and_target_updated_privately(self):
        self.home.mkdir()
        target = self.root / "saved.env"
        target.write_text("OTHER_SETTING=value\nDEEPSEEK_API_KEY=sk-old\n")
        self.env_file.symlink_to(target)
        self.source.write_text("sk-new\n")
        self.assertTrue(self.sync.sync())
        self.assertTrue(self.env_file.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_text(), "OTHER_SETTING=value\nDEEPSEEK_API_KEY=sk-new\n")

    def test_installed_source_reference_and_environment_override(self):
        with patch("swarm_app.deepseek_credentials.__file__",
                   str(self.root / "swarm_app/deepseek_credentials.py")), \
                patch.dict(os.environ, {"SWARM_DEEPSEEK_KEY_FILE": ""}):
            self.assertEqual(_source_path(), self.source)
            reference = self.root / ".deepseek-key-source"
            original = self.root / "original source/deepseek.txt"
            reference.write_text(str(original) + "\n")
            self.assertEqual(_source_path(), original)
            os.environ["SWARM_DEEPSEEK_KEY_FILE"] = str(self.source)
            self.assertEqual(_source_path(), self.source)


if __name__ == "__main__":
    unittest.main()
