"""Interpret terminal activity signals, independently of process liveness.

Codex and Hermes Ink emit OSC titles exposed by VTE's window-title property.
Classic Hermes exposes its state in the live composer prompt instead.
Unknown activity stays quiet, rather than making an idle CLI look busy.
"""

import re


# Codex TUI terminal-title spinner frames (including its disabled-animation
# status-word fallback for sessions launched with our per-process override).
SPINNER_FRAMES = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
ACTIVITY_TITLE_CONFIG = 'tui.terminal_title=["spinner","status"]'


def codex_agent_command(executable: str) -> list[str]:
    return [executable, "--yolo", "-c", ACTIVITY_TITLE_CONFIG]


def hermes_title_activity(title: str | None) -> bool | None:
    """Read Hermes Ink's leading busy/idle/attention title marker."""
    if not isinstance(title, str):
        return None
    text = title.strip().replace("\ufe0f", "")
    marker, _, _suffix = text.partition(" ")
    if marker == "⏳":
        return True
    if marker in {"✓", "⚠"}:
        return False
    return None


def hermes_prompt_activity(lines: list[str], *, columns: int) -> bool | None:
    """Read classic Hermes's live composer, never past prompts in scrollback.

    Callers supply physical rows ending at the live cursor. The full-width
    input rule anchors the prompt; markers elsewhere in output or draft text
    are not activity signals. Unknown/custom layouts stay unavailable.
    """
    if columns < 2:
        return None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].rstrip() != "─" * columns:
            continue
        prompt_rows = lines[index + 1:]
        while prompt_rows and prompt_rows[0].strip().startswith("[📎 ") and prompt_rows[0].rstrip().endswith("]"):
            prompt_rows = prompt_rows[1:]
        if not prompt_rows:
            return None
        prompt = prompt_rows[0].strip().replace("\ufe0f", "")
        marker, _, _suffix = prompt.partition(" ")
        if marker == "⚕":
            return True
        if marker in {"⚠", "🔐", "🔑", "✎", "?", "🎤", "●", "◉"}:
            return False
        # The default prompt is ❯; profiles can prepend their name. Other
        # common prompt arrows come from Hermes's skin settings.
        if re.match(r"^(?:[\w.-]+\s+)?[❯>$#›»→](?:\s|$)", prompt):
            return False
        return None
    return None


def title_is_ready(title: str | None, *, managed: bool = False) -> bool:
    """Require affirmative Ready from a title whose format SWARM controls.

    A missing manual spinner cannot distinguish idle from disabled/custom title
    settings. Starting and Action Required also mean not busy, but not ready.
    """
    if not managed or not isinstance(title, str):
        return False
    text = title.strip()
    if text.startswith("● "):
        text = text[2:].lstrip()
    if text and text[0] in SPINNER_FRAMES:
        text = text[1:].lstrip(" \t·|—-:")
    return text == "Ready"


def title_activity(title: str | None, *, managed: bool = False) -> bool | None:
    """True means work in progress; False means not working; None is unavailable.

    Managed titles contain only Codex's activity marker and status. Manual
    sessions keep the user's title configuration, so arbitrary project/thread
    names must never be interpreted as status words.
    """
    if not isinstance(title, str) or not title.strip():
        return None
    text = title.strip()
    # Codex can prefix an activity title with a microphone-listening indicator.
    if text.startswith("● "):
        text = text[2:].lstrip()
    if text.startswith(("[ ! ] Action Required", "[ . ] Action Required")):
        return False
    spinning = bool(text) and text[0] in SPINNER_FRAMES
    if spinning:
        text = text[1:].lstrip(" \t·|—-:")
    if managed:
        # Ready takes precedence over any decorative/thread-naming animation.
        if text in {"Ready", "Starting"}:
            return False
        if text in {"Working", "Thinking", "Waiting"}:
            return True
        return None
    return True if spinning else None


def title_finished(title: str | None, busy_title: str | None, *, managed: bool = False) -> bool:
    """Recognize completion after a broadcast has already observed work.

    Managed titles supply an explicit Ready state. Default manual Codex titles
    retain the thread or project name when the activity spinner disappears.
    A missing or cleared title is unavailable status, never completion.
    """
    if managed:
        return title_is_ready(title, managed=True)
    if title_activity(busy_title) is not True or not isinstance(title, str):
        return False
    text = title.strip()
    if text.startswith("● "):
        text = text[2:].lstrip()
    if not text or text == "●" or text[0] in SPINNER_FRAMES:
        return False
    if text.startswith(("[ ! ] Action Required", "[ . ] Action Required")):
        return False
    return text not in {"Starting", "Working", "Thinking", "Waiting"}
