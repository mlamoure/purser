#!/bin/bash
# Give a Mac a Docker runtime for Purser: Colima, the Docker CLI and Compose.
#
#   curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.11.7/scripts/install-docker-macos.sh
#   bash install-docker-macos.sh
#
# Purser runs in Linux containers, and macOS has no container runtime of its
# own. This script installs Colima (a small Linux virtual machine that runs
# the containers; open source, no account, no licence screen, nothing to
# click), so a person's AI agent can run it start to finish. It does nothing
# when Docker already works, and it never installs a second runtime beside
# Docker Desktop or OrbStack.
#
# The one thing it cannot do is install Homebrew, which asks for the Mac's
# password: when Homebrew is missing it prints the command for the person and
# stops. Safe to run again at any point. Written for the bash 3.2 that macOS
# ships.
set -eu

APPS_DIR=${PURSER_MAC_APPS_DIR:-/Applications}   # overridden only by the tests
WAIT_SECONDS=${PURSER_MAC_WAIT_SECONDS:-300}

say() { printf '%s\n' "$*"; }
stop() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

docker_works() {
  command -v docker >/dev/null 2>&1 \
    && docker info >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1
}

[ "$(uname -s)" = "Darwin" ] || stop "this script is for macOS. On Linux, install Docker Engine: https://docs.docker.com/engine/install/"

if docker_works; then
  say "OK: Docker already works on this Mac ($(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')). Nothing was installed."
  exit 0
fi

for app in Docker OrbStack; do
  if [ -d "$APPS_DIR/$app.app" ]; then
    stop "$app is installed but not running. Open $app, wait until it says it is running, then run this script again. Nothing was installed."
  fi
done

BREW=$(command -v brew || true)
if [ -z "$BREW" ]; then
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$candidate" ]; then BREW=$candidate; break; fi
  done
fi
if [ -z "$BREW" ]; then
  stop "Homebrew is not installed, and installing it needs this Mac's password. Open Terminal, run

  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"

and then run this script again."
fi
eval "$("$BREW" shellenv)"

say "Installing Colima, the Docker command and Docker Compose with Homebrew..."
brew install colima docker docker-compose

# Homebrew installs Compose where the Docker command does not look for it.
# Linking it into the per-user plugin directory avoids editing Docker's
# config file, which may hold the person's other settings.
mkdir -p "$HOME/.docker/cli-plugins"
ln -sfn "$(brew --prefix)/opt/docker-compose/bin/docker-compose" "$HOME/.docker/cli-plugins/docker-compose"

# As a Homebrew service Colima also starts when the person logs in, so Purser
# comes back after a restart.
say "Starting Colima (the first start downloads a small Linux image)..."
brew services start colima >/dev/null 2>&1 || true

waited=0
until docker_works; do
  if [ "$waited" -ge "$WAIT_SECONDS" ]; then
    # The service did not come up: start it directly so the reason is printed.
    colima start || stop "Colima did not start. The lines above say why."
    docker_works || stop "Colima started but Docker does not answer. Run 'colima status' and 'docker info' to see why."
    break
  fi
  sleep 5
  waited=$((waited + 5))
done

say "OK: Docker is ready on this Mac ($(docker version --format '{{.Server.Os}}/{{.Server.Arch}}'), $(docker compose version))."
say "Colima starts at login. Continue with the Purser setup guide."
