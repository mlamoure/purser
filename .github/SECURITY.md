# Security policy

## Supported versions

Only the latest release receives security fixes. Releases use calendar
versions (`YYYY.M.PATCH`, for example `2026.10.0`); the image is
`ghcr.io/mlamoure/purser:v<version>`.

## Reporting a vulnerability

Please report vulnerabilities privately: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue, pull
request or discussion for a vulnerability.

Include what you can of:

- the affected version or image tag;
- the component (REST API, MCP server, web UI, worker, Docker image, Helm chart);
- steps to reproduce, and the impact you expect;
- whether it needs an authenticated token or session, and which scope.

Use synthetic data only. Never include real bank credentials, SimpleFIN access
URLs, API tokens or encryption keys, yours or anyone else's.

You will get an acknowledgement in the advisory thread. The fix ships in a new
release, and the advisory is published once that release is available.

## Scope

In scope: this repository's code, the published container image and the Helm
chart. Out of scope: vulnerabilities in a deployment's own infrastructure
(reverse proxy, identity provider, host), and third-party dependencies that are
already public upstream (report those upstream; an issue asking for a
dependency update is welcome).

The security model is described at
https://mlamoure.github.io/purser/explanation/security/.
