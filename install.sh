#!/bin/sh
set -eu

usage() {
  printf '%s\n' \
    'Usage: ./install.sh' \
    'Install SWARM desktop dependencies on Debian 12 or newer.' \
    'Uses sudo when not running as root. Codex CLI is installed separately.'
}

if [ "$#" -gt 0 ]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
fi

if ! command -v apt-get >/dev/null 2>&1; then
  printf '%s\n' 'This installer requires apt-get on Debian 12 or newer.' >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
  printf '%s\n' 'Install sudo or run this script as root to install system packages.' >&2
  exit 1
fi

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

printf '%s\n' 'Installing SWARM desktop dependencies...'
as_root apt-get update
as_root apt-get install -y python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91

printf '\n%s\n' \
  'Desktop dependencies installed.' \
  'Make sure Codex CLI is installed separately and codex is on your PATH.' \
  'From the SWARM source folder, launch with ./swarm.' \
  'For an application-menu entry, run ./scripts/install-local.sh as your normal user.'
