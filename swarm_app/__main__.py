"""Command-line entry point; use Debian's system Python for GI bindings."""

import argparse
import os
from pathlib import Path
import sys

from . import __version__


def main():
    parser = argparse.ArgumentParser(description="SWARM — a terminal workspace for Codex agents")
    parser.add_argument("--version", action="version", version=f"SWARM {__version__}")
    parser.add_argument("--directory", "-C", type=Path, default=Path.home(),
                        help="starting folder for the first terminal (default: your home folder)")
    parser.add_argument("--codex", default=os.environ.get("SWARM_CODEX", "codex"),
                        help="Codex executable name or path")
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
    return SwarmApplication(str(directory), args.codex).run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
