"""Working out the address a user can actually reach the portal on.

The portal used to hand out `http://127.0.0.1:4419`. On a machine you are sitting
at, that works. On the remote Linux box these bots normally run on, it is a link
to the user's own computer, where nothing is listening — so every attempt to
connect an account failed with a browser error and no explanation.

Detection order, most trustworthy first:

1. **`auth_portal.public_url` in the config.** Explicit always wins. This is the
   only option that can be right when the bot is behind a reverse proxy, on a
   non-standard port, or reachable by a domain name.
2. **`STREAMERBOT_PUBLIC_URL` in the environment**, for compose files and hosts
   that inject it.
3. **The address of the interface that routes to the internet.** On a VPS with a
   public IP, that is the public IP, which is the common case here.
4. **The configured bind address**, as a last resort.

Deliberately absent: asking a third-party service like ifconfig.me what our IP
is. It would give a better answer behind NAT, but it tells someone else's server
where this bot lives every time the portal starts, and the honest alternative —
detecting NAT and saying "set public_url" — costs the user one line of config and
no privacy.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

ENV_VAR = "STREAMERBOT_PUBLIC_URL"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def detect_outbound_address() -> Optional[str]:
    """The local address of the interface that would reach the internet.

    Uses a UDP socket, which sends nothing: connect() on a datagram socket only
    sets the default peer, and the kernel picks the source interface. Nothing
    leaves the machine and 8.8.8.8 is never contacted.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError as error:
        logger.debug(f"Could not determine the outbound address: {error}")
        return None
    finally:
        sock.close()


def is_private(address: str) -> bool:
    """True for a LAN address, which means the box is behind NAT."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # A hostname. Assume it is routable; the user chose it.
        return False
    return parsed.is_private or parsed.is_loopback or parsed.is_link_local


def normalise_base(value: str, port: int) -> str:
    """Turn whatever the user wrote into a usable base URL.

    Accepts `example.org`, `example.org:8080`, `http://example.org` and
    `https://example.org/streamerbot`, because people write all four and being
    strict here only produces a broken link with a confusing error.
    """
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"

    scheme, _, remainder = value.partition("://")
    host_part = remainder.split("/", 1)[0]
    path_part = remainder[len(host_part):]

    # Add the port only when one is not already given, and never to an https URL
    # on the default port, which is almost always a reverse proxy.
    has_port = ":" in host_part.rsplit("]", 1)[-1]
    if not has_port and not (scheme == "https" and not path_part):
        host_part = f"{host_part}:{port}"

    return f"{scheme}://{host_part}{path_part}".rstrip("/")


def resolve(config) -> Tuple[str, str]:
    """Return (base_url, how) where `how` explains which source was used.

    `how` is not decoration: it is what lets the bot tell a user why a link looks
    the way it does, and what to change when it is wrong.
    """
    port = getattr(config, "port", 4419)

    configured = (getattr(config, "public_url", "") or "").strip()
    if configured:
        return normalise_base(configured, port), "the public_url setting"

    from_env = (os.environ.get(ENV_VAR, "") or "").strip()
    if from_env:
        return normalise_base(from_env, port), f"the {ENV_VAR} environment variable"

    detected = detect_outbound_address()
    if detected and not is_private(detected):
        return normalise_base(detected, port), "this machine's public address"
    if detected:
        # A LAN address is right for someone on the same network and wrong for
        # anyone else. Better to hand it over and say so than to hand over
        # loopback, which is wrong for everybody but the machine itself.
        return (
            normalise_base(detected, port),
            "this machine's local network address, because it is behind NAT",
        )

    host = getattr(config, "host", "127.0.0.1")
    return normalise_base(host, port), "the configured bind address"


def reachability_warning(config, base: str) -> Optional[str]:
    """A sentence naming the thing that will stop this link working, or None.

    Handing out a link that cannot possibly connect, with no explanation, is the
    bug this module exists to fix. Detecting the address is only half of it.
    """
    host = (getattr(config, "host", "") or "").strip()
    port = getattr(config, "port", 4419)

    if host in ("127.0.0.1", "localhost", "::1") and "127.0.0.1" not in base:
        return (
            f"The portal is only listening on {host}, so this link will not connect "
            f"from another computer. Set auth_portal.host to 0.0.0.0 in this bot's "
            f"config.json and restart it, then allow port {port} through the "
            f"firewall."
        )

    if "://127.0.0.1" in base or "://localhost" in base:
        return (
            "This link points at the machine the bot runs on, so it only works "
            "from that machine. Set auth_portal.public_url to this server's "
            "address or domain name."
        )

    return None


__all__ = [
    "resolve",
    "reachability_warning",
    "detect_outbound_address",
    "is_private",
    "normalise_base",
    "ENV_VAR",
]
