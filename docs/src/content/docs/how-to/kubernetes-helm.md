---
title: Deploy on Kubernetes with Helm
description: Experimental. Install the Helm chart against an external Postgres with the encryption key in a Secret, review its locked-down defaults, and optionally split the worker role.
---

**Experimental: outside the supported release matrix.** The Helm chart is kept
and its defaults are hardened, but it has **not been qualified against a real
cluster**, and releases are not tested on it. Docker Compose, in the
[Quickstart](../../tutorials/quickstart/), is the supported deployment. Expect
rough edges here.

The chart lives in the repository at `charts/purser`. Its default image is the
published `ghcr.io/mlamoure/purser`, tagged `v` + the chart's appVersion
(for example `v2026.10.0`); to run your own build, push it to your registry
and set `image.repository`.

It runs the same one-image, multi-role design as every other deployment: an
`api` Deployment (`purser serve`) and an optional `worker`.

## Install

The chart ships no encryption key, database URL or password, and refuses to
render without them. Put them in a Secret first. The Secret must carry
`PURSER_ENCRYPTION_KEY` and `PURSER_DATABASE_URL`; every other key in it also
reaches the pods as an environment variable, so OIDC or AI-provider secrets can
live there too.

```bash
kubectl create secret generic purser-secrets \
  --from-literal=PURSER_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  --from-literal=PURSER_DATABASE_URL='postgresql+psycopg://purser:CHANGE_ME@db.example.com:5432/purser'

helm install purser charts/purser --set existingSecret=purser-secrets
```

**Record the encryption key** somewhere safe (read it back with
`kubectl get secret purser-secrets -o jsonpath='{.data.PURSER_ENCRYPTION_KEY}' | base64 -d`):
encrypted data is unrecoverable without it. `purser keygen` prints a key too.

Instead of `existingSecret` you can pass `secrets.encryptionKey` and
`secrets.databaseUrl` (plus `secrets.extra` for further secret variables), and
the chart creates the Secret. Those values then live in your Helm release
history, so prefer an existing Secret.

Then port-forward to reach the first-run wizard, and connect agents to `/mcp`
with a Bearer token exactly as elsewhere:

```bash
kubectl port-forward svc/purser 8000:8000
```

## Postgres: external or bundled

`postgres.mode` chooses the database:

- **`external`** (default): bring your own database through the Secret's
  `PURSER_DATABASE_URL`. It needs the `ltree` and `pgcrypto` extensions. For
  CloudNativePG, point the URL at the cluster's read-write service, e.g.
  `postgresql+psycopg://purser:CHANGE_ME@purser-rw:5432/purser`.
- **`bundled`**: a single-replica Postgres StatefulSet in the release, for
  evaluation only. It has no backups. Set `postgres.password` (16 or more
  characters of letters, digits and `. _ ~ -`; `openssl rand -hex 24` gives
  one) or `postgres.existingSecret`. The chart keeps the password in its own
  Secret, and derives the app's database URL from it when you do not set one.

## Secure defaults

Every pod runs as the image's unprivileged user (UID and GID 999) with
`runAsNonRoot`, a read-only root filesystem, no privilege escalation, every
Linux capability dropped and the `RuntimeDefault` seccomp profile. `/tmp` is a
size-limited `emptyDir`; Purser writes nothing else to disk. No service-account
token is mounted, because Purser never calls the Kubernetes API.

The chart refuses to render when:

- the encryption key or the database URL is missing;
- a secret-shaped variable (one whose name contains `SECRET`, `PASSWORD`,
  `TOKEN` or `API_KEY`, the encryption key or the database URL) is set under
  `env`, which renders as plain text in the pod spec: use `secrets.extra` or
  your Secret;
- more than one scheduler would run (see the worker split below);
- `image.digest` is not a `sha256:` digest.

| Value | Default | Notes |
|---|---|---|
| `image.repository` | `ghcr.io/mlamoure/purser` | The published image. |
| `image.tag` | empty: `v` + the chart's appVersion | The release tag format. |
| `image.digest` | empty | `sha256:...` pins the exact image. |
| `image.pullPolicy` | `IfNotPresent` | |
| `existingSecret` | empty | Recommended. Needs `PURSER_ENCRYPTION_KEY` and `PURSER_DATABASE_URL`. |
| `secrets.encryptionKey`, `secrets.databaseUrl` | empty | Required when `existingSecret` is not set. |
| `secrets.extra` | empty | Further secret variables for the chart's Secret. |
| `env` | `PURSER_LOG_LEVEL: INFO` | Plain settings for both roles. |
| `postgres.mode` | `external` | `bundled` for evaluation only. |
| `postgres.password` | empty | Bundled mode only; no default. |
| `api.replicas` | `1` | Above 1 only with the worker enabled. |
| `api.resources` | requests 100m CPU, 256Mi; limits 1 CPU, 1Gi | An idle API holds about 155 MiB. |
| `worker.enabled` | `false` | |
| `worker.resources` | requests 50m CPU, 192Mi; limits 1 CPU, 1Gi | An idle worker holds about 95 MiB. |
| `service.type` | `ClusterIP` | |
| `ingress.enabled` | `false` | |
| `networkPolicy.enabled` | `false` | Ingress rules for the API and the bundled database. |
| `serviceAccount.automountToken` | `false` | |
| `podSecurityContext`, `containerSecurityContext` | locked down as above | |

Request bodies are capped at 32 MiB (`PURSER_MAX_REQUEST_BYTES`), which bounds
the memory an import can take.

## Probes and migrations

Every role applies database migrations when it starts, before the API listens.
The startup probe calls `/healthz` and allows five minutes by default
(`api.startupProbe`), so long migrations are not killed. Readiness also uses
`/healthz`, which answers 200 only while the database answers, so a database
outage takes the pod out of the Service. Liveness checks only that the server
accepts connections, so a database outage does not restart the pod.

## Optional worker split

By default the API process also runs the scheduler. To split jobs onto a
dedicated worker (same image, `purser worker`), enable it:

```bash
helm upgrade purser charts/purser --reuse-values --set worker.enabled=true
```

The chart then sets `PURSER_DISABLE_SCHEDULER=true` on the API pods, so only
the worker runs jobs. Exactly one scheduler ever runs: the chart pins the worker
to one replica, refuses `api.replicas` above 1 while there is no worker, and
uses the `Recreate` strategy for whichever Deployment runs the scheduler, so an
old and a new pod never overlap during a rollout.

Both roles apply database migrations when they start. They take turns on a
Postgres advisory lock, so pods starting together are safe: the second waits
for the first, then finds the schema already current.

## Ingress and TLS

The ingress is off by default, and the Service is a ClusterIP on port 8000.
`ingress.enabled=true` (with `ingress.host`, `className`, `tls` and
`annotations`) exposes it. Set `ingress.tls` to a Secret holding the
certificate, or terminate TLS in front of the ingress:

```yaml
ingress:
  enabled: true
  className: nginx
  host: purser.example.com
  tls:
    - secretName: purser-tls
      hosts: [purser.example.com]
```

With an ingress, the chart sets `PURSER_PUBLIC_URL` to `https://` plus
`ingress.host`. That URL drives secure cookies, the OIDC redirect URI, and the
host allowlist of the MCP endpoint, which rejects requests for any other host
(HTTP 421). Set `env.PURSER_PUBLIC_URL` yourself when users reach Purser at
another URL or through something other than this ingress.

App settings go under `env`, which feeds both Deployments; a variable you set
there always replaces the value the chart would derive. Every `PURSER_*`
variable is documented in [Configuration](../../reference/configuration/).

Behind an ingress, every request reaches the pod from the ingress controller, so
set `env.FORWARDED_ALLOW_IPS` (uvicorn's trusted-proxy list) to the controller's
pod network. Otherwise
[sign-in throttling](../../reference/configuration/#sign-in-throttling) counts
every client as one address. Throttle counts live in each pod: with
`api.replicas` above 1, each replica keeps its own.

## Network policy

`networkPolicy.enabled=true` limits ingress to the API port to the peers in
`networkPolicy.apiIngressFrom` (by default, pods in the release's namespace;
add your ingress controller's namespace), refuses all ingress to the worker,
and lets only this release's Purser pods reach the bundled database. Egress is
not restricted: Purser calls your bank feed, OIDC provider, AI provider and
webhook targets.

The authoritative deployment topology is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 2.
