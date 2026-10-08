"""Copy changed local DeepSeek keys into Hermes without exposing credentials."""

import os
from pathlib import Path
import re
import tempfile


MAX_FILE_BYTES = 256 * 1024
KEY_LINE = re.compile(
    r"(?:(?:export\s+)?DEEPSEEK_API_KEY\s*=\s*)?"
    r"(['\"]?)(sk-[A-Za-z0-9_-]+)\1(?:\s+#.*)?")
ENV_KEY_LINE = re.compile(r"\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=")


class DeepSeekKeyError(Exception):
    """A safe-to-display credential sync failure, with no file contents."""


def _read_text(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return ""
    if len(raw) > MAX_FILE_BYTES:
        raise DeepSeekKeyError("DeepSeek key sync refused an oversized credential file.")
    return raw.decode("utf-8-sig")


def _source_path():
    configured = os.environ.get("SWARM_DEEPSEEK_KEY_FILE", "").strip()
    root = Path(__file__).resolve().parent.parent
    # User-local installs watch the original source file, without copying its key.
    configured = configured or _read_text(root / ".deepseek-key-source").strip()
    return Path(configured).expanduser().absolute() if configured else root / "deepseek.txt"


def _latest_key(text):
    key = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = KEY_LINE.fullmatch(line)
        if match is None or len(match[2]) > 4096:
            raise DeepSeekKeyError(
                "deepseek.txt must contain a key starting with sk-, "
                "or a DEEPSEEK_API_KEY assignment.")
        key = match[2]
    return key


def _signature(stat):
    # Reading can change atime, which must not look like an unfinished edit.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _updated_env(text, key):
    lines = text.splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        if ENV_KEY_LINE.match(line):
            # Update duplicates too: different dotenv readers choose different rows.
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"DEEPSEEK_API_KEY={key}{ending}"
            found = True
    if not found:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(f"DEEPSEEK_API_KEY={key}\n")
    return "".join(lines)


def _save_env(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix=".deepseek-env-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DeepSeekKeySync:
    def __init__(self, source_path=None, hermes_home=None):
        self.source_path = Path(source_path) if source_path is not None else None
        self.hermes_home = Path(hermes_home) if hermes_home is not None else None
        self._signature = None

    def sync(self):
        """Return whether credentials changed; missing/empty sources keep the key."""
        try:
            source = self.source_path if self.source_path is not None else _source_path()
            try:
                stat = source.stat()
            except FileNotFoundError:
                self._signature = None
                return False
            signature = _signature(stat)
            if signature == self._signature:
                return False
            key = _latest_key(_read_text(source))
            if _signature(source.stat()) != signature:
                # An editor is still writing/replacing it. Try again next poll.
                return False
            if key is None:
                self._signature = signature
                return False
            home = self.hermes_home or Path(
                os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
            # Follow an existing .env symlink rather than replacing the link.
            destination = (home / ".env").resolve()
            original = _read_text(destination)
            updated = _updated_env(original, key)
            changed = updated != original or os.environ.get("DEEPSEEK_API_KEY") != key
            if updated != original:
                _save_env(destination, updated)
            # Newly launched agent processes must not inherit a stale shell key.
            os.environ["DEEPSEEK_API_KEY"] = key
            self._signature = signature
            return changed
        except (OSError, UnicodeError) as exc:
            raise DeepSeekKeyError(
                "Could not sync deepseek.txt to Hermes's .env. Check file access and encoding.") from exc
