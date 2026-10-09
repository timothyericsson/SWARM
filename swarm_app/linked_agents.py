"""Editable, ordered startup commands, including migration from harness switches."""

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import shlex
import tempfile
import unicodedata


GLM_MODEL = "glm-5.3-flash"
GLM_AGENT_NAME = "GLM-5.3 Flash Thinking · Hermes"
LEGACY_GLM_AGENT_NAME = "GLM-5.3 Flash · Hermes"
LEGACY_GLM_COMMAND = "hermes chat --provider zai --model glm-5.3-flash --yolo"
INVALID_GLM_COMMAND = "hermes chat --provider zai --model glm-5.3-flash-thinking --yolo"

HARNESS_NAMES = {"codex": "Codex", "hermes": GLM_AGENT_NAME,
                 "deepseek": "DeepSeek V4.1 Flash · Hermes"}


@dataclass(frozen=True)
class StartupAgent:
    id: str
    name: str
    command: str
    enabled: bool = True


def uses_default_usage_account(agent, harness):
    """Whether implicit usage can safely reuse the user's default CLI account.

    Explicitly assigned SWARM credentials are handled separately by callers.
    A CLI profile, remote server or local/custom model provider may authenticate
    elsewhere; never attach the default account's percentage to that launch.
    """
    if harness not in {"codex", "hermes"}:
        return False
    try:
        argv = shlex.split(agent.command)
    except (AttributeError, TypeError, ValueError):
        return False
    if not argv:
        return False
    arguments = argv[1:argv.index("--")] if "--" in argv else argv[1:]
    provider = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if (argument in {"--profile", "-p"} or argument.startswith("--profile=")
                or argument.startswith("-p") and not argument.startswith("--")):
            return False
        if harness != "codex":
            continue
        if (argument in {"--oss", "--local-provider", "--remote"}
                or argument.startswith(("--oss=", "--local-provider=", "--remote="))):
            return False
        setting = None
        if argument in {"-c", "--config"}:
            if index == len(arguments):
                return False
            setting = arguments[index]
            index += 1
        elif argument.startswith("--config="):
            setting = argument[len("--config="):]
        elif argument.startswith("-c") and not argument.startswith("--"):
            setting = argument[2:]
        if setting is not None:
            name, separator, value = setting.partition("=")
            name = name.strip()
            if name == "model_provider":
                if not separator:
                    return False
                # Codex accepts a raw string or a quoted TOML string. Only the
                # known built-in provider may reuse its normal account read.
                provider = "openai" if value.strip() in {"openai", '"openai"', "'openai'"} else "custom"
            elif name == "model_providers.openai" or name.startswith("model_providers.openai."):
                return False
    return provider in {None, "openai"}



def parse_startup_command(command):
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Enter a startup command for each agent.")
    if any(unicodedata.category(char) == "Cc" for char in command):
        raise ValueError("Startup commands must be a single line without control characters.")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise ValueError(f"Invalid startup command: {error}") from error
    if not argv or not argv[0]:
        raise ValueError("Enter an executable at the start of each command.")
    return argv


def command_harness(argv, *, codex="codex", hermes="hermes"):
    executable = argv[0]
    if executable == codex or Path(executable).name == "codex":
        return "codex"
    if executable == hermes or Path(executable).name == "hermes":
        return "hermes"
    return "custom"


def agent_command(executable, profile):
    """Choose the model per launch, without changing Hermes' saved default."""
    if profile == "codex":
        from .activity import codex_agent_command
        return codex_agent_command(executable)
    if profile == "hermes":
        return [executable, "chat", "--provider", "zai", "--model", GLM_MODEL,
                "--reasoning", "max", "--yolo"]
    if profile == "deepseek":
        return [executable, "chat", "--provider", "deepseek", "--model", "deepseek-flash",
                "--reasoning", "max", "--yolo"]
    raise ValueError(f"Unknown agent profile: {profile}")


def default_startup_agents():
    return [StartupAgent(profile, name, "codex --yolo" if profile == "codex" else
                         shlex.join(agent_command("hermes", profile)))
            for profile, name in HARNESS_NAMES.items()]


def _validate_agents(agents):
    if not isinstance(agents, (list, tuple)):
        raise ValueError("Expected a list of startup agents.")
    identities = set()
    for agent in agents:
        if not isinstance(agent, StartupAgent):
            raise ValueError("Invalid startup agent.")
        if not isinstance(agent.id, str) or not agent.id or agent.id in identities:
            raise ValueError("Each startup agent must have a unique ID.")
        identities.add(agent.id)
        if (not isinstance(agent.name, str) or not agent.name.strip()
                or any(unicodedata.category(char) == "Cc" for char in agent.name)):
            raise ValueError("Enter a name for each agent, without control characters.")
        if type(agent.enabled) is not bool:
            raise ValueError("An agent's enabled setting must be true or false.")
        parse_startup_command(agent.command)


class LinkedAgentsSettings:
    def __init__(self, path=None):
        if path is None:
            config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
            if not config.is_absolute():
                config = Path.home() / ".config"
            path = config / "swarm" / "linked-agents.json"
        self.path = Path(path)
        self.agents = default_startup_agents()
        self.dont_show_again = False
        self.load_error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            if "version" in data or "agents" in data:
                if data.get("version") != 2 or not isinstance(data.get("agents"), list):
                    raise ValueError("Unsupported startup swarm settings format.")
                selected = []
                for row in data["agents"]:
                    if not isinstance(row, dict):
                        raise ValueError("Invalid startup agent.")
                    selected.append(StartupAgent(row.get("id"), row.get("name"),
                                                 row.get("command"), row.get("enabled", True)))
                _validate_agents(selected)
                current_glm_command = shlex.join(agent_command("hermes", "hermes"))
                glm_default_commands = {LEGACY_GLM_COMMAND, INVALID_GLM_COMMAND}
                glm_default_names = {LEGACY_GLM_AGENT_NAME}
                selected = [
                    replace(agent,
                            name=GLM_AGENT_NAME if agent.name in glm_default_names else agent.name,
                            command=current_glm_command)
                    if agent.id == "hermes" and agent.command in glm_default_commands else agent
                    for agent in selected
                ]
                hidden = data.get("dont_show_again", False)
                if type(hidden) is not bool:
                    raise ValueError("Don't show again must be true or false.")
            else:
                selected = []
                for agent in self.agents:
                    enabled = data.get(agent.id, True)
                    if type(enabled) is not bool:
                        raise ValueError(f"{agent.name} must be true or false")
                    selected.append(replace(agent, enabled=enabled))
                hidden = False
            self.agents = selected
            self.dont_show_again = hidden
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError) as error:
            self.load_error = str(error)

    def set_enabled(self, harness, enabled):
        """Compatibility for the original linked-agent switches."""
        if harness not in {agent.id for agent in self.agents}:
            raise ValueError(f"Unknown agent harness: {harness}")
        if type(enabled) is not bool:
            raise ValueError("An agent's enabled setting must be true or false")
        self.save([replace(agent, enabled=enabled) if agent.id == harness else agent
                   for agent in self.agents], self.dont_show_again)

    @property
    def enabled(self):
        return {**dict.fromkeys(HARNESS_NAMES, False),
                **{agent.id: agent.enabled for agent in self.agents}}

    def save(self, agents, dont_show_again):
        """Save the whole form atomically before changing preferences in memory."""
        _validate_agents(agents)
        if type(dont_show_again) is not bool:
            raise ValueError("Don't show again must be true or false.")
        selected = list(agents)
        data = {"version": 2, "agents": [asdict(agent) for agent in selected],
                "dont_show_again": dont_show_again}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(data, stream, indent=2)
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
        self.agents = selected
        self.dont_show_again = dont_show_again
        self.load_error = None
