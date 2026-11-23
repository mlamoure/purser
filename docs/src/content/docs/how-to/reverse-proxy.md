---
title: Put Purser behind a reverse proxy
description: Serve Purser over HTTPS with Caddy, nginx or Traefik, keep port 8000 private, trust only the proxy's forwarded headers, and keep MCP streaming and imports working.
---

Purser serves plain HTTP on port 8000. That is how it is used on the machine it
runs on and, if you choose, from other machines on your home network (see
[Another machine on your home network](../docker-compose/#another-machine-on-your-home-network)).
For anything beyond your home network, or if you want encryption inside it,
put your own HTTPS reverse proxy in front. Purser does not test or support
internet-facing setups; what follows is what a proxy needs. This page gives working
examples for Caddy, nginx and Traefik with the
[Docker Compose](../docker-compose/) setup, then explains each setting they
depend on.

Serve Purser at the root of its own host name, such as
`https://purser.example.com`. A path prefix (`https://example.com/purser`) is
not supported: the web UI, the API and `/mcp` all use root-absolute paths.

## What every setup needs

1. **`PURSER_PUBLIC_URL` is the HTTPS address**, exactly as people and agents
   type it: `https://purser.example.com`, with no path.
2. **Port 8000 stays private.** Keep `PURSER_BIND_ADDRESS` at its default,
   `127.0.0.1`, so only the proxy reaches Purser.
3. **The proxy forwards the original `Host`**, sets `X-Forwarded-For` and
   `X-Forwarded-Proto`, and passes the browser's `Origin` and `Sec-Fetch-Site`
   headers through unchanged.
4. **Purser trusts the forwarded headers from the proxy's address only**,
   through `FORWARDED_ALLOW_IPS`.
5. **The proxy allows request bodies up to 32 MiB**, and does not buffer the
   MCP endpoint's streamed responses.

Caddy and Traefik meet points 3 and 5 with their defaults; nginx needs the
settings shown below.

## Give Purser a fixed network address

Inside the container, Purser never sees a request as coming from
`127.0.0.1`. A proxy on the Docker host reaches it through the published port,
so requests arrive from the gateway address of the Compose network; a proxy
running in Compose connects from its own container address. Docker assigns
both when it creates the network, so pin the network's address range in
`docker-compose.override.yml` (next to `docker-compose.yml`; see
[Keep your changes in an override file](../docker-compose/#keep-your-changes-in-an-override-file)).
Pick a private range that nothing else on your network uses:

```yaml
# docker-compose.override.yml, for a proxy installed on the Docker host
services:
  purser:
    environment:
      FORWARDED_ALLOW_IPS: 172.30.80.1

networks:
  default:
    ipam:
      config:
        - subnet: 172.30.80.0/24
          gateway: 172.30.80.1
```

Docker applies a new address range only to a new network, so recreate it once:
`docker compose down` (without `-v`, which would delete your data), then
`docker compose up -d`.

## Caddy

[Caddy](https://caddyserver.com/) installed on the Docker host obtains and
renews the certificate itself. The whole `Caddyfile`:

```text
purser.example.com {
	reverse_proxy 127.0.0.1:8000
}
```

In `.env`:

```bash
PURSER_PUBLIC_URL=https://purser.example.com
PURSER_BIND_ADDRESS=127.0.0.1
```

Caddy forwards the original `Host`, sets `X-Forwarded-For` and
`X-Forwarded-Proto`, sends server-sent event responses on without buffering,
and sets no request body limit. Use the override file from
[Give Purser a fixed network address](#give-purser-a-fixed-network-address).

## nginx

nginx installed on the Docker host, with a certificate you manage (for example
with certbot). Use the same `.env` values and override file as for Caddy:

```nginx
server {
    listen 80;
    server_name purser.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name purser.example.com;

    ssl_certificate     /etc/letsencrypt/live/purser.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/purser.example.com/privkey.pem;

    # Purser accepts bodies up to 32 MiB (PURSER_MAX_REQUEST_BYTES).
    # nginx's own default is 1 MiB, too small for a long QIF or CSV import.
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # MCP answers each request with a server-sent event stream: pass it on
    # as it arrives, and allow slow tool calls to finish.
    location /mcp {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

`http2 on;` needs nginx 1.25.1 or later; on an older nginx, write
`listen 443 ssl http2;` instead. Check the file with `nginx -t` before
reloading.

## Traefik with Compose labels

[Traefik](https://traefik.io/) can run as another service in the same Compose
project and find Purser through container labels. This override file adds
Traefik with a Let's Encrypt certificate, routes `purser.example.com` to
Purser, and gives Traefik a fixed address for `FORWARDED_ALLOW_IPS`:

```yaml
# docker-compose.override.yml, with Traefik in the same project
services:
  traefik:
    image: traefik:v3.6
    restart: unless-stopped
    command:
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --entrypoints.web.address=:80
      - --entrypoints.web.http.redirections.entrypoint.to=websecure
      - --entrypoints.web.http.redirections.entrypoint.scheme=https
      - --entrypoints.websecure.address=:443
      - --certificatesresolvers.letsencrypt.acme.tlschallenge=true
      - --certificatesresolvers.letsencrypt.acme.email=admin@example.com
      - --certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - traefik-letsencrypt:/letsencrypt
    networks:
      default:
        ipv4_address: 172.30.80.10

  purser:
    environment:
      FORWARDED_ALLOW_IPS: 172.30.80.10
    labels:
      - traefik.enable=true
      - traefik.http.routers.purser.rule=Host(`purser.example.com`)
      - traefik.http.routers.purser.entrypoints=websecure
      - traefik.http.routers.purser.tls.certresolver=letsencrypt
      - traefik.http.services.purser.loadbalancer.server.port=8000

networks:
  default:
    ipam:
      config:
        - subnet: 172.30.80.0/24

volumes:
  traefik-letsencrypt:
```

In `.env`, set `PURSER_PUBLIC_URL=https://purser.example.com` and keep
`PURSER_BIND_ADDRESS=127.0.0.1`: Traefik reaches Purser over the Compose
network, so the published port only serves the host itself. Traefik forwards
the original `Host`, sets the forwarded headers, streams responses and sets no
body limit by default. Access to the Docker socket gives Traefik control of
Docker, even read-only; a socket proxy narrows that if it concerns you. If
you also run [the worker separately](../docker-compose/#run-the-worker-separately),
give it no labels.

## Why each setting matters

### PURSER_PUBLIC_URL

Purser reads `PURSER_PUBLIC_URL` at startup; run `docker compose up -d` after
changing it. It decides:

- **Secure cookies.** An `https://` address marks the session cookie
  `Secure`, so the browser never sends it over plain HTTP.
- **The single sign-on address.** The redirect address Purser sends to your
  identity provider is `PURSER_PUBLIC_URL` followed by `/auth/oidc/callback`,
  for example `https://purser.example.com/auth/oidc/callback`. Register exactly
  that with the provider; see [Single sign-on with OIDC](../oidc/).
- **The MCP endpoint's allowed host.** `/mcp` accepts requests only for the
  public host, the names in `PURSER_ALLOWED_HOSTS` and local addresses such
  as `localhost`, and answers any other `Host` with
  `421 Misdirected Request`. A mismatch between the address an agent uses and
  `PURSER_PUBLIC_URL` shows up as 421 errors in the agent. When agents also
  use a second name (the proxy's internal name, or the Docker host's address
  from inside the home network), list it exactly in `PURSER_ALLOWED_HOSTS`
  rather than changing `PURSER_PUBLIC_URL`, which must stay the address people
  use.
- **Links.** Links that tools return for you to open in the web UI start with
  this address.

### Forwarded headers and FORWARDED_ALLOW_IPS

The image runs Purser under uvicorn, which honours `X-Forwarded-For` and
`X-Forwarded-Proto` only from the addresses listed in its own
`FORWARDED_ALLOW_IPS` variable (default `127.0.0.1,::1`). It accepts single
addresses and networks such as `172.30.80.0/24`. Purser uses the resulting
client address for [sign-in throttling](../../reference/configuration/#sign-in-throttling)
and records it on failed sign-ins in the audit log.

List the proxy's address and nothing else. Any address on the list can claim
to be any client, so never use `*`, and do not list a network that untrusted
machines share. If the proxy's address is missing from the list, every request
appears to come from the proxy: the per-address sign-in limit then counts all
failed sign-ins together, and a password-guessing attack pauses password
sign-in for everyone until the lockout ends.

To check it, make one failed sign-in from another machine and look at the
`auth.login_failed` entry in the audit log: it should show that machine's
address, not the proxy's.

### Host, Origin and Sec-Fetch-Site

The web UI signs you in with a session cookie, and Purser refuses (`403`) a
write made with that cookie when the browser reports another site sent it:
`Sec-Fetch-Site` other than `same-origin` or `none`, or, without that header,
an `Origin` that is not Purser's own address. See
[Requests from other sites](../../explanation/security/#requests-from-other-sites).
A proxy that rewrites or replaces `Origin` therefore breaks saving in the web
UI. Forward the original `Host` too: it is what the MCP allowlist checks.
Names in `PURSER_ALLOWED_HOSTS` count as Purser's own address for both checks.

### Request body size

Purser refuses request bodies larger than 32 MiB with `413`
([`PURSER_MAX_REQUEST_BYTES`](../../reference/configuration/#environment-only)).
A proxy's own limit applies first, so set it at least as high. Caddy and
Traefik set none by default; nginx defaults to 1 MiB, which is why the example
sets `client_max_body_size 32m`. A QIF or CSV import larger than the proxy's
limit fails with a `413` from the proxy before Purser sees it.

### MCP streaming

`/mcp` uses MCP's streamable HTTP transport without sessions: each request is
a `POST`, and Purser answers it with a server-sent event stream
(`text/event-stream`) that closes when the answer is complete. A proxy that
buffers responses holds the stream back until it ends, so turn buffering off
for `/mcp` (`proxy_buffering off` in nginx). Long tool calls keep the response
open; allow them more than nginx's default 60 second read timeout. An agent
may call `/mcp` without the trailing slash; Purser redirects it to `/mcp/`, so
route the whole `/mcp` prefix to Purser.

## Check the result

From another machine:

```bash
curl https://purser.example.com/healthz
```

Expected: `{"status":"ok","database":"ok"}`. Then sign in to the web UI over
HTTPS, change a setting to confirm that writes are accepted, and connect an
agent to `https://purser.example.com/mcp` (see
[Connect an agent](../connect-an-agent/)).

## Related

- [Run with Docker Compose](../docker-compose/): the compose files, `.env` and the override file.
- [Configuration](../../reference/configuration/): `PURSER_PUBLIC_URL`, `PURSER_ALLOWED_HOSTS`, `PURSER_MAX_REQUEST_BYTES` and sign-in throttling.
- [Deploy on Kubernetes with Helm](../kubernetes-helm/): the same settings with an ingress.
- [Security model](../../explanation/security/): sessions, cross-site writes and throttling.
