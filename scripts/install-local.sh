#!/bin/sh
set -eu

# Install for the current user. Set PREFIX to choose a different user-owned prefix.
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prefix=${PREFIX:-"$HOME/.local"}
case "$prefix" in
  /*) ;;
  *) printf '%s\n' 'PREFIX must be an absolute path.' >&2; exit 1 ;;
esac
app_dir="$prefix/lib/swarm-terminal"

if [ ! -f "$source_dir/swarm" ] || [ ! -d "$source_dir/swarm_app" ]; then
  printf '%s\n' 'Run this script from the complete SWARM source distribution.' >&2
  exit 1
fi
if [ -e "$app_dir" ] && [ ! -f "$app_dir/.swarm-installation" ]; then
  printf 'Refusing to replace an unrecognized directory: %s\n' "$app_dir" >&2
  exit 1
fi
if [ ! -f "$app_dir/.swarm-installation" ] && [ -e "$prefix/bin/swarm" ]; then
  printf 'An unrelated command already exists: %s/bin/swarm\n' "$prefix" >&2
  exit 1
fi
mkdir -p "$prefix/lib" "$prefix/bin" "$prefix/share/applications" "$prefix/share/icons/hicolor/scalable/apps"
staging=$(mktemp -d "$prefix/lib/.swarm-install.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT HUP INT TERM

/usr/bin/python3 - "$source_dir" "$staging" <<'PY'
import pathlib
import shutil
import sys

source, target = map(pathlib.Path, sys.argv[1:])
shutil.copy2(source / "swarm", target / "swarm")
shutil.copytree(source / "swarm_app", target / "swarm_app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copy2(source / "LICENSE", target / "LICENSE")
(target / ".swarm-installation").write_text("SWARM local installation\n")
PY
chmod 755 "$staging/swarm"
if [ -d "$app_dir" ]; then
  rm -rf -- "$app_dir"
fi
mv -- "$staging" "$app_dir"

cat > "$prefix/bin/swarm" <<'SH'
#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../lib/swarm-terminal" && pwd)
exec "$app_dir/swarm" "$@"
SH
chmod 755 "$prefix/bin/swarm"

/usr/bin/python3 - "$source_dir/packaging/swarm-terminal.desktop" "$prefix/share/applications/swarm-terminal.desktop" "$prefix/bin/swarm" <<'PY'
import pathlib
import sys

source, destination, executable = sys.argv[1:]
escaped = executable.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
# Desktop entries unescape string values before parsing Exec arguments.
quoted_argument = ('"' + escaped + '"').replace("\\", "\\\\")
desktop = pathlib.Path(source).read_text().replace("Exec=swarm\n", f"Exec={quoted_argument}\n")
pathlib.Path(destination).write_text(desktop)
PY
cp -- "$source_dir/assets/swarm-terminal.svg" "$prefix/share/icons/hicolor/scalable/apps/swarm-terminal.svg"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$prefix/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$prefix/share/icons/hicolor" >/dev/null 2>&1 || true
fi
printf 'Installed SWARM. Launch it from your application menu or run:\n  %s/bin/swarm\n' "$prefix"
