---
title: Docker on macOS
description: Run Purser in Linux containers on macOS; Apple Silicon verification of the published image is pending.
---

Purser uses Docker Compose on macOS. There is no supported native macOS
installer, Homebrew database installation, or Purser LaunchAgent.

## Apple Silicon status

Docker Desktop on Apple Silicon runs Linux arm64 containers. Purser is built
for linux/amd64 and linux/arm64, each natively on its own architecture;
Apple Silicon verification of the published image is pending.

Follow [Set up Purser with your AI](../../tutorials/ai-setup/) or the
[Quickstart](../../tutorials/quickstart/).
Docker runs both Purser and PostgreSQL. You do not need host Python or Node.
Keep the encryption key separately from database backups and retain the
PostgreSQL named volume when recreating containers.

## Restart and recovery

Compose restarts containers while the Docker engine is running. Configure your
Docker runtime to start when you log in if you want Purser available after a
Mac restart. Purser does not install a macOS login agent.

Use [Backup and restore](../../how-to/backup-restore/) before upgrades. Never
remove the database volume as a troubleshooting step without a verified backup.

Purser is not published on PyPI. Do not install an unrelated package by the bare
name `purser`.
