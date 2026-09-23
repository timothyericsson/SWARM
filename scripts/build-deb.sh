#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=${1:-0.1.14}
case "$version" in
  ''|*[!0-9A-Za-z.+:~\-]*) printf '%s\n' 'Invalid Debian package version.' >&2; exit 1 ;;
esac
if ! command -v dpkg-deb >/dev/null 2>&1; then
  printf '%s\n' 'Install dpkg-dev to build a Debian package.' >&2
  exit 1
fi
if [ ! -f "$source_dir/swarm" ] || [ ! -d "$source_dir/swarm_app" ]; then
  printf '%s\n' 'Run this script from the complete SWARM source distribution.' >&2
  exit 1
fi
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT HUP INT TERM
mkdir -p "$staging/DEBIAN" "$staging/usr/lib/swarm-terminal" "$staging/usr/bin" "$staging/usr/share/applications" "$staging/usr/share/icons/hicolor/scalable/apps" "$staging/usr/share/doc/swarm-terminal" "$source_dir/dist"

/usr/bin/python3 - "$source_dir" "$staging" "$version" <<'PY'
import pathlib
import shutil
import sys

source, staging = map(pathlib.Path, sys.argv[1:3])
version = sys.argv[3]
control = (source / "packaging/control").read_text()
control = "\n".join(f"Version: {version}" if line.startswith("Version:") else line for line in control.splitlines()) + "\n"
(staging / "DEBIAN/control").write_text(control)
target = staging / "usr/lib/swarm-terminal"
shutil.copy2(source / "swarm", target / "swarm")
shutil.copytree(source / "swarm_app", target / "swarm_app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copy2(source / "README.md", staging / "usr/share/doc/swarm-terminal/README.md")
shutil.copy2(source / "LICENSE", staging / "usr/share/doc/swarm-terminal/copyright")
PY
cat > "$staging/usr/bin/swarm" <<'SH'
#!/bin/sh
exec /usr/lib/swarm-terminal/swarm "$@"
SH
cp -- "$source_dir/packaging/swarm-terminal.desktop" "$staging/usr/share/applications/"
cp -- "$source_dir/assets/swarm-terminal.svg" "$staging/usr/share/icons/hicolor/scalable/apps/"
find "$staging" -type d -exec chmod 755 {} +
find "$staging" -type f -exec chmod 644 {} +
chmod 755 "$staging/usr/bin/swarm" "$staging/usr/lib/swarm-terminal/swarm"
dpkg-deb --root-owner-group --build "$staging" "$source_dir/dist/swarm-terminal_${version}_all.deb"
printf 'Package ready: %s/dist/swarm-terminal_%s_all.deb\n' "$source_dir" "$version"
