"""Interpret Codex's terminal-title activity, independently of process liveness.

The TUI emits these title updates over OSC; VTE exposes them through its
window-title property. No conversation text or saved conversations are read.
Unknown activity stays quiet, rather than making an idle CLI look busy.
"""


# Codex TUI terminal-title spinner frames (including its disabled-animation
# status-word fallback for sessions launched with our per-process override).
SPINNER_FRAMES = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
ACTIVITY_TITLE_CONFIG = 'tui.terminal_title=["spinner","status"]'


def codex_agent_command(executable: str) -> list[str]:
    return [executable, "--yolo", "-c", ACTIVITY_TITLE_CONFIG]


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
