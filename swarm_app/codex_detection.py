"""Identify interactive Codex processes in one terminal's foreground group.

Only Linux process metadata is inspected. No terminal text is interpreted,
commands are never executed, and command lines are never logged or returned.
The caller must recheck its PTY foreground group before acting on a result.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat


_PROC = Path("/proc")
_MAX_CMDLINE_BYTES = 128 * 1024
_MAX_DESCENDANTS = 512
_INACTIVE_STATES = frozenset({"Z", "X", "x", "T", "t"})
_NONINTERACTIVE_COMMANDS = frozenset({
    "a", "app", "app-server", "apply", "archive", "cloud", "cloud-tasks",
    "completion", "completions", "debug", "delete", "doctor", "e", "exec",
    "exec-server", "execpolicy", "features", "help", "login", "logout", "mcp",
    "mcp-server", "plugin", "plugins", "remote-control", "review", "sandbox",
    "unarchive", "update", "version",
})
_VALUE_FLAGS = frozenset({
    "--add-dir", "--ask-for-approval", "--cd", "--config", "--disable", "--enable",
    "--image", "--local-provider", "--model", "--profile", "--remote",
    "--remote-auth-token-env", "--sandbox", "--permission-profile",
    "-a", "-C", "-c", "-i", "-m", "-p", "-s", "-P",
})
_BOOLEAN_FLAGS = frozenset({
    "--dangerously-bypass-approvals-and-sandbox", "--yolo",
    "--dangerously-bypass-hook-trust", "--full-auto", "--no-alt-screen",
    "--oss", "--search", "--strict-config", "--ignore-user-config", "--ignore-rules",
})


@dataclass(frozen=True)
class DetectedCodex:
    pid: int
    start: int
    session: int
    group: int
    directory: str | None


@dataclass(frozen=True)
class _Process:
    pid: int
    start: int
    session: int
    group: int
    threads: int


def _read_limited(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Process metadata is too large")
    return data


def _read_process(pid: int) -> _Process | None:
    try:
        directory = _PROC / str(pid)
        if directory.stat().st_uid != os.getuid():
            return None
        fields = _read_limited(directory / "stat", 16384).rsplit(b")", 1)[1].split()
        if fields[0].decode("ascii") in _INACTIVE_STATES:
            return None
        return _Process(pid, int(fields[19]), int(fields[3]), int(fields[2]), int(fields[17]))
    except (OSError, ValueError, IndexError):
        return None


def _same_process(process: _Process) -> bool:
    current = _read_process(process.pid)
    return current is not None and (
        current.pid, current.start, current.session, current.group
    ) == (process.pid, process.start, process.session, process.group)


def _configured_path(configured: str) -> str | None:
    if not isinstance(configured, str) or not configured or "\0" in configured:
        return None
    try:
        located = shutil.which(configured)
        return str(Path(located).resolve(strict=True)) if located else None
    except (OSError, ValueError, RuntimeError):
        return None


def _resolve_script(argument: str, process: _Process) -> str | None:
    try:
        path = Path(argument)
        if not path.is_absolute():
            path = _PROC / str(process.pid) / "cwd" / path
        return str(path.resolve(strict=True))
    except (OSError, ValueError, RuntimeError):
        return None


def _script_index(executable: str, argv: tuple[str, ...]) -> int | None:
    """Find an actual interpreter script, excluding inline code and module mode."""
    name = Path(executable).name
    if name in {"node", "nodejs"}:
        allowed = {"--no-warnings", "--no-deprecation", "--trace-warnings", "--trace-deprecation"}
        values = {"--require", "-r", "--import"}
    elif name == "python" or (name.startswith("python") and name[6:].replace(".", "").isdigit()):
        allowed = {"-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO"}
        values = {"-W", "-X"}
    elif name in {"bash", "dash", "sh", "zsh", "ksh"}:
        allowed = {"--noprofile", "--norc", "-i", "-l"}
        values = set()
    else:
        return None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            return index + 1 if index + 1 < len(argv) else None
        if not argument.startswith("-"):
            return index
        if argument in allowed:
            index += 1
        elif argument in values and index + 1 < len(argv):
            index += 2
        elif any(argument.startswith(flag + "=") for flag in values if flag.startswith("--")):
            index += 1
        else:
            # In particular, -c, -e, -p and -m are not script execution.
            return None
    return None


def _interactive_arguments(arguments: tuple[str, ...]) -> bool:
    """Recognize the interactive CLI shape without mistaking option values for commands."""
    first_positional = True
    subcommand = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return True  # The remaining arguments are literal prompt/session text.
        if argument in {"--help", "-h", "--version", "-V"}:
            return False
        if argument.startswith("--"):
            flag, separator, _value = argument.partition("=")
            if flag in _VALUE_FLAGS:
                if not separator:
                    if index + 1 >= len(arguments):
                        return False
                    index += 1
            elif flag in _BOOLEAN_FLAGS and not separator:
                pass
            elif subcommand in {"resume", "fork"} and flag in {"--last", "--all"} and not separator:
                pass
            else:
                return False
        elif argument.startswith("-") and argument != "-":
            # Clap permits attached short-option values such as -mMODEL/-cKEY=VALUE.
            flag = argument[:2]
            if flag not in _VALUE_FLAGS:
                return False
            if len(argument) == 2:
                if index + 1 >= len(arguments):
                    return False
                index += 1
        elif first_positional:
            first_positional = False
            if argument in _NONINTERACTIVE_COMMANDS:
                return False
            if argument in {"resume", "fork"}:
                subcommand = argument
        index += 1
    return True


def _command_kind(process: _Process, configured: str | None) -> int:
    """Return 2 for native Codex, 1 for a recognized launcher, or 0."""
    directory = _PROC / str(process.pid)
    try:
        if not _same_process(process):
            return 0
        executable_link = directory / "exe"
        executable = os.readlink(executable_link)
        before = executable_link.stat()
        raw = _read_limited(directory / "cmdline", _MAX_CMDLINE_BYTES)
        if not raw or not raw.endswith(b"\0"):
            return 0
        argv = tuple(os.fsdecode(argument) for argument in raw[:-1].split(b"\0"))
        if not argv or not argv[0]:
            return 0
        after = executable_link.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not _same_process(process):
            return 0
        if Path(executable).name == "codex" or (configured is not None and executable == configured):
            return 2 if _interactive_arguments(argv[1:]) else 0
        script_index = _script_index(executable, argv)
        if script_index is None:
            return 0
        script = _resolve_script(argv[script_index], process)
        if script is None:
            return 0
        official_node_wrapper = (
            Path(executable).name in {"node", "nodejs"}
            and Path(script).parts[-4:] == ("@openai", "codex", "bin", "codex.js")
        )
        if not official_node_wrapper and (configured is None or script != configured):
            return 0
        return 1 if _interactive_arguments(argv[script_index + 1:]) else 0
    except (OSError, ValueError, IndexError, RuntimeError):
        return 0


def _children(process: _Process) -> set[int] | None:
    """Read per-thread child lists; Node can spawn its native child from a thread."""
    try:
        task = _PROC / str(process.pid) / "task"
        tasks = [task / str(process.pid)] if process.threads == 1 else list(task.iterdir())
        if len(tasks) > _MAX_DESCENDANTS:
            return None
        children = set()
        for thread in tasks:
            children.update(int(pid) for pid in _read_limited(thread / "children", 65536).split())
        return children if _same_process(process) else None
    except (OSError, ValueError):
        return None


def _directory(process: _Process) -> str | None:
    try:
        link = _PROC / str(process.pid) / "cwd"
        directory = os.readlink(link)
        actual = link.stat()
        named = Path(directory).stat()
        if (
            not stat.S_ISDIR(named.st_mode)
            or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)
            or not _same_process(process)
        ):
            return None
        return directory
    except (OSError, ValueError):
        return None


def find_foreground_codex(
    leader_pid: int,
    leader_start: int,
    foreground_group: int,
    configured_codex: str = "codex",
) -> DetectedCodex | None:
    """Find interactive Codex in the caller's latest PTY foreground process group.

    The original shell must still have the supplied PID/start identity and own
    a private session. A shell that execs Codex retains that identity. This
    function does not guarantee that Codex's composer is ready to accept input.
    """
    if any(type(value) is not int or value <= 0 for value in (leader_pid, leader_start, foreground_group)):
        return None
    leader = _read_process(leader_pid)
    if leader is None or leader.start != leader_start or leader.session != leader_pid:
        return None
    configured = _configured_path(configured_codex)
    foreground = _read_process(foreground_group)
    pending = deque([foreground]) if (
        foreground is not None
        and foreground.session == leader_pid
        and foreground.group == foreground_group
    ) else deque()
    seen: set[int] = set()
    launcher = None
    native = None
    incomplete = not pending

    # Usually the group leader is Codex itself, or Node with one native child.
    # Full /proc enumeration is reserved for disappearing/reparented group leaders
    # or unavailable child lists, rather than every terminal polling interval.
    while pending and len(seen) < _MAX_DESCENDANTS:
        process = pending.popleft()
        if process.pid in seen:
            continue
        seen.add(process.pid)
        if process.session != leader_pid:
            continue
        if process.group == foreground_group:
            kind = _command_kind(process, configured)
            if kind == 2:
                native = process
                break
            if kind == 1:
                launcher = process
        children = _children(process)
        if children is None:
            incomplete = True
            continue
        for pid in children:
            child = _read_process(pid)
            if child is not None and child.session == leader_pid and child.group == foreground_group:
                pending.append(child)
    incomplete = incomplete or bool(pending)
    if native is None and incomplete:
        try:
            for entry in _PROC.iterdir():
                if not entry.name.isdecimal() or int(entry.name) in seen:
                    continue
                process = _read_process(int(entry.name))
                if process is None or process.session != leader_pid or process.group != foreground_group:
                    continue
                kind = _command_kind(process, configured)
                if kind == 2:
                    native = process
                    break
                if kind == 1:
                    launcher = process
        except OSError:
            return None
    target = native or launcher
    if target is None or not _same_process(leader) or not _same_process(target):
        return None
    directory = _directory(target)
    # Exec preserves PID/start, so recheck command identity as well as /proc stat.
    if not _command_kind(target, configured) or not _same_process(leader):
        return None
    return DetectedCodex(target.pid, target.start, target.session, target.group, directory)
