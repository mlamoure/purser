---
title: Docker on macOS
description: Run Purser in Linux containers on macOS, install a Docker runtime with one script, and what to expect on Apple Silicon and on a Mac that sleeps.
---

Purser uses Docker Compose on macOS. There is no supported native macOS
installer, Homebrew database installation, or Purser LaunchAgent.

## No Docker yet: the install script

A Mac has no container runtime of its own. If `docker` is not installed, this
script installs one, so that you or your AI agent can go straight on with the
setup:

```bash
curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.11.6/scripts/install-docker-macos.sh
bash install-docker-macos.sh
```

It installs [Colima](https://github.com/abiosoft/colima), the Docker command
and Docker Compose with [Homebrew](https://brew.sh/), starts Colima and
registers it as a Homebrew service, so it starts again when you log in. Colima
is a small Linux virtual machine that runs the containers. It is open source
and has no account, licence screen or window to click through, which is why an
agent can run the whole script.

What the script will not do:

- **Install Homebrew.** That asks for your Mac's password. If Homebrew is
  missing, the script prints the command and stops; run it in Terminal
  yourself, then run the script again.
- **Install beside another runtime.** If Docker already works, it changes
  nothing. If Docker Desktop or OrbStack is installed but not running, it asks
  you to start that instead. Both run Purser equally well.
- **Edit Docker's configuration file.** It links Compose into
  `~/.docker/cli-plugins`, where the Docker command looks for plugins.

It finishes with a line starting `OK:` and is safe to run again.

## Apple Silicon status

Colima and Docker Desktop on Apple Silicon run Linux arm64 containers. Purser
is built for linux/amd64 and linux/arm64, each natively on its own
architecture, and Compose pulls the arm64 image on an Apple Silicon Mac. The
published image is verified there: on 2026-10-28 it passed the release's
end-to-end drills (onboarding, backup and restore, upgrade, crash and outage
recovery, and failure journeys) on an Apple M1 with Colima and Docker, running
natively with no emulation.

Follow [Set up Purser with your AI](../../tutorials/ai-setup/) or the
[Quickstart](../../tutorials/quickstart/).
Docker runs both Purser and PostgreSQL. You do not need host Python, Node or
Git. Keep the encryption key separately from database backups and retain the
PostgreSQL named volume when recreating containers.

## Restart and recovery

Compose restarts containers while the Docker engine is running. The install
script sets Colima to start when you log in; with Docker Desktop or OrbStack,
turn on its own start-at-login setting if you want Purser available after a
Mac restart. Purser does not install a macOS login agent.

A Mac that is asleep runs nothing. When it wakes, or when Purser starts
again, Purser notices a bank sync whose time has passed and runs it once,
however many days were missed. You do not need to keep the Mac awake at the
sync time.

Use [Backup and restore](../../how-to/backup-restore/) before upgrades. Never
remove the database volume as a troubleshooting step without a verified backup.

Purser is not published on PyPI. Do not install an unrelated package by the bare
name `purser`.
