"""Command-line entry point; use Debian's system Python for GI bindings."""

import argparse
import os
from pathlib import Path
import sys

from . import __version__
from .harnesses import HARNESS_ORDER


def main():
    parser = argparse.ArgumentParser(description="SWARM — a terminal workspace for AI agent harnesses")
    parser.add_argument("--version", action="version", version=f"SWARM {__version__}")
    parser.add_argument("--directory", "-C", type=Path, default=Path.home(),
                        help="starting folder for the first terminal (default: your home folder)")
    # One executable flag per registered harness: --codex, --hermes, …
    for spec in HARNESS_ORDER:
        parser.add_argument(f"--{spec.key}",
                            default=os.environ.get(spec.executable_environment(),
                                                   spec.default_executable),
                            help=f"{spec.name} executable name or path")
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        parser.error(f"Working folder does not exist: {directory}")
    try:
        from .app import SwarmApplication, Gtk
    except (ImportError, ValueError) as error:
        print(f"SWARM needs GTK and VTE: {error}\n"
              "Install: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91",
              file=sys.stderr)
        return 1
    if not Gtk.init_check([])[0]:
        print("SWARM needs a graphical desktop. Run it from your Debian desktop terminal.",
              file=sys.stderr)
        return 1
    return SwarmApplication(str(directory), *(getattr(args, spec.key) for spec in HARNESS_ORDER)).run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
