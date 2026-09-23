#!/bin/sh
set -eu

prefix=${PREFIX:-"$HOME/.local"}
case "$prefix" in
  /*) ;;
  *) printf '%s\n' 'PREFIX must be an absolute path.' >&2; exit 1 ;;
esac
app_dir="$prefix/lib/swarm-terminal"
if [ ! -f "$app_dir/.swarm-installation" ]; then
  printf 'No recognized SWARM local installation at %s\n' "$app_dir" >&2
  exit 1
fi
rm -rf -- "$app_dir"
rm -f -- "$prefix/bin/swarm" "$prefix/share/applications/swarm-terminal.desktop" "$prefix/share/icons/hicolor/scalable/apps/swarm-terminal.svg"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$prefix/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$prefix/share/icons/hicolor" >/dev/null 2>&1 || true
fi
printf '%s\n' 'Removed SWARM. Codex CLI settings and conversations were left in place.'

