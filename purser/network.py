"""The names Purser answers to (#633, decision 132) and how it is reached (#632).

``PURSER_PUBLIC_URL`` is the one address Purser gives out: connection steps,
links and the OIDC callback use it. ``PURSER_ALLOWED_HOSTS`` lists the other
names the same machine is reached by on a home network (its IP address, its
short host name, ``<name>.local``): the MCP endpoint's Host/Origin guard and
the admin's cross-site check accept them as well, but nothing links to them.

Every entry is an exact host name or IP address, optionally with ``:port``.
Wildcards are refused: the Host guard exists to stop DNS rebinding, in which
a page on an attacker's name is pointed at this machine, and a pattern would
let such a name through. This module only parses and derives; the guards
themselves are FastMCP's (``purser/mcp/mount.py``) and ``admin.auth``.
"""

from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Literal
from urllib.parse import urlsplit

SETTING = "PURSER_ALLOWED_HOSTS"
EXAMPLE = "192.168.1.20,homeserver,homeserver.local"

# One DNS label. Underscores are tolerated: some home routers hand them out,
# and browsers send them.
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_BRACKETED = re.compile(r"\[([0-9a-f:.]+)\](?::([0-9]+))?")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _refuse(entry: str, why: str) -> ValueError:
    return ValueError(
        f"{SETTING}: {entry!r} {why}. List exact host names or IP addresses separated "
        f"by commas, for example {EXAMPLE}."
    )


def _port(entry: str, text: str) -> int:
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise _refuse(entry, "has a port that is not a number from 1 to 65535")
    return int(text)


def _name(entry: str, text: str) -> str:
    """A host name or an IPv4 address, lowercased; refuses anything else."""
    if len(text) > 253 or not all(_LABEL.fullmatch(label) for label in text.split(".")):
        raise _refuse(entry, "is not a host name or IP address")
    if all(label.isdigit() for label in text.split(".")):
        try:
            return str(IPv4Address(text))
        except ValueError:
            raise _refuse(entry, "is not a valid IPv4 address") from None
    return text


def parse_entry(raw: str) -> tuple[str, int | None]:
    """One entry as (host, port): an IPv6 address comes back in brackets, the
    way it appears in a Host header and an origin."""
    entry = raw.strip().lower()
    if any(c in entry for c in "*?"):
        raise _refuse(entry, "is a wildcard, and only exact names keep the DNS-rebinding check")
    if "/" in entry or "@" in entry:
        raise _refuse(entry, "is not a bare host name (leave out http:// and any path)")
    if bracketed := _BRACKETED.fullmatch(entry):
        try:
            address = IPv6Address(bracketed.group(1))
        except ValueError:
            raise _refuse(entry, "is not a valid IPv6 address") from None
        port = _port(entry, bracketed.group(2)) if bracketed.group(2) else None
        return f"[{address.compressed}]", port
    if entry.count(":") > 1:
        try:
            return f"[{IPv6Address(entry).compressed}]", None
        except ValueError:
            why = "is not a valid IPv6 address (write it as [address]:port)"
            raise _refuse(entry, why) from None
    host, colon, port_text = entry.partition(":")
    return _name(entry, host), (_port(entry, port_text) if colon else None)


def parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    """The setting's entries, normalized and without repeats: ``host`` or
    ``host:port``. Blank entries (a trailing comma) are skipped; anything
    else that is not an exact name raises ``ValueError`` naming the entry."""
    out: dict[str, None] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        host, port = parse_entry(item)
        out[f"{host}:{port}" if port else host] = None
    return tuple(out)


def host_of(netloc: str) -> str:
    """The host part of a Host header or entry, lowercased, without its port
    (IPv6 keeps its brackets). Never raises: a malformed value comes back as
    written, and simply matches nothing."""
    text = netloc.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[: end + 1] if end != -1 else text
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    return text


def is_loopback(host: str) -> bool:
    """Whether a host (no port; IPv6 bracketed or bare) names this machine."""
    bare = host.strip("[]").lower()
    if bare == "localhost":
        return True
    try:
        return ip_address(bare).is_loopback
    except ValueError:
        return False


def normalize_origin(url: str) -> str:
    """``scheme://host[:port]``, lowercased, the default port left out, so
    two spellings of one origin compare equal. A value that is not an origin
    (``null``, garbage) comes back lowercased and matches nothing real."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip().lower()
    scheme = parts.scheme.lower()
    if not scheme or not parts.hostname:
        return url.strip().lower()
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def public_port(public_url: str) -> int:
    """The port PURSER_PUBLIC_URL names, or its scheme's default."""
    parts = urlsplit(public_url)
    return parts.port or _DEFAULT_PORTS.get(parts.scheme.lower(), 80)


def allowed_origins(public_url: str, entries: tuple[str, ...]) -> set[str]:
    """The browser origins of the allowed names: each on the port written
    with it, else on PURSER_PUBLIC_URL's port, by http or https (Purser
    serves one of them on that port; nothing else can serve the other)."""
    default = public_port(public_url)
    origins: set[str] = set()
    for entry in entries:
        host, port = parse_entry(entry)
        for scheme in ("http", "https"):
            origins.add(normalize_origin(f"{scheme}://{host}:{port or default}"))
    return origins


def accepted_hosts(public_url: str, entries: tuple[str, ...]) -> set[str]:
    """The host names (no ports) agents may address: PURSER_PUBLIC_URL's host
    and the allowed names. Loopback names are accepted separately."""
    hosts = {host_of(entry) for entry in entries}
    public = urlsplit(public_url).hostname
    if public:
        hosts.add(f"[{public.lower()}]" if ":" in public else public.lower())
    return hosts


def accepts(host_header: str, public_url: str, entries: tuple[str, ...]) -> bool:
    """Whether an agent addressing ``host_header`` gets past the MCP Host
    guard: the public host, an allowed name or a loopback name, on any port,
    as FastMCP's guard compares them."""
    host = host_of(host_header)
    return is_loopback(host) or host in accepted_hosts(public_url, entries)


# -- How the install is reached (#632) -------------------------------------

Reach = Literal["this_machine", "home_network", "reverse_proxy", "unknown"]


def reach_mode(public_url: str, bind_address: str | None) -> Reach:
    """How people and agents reach this Purser, from what the deployment says.

    - ``reverse_proxy``: PURSER_PUBLIC_URL is https. Purser serves plain HTTP
      only, so something in front of it terminates TLS; who can reach that
      address is the proxy's business.
    - ``unknown``: the deployment does not report the address it publishes
      Purser's port on (Compose passes PURSER_BIND_ADDRESS; a custom
      deployment may not), or reports something unreadable. Never guessed.
    - ``this_machine``: the port is published on a loopback address.
    - ``home_network``: on any other address (``0.0.0.0``, a LAN address).

    The app runs inside a container and cannot see the host's port
    publishing itself, which is why the deployment has to say."""
    if urlsplit(public_url).scheme.lower() == "https":
        return "reverse_proxy"
    bind = (bind_address or "").strip()
    if not bind:
        return "unknown"
    if bind.lower() == "localhost":
        return "this_machine"
    try:
        address = ip_address(bind.strip("[]"))
    except ValueError:
        return "unknown"
    return "this_machine" if address.is_loopback else "home_network"


def public_url_is_local(public_url: str) -> bool:
    """Whether PURSER_PUBLIC_URL names this machine only (localhost): agents
    on other machines are then refused unless they use an allowed name."""
    host = urlsplit(public_url).hostname or ""
    return is_loopback(host)


# The published docs site; PURSER_DOCS_URL points at another copy.
DOCS_SITE = "https://mlamoure.github.io/purser"
GUIDE_SECTION = "/tutorials/ai-setup/#use-purser-from-another-machine-on-the-home-network"


def guide_url(docs_url: str | None) -> str:
    """The setup guide's home-network section, on the configured docs site."""
    return f"{(docs_url or DOCS_SITE).rstrip('/')}{GUIDE_SECTION}"
