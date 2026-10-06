"""Persist which agent harnesses are launched by Open Swarm."""

import json
import os
from pathlib import Path
import tempfile


HARNESS_NAMES = {"codex": "Codex", "hermes": "GLM-5.3 Flash · Hermes",
                 "deepseek": "DeepSeek V4.1 Flash · Hermes"}


def agent_command(executable, profile):
    """Choose the model per launch, without changing Hermes' saved default."""
    if profile == "codex":
        from .activity import codex_agent_command
        return codex_agent_command(executable)
    if profile == "hermes":
        return [executable, "chat", "--provider", "zai", "--model", "glm-5.3-flash", "--yolo"]
    if profile == "deepseek":
        return [executable, "chat", "--provider", "deepseek", "--model", "deepseek-flash",
                "--reasoning", "max", "--yolo"]
    raise ValueError(f"Unknown agent profile: {profile}")


class LinkedAgentsSettings:
    def __init__(self, path=None):
        if path is None:
            config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
            if not config.is_absolute():
                config = Path.home() / ".config"
            path = config / "swarm" / "linked-agents.json"
        self.path = Path(path)
        self.enabled = dict.fromkeys(HARNESS_NAMES, True)
        self.load_error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            selected = dict(self.enabled)
            for harness in HARNESS_NAMES:
                if harness in data:
                    if type(data[harness]) is not bool:
                        raise ValueError(f"{HARNESS_NAMES[harness]} must be true or false")
                    selected[harness] = data[harness]
            self.enabled = selected
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError) as error:
            self.load_error = str(error)

    def set_enabled(self, harness, enabled):
        """Save atomically before changing the preferences in memory."""
        if harness not in HARNESS_NAMES:
            raise ValueError(f"Unknown agent harness: {harness}")
        if type(enabled) is not bool:
            raise ValueError("An agent's enabled setting must be true or false")
        selected = {**self.enabled, harness: enabled}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(selected, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    # Preserve the original save failure if cleanup also fails.
                    pass
        self.enabled = selected
        self.load_error = None
