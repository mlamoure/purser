---
title: Docker on macOS
description: Run Purser in Linux containers on macOS; the published arm64 image is verified on Apple Silicon.
---

Purser uses Docker Compose on macOS. There is no supported native macOS
installer, Homebrew database installation, or Purser LaunchAgent.

## Apple Silicon status

Docker Desktop and Colima on Apple Silicon run Linux arm64 containers. Purser
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

Compose restarts containers while the Docker engine is running. Configure your
Docker runtime to start when you log in if you want Purser available after a
Mac restart. Purser does not install a macOS login agent.

Use [Backup and restore](../../how-to/backup-restore/) before upgrades. Never
remove the database volume as a troubleshooting step without a verified backup.

Purser is not published on PyPI. Do not install an unrelated package by the bare
name `purser`.
