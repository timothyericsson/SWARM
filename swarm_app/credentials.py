"""Private API credentials and launch-scoped provider configuration."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from urllib.parse import urlsplit

from .linked_agents import uses_default_usage_account


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    env_vars: tuple[str, ...]


PROVIDERS = {provider.id: provider for provider in (
    Provider("openai", "OpenAI", ("OPENAI_API_KEY",)),
    Provider("anthropic", "Anthropic", ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN")),
    Provider("deepseek", "DeepSeek", ("DEEPSEEK_API_KEY",)),
    Provider("zai", "Z.ai / GLM", ("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY")),
    Provider("openrouter", "OpenRouter", ("OPENROUTER_API_KEY",)),
    Provider("gemini", "Google / Gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    Provider("groq", "Groq", ("GROQ_API_KEY",)),
    Provider("xai", "xAI", ("XAI_API_KEY",)),
    Provider("mistral", "Mistral", ("MISTRAL_API_KEY",)),
    Provider("custom", "Other / custom", ()),
)}
_VARIABLE_PROVIDERS = {variable: provider.id for provider in PROVIDERS.values()
                       for variable in provider.env_vars}
_PREFIXES = (("sk-or-v1-", "openrouter"), ("sk-ant-api", "anthropic"),
             ("sk-proj-", "openai"), ("sk-svcacct-", "openai"),
             ("gsk_", "groq"), ("xai-", "xai"), ("AIza", "gemini"))
_ALIASES = {"openai-api": "openai", "openai-codex": "openai", "google": "gemini",
            "google-ai": "gemini", "z.ai": "zai", "z-ai": "zai", "glm": "zai"}
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ASSIGNMENT = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\Z")
_MAX_STORE_BYTES = 1024 * 1024
# Empty Z.ai URL asks Hermes to detect the appropriate official plan endpoint.
_HERMES_ENDPOINTS = {
    "openai": ("OPENAI_BASE_URL", "https://api.openai.com/v1", {"api.openai.com"}),
    "anthropic": ("ANTHROPIC_BASE_URL", "https://api.anthropic.com", {"api.anthropic.com"}),
    "deepseek": ("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1", {"api.deepseek.com"}),
    "zai": ("GLM_BASE_URL", "", {"api.z.ai", "open.bigmodel.cn"}),
    "openrouter": ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1", {"openrouter.ai"}),
    "gemini": ("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta",
               {"generativelanguage.googleapis.com"}),
    "groq": ("GROQ_BASE_URL", "https://api.groq.com/openai/v1", {"api.groq.com"}),
    "xai": ("XAI_BASE_URL", "https://api.x.ai/v1", {"api.x.ai"}),
    "mistral": ("MISTRAL_BASE_URL", "https://api.mistral.ai/v1", {"api.mistral.ai"}),
}


class CredentialError(ValueError):
    """A safe-to-display error which never includes a key or file contents."""


@dataclass(frozen=True)
class CredentialInput:
    secret: str = field(repr=False)
    provider: str | None = None
    variable: str | None = None


@dataclass(frozen=True)
class Credential:
    provider: str
    secret: str = field(repr=False)
    variable: str
    agent_id: str | None = None


def _secret(value):
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CredentialError("Enter one API key without spaces or line breaks.")
    return value


def parse_key(text):
    """Identify explicit assignments and distinctive prefixes without networking."""
    if not isinstance(text, str):
        raise CredentialError("Paste an API key or an environment variable assignment.")
    text = text.strip()
    match = _ASSIGNMENT.fullmatch(text)
    variable = match[1] if match else None
    value = match[2] if match else text
    if match:
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise CredentialError("Check the quotes around the API key assignment.") from None
        if len(parts) != 1:
            raise CredentialError("Paste one API key assignment at a time.")
        value = parts[0]
    secret = _secret(value)
    explicit = _VARIABLE_PROVIDERS.get(variable)
    prefix = next((provider for marker, provider in _PREFIXES if secret.startswith(marker)), None)
    if explicit and prefix and explicit != prefix:
        raise CredentialError("The key prefix and environment variable identify different providers.")
    return CredentialInput(secret, explicit or prefix, variable)


def _command(agent):
    try:
        return shlex.split(agent.command)
    except (AttributeError, TypeError, ValueError):
        return []


def _option(argv, name):
    value = None
    for index, item in enumerate(argv):
        if item == "--":
            break
        if item.startswith(name + "="):
            value = item.split("=", 1)[1]
        elif item == name and index + 1 < len(argv):
            value = argv[index + 1]
    return value


def provider_for_agent(agent):
    argv = _command(agent)
    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable == "codex":
        return "openai" if uses_default_usage_account(agent, "codex") else None
    if executable in {"claude", "claude-code"}:
        return "anthropic"
    if executable == "gemini":
        return "gemini"
    if executable != "hermes":
        return None
    provider = _option(argv, "--provider")
    if provider:
        provider = _ALIASES.get(provider.lower(), provider.lower())
        return provider if provider in PROVIDERS and provider != "custom" else None
    model = (_option(argv, "--model") or _option(argv, "-m") or "").lower()
    for prefix, provider in (("glm", "zai"), ("deepseek", "deepseek"), ("claude", "anthropic"),
                             ("gemini", "gemini"), ("grok", "xai"), ("mistral", "mistral"),
                             ("gpt-", "openai")):
        if model.startswith(prefix):
            return provider
    return None


def _validated(provider, secret, variable=None, agent_id=None):
    if provider not in PROVIDERS:
        raise CredentialError("Choose a supported provider or Other / custom.")
    secret = _secret(secret)
    if variable is None:
        variable = next(iter(PROVIDERS[provider].env_vars), None)
    if (not isinstance(variable, str) or not _VARIABLE.fullmatch(variable)
            or len(variable) > 128):
        raise CredentialError("Enter a valid environment variable name for this key.")
    if provider != "custom" and variable not in PROVIDERS[provider].env_vars:
        raise CredentialError("Use this provider's key variable, or select Other / custom.")
    if variable in {"HOME", "PATH", "SHELL", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH",
                    "PYTHONHOME", "HERMES_HOME", "HERMES_MANAGED_DIR", "CODEX_HOME"}:
        raise CredentialError("Choose a credential variable instead of a process configuration variable.")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id or len(agent_id) > 256):
        raise CredentialError("Choose a valid startup agent.")
    detected = parse_key(secret).provider
    if detected is not None and provider not in {detected, "custom"}:
        raise CredentialError("The key prefix identifies a different provider.")
    return Credential(provider, secret, variable, agent_id)


class CredentialStore:
    def __init__(self, path=None):
        if path is None:
            root = Path(os.environ.get("XDG_CONFIG_HOME", ""))
            if not root.is_absolute():
                root = Path.home() / ".config"
            path = root / "swarm" / "credentials.json"
        self.path = Path(path)
        self.load_error = None
        self._providers = {}
        self._agents = {}
        self._launch_directories = []
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_STORE_BYTES + 1)
            if len(raw) > _MAX_STORE_BYTES:
                raise ValueError
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("providers"), dict)
                    or not isinstance(data.get("agents"), dict)):
                raise ValueError
            providers = {provider: _validated(provider, record["secret"], record["variable"])
                         for provider, record in data["providers"].items()}
            agents = {identity: _validated(record["provider"], record["secret"], record["variable"], identity)
                      for identity, record in data["agents"].items()}
            self._providers, self._agents = providers, agents
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            self.load_error = "Could not load saved API keys. Check the credentials file and its permissions."

    @property
    def records(self):
        return tuple(self._providers.values()) + tuple(self._agents.values())

    def get(self, provider, agent_id=None):
        if agent_id is not None:
            record = self._agents.get(agent_id)
            if record is not None and record.provider == provider:
                return record
        return self._providers.get(provider)

    def credential_for_agent(self, agent):
        provider = provider_for_agent(agent)
        bound = self._agents.get(agent.id)
        if bound is not None:
            if provider and bound.provider not in {provider, "custom"}:
                raise CredentialError("This agent's provider changed. Update its saved API key before launching.")
            return bound
        return self._providers.get(provider)

    def set(self, provider, secret, *, agent_id=None, variable=None):
        record = _validated(provider, secret, variable, agent_id)
        providers, agents = dict(self._providers), dict(self._agents)
        if agent_id is None:
            providers[provider] = record
        else:
            agents[agent_id] = record
        self._save(providers, agents)
        return record

    def delete(self, provider, *, agent_id=None):
        providers, agents = dict(self._providers), dict(self._agents)
        if agent_id is None:
            providers.pop(provider, None)
        elif agent_id in agents and agents[agent_id].provider == provider:
            agents.pop(agent_id)
        self._save(providers, agents)

    def _save(self, providers, agents):
        def fields(record):
            return {"provider": record.provider, "secret": record.secret, "variable": record.variable}
        data = {"version": 1, "providers": {key: fields(value) for key, value in providers.items()},
                "agents": {key: fields(value) for key, value in agents.items()}}
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".credentials-", delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, UnicodeError):
            raise CredentialError("Could not save API keys. Check the configuration folder's permissions.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        self._providers, self._agents = providers, agents
        self.load_error = None

    def command_for_agent(self, agent, argv):
        """Select API auth explicitly; keys themselves never enter argv."""
        command = list(argv)
        record = self.credential_for_agent(agent)
        if record is None or not command:
            return command
        original = _command(agent)
        executable = Path(original[0]).name if original else Path(command[0]).name
        if executable == "codex":
            if record.provider != "openai":
                raise CredentialError("Codex API-key launches require an OpenAI key.")
            if _option(command, "--remote"):
                raise CredentialError("API keys cannot be applied to a remote Codex server from this launch.")
            # An explicit row binding can deliberately switch a local route
            # back to OpenAI. Remove flags that otherwise outrank -c settings.
            boundary = command.index("--") if "--" in command else len(command)
            head, suffix = command[:boundary], command[boundary:]
            command = [head[0]]
            index = 1
            while index < len(head):
                item = head[index]
                if item == "--local-provider":
                    index += 2
                    continue
                if item != "--oss" and not item.startswith("--local-provider="):
                    command.append(item)
                index += 1
            command += suffix
            options = ["--no-daemon", "-c", 'model_provider="swarm_openai"', "-c",
                       'model_providers.swarm_openai={name="OpenAI",base_url="https://api.openai.com/v1",'
                       'env_key="OPENAI_API_KEY",wire_api="responses",requires_openai_auth=false}']
            index = command.index("--") if "--" in command else len(command)
            command[index:index] = options
        elif executable == "hermes" and record.provider != "custom":
            provider = "openai-api" if record.provider == "openai" else record.provider
            boundary = command.index("--") if "--" in command else len(command)
            suffix = command[boundary:]
            command = command[:boundary]
            filtered = [command[0]]
            index = 1
            while index < len(command):
                item = command[index]
                if item == "--provider":
                    index += 2
                    continue
                if not item.startswith("--provider="):
                    filtered.append(item)
                index += 1
            command = filtered + ["--provider", provider] + suffix
        return command

    def environment_for_agent(self, agent):
        record = self.credential_for_agent(agent)
        if record is None:
            return {}
        variables = PROVIDERS[record.provider].env_vars
        if record.variable not in variables:
            variables = (record.variable,)
        environment = dict.fromkeys(variables, record.secret)
        if record.provider == "anthropic":
            # Hermes treats a nonempty token as permission to rediscover and
            # rotate onto cached Claude OAuth credentials.
            environment = {"ANTHROPIC_API_KEY": record.secret, "ANTHROPIC_TOKEN": "",
                           "CLAUDE_CODE_OAUTH_TOKEN": ""}
        argv = _command(agent)
        if argv and Path(argv[0]).name == "hermes":
            if not uses_default_usage_account(agent, "hermes"):
                raise CredentialError("Choose a Hermes command without --profile when assigning a SWARM key.")
            if "${" in record.secret:
                raise CredentialError("This key contains syntax Hermes would expand. Use Hermes's own key setup.")
            if record.provider in _HERMES_ENDPOINTS:
                variable, endpoint, _hosts = _HERMES_ENDPOINTS[record.provider]
                environment[variable] = endpoint
                if record.provider == "openrouter":
                    environment["CUSTOM_BASE_URL"] = ""
            environment["HERMES_HOME"] = self._hermes_environment(environment, record.provider)
        return environment

    def _check_hermes_routing(self, original, environment, provider):
        """Keep managed routing policy intact and isolate local endpoint overrides."""
        if provider == "custom":
            return None
        managed = Path(os.environ.get("HERMES_MANAGED_DIR", "").strip() or "/etc/hermes")
        try:
            import yaml
        except ImportError:
            raise CredentialError("Hermes key launches need PyYAML. Install the python3-yaml package.") from None
        try:
            local_config = None
            for directory in (original, managed):
                path = directory / "config.yaml"
                if not path.is_file():
                    continue
                raw = path.read_bytes()
                if len(raw) > _MAX_STORE_BYTES:
                    raise ValueError
                config = yaml.safe_load(raw) or {}
                if not isinstance(config, dict):
                    raise ValueError
                model = config.get("model", {})
                if not isinstance(model, dict):
                    model = {}
                # Hermes also accepts root-level model routing keys.
                endpoint = model.get("base_url", config.get("base_url", ""))
                if endpoint:
                    parsed = urlsplit(str(endpoint))
                    hosts = _HERMES_ENDPOINTS[provider][2]
                    if (parsed.scheme != "https" or parsed.hostname not in hosts
                            or parsed.username or parsed.password or parsed.port not in {None, 443}):
                        if directory == managed:
                            raise CredentialError("Hermes has a different provider endpoint configured in machine-managed settings. Update those settings before assigning a SWARM key.")
                        # A user's shared Hermes endpoint must not route a
                        # provider-specific startup row to another backend.
                        # The launch profile is private, so clear both forms
                        # there and let the selected provider's environment
                        # choose its own endpoint. Keep the saved config intact.
                        config.pop("base_url", None)
                        model.pop("base_url", None)
                        local_config = config
                if model.get("api_key") or model.get("api") or config.get("api_key") or config.get("api"):
                    raise CredentialError("Hermes has a key in config.yaml. Remove that override before assigning a SWARM key.")
            managed_env = managed / ".env"
            if managed_env.is_file():
                raw = managed_env.read_text(encoding="utf-8-sig")
                if len(raw) > _MAX_STORE_BYTES:
                    raise ValueError
                names = set(re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", raw, re.MULTILINE))
                if names.intersection(environment):
                    raise CredentialError("Machine-managed Hermes settings override this key or endpoint. Update those settings before assigning a SWARM key.")
        except CredentialError:
            raise
        except (OSError, ValueError, UnicodeError, yaml.YAMLError):
            raise CredentialError("Could not check Hermes provider settings before applying the saved key.") from None
        return local_config

    def _hermes_environment(self, environment, provider):
        """Isolate .env and auth pools while linking the existing profile resources.

        Hermes rereads profile .env ahead of process env and can rotate through
        its saved credential pool. A private launch profile avoids both stale
        sources without rewriting the user's credentials or login state.
        """
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = tempfile.TemporaryDirectory(prefix=".hermes-launch-", dir=self.path.parent)
            directory = Path(temporary.name)
            original = Path(os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
            local_config = self._check_hermes_routing(original, environment, provider)
            prior = ""
            auth = {"version": 1, "providers": {}}
            if original.is_dir():
                for entry in original.iterdir():
                    excluded = {".env", "auth.json", "auth.lock", "auth.json.lock", "active_profile"}
                    if local_config is not None:
                        excluded.add("config.yaml")
                    if entry.name not in excluded:
                        (directory / entry.name).symlink_to(entry.absolute(), target_is_directory=entry.is_dir())
                if (original / ".env").is_file():
                    with (original / ".env").open("rb") as stream:
                        raw = stream.read(_MAX_STORE_BYTES + 1)
                    if len(raw) > _MAX_STORE_BYTES:
                        raise OSError
                    prior = raw.decode("utf-8-sig")
                if (original / "auth.json").is_file():
                    with (original / "auth.json").open("rb") as stream:
                        raw = stream.read(_MAX_STORE_BYTES + 1)
                    if len(raw) > _MAX_STORE_BYTES:
                        raise OSError
                    auth = json.loads(raw)
                    if not isinstance(auth, dict):
                        raise ValueError
            # A first Hermes launch may have no history yet. Keep new sessions,
            # database journals, and prompt history outside the temporary home.
            identity = hashlib.sha256(str(original.absolute()).encode()).hexdigest()[:16]
            state = self.path.parent / "hermes-state" / identity
            for name in ("sessions", "logs", "memories"):
                if not (directory / name).exists():
                    resource = state / name
                    resource.mkdir(parents=True, exist_ok=True, mode=0o700)
                    (directory / name).symlink_to(resource.absolute(), target_is_directory=True)
            for name in ("state.db", ".hermes_history"):
                if not (directory / name).exists():
                    state.mkdir(parents=True, exist_ok=True, mode=0o700)
                    resource = state / name
                    descriptor = os.open(resource, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                    (directory / name).symlink_to(resource.absolute())
            if local_config is not None:
                import yaml
                (directory / "config.yaml").write_text(yaml.safe_dump(local_config, sort_keys=False),
                                                       encoding="utf-8")
            selected = "openai-api" if provider == "openai" else provider
            for section in ("providers", "credential_pool", "suppressed_sources"):
                if isinstance(auth.get(section), dict):
                    auth[section].pop(selected, None)
            # The profile lives outside Hermes's named-profiles hierarchy, so
            # removed provider pools cannot fall back to global auth.json.
            descriptor = os.open(directory / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(auth, stream)
            rows = [name + "='" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
                    for name, value in environment.items()]
            descriptor = os.open(directory / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(prior + "\n" + "\n".join(rows) + "\n")
            self._launch_directories.append(temporary)
            return str(directory)
        except (OSError, UnicodeError, ValueError) as error:
            if temporary is not None:
                temporary.cleanup()
            if isinstance(error, CredentialError):
                raise
            raise CredentialError("Could not prepare the selected API key for Hermes.") from None

    def close(self):
        for directory in self._launch_directories:
            directory.cleanup()
        self._launch_directories.clear()
